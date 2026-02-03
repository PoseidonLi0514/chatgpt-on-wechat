# encoding:utf-8

import json
import re
import ast
from typing import Optional, Dict

import plugins
from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from plugins import Event, EventAction, EventContext, Plugin


def _strip_code_fence(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if not lines:
        return s
    if not lines[0].startswith("```"):
        return s
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return s


def _extract_json_object(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return s[start : end + 1].strip()
    return ""


_RE_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_RE_PY_TRUE = re.compile(r"\bTrue\b")
_RE_PY_FALSE = re.compile(r"\bFalse\b")
_RE_PY_NONE = re.compile(r"\bNone\b")
_RE_PLUS_NUMBER_VALUE = re.compile(r"(:\s*)\+(\d+(?:\.\d+)?)")


def _sanitize_nonstandard_json(text: str) -> str:
    """
    兼容模型常见的“几乎是 JSON”的输出：
    - 允许 `: +5` 这种前导 `+` 数字（标准 JSON 不允许），转成字符串 `\"+5\"` 保留符号
    - 去掉 `}` / `]` 前的尾逗号
    - 把 Python 风格的 True/False/None 转成 JSON 的 true/false/null
    """
    if not text:
        return ""
    s = text
    s = _RE_PLUS_NUMBER_VALUE.sub(r'\1"+\2"', s)
    s = _RE_TRAILING_COMMA.sub(r"\1", s)
    s = _RE_PY_TRUE.sub("true", s)
    s = _RE_PY_FALSE.sub("false", s)
    s = _RE_PY_NONE.sub("null", s)
    return s


def _loads_relaxed_object(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    sanitized = _sanitize_nonstandard_json(text)
    if sanitized and sanitized != text:
        try:
            return json.loads(sanitized)
        except Exception:
            pass
    # 最后兜底：兼容 Python dict 风格（单引号、+5 等），不执行任意代码
    try:
        return ast.literal_eval(text)
    except Exception:
        pass
    if sanitized and sanitized != text:
        try:
            return ast.literal_eval(sanitized)
        except Exception:
            pass
    return None


def _parse_catgirl_payload(text: str) -> Optional[Dict]:
    s = _strip_code_fence(text)
    obj = _loads_relaxed_object(s)
    if obj is None:
        candidate = _extract_json_object(s)
        if not candidate:
            return None
        obj = _loads_relaxed_object(candidate)
        if obj is None:
            return None
    if not isinstance(obj, dict):
        return None
    if "content" not in obj:
        return None
    if not any(k in obj for k in ("action", "actions", "mood", "fav_current", "fav_change")):
        return None
    return obj


def _to_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return None
    return None


def _wrap_action(action: str) -> str:
    if not action:
        return ""
    a = str(action).strip()
    if not a:
        return ""
    a = a.strip("()（） \t\r\n")
    if not a:
        return ""
    return f"（{a}）"


def _extract_signed_number_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v))
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"[+-]?\d+", s)
    if not m:
        return None
    return m.group(0)


def _build_status_line(mood, fav_current, fav_change) -> str:
    mood = (str(mood).strip() if mood is not None else "").strip()
    cur_str = _extract_signed_number_str(fav_current)
    if cur_str and cur_str.startswith("+"):
        cur_str = cur_str[1:]
    chg_str = _extract_signed_number_str(fav_change)
    parts = []
    if mood:
        parts.append(f"心情：{mood}")
    if cur_str is not None:
        if chg_str is None:
            parts.append(f"好感度：{cur_str}")
        else:
            # change 的正负号按模型返回展示，不额外加工
            parts.append(f"好感度：{cur_str} ({chg_str})")
    return " | ".join(parts)


def _format_catgirl_display(payload: dict) -> str:
    action = payload.get("action") or payload.get("actions") or ""
    content = payload.get("content") or ""
    mood = payload.get("mood")
    fav_current = payload.get("fav_current")
    fav_change = payload.get("fav_change")

    lines = []
    action_line = _wrap_action(action)
    if action_line:
        lines.append(action_line)
    content = str(content).strip()
    if content:
        lines.append(content)
    status_line = _build_status_line(mood, fav_current, fav_change)
    if status_line:
        lines.append(status_line)
    # 微信部分客户端可能会折叠“纯空行”，用零宽字符占位确保中间留白
    block_sep = "\n\u200b\n"
    return block_sep.join(lines).strip()


