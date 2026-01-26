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
            logger.info("[OPEN_AI] image_query={}".format(query))
            response = openai.Image.create(
                api_key=api_key,
                prompt=query,  # 图片描述
                n=1,  # 每次生成图片的数量
                model=conf().get("text_to_image") or "dall-e-2",
                # size=conf().get("image_create_size", "256x256"),  # 图片大小,可选有 256x256, 512x512, 1024x1024
            )
            image_url = response["data"][0]["url"]
            logger.info("[OPEN_AI] image_url={}".format(image_url))
            return True, image_url
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

    def _create_img_by_chat_model(self, query, api_key=None, api_base=None):
        prefix = "请你不要输出任何多余的话，只按照以下prompt来进行绘图，比例自定，质量始终为high。输出结果只需要一个图片。"
        prompt = f"{prefix}\n\n{(query or '').strip()}"
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
            image_urls = utils.extract_markdown_image_urls(content)
            if image_urls:
                logger.info("[OPEN_AI] image_url(chatmodel)={}".format(image_urls[0]))
                return True, image_urls[0]
            return False, "画图返回内容中未找到图片链接"
        finally:
            openai.api_base = old_api_base
