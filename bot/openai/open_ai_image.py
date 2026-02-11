import time

import openai
import openai.error

from common.log import logger
from common.token_bucket import TokenBucket
from common import utils
from config import conf


# OPENAI提供的画图接口
class OpenAIImage(object):
    def __init__(self):
        openai.api_key = conf().get("open_ai_api_key")
        if conf().get("rate_limit_dalle"):
            self.tb4dalle = TokenBucket(conf().get("rate_limit_dalle", 50))

    def create_img(self, query, retry_count=0, api_key=None, api_base=None):
        try:
            if conf().get("rate_limit_dalle") and not self.tb4dalle.get_token():
                return False, "请求太快了，请休息一下再问我吧"
            if conf().get("image_create_use_chat_model"):
                return self._create_img_by_chat_model(query=query, api_key=api_key, api_base=api_base)
            image_n, clean_query = utils.parse_image_n_from_prompt(query, default_n=1, min_n=1, max_n=4)
            logger.info("[OPEN_AI] image_query={}".format(query))
            response = openai.Image.create(
                api_key=api_key,
                prompt=clean_query,  # 图片描述
                n=image_n,  # 每次生成图片的数量（支持从提示词 n=x 解析）
                model=conf().get("text_to_image") or "dall-e-2",
                # size=conf().get("image_create_size", "256x256"),  # 图片大小,可选有 256x256, 512x512, 1024x1024
            )
            image_sources = self._extract_image_sources_from_generation_response(response)
            if not image_sources:
                return False, "画图返回内容中未识别到图片数据"
            logger.info("[OPEN_AI] image_count={}".format(len(image_sources)))
            if len(image_sources) == 1:
                logger.info("[OPEN_AI] image_source={}".format(image_sources[0][:200]))
                return True, image_sources[0]
            return True, image_sources
        except openai.error.RateLimitError as e:
            logger.warn(e)
            if retry_count < 1:
                time.sleep(5)
                logger.warn("[OPEN_AI] ImgCreate RateLimit exceed, 第{}次重试".format(retry_count + 1))
                return self.create_img(query, retry_count + 1)
            else:
                return False, "画图出现问题，请休息一下再问我吧"
        except Exception as e:
            logger.exception(e)
            return False, "画图出现问题，请休息一下再问我吧"

    def _extract_image_sources_from_generation_response(self, response):
        def _safe_get(obj, key):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(key)
            try:
                return obj.get(key)
            except Exception:
                return getattr(obj, key, None)

        data_list = _safe_get(response, "data")
        if not isinstance(data_list, list):
            return []

        sources = []
        for item in data_list:
            url = _safe_get(item, "url")
            if isinstance(url, str):
                source = url.strip()
                if source:
                    sources.append(source)

            b64_json = _safe_get(item, "b64_json")
            if isinstance(b64_json, str):
                b64_payload = "".join(b64_json.split())
                if b64_payload:
                    sources.append(f"data:image/png;base64,{b64_payload}")

            base64_image = _safe_get(item, "base64")
            if isinstance(base64_image, str):
                b64_payload = "".join(base64_image.split())
                if b64_payload:
                    sources.append(f"data:image/png;base64,{b64_payload}")

        unique_sources = []
        seen = set()
        for source in sources:
            if source in seen:
                continue
            seen.add(source)
            unique_sources.append(source)
        return unique_sources

    def _create_img_by_chat_model(self, query, api_key=None, api_base=None):
        _, clean_query = utils.parse_image_n_from_prompt(query, default_n=1, min_n=1, max_n=4)
        prefix = (
            "请不要输出任何解释或额外文本，只返回图片结果。"
            "你可以返回一张或多张图片。"
            "图片结果格式可为 Markdown 图片、HTML img、直接 URL，或 data:image/...;base64 数据。"
        )
        prompt = f"{prefix}\n\n{(clean_query or '').strip()}"
        logger.info("[OPEN_AI] image_query(chatmodel)={}".format(query))

        old_api_base = openai.api_base
        if api_base:
            openai.api_base = api_base
        try:
            response = openai.ChatCompletion.create(
                api_key=api_key,
                model=conf().get("model") or "gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                request_timeout=conf().get("request_timeout", 180),
                timeout=conf().get("request_timeout", 180),
            )
            content = response.choices[0]["message"]["content"] or ""
            image_sources = utils.extract_image_sources(content)
            if image_sources:
                logger.info("[OPEN_AI] image_count(chatmodel)={}".format(len(image_sources)))
                if len(image_sources) == 1:
                    logger.info("[OPEN_AI] image_source(chatmodel)={}".format(image_sources[0][:200]))
                    return True, image_sources[0]
                return True, image_sources
            return False, "画图返回内容中未识别到图片数据"
        finally:
            openai.api_base = old_api_base