def _decorate_plain_text(context, text: str) -> str:
    if text is None:
        text = ""
    text = str(text).strip()
    if context.get("isgroup", False):
        if not context.get("no_need_at", False):
            text = "@" + context["msg"].actual_user_nickname + "\n" + text
        return conf().get("group_chat_reply_prefix", "") + text + conf().get("group_chat_reply_suffix", "")
    return conf().get("single_chat_reply_prefix", "") + text + conf().get("single_chat_reply_suffix", "")


@plugins.register(
    name="CatgirlJson",
    desire_priority=-100,
    namecn="猫娘JSON格式化",
    desc="适配猫娘提示词输出的JSON，默认格式化展示；可用前缀触发语音多段回复。",
    version="1.0",
    author="codex",
)
class CatgirlJsonPlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_DECORATE_REPLY] = self.on_decorate_reply
        self.handlers[Event.ON_SEND_REPLY] = self.on_send_reply

    def on_decorate_reply(self, e_context: EventContext):
        reply = e_context["reply"]
        context = e_context["context"]
        if not reply or reply.type != ReplyType.TEXT:
            return
        if context.get("__catgirl_multisend"):
            return
        # 记录模型原始输出（此时还未被群聊@、前后缀等包装），供 voice_mode 在发送阶段使用
        context["catgirl_raw_reply"] = reply.content
        payload = _parse_catgirl_payload(reply.content)
        if not payload:
            return
        context["catgirl_json_payload"] = payload
        if context.get("catgirl_voice_mode"):
            return
        reply.content = _format_catgirl_display(payload)

    def on_send_reply(self, e_context: EventContext):
        context = e_context["context"]
        channel = e_context["channel"]
        reply = e_context["reply"]
        if not context.get("catgirl_voice_mode"):
            return
        if context.get("__catgirl_multisend"):
            return

        payload = context.get("catgirl_json_payload") if isinstance(context.get("catgirl_json_payload"), dict) else None
        if not payload and reply and reply.type == ReplyType.TEXT:
            payload = _parse_catgirl_payload(reply.content)

        if payload:
            action = payload.get("action") or payload.get("actions") or ""
            content = payload.get("content") or ""
            mood = payload.get("mood")
            fav_current = payload.get("fav_current")
            fav_change = payload.get("fav_change")
        else:
            action = ""
            content = context.get("catgirl_raw_reply") if context.get("catgirl_raw_reply") else (reply.content if reply and reply.content else "")
            mood = None
            fav_current = None
            fav_change = None

        action_line = _wrap_action(action)
        status_line = _build_status_line(mood, fav_current, fav_change)

        context["__catgirl_multisend"] = True
        try:
            if action_line:
                channel.send(Reply(ReplyType.TEXT, _decorate_plain_text(context, action_line)), context)

            if ReplyType.VOICE not in getattr(channel, "NOT_SUPPORT_REPLYTYPE", []) and str(content).strip():
                voice_reply = channel.build_text_to_voice(str(content).strip())
                if voice_reply and voice_reply.type == ReplyType.VOICE:
                    channel.send(voice_reply, context)
                else:
                    logger.warning("[CatgirlJson] build_text_to_voice failed, fallback to text")
                    channel.send(Reply(ReplyType.TEXT, _decorate_plain_text(context, str(content).strip())), context)
            else:
                channel.send(Reply(ReplyType.TEXT, _decorate_plain_text(context, str(content).strip())), context)

            if status_line:
                channel.send(Reply(ReplyType.TEXT, _decorate_plain_text(context, status_line)), context)
        except Exception as e:
            logger.exception("[CatgirlJson] voice mode send failed: %s" % e)
            # 最坏情况回退：让默认发送逻辑继续处理当前reply
            context["__catgirl_multisend"] = False
            return
        finally:
            context["__catgirl_multisend"] = False

        e_context.action = EventAction.BREAK_PASS
