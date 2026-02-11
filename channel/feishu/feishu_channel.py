"""
飞书通道接入

@author Saboteur7
@Date 2023/11/19
"""

# -*- coding=utf-8 -*-
import uuid
import io
import imghdr

import requests
import web
from channel.feishu.feishu_message import FeishuMessage
from bridge.context import Context
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.singleton import singleton
from config import conf
from common.expired_dict import ExpiredDict
from bridge.context import ContextType
from channel.chat_channel import ChatChannel, check_prefix
from common import utils
import json

URL_VERIFICATION = "url_verification"


@singleton
class FeiShuChanel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]

    feishu_app_id = conf().get('feishu_app_id')
    feishu_app_secret = conf().get('feishu_app_secret')
    feishu_token = conf().get('feishu_token')

    def __init__(self):
        super().__init__()
        # 历史消息id暂存，用于幂等控制
        self.receivedMsgs = ExpiredDict(60 * 60 * 7.1)
        logger.info("[FeiShu] app_id={}, app_secret={} verification_token={}".format(
            self.feishu_app_id, self.feishu_app_secret, self.feishu_token))
        # 无需群校验和前缀
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        conf()["single_chat_prefix"] = [""]

    def startup(self):
        urls = (
            '/', 'channel.feishu.feishu_channel.FeishuController'
        )
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("feishu_port", 9891)
        web.httpserver.runsimple(app.wsgifunc(), ("0.0.0.0", port))

    def send(self, reply: Reply, context: Context):
        msg = context.get("msg")
        is_group = context["isgroup"]
        if msg:
            access_token = msg.access_token
        else:
            access_token = self.fetch_access_token()
        headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        }
        msg_type = "text"
        logger.info(f"[FeiShu] start send reply message, type={context.type}, content={reply.content}")
        reply_content = reply.content
        content_key = "text"
        if reply.type == ReplyType.IMAGE_URL:
            # 图片上传
            if isinstance(reply.content, str) and reply.content.startswith("data:image/"):
                _, image_bytes = utils.decode_base64_image(reply.content)
                reply_content = self._upload_image_bytes(image_bytes, access_token) if image_bytes else None
            else:
                reply_content = self._upload_image_url(reply.content, access_token)
            if not reply_content:
                logger.warning("[FeiShu] upload file failed")
                return
            msg_type = "image"
            content_key = "image_key"
        elif reply.type == ReplyType.IMAGE:
            image_storage = reply.content
            image_storage.seek(0)
            reply_content = self._upload_image_bytes(image_storage.read(), access_token)
            if not reply_content:
                logger.warning("[FeiShu] upload file failed")
                return
            msg_type = "image"
            content_key = "image_key"
        if is_group:
            # 群聊中直接回复
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.msg_id}/reply"
            data = {
                "msg_type": msg_type,
                "content": json.dumps({content_key: reply_content})
            }
            res = requests.post(url=url, headers=headers, json=data, timeout=(5, 10))
        else:
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            params = {"receive_id_type": context.get("receive_id_type") or "open_id"}
            data = {
                "receive_id": context.get("receiver"),
                "msg_type": msg_type,
                "content": json.dumps({content_key: reply_content})
            }
            res = requests.post(url=url, headers=headers, params=params, json=data, timeout=(5, 10))
        res = res.json()
        if res.get("code") == 0:
            logger.info(f"[FeiShu] send message success")
        else:
            logger.error(f"[FeiShu] send message failed, code={res.get('code')}, msg={res.get('msg')}")


    def fetch_access_token(self) -> str:
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
        headers = {
            "Content-Type": "application/json"
        }
        req_body = {
            "app_id": self.feishu_app_id,
            "app_secret": self.feishu_app_secret
        }
        data = bytes(json.dumps(req_body), encoding='utf8')
        response = requests.post(url=url, data=data, headers=headers)
        if response.status_code == 200:
            res = response.json()
            if res.get("code") != 0:
                logger.error(f"[FeiShu] get tenant_access_token error, code={res.get('code')}, msg={res.get('msg')}")
                return ""
            else:
                return res.get("tenant_access_token")
        else:
            logger.error(f"[FeiShu] fetch token error, res={response}")


    def _upload_image_url(self, img_url, access_token):
        logger.debug(f"[WX] start download image, img_url={img_url}")
        response = requests.get(img_url)
        if response.status_code != 200:
            return None
        suffix = utils.get_path_suffix(img_url) or "png"
        return self._upload_image_bytes(response.content, access_token, suffix=suffix)

    def _upload_image_bytes(self, image_bytes, access_token, suffix=None):
        if not image_bytes:
            return None

        if not suffix:
            suffix = imghdr.what(None, h=image_bytes) or "png"

        # upload
        upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
        data = {
            'image_type': 'message'
        }
        headers = {
            'Authorization': f'Bearer {access_token}',
        }
        filename = f"{uuid.uuid4()}.{suffix}"
        content_type = f"image/{'jpeg' if suffix in ['jpg', 'jpeg'] else suffix}"
        file_obj = io.BytesIO(image_bytes)
        upload_response = requests.post(
            upload_url,
            files={"image": (filename, file_obj, content_type)},
            data=data,
            headers=headers
        )
        logger.info(f"[FeiShu] upload file, res={upload_response.content}")
        return upload_response.json().get("data", {}).get("image_key")



