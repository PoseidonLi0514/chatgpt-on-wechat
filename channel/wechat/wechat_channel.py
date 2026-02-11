# encoding:utf-8

"""
wechat channel
"""

import io
import json
import os
import re
import threading
import time
import requests
import openai
import openai.error

from bridge.context import *
from bridge.reply import *
from bridge.bridge import Bridge
from channel.chat_channel import ChatChannel
from channel import chat_channel
from channel.wechat.wechat_message import *
from common import const, utils
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from common.time_check import time_checker
from common.utils import convert_webp_to_png, remove_markdown_symbol
from config import conf, get_appdata_dir
from lib import itchat
from lib.itchat.content import *


@itchat.msg_register([TEXT, VOICE, PICTURE, NOTE, ATTACHMENT, SHARING])
def handler_single_msg(msg):
    if _raw_should_ignore_itchat_msg(msg):
        return None
    try:
        cmsg = WechatMessage(msg, False)
    except NotImplementedError as e:
        logger.debug("[WX]single message {} skipped: {}".format(msg["MsgId"], e))
        return None
    WechatChannel().handle_single(cmsg)
    return None


@itchat.msg_register([TEXT, VOICE, PICTURE, NOTE, ATTACHMENT, SHARING], isGroupChat=True)
def handler_group_msg(msg):
    if _raw_should_ignore_itchat_msg(msg):
        return None
    try:
        cmsg = WechatMessage(msg, True)
    except NotImplementedError as e:
        logger.debug("[WX]group message {} skipped: {}".format(msg["MsgId"], e))
        return None
    WechatChannel().handle_group(cmsg)
    return None


def _check(func):
    def wrapper(self, cmsg: ChatMessage):
        msgId = cmsg.msg_id
        if msgId in self.receivedMsgs:
            # hot_reload 场景下 itchat 可能会重复派发同一条消息，避免刷屏降级为 debug
            logger.debug("Wechat message {} already received, ignore".format(msgId))
            return
        self.receivedMsgs[msgId] = True
        create_time = cmsg.create_time  # 消息时间戳
        if conf().get("hot_reload") == True and int(create_time) < int(time.time()) - 60:  # 跳过1分钟前的历史消息
            logger.debug("[WX]history message {} skipped".format(msgId))
            return
        if cmsg.my_msg and not cmsg.is_group:
            logger.debug("[WX]my message {} skipped".format(msgId))
            return
        return func(self, cmsg)

    return wrapper


_raw_received_msgs = ExpiredDict(600)  # 原始 itchat 消息去重，避免重复构造 WechatMessage 导致重启卡顿


def _raw_should_ignore_itchat_msg(msg) -> bool:
    """
    itchat 在 hotReload/重连时可能重复派发消息，且会带回一批历史消息。
    这里在构造 WechatMessage 之前做快速过滤，减少 CPU/IO 开销。
    """
    try:
        msg_id = msg.get("MsgId")
        if msg_id:
            if msg_id in _raw_received_msgs:
                return True
            _raw_received_msgs[msg_id] = True
        if conf().get("hot_reload") == True:
            create_time = msg.get("CreateTime")
            if create_time and int(create_time) < int(time.time()) - 60:
                return True
    except Exception as e:
        logger.debug(f"[WX] raw msg ignore check failed: {e}")
    return False


# 可用的二维码生成接口
# https://api.qrserver.com/v1/create-qr-code/?size=400×400&data=https://www.abc.com
# https://api.isoyu.com/qr/?m=1&e=L&p=20&url=https://www.abc.com
def qrCallback(uuid, status, qrcode):
    # logger.debug("qrCallback: {} {}".format(uuid,status))
    if status == "0":
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(qrcode))
            _thread = threading.Thread(target=img.show, args=("QRCode",))
            _thread.setDaemon(True)
            _thread.start()
        except Exception as e:
            pass

        import qrcode

        url = f"https://login.weixin.qq.com/l/{uuid}"

        qr_api1 = "https://api.isoyu.com/qr/?m=1&e=L&p=20&url={}".format(url)
        qr_api2 = "https://api.qrserver.com/v1/create-qr-code/?size=400×400&data={}".format(url)
        qr_api3 = "https://api.pwmqr.com/qrcode/create/?url={}".format(url)
        qr_api4 = "https://my.tv.sohu.com/user/a/wvideo/getQRCode.do?text={}".format(url)
        print("You can also scan QRCode in any website below:")
        print(qr_api3)
        print(qr_api4)
        print(qr_api2)
        print(qr_api1)
        _send_qr_code([qr_api3, qr_api4, qr_api2, qr_api1])
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        try:
            qr.print_ascii(invert=True)
        except UnicodeEncodeError:
            print("ASCII QR code printing failed due to encoding issues.")


