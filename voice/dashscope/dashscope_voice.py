# encoding:utf-8

import datetime
import os
import random

import requests

from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from voice.voice import Voice


class DashScopeVoice(Voice):
    """
    DashScope 语音服务（文本转语音为主）。

    目前主要用于 qwen3-tts-flash 等 TTS 模型：
    - 接口：POST {dashscope_api_base}/api/v1/services/aigc/multimodal-generation/generation
    - Header：Authorization: Bearer {dashscope_api_key}
    """

    def __init__(self):
        self.api_key = conf().get("dashscope_api_key")
        self.api_base = conf().get("dashscope_api_base") or "https://dashscope.aliyuncs.com"
        os.makedirs("tmp", exist_ok=True)

    def voiceToText(self, voice_file):
        return Reply(ReplyType.ERROR, "DashScopeVoice 暂未实现语音识别")

    def textToVoice(self, text):
        if not self.api_key:
            return Reply(ReplyType.ERROR, "DashScope API Key 未配置（dashscope_api_key）")

        model = conf().get("dashscope_tts_model") or conf().get("text_to_voice_model") or "qwen3-tts-flash"
        voice = conf().get("dashscope_tts_voice") or conf().get("tts_voice_id") or "Cherry"
        language_type = conf().get("dashscope_tts_language_type") or "Auto"

        url = self.api_base.rstrip("/") + "/api/v1/services/aigc/multimodal-generation/generation"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "input": {
                "text": str(text),
                "voice": voice,
                "language_type": language_type,
            },
        }

        try:
            timeout = conf().get("request_timeout", 180)
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            data = resp.json()
            audio_url = (((data.get("output") or {}).get("audio") or {}).get("url")) if isinstance(data, dict) else None
            if not audio_url:
                logger.error(f"[DashScopeVoice] textToVoice failed, status={resp.status_code}, resp={data}")
                return Reply(ReplyType.ERROR, "语音合成失败")

            audio_resp = requests.get(audio_url, timeout=timeout)
            if audio_resp.status_code >= 400:
                logger.error(f"[DashScopeVoice] download audio failed, status={audio_resp.status_code}, url={audio_url}")
                return Reply(ReplyType.ERROR, "语音合成失败")

            file_name = "tmp/" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + str(random.randint(0, 1000)) + ".wav"
            with open(file_name, "wb") as f:
                f.write(audio_resp.content)
            logger.info(f"[DashScopeVoice] textToVoice success, file={file_name}, model={model}, voice={voice}")
            return Reply(ReplyType.VOICE, file_name)
        except Exception as e:
            logger.exception("[DashScopeVoice] textToVoice error: %s" % e)
            return Reply(ReplyType.ERROR, "遇到了一点小问题，请稍后再试")