class FeishuController:
    # 类常量
    FAILED_MSG = '{"success": false}'
    SUCCESS_MSG = '{"success": true}'
    MESSAGE_RECEIVE_TYPE = "im.message.receive_v1"

    def GET(self):
        return "Feishu service start success!"

    def POST(self):
        try:
            channel = FeiShuChanel()

            request = json.loads(web.data().decode("utf-8"))
            logger.debug(f"[FeiShu] receive request: {request}")

            # 1.事件订阅回调验证
            if request.get("type") == URL_VERIFICATION:
                varify_res = {"challenge": request.get("challenge")}
                return json.dumps(varify_res)

            # 2.消息接收处理
            # token 校验
            header = request.get("header")
            if not header or header.get("token") != channel.feishu_token:
                return self.FAILED_MSG

            # 处理消息事件
            event = request.get("event")
            if header.get("event_type") == self.MESSAGE_RECEIVE_TYPE and event:
                if not event.get("message") or not event.get("sender"):
                    logger.warning(f"[FeiShu] invalid message, msg={request}")
                    return self.FAILED_MSG
                msg = event.get("message")

                # 幂等判断
                if channel.receivedMsgs.get(msg.get("message_id")):
                    logger.warning(f"[FeiShu] repeat msg filtered, event_id={header.get('event_id')}")
                    return self.SUCCESS_MSG
                channel.receivedMsgs[msg.get("message_id")] = True

                is_group = False
                chat_type = msg.get("chat_type")
                if chat_type == "group":
                    if not msg.get("mentions") and msg.get("message_type") == "text":
                        # 群聊中未@不响应
                        return self.SUCCESS_MSG
                    if msg.get("mentions")[0].get("name") != conf().get("feishu_bot_name") and msg.get("message_type") == "text":
                        # 不是@机器人，不响应
                        return self.SUCCESS_MSG
                    # 群聊
                    is_group = True
                    receive_id_type = "chat_id"
                elif chat_type == "p2p":
                    receive_id_type = "open_id"
                else:
                    logger.warning("[FeiShu] message ignore")
                    return self.SUCCESS_MSG
                # 构造飞书消息对象
                feishu_msg = FeishuMessage(event, is_group=is_group, access_token=channel.fetch_access_token())
                if not feishu_msg:
                    return self.SUCCESS_MSG

                context = self._compose_context(
                    feishu_msg.ctype,
                    feishu_msg.content,
                    isgroup=is_group,
                    msg=feishu_msg,
                    receive_id_type=receive_id_type,
                    no_need_at=True
                )
                if context:
                    channel.produce(context)
                logger.info(f"[FeiShu] query={feishu_msg.content}, type={feishu_msg.ctype}")
            return self.SUCCESS_MSG

        except Exception as e:
            logger.error(e)
            return self.FAILED_MSG

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]
        context["session_id"] = cmsg.from_user_id
        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            # 1.文本请求
            # 图片生成处理
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()

        elif context.type == ContextType.VOICE:
            # 2.语音请求
            if "desire_rtype" not in context and conf().get("voice_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE

        return context