@singleton
class WechatChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []
    NSFW_WARNING_TEXT = "侦测到NSFW内容，将会在一分钟后撤回消息"
    NSFW_RETRY_CHECKPOINTS = [0, 5, 10, 20, 25, 30, 40, 45, 50, 55, 60]  # 3 + 3 + 5 = 11次
    NSFW_ATTEMPT_TIMEOUT_SECONDS = 5
    NSFW_SYSTEM_PROMPT = (
        "你是图像生成内容审核器。"
        "请判断用户给出的图像提示词是否包含NSFW内容。"
        "NSFW包括但不限于：露骨性行为、裸露生殖器/乳头、色情描写、未成年人性相关内容。"
        "只输出严格JSON，不要输出其它字符：{\"nsfw\": true} 或 {\"nsfw\": false}。"
    )

    def __init__(self):
        super().__init__()
        self.receivedMsgs = ExpiredDict(conf().get("expires_in_seconds", 3600))
        self.auto_login_times = 0

    def startup(self):
        try:
            time.sleep(3)
            logger.warning(
                "[WechatChannel] 准备启动 wx 通道（基于 itchat/webwx，可能因微信风控/协议变更导致不可用；更稳定的方案通常是 web/wechatmp/wechatcom_app/wcf 等）。"
            )

            itchat.instance.receivingRetryCount = 600  # 修改断线超时时间
            # login by scan QRCode
            hotReload = conf().get("hot_reload", False)
            status_path = os.path.join(get_appdata_dir(), "itchat.pkl")
            itchat.auto_login(
                enableCmdQR=2,
                hotReload=hotReload,
                statusStorageDir=status_path,
                qrCallback=qrCallback,
                exitCallback=self.exitCallback,
                loginCallback=self.loginCallback,
            )
            self.user_id = itchat.instance.storageClass.userName
            self.name = itchat.instance.storageClass.nickName
            logger.info("Wechat login success, user_id: {}, nickname: {}".format(self.user_id, self.name))
            # start message listener
            itchat.run()
        except Exception as e:
            logger.error(
                """[WechatChannel] wx 通道启动失败。当前支持的 channel_type 包含:
    1. terminal: 终端
    2. wechatmp: 个人公众号
    3. wechatmp_service: 企业公众号
    4. wechatcom_app: 企微自建应用
    5. dingtalk: 钉钉
    6. feishu: 飞书
    7. web: 网页
    8. wcf: wechat (需Windows环境，参考 https://github.com/zhayujie/chatgpt-on-wechat/pull/2562 )
可修改 config.json 配置文件的 channel_type 字段进行切换"""
            )
            logger.exception(e)
            raise

    def exitCallback(self):
        try:
            from common.linkai_client import chat_client
            if chat_client.client_id and conf().get("use_linkai"):
                _send_logout()
                time.sleep(2)
                self.auto_login_times += 1
                if self.auto_login_times < 100:
                    chat_channel.handler_pool._shutdown = False
                    self.startup()
        except Exception as e:
            pass

    def loginCallback(self):
        logger.debug("Login success")
        _send_login_success()

    # handle_* 系列函数处理收到的消息后构造Context，然后传入produce函数中处理Context和发送回复
    # Context包含了消息的所有信息，包括以下属性
    #   type 消息类型, 包括TEXT、VOICE、IMAGE_CREATE
    #   content 消息内容，如果是TEXT类型，content就是文本内容，如果是VOICE类型，content就是语音文件名，如果是IMAGE_CREATE类型，content就是图片生成命令
    #   kwargs 附加参数字典，包含以下的key：
    #        session_id: 会话id
    #        isgroup: 是否是群聊
    #        receiver: 需要回复的对象
    #        msg: ChatMessage消息对象
    #        origin_ctype: 原始消息类型，语音转文字后，私聊时如果匹配前缀失败，会根据初始消息是否是语音来放宽触发规则
    #        desire_rtype: 希望回复类型，默认是文本回复，设置为ReplyType.VOICE是语音回复
    @time_checker
    @_check
    def handle_single(self, cmsg: ChatMessage):
        # filter system message
        if cmsg.other_user_id in ["weixin"]:
            return
        if cmsg.ctype == ContextType.VOICE:
            if conf().get("speech_recognition") != True:
                return
            logger.debug("[WX]receive voice msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[WX]receive image msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.PATPAT:
            logger.debug("[WX]receive patpat msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            logger.debug("[WX]receive text msg: {}, cmsg={}".format(json.dumps(cmsg._rawmsg, ensure_ascii=False), cmsg))
        else:
            logger.debug("[WX]receive msg: {}, cmsg={}".format(cmsg.content, cmsg))
        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=False, msg=cmsg)
        if context:
            self.produce(context)

    @time_checker
    @_check
    def handle_group(self, cmsg: ChatMessage):
        if cmsg.ctype == ContextType.VOICE:
            if conf().get("group_speech_recognition") != True:
                return
            logger.debug("[WX]receive voice for group msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.IMAGE:
            logger.debug("[WX]receive image for group msg: {}".format(cmsg.content))
        elif cmsg.ctype in [ContextType.JOIN_GROUP, ContextType.PATPAT, ContextType.ACCEPT_FRIEND, ContextType.EXIT_GROUP]:
            logger.debug("[WX]receive note msg: {}".format(cmsg.content))
        elif cmsg.ctype == ContextType.TEXT:
            # logger.debug("[WX]receive group msg: {}, cmsg={}".format(json.dumps(cmsg._rawmsg, ensure_ascii=False), cmsg))
            pass
        elif cmsg.ctype == ContextType.FILE:
            logger.debug(f"[WX]receive attachment msg, file_name={cmsg.content}")
        else:
            logger.debug("[WX]receive group msg: {}".format(cmsg.content))
        context = self._compose_context(cmsg.ctype, cmsg.content, isgroup=True, msg=cmsg, no_need_at=conf().get("no_need_at", False))
        if context:
            self.produce(context)

    def _handle(self, context: Context):
        if context is None or not context.content:
            return
        if context.type == ContextType.IMAGE_CREATE:
            self._handle_image_create_with_nsfw(context)
            return
        return super()._handle(context)

    def _handle_image_create_with_nsfw(self, context: Context):
        logger.debug("[WX] start image_create with nsfw check, prompt={}".format(context.content))
        detect_future = chat_channel.handler_pool.submit(
            self._detect_nsfw_for_image_prompt_with_retry, context.content, context
        )

        reply = self._generate_reply(context)
        logger.debug("[WX] image_create reply generated: {}".format(reply))
        if not reply or not reply.content:
            return

        reply = self._decorate_reply(context, reply)
        if not reply or not reply.type:
            return

        # 只针对实际图片回复走告警+撤回逻辑，错误文本等按原流程直接发送
        if reply.type not in [ReplyType.IMAGE_URL, ReplyType.IMAGE]:
            self._send_reply(context, reply)
            return

        # 先发送图片，随后根据审核结果决定是否撤回
        image_meta = self._send_and_collect_wx_messages(context, reply, use_send_reply=True)

        detect_result = None
        try:
            detect_result = detect_future.result(timeout=70)
        except Exception as e:
            logger.warning("[WX] nsfw check failed with exception, conservative revoke images: {}".format(e))
            if image_meta:
                self._schedule_wx_revoke(image_meta, delay_seconds=1)
            return

        if not isinstance(detect_result, dict):
            logger.warning("[WX] nsfw check invalid result, conservative revoke images")
            if image_meta:
                self._schedule_wx_revoke(image_meta, delay_seconds=1)
            return

        if detect_result.get("status") == "failed":
            logger.warning("[WX] nsfw check all attempts failed, conservative revoke images")
            if image_meta:
                self._schedule_wx_revoke(image_meta, delay_seconds=1)
            return

        if not detect_result.get("nsfw", False):
            return

        warning_reply = Reply(ReplyType.TEXT, self.NSFW_WARNING_TEXT)
        warning_meta = self._send_and_collect_wx_messages(context, warning_reply, use_send_reply=False)
        revoke_meta = warning_meta + image_meta
        if revoke_meta:
            self._schedule_wx_revoke(revoke_meta, delay_seconds=60)

    def _send_and_collect_wx_messages(self, context: Context, reply: Reply, use_send_reply=False):
        sent_meta = context.kwargs.setdefault("_wx_sent_msg_meta", [])
        start = len(sent_meta)
        if use_send_reply:
            self._send_reply(context, reply)
        else:
            self._send(reply, context)
        return sent_meta[start:]

    def _schedule_wx_revoke(self, msg_meta_list, delay_seconds=60):
        # 按 msg_id + to_user 去重
        unique_meta = []
        dedup = set()
        for item in msg_meta_list:
            msg_id = str(item.get("msg_id") or "")
            to_user = str(item.get("to_user") or "")
            key = "{}::{}".format(msg_id, to_user)
            if not msg_id or not to_user or key in dedup:
                continue
            dedup.add(key)
            unique_meta.append({"msg_id": msg_id, "to_user": to_user})

        if not unique_meta:
            return

        def _revoke_worker():
            time.sleep(delay_seconds)
            for item in unique_meta:
                msg_id = item["msg_id"]
                to_user = item["to_user"]
                try:
                    ret = itchat.revoke(msg_id, to_user)
                    if ret:
                        logger.info("[WX] revoke success, msg_id={}, to_user={}".format(msg_id, to_user))
                    else:
                        logger.warning("[WX] revoke failed, msg_id={}, to_user={}, ret={}".format(msg_id, to_user, ret))
                except Exception as e:
                    logger.warning("[WX] revoke exception, msg_id={}, to_user={}, err={}".format(msg_id, to_user, e))

        thread = threading.Thread(target=_revoke_worker, daemon=True)
        thread.start()

    def _detect_nsfw_for_image_prompt_with_retry(self, prompt: str, context: Context) -> dict:
        _, clean_prompt = utils.parse_image_n_from_prompt(prompt, default_n=1, min_n=1, max_n=4)
        clean_prompt = (clean_prompt or "").strip()
        if not clean_prompt:
            return {"status": "ok", "nsfw": False, "attempts": 0}

        start_ts = time.time()
        for idx, checkpoint in enumerate(self.NSFW_RETRY_CHECKPOINTS, start=1):
            target_ts = start_ts + checkpoint
            wait_seconds = target_ts - time.time()
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            raw_result = self._request_nsfw_result(clean_prompt, context)
            nsfw = self._parse_nsfw_flag(raw_result)
            if nsfw is not None:
                logger.info(
                    "[WX] nsfw check success, nsfw={}, attempts={}, prompt={}, result={}".format(
                        nsfw, idx, clean_prompt, (raw_result or "")[:200]
                    )
                )
                return {"status": "ok", "nsfw": nsfw, "attempts": idx}

            logger.warning(
                "[WX] nsfw check attempt {} failed, prompt={}, raw_result={}".format(
                    idx, clean_prompt, (raw_result or "")[:200]
                )
            )

        return {"status": "failed", "nsfw": None, "attempts": len(self.NSFW_RETRY_CHECKPOINTS)}

    def _request_nsfw_result(self, clean_prompt: str, context: Context) -> str:
        btype = Bridge().get_bot_type("chat")
        if btype in [const.CHATGPT, const.OPEN_AI, const.CHATGPTONAZURE]:
            result = self._request_nsfw_by_openai(clean_prompt, context, btype)
            if result:
                return result
        return self._request_nsfw_by_bridge(clean_prompt, context)

    def _request_nsfw_by_openai(self, clean_prompt: str, context: Context, bot_type: str) -> str:
        messages = [
            {"role": "system", "content": self.NSFW_SYSTEM_PROMPT},
            {"role": "user", "content": clean_prompt},
        ]
        old_api_key = getattr(openai, "api_key", None)
        old_api_base = getattr(openai, "api_base", None)
        old_api_type = getattr(openai, "api_type", None)
        old_api_version = getattr(openai, "api_version", None)
        try:
            openai.api_key = context.get("openai_api_key") or conf().get("open_ai_api_key")
            if conf().get("open_ai_api_base"):
                openai.api_base = conf().get("open_ai_api_base")

            req_kwargs = {
                "api_key": context.get("openai_api_key") or conf().get("open_ai_api_key"),
                "messages": messages,
                "request_timeout": min(conf().get("request_timeout", 60), self.NSFW_ATTEMPT_TIMEOUT_SECONDS),
                "timeout": min(conf().get("request_timeout", 60), self.NSFW_ATTEMPT_TIMEOUT_SECONDS),
            }
            if bot_type == const.CHATGPTONAZURE:
                openai.api_type = "azure"
                openai.api_version = conf().get("azure_api_version", "2023-06-01-preview")
                deployment_id = conf().get("azure_deployment_id")
                if deployment_id:
                    req_kwargs["deployment_id"] = deployment_id
                else:
                    req_kwargs["model"] = conf().get("model") or "gpt-3.5-turbo"
            else:
                openai.api_type = "open_ai"
                req_kwargs["model"] = context.get("gpt_model") or conf().get("model") or "gpt-3.5-turbo"

            response = openai.ChatCompletion.create(**req_kwargs)
            return (response.choices[0]["message"]["content"] or "").strip()
        except Exception as e:
            logger.warning("[WX] nsfw openai check failed, fallback to bot reply: {}".format(e))
            return ""
        finally:
            openai.api_key = old_api_key
            openai.api_base = old_api_base
            openai.api_type = old_api_type
            openai.api_version = old_api_version

    def _request_nsfw_by_bridge(self, clean_prompt: str, context: Context) -> str:
        # 回退方案：仍使用当前 chat bot，但以独立 session 发起判断请求，避免污染用户主会话
        nsfw_query = (
            "请只输出严格JSON，不要输出任何其它字符："
            "{\"nsfw\": true} 或 {\"nsfw\": false}。\n"
            "判断下述图像提示词是否属于NSFW（色情、裸露、露骨性行为、未成年人性相关）：\n"
            f"{clean_prompt}"
        )
        nsfw_ctx = Context(ContextType.TEXT, nsfw_query)
        nsfw_session_id = "nsfw-check-{}-{}".format(context.get("session_id"), int(time.time() * 1000))
        nsfw_ctx["session_id"] = nsfw_session_id
        nsfw_ctx["receiver"] = context.get("receiver")
        nsfw_ctx["isgroup"] = context.get("isgroup", False)
        nsfw_ctx["openai_api_key"] = context.get("openai_api_key")
        nsfw_ctx["gpt_model"] = context.get("gpt_model")

        try:
            reply = Bridge().fetch_reply_content(nsfw_query, nsfw_ctx)
            if reply and reply.content:
                return str(reply.content).strip()
            return ""
        except Exception as e:
            logger.warning("[WX] nsfw bridge check failed: {}".format(e))
            return ""
        finally:
            try:
                bot = Bridge().get_bot("chat")
                if hasattr(bot, "sessions"):
                    bot.sessions.clear_session(nsfw_session_id)
            except Exception:
                pass

    def _parse_nsfw_flag(self, result_text: str):
        text = (result_text or "").strip()
        if not text:
            return None

        candidates = [text]
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            candidates.insert(0, json_match.group(0))

        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    value = obj.get("nsfw")
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, str):
                        val = value.strip().lower()
                        if val == "true":
                            return True
                        if val == "false":
                            return False
            except Exception:
                continue

        lowered = text.lower()
        if re.search(r'"?nsfw"?\s*[:=]\s*true', lowered):
            return True
        if re.search(r'"?nsfw"?\s*[:=]\s*false', lowered):
            return False
        if lowered in ["true", "false"]:
            return lowered == "true"
        # 更严格策略：只要不是明确 false，其它非空返回一律按 NSFW 处理
        return True

    def _extract_wx_msg_id(self, send_result):
        if not isinstance(send_result, dict):
            return None
        for key in ["MsgID", "MsgId", "msg_id", "msgId", "SvrMsgId", "NewMsgId"]:
            value = send_result.get(key)
            if value:
                return str(value)
        return None

    def _record_wx_sent_msg(self, context: Context, receiver: str, reply_type: ReplyType, send_result):
        msg_id = self._extract_wx_msg_id(send_result)
        if not msg_id:
            return
        sent_meta = context.kwargs.setdefault("_wx_sent_msg_meta", [])
        sent_meta.append({
            "msg_id": msg_id,
            "to_user": receiver,
            "reply_type": str(reply_type),
        })

    # 统一的发送函数，每个Channel自行实现，根据reply的type字段发送不同类型的消息
    def send(self, reply: Reply, context: Context):
        receiver = context["receiver"]
        send_result = None
        if reply.type == ReplyType.TEXT:
            reply.content = remove_markdown_symbol(reply.content)
            send_result = itchat.send(reply.content, toUserName=receiver)
            logger.info("[WX] sendMsg={}, receiver={}".format(reply, receiver))
        elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
            reply.content = remove_markdown_symbol(reply.content)
            send_result = itchat.send(reply.content, toUserName=receiver)
            logger.info("[WX] sendMsg={}, receiver={}".format(reply, receiver))
        elif reply.type == ReplyType.VOICE:
            send_result = itchat.send_file(reply.content, toUserName=receiver)
            logger.info("[WX] sendFile={}, receiver={}".format(reply.content, receiver))
        elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
            img_url = reply.content
            if isinstance(img_url, str) and img_url.startswith("data:image/"):
                _, image_bytes = utils.decode_base64_image(img_url)
                if image_bytes is None:
                    logger.warning("[WX] invalid data-uri image, skip send")
                    send_result = itchat.send("图片数据无效，发送失败", toUserName=receiver)
                    self._record_wx_sent_msg(context, receiver, ReplyType.ERROR, send_result)
                    return send_result
                image_storage = io.BytesIO(image_bytes)
                image_storage.seek(0)
                send_result = itchat.send_image(image_storage, toUserName=receiver)
                logger.info("[WX] sendImage data-uri, receiver={}".format(receiver))
                self._record_wx_sent_msg(context, receiver, reply.type, send_result)
                return send_result
            logger.debug(f"[WX] start download image, img_url={img_url}")
            pic_res = requests.get(img_url, stream=True)
            image_storage = io.BytesIO()
            size = 0
            for block in pic_res.iter_content(1024):
                size += len(block)
                image_storage.write(block)
            logger.info(f"[WX] download image success, size={size}, img_url={img_url}")
            image_storage.seek(0)
            if ".webp" in img_url:
                try:
                    image_storage = convert_webp_to_png(image_storage)
                except Exception as e:
                    logger.error(f"Failed to convert image: {e}")
                    return
            send_result = itchat.send_image(image_storage, toUserName=receiver)
            logger.info("[WX] sendImage url={}, receiver={}".format(img_url, receiver))
        elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
            image_storage = reply.content
            image_storage.seek(0)
            send_result = itchat.send_image(image_storage, toUserName=receiver)
            logger.info("[WX] sendImage, receiver={}".format(receiver))
        elif reply.type == ReplyType.FILE:  # 新增文件回复类型
            file_storage = reply.content
            send_result = itchat.send_file(file_storage, toUserName=receiver)
            logger.info("[WX] sendFile, receiver={}".format(receiver))
        elif reply.type == ReplyType.VIDEO:  # 新增视频回复类型
            video_storage = reply.content
            send_result = itchat.send_video(video_storage, toUserName=receiver)
            logger.info("[WX] sendFile, receiver={}".format(receiver))
        elif reply.type == ReplyType.VIDEO_URL:  # 新增视频URL回复类型
            video_url = reply.content
            logger.debug(f"[WX] start download video, video_url={video_url}")
            video_res = requests.get(video_url, stream=True)
            video_storage = io.BytesIO()
            size = 0
            for block in video_res.iter_content(1024):
                size += len(block)
                video_storage.write(block)
            logger.info(f"[WX] download video success, size={size}, video_url={video_url}")
            video_storage.seek(0)
            send_result = itchat.send_video(video_storage, toUserName=receiver)
            logger.info("[WX] sendVideo url={}, receiver={}".format(video_url, receiver))
        self._record_wx_sent_msg(context, receiver, reply.type, send_result)
        return send_result

def _send_login_success():
    try:
        from common.linkai_client import chat_client
        if chat_client.client_id:
            chat_client.send_login_success()
    except Exception as e:
        pass


def _send_logout():
    try:
        from common.linkai_client import chat_client
        if chat_client.client_id:
            chat_client.send_logout()
    except Exception as e:
        pass


def _send_qr_code(qrcode_list: list):
    try:
        from common.linkai_client import chat_client
        if chat_client.client_id:
            chat_client.send_qrcode(qrcode_list)
    except Exception as e:
        pass
