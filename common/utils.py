import io
import os
import re
import base64
from urllib.parse import urlparse
from PIL import Image
from common.log import logger

def fsize(file):
    if isinstance(file, io.BytesIO):
        return file.getbuffer().nbytes
    elif isinstance(file, str):
        return os.path.getsize(file)
    elif hasattr(file, "seek") and hasattr(file, "tell"):
        pos = file.tell()
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(pos)
        return size
    else:
        raise TypeError("Unsupported type")


def compress_imgfile(file, max_size):
    if fsize(file) <= max_size:
        return file
    file.seek(0)
    img = Image.open(file)
    rgb_image = img.convert("RGB")
    quality = 95
    while True:
        out_buf = io.BytesIO()
        rgb_image.save(out_buf, "JPEG", quality=quality)
        if fsize(out_buf) <= max_size:
            return out_buf
        quality -= 5


def split_string_by_utf8_length(string, max_length, max_split=0):
    encoded = string.encode("utf-8")
    start, end = 0, 0
    result = []
    while end < len(encoded):
        if max_split > 0 and len(result) >= max_split:
            result.append(encoded[start:].decode("utf-8"))
            break
        end = min(start + max_length, len(encoded))
        # 如果当前字节不是 UTF-8 编码的开始字节，则向前查找直到找到开始字节为止
        while end < len(encoded) and (encoded[end] & 0b11000000) == 0b10000000:
            end -= 1
        result.append(encoded[start:end].decode("utf-8"))
        start = end
    return result


def parse_image_n_from_prompt(prompt: str, default_n=1, min_n=1, max_n=4):
    """
    从提示词中提取图片数量参数，严格匹配独立 token：n=数字（不支持 n = 1）。
    - 允许范围：min_n ~ max_n
    - 超出范围时回退 default_n
    - 返回: (image_n, cleaned_prompt)
    """
    text = (prompt or "").strip()
    if not text:
        return default_n, text

    # 严格匹配独立参数 token，例如 "... n=3 ..."
    # 不会匹配 "n = 3"、"abc_n=3" 这类形式
    match = re.search(r"(?<!\S)n=(\d+)(?!\S)", text)
    if not match:
        return default_n, text

    try:
        raw_n = int(match.group(1))
    except Exception:
        raw_n = default_n

    image_n = raw_n if min_n <= raw_n <= max_n else default_n
    cleaned = (text[:match.start()] + " " + text[match.end():]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return image_n, cleaned


def get_path_suffix(path):
    path = urlparse(path).path
    return os.path.splitext(path)[-1].lstrip('.')


def convert_webp_to_png(webp_image):
    from PIL import Image
    try:
        webp_image.seek(0)
        img = Image.open(webp_image).convert("RGBA")
        png_image = io.BytesIO()
        img.save(png_image, format="PNG")
        png_image.seek(0)
        return png_image
    except Exception as e:
        logger.error(f"Failed to convert WEBP to PNG: {e}")
        raise


def remove_markdown_symbol(text: str):
    # 移除markdown格式，目前先移除**
    if not text:
        return text
    return re.sub(r'\*\*(.*?)\*\*', r'\1', text)


def extract_markdown_image_urls(text: str):
    if not text:
        return []
    urls = []
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
        inside = match.group(1).strip()
        if inside.startswith("<") and ">" in inside:
            inside = inside[1:inside.index(">")].strip()
        else:
            inside = inside.split()[0].strip().strip('"').strip("'")
        if inside.startswith("http://") or inside.startswith("https://"):
            urls.append(inside)
    for match in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.IGNORECASE):
        url = match.group(1).strip()
        if url.startswith("http://") or url.startswith("https://"):
            urls.append(url)
    if urls:
        return _unique_keep_order(urls)
    return extract_https_urls(text)


def extract_image_sources(text: str):
    """
    提取文本中的图片来源，支持：
    1) http/https 图片链接
    2) data:image/...;base64,... 数据
    3) 常见 JSON 字段中的 base64（如 b64_json/base64/image_base64）
    """
    if not text:
        return []

    sources = []

    def _append_if_image_source(raw: str):
        if not raw:
            return
        item = raw.strip()
        if item.startswith("<") and ">" in item:
            item = item[1:item.index(">")].strip()
        item = item.strip().strip('"').strip("'")
        if _is_http_url(item):
            sources.append(item)
            return
        if _is_data_image_url(item):
            sources.append(_normalize_data_image_url(item))

    # Markdown 图片语法
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
        inside = match.group(1).strip()
        if inside.startswith("<") and ">" in inside:
            inside = inside[1:inside.index(">")].strip()
        else:
            inside = inside.split()[0].strip().strip('"').strip("'")
        _append_if_image_source(inside)

    # HTML img 标签
    for match in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.IGNORECASE):
        _append_if_image_source(match.group(1).strip())

    # 文本中直接出现 data:image
    data_uri_pattern = r"data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+"
    for match in re.finditer(data_uri_pattern, text, flags=re.IGNORECASE):
        _append_if_image_source(match.group(0))

    # 常见 JSON base64 字段
    for key in ["b64_json", "base64", "image_base64"]:
        pattern = rf"\"{key}\"\s*:\s*\"([A-Za-z0-9+/=\s]+)\""
        for match in re.finditer(pattern, text):
            b64_payload = re.sub(r"\s+", "", match.group(1))
            if b64_payload:
                sources.append(f"data:image/png;base64,{b64_payload}")

    # 补充提取纯文本中的 URL（与 markdown/html 混合时也能识别）
    sources.extend(extract_https_urls(text))

    # 回退：尝试提取 http/https URL
    if not sources:
        return []

    return _unique_keep_order(sources)


def extract_https_urls(text: str):
    if not text:
        return []
    urls = []
    for match in re.finditer(r"https?://[^\s<>()\[\]\"']+", text):
        url = match.group(0).strip()
        url = url.rstrip(".,;:!?)]}\"'")
        if url.startswith("http://") or url.startswith("https://"):
            urls.append(url)
    return _unique_keep_order(urls)


def _unique_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def decode_base64_image(data: str):
    """
    解析 data:image 或裸 base64 图片字符串，返回 (mime_type, bytes)。
    解析失败返回 (None, None)。
    """
    if not data or not isinstance(data, str):
        return None, None

    raw = data.strip().strip('"').strip("'")
    mime_type = "image/png"
    payload = raw

    if _is_data_image_url(raw):
        header, payload = raw.split(",", 1)
        mime_match = re.match(r"^data:(image\/[a-zA-Z0-9.+-]+);base64$", header.strip(), re.IGNORECASE)
        if mime_match:
            mime_type = mime_match.group(1).lower()
    elif raw.lower().startswith("base64,"):
        payload = raw.split(",", 1)[1]
    else:
        # 裸 base64 做严格字符判断，避免误判普通文本
        if not re.fullmatch(r"[A-Za-z0-9+/=_\-\s]+", raw):
            return None, None
        if len(re.sub(r"\s+", "", raw)) < 64:
            return None, None

    payload = re.sub(r"\s+", "", payload)
    if not payload:
        return None, None

    # base64 padding 补齐
    payload += "=" * (-len(payload) % 4)
    image_bytes = None
    try:
        image_bytes = base64.b64decode(payload, validate=True)
    except Exception:
        try:
            image_bytes = base64.urlsafe_b64decode(payload)
        except Exception:
            return None, None

    if not _is_valid_image_bytes(image_bytes):
        return None, None
    return mime_type, image_bytes


def _is_http_url(url: str):
    return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))


def _is_valid_image_bytes(image_bytes: bytes):
    if not image_bytes or len(image_bytes) < 16:
        return False
    try:
        image_buffer = io.BytesIO(image_bytes)
        with Image.open(image_buffer) as image:
            image.verify()
        return True
    except Exception:
        return False


def _is_data_image_url(url: str):
    if not isinstance(url, str):
        return False
    return bool(re.match(r"^data:image\/[a-zA-Z0-9.+-]+;base64,", url.strip(), re.IGNORECASE))


def _normalize_data_image_url(url: str):
    if not _is_data_image_url(url):
        return url
    header, payload = url.split(",", 1)
    payload = re.sub(r"\s+", "", payload)
    return f"{header},{payload}"
