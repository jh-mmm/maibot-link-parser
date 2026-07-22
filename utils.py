"""链接解析器工具函数模块。

提供 HTTP 请求、文本处理、数字格式化等通用工具函数，
供各平台解析器共享使用。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 通用浏览器请求头，模拟正常浏览器访问以减少被反爬拦截的概率
# ---------------------------------------------------------------------------
COMMON_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------------------
# URL 提取正则
# ---------------------------------------------------------------------------
_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]]+",
    re.IGNORECASE,
)

# HTML 标签匹配正则
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

# 多个空白字符压缩正则
_WHITESPACE_PATTERN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# HTTP 请求工具
# ---------------------------------------------------------------------------


async def fetch_json(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
    allow_redirects: bool = True,
) -> dict:
    """发起 HTTP GET 请求并返回 JSON 响应。

    Args:
        url: 请求目标 URL。
        headers: 自定义请求头，为 None 时使用 COMMON_HEADERS。
        timeout: 请求超时时间（秒）。
        allow_redirects: 是否跟随 HTTP 重定向。

    Returns:
        解析后的 JSON 字典。

    Raises:
        aiohttp.ClientError: 网络请求失败时抛出。
        ValueError: 响应体无法解析为 JSON 时抛出。
    """
    request_headers = {**COMMON_HEADERS, **(headers or {})}
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout,
            headers=request_headers,
        ) as session:
            async with session.get(url, allow_redirects=allow_redirects) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                logger.debug("成功获取 JSON: %s (状态码 %d)", url, response.status)
                return data
    except aiohttp.ContentTypeError as exc:
        logger.error("响应内容无法解析为 JSON: %s — %s", url, exc)
        raise ValueError(f"非 JSON 响应: {url}") from exc
    except aiohttp.ClientError as exc:
        logger.error("请求失败: %s — %s", url, exc)
        raise


async def fetch_text(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 15,
) -> str:
    """发起 HTTP GET 请求并返回文本响应。

    Args:
        url: 请求目标 URL。
        headers: 自定义请求头，为 None 时使用 COMMON_HEADERS。
        timeout: 请求超时时间（秒）。

    Returns:
        响应文本内容。

    Raises:
        aiohttp.ClientError: 网络请求失败时抛出。
    """
    request_headers = {**COMMON_HEADERS, **(headers or {})}
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout,
            headers=request_headers,
        ) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                text = await response.text()
                logger.debug("成功获取文本: %s (状态码 %d, 长度 %d)", url, response.status, len(text))
                return text
    except aiohttp.ClientError as exc:
        logger.error("请求失败: %s — %s", url, exc)
        raise


# ---------------------------------------------------------------------------
# 文本处理工具
# ---------------------------------------------------------------------------


def truncate_text(text: str, max_length: int = 500) -> str:
    """截断文本至指定长度，尽量在句子边界处截断。

    优先在句号、问号、感叹号等标点处截断，保持语义完整性。
    如果在标点处截断后文本仍超过限制，则强制在 max_length 处截断。

    Args:
        text: 待截断的文本。
        max_length: 最大字符数（默认 500）。

    Returns:
        截断后的文本，超长时末尾追加 "..."。
    """
    if not text or len(text) <= max_length:
        return text

    # 在最大长度范围内，尝试找到最后一个句子结束标点
    truncated = text[:max_length]
    sentence_endings = re.compile(r"[。！？.!?\n]")

    # 从截断位置向前查找最近的句子结束符
    last_end = -1
    for match in sentence_endings.finditer(truncated):
        last_end = match.end()

    # 如果找到了合适的断句点且不会导致内容过短（至少保留 1/3 长度）
    if last_end > max_length // 3:
        return truncated[:last_end].rstrip() + "..."

    # 没有合适的断句点，强制在 max_length 处截断
    return truncated.rstrip() + "..."


def clean_html(html_text: str) -> str:
    """清除 HTML 标签并整理文本。

    移除所有 HTML 标签，解码 HTML 实体（如 &amp; → &），
    并将连续空白字符压缩为单个空格。

    Args:
        html_text: 包含 HTML 标签的文本。

    Returns:
        清理后的纯文本。
    """
    if not html_text:
        return ""

    # 将 <br> / <br/> / <p> 转换为换行符，保留段落结构
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)

    # 移除所有 HTML 标签
    text = _HTML_TAG_PATTERN.sub("", text)

    # 解码 HTML 实体
    text = html.unescape(text)

    # 压缩连续空白（保留换行符）
    lines = text.splitlines()
    cleaned_lines = [_WHITESPACE_PATTERN.sub(" ", line).strip() for line in lines]
    text = "\n".join(line for line in cleaned_lines if line)

    return text.strip()


def format_number(num: int) -> str:
    """将数字格式化为易读的中文表示。

    - < 10,000: 原样显示（如 "9,999"）
    - >= 10,000: 以"万"为单位（如 "1.2万"）
    - >= 100,000,000: 以"亿"为单位（如 "1.5亿"）

    Args:
        num: 待格式化的整数。

    Returns:
        格式化后的字符串。
    """
    if num < 0:
        return f"-{format_number(-num)}"

    if num >= 100_000_000:
        value = num / 100_000_000
        # 整数亿不显示小数点
        if value == int(value):
            return f"{int(value)}亿"
        return f"{value:.1f}亿"

    if num >= 10_000:
        value = num / 10_000
        if value == int(value):
            return f"{int(value)}万"
        return f"{value:.1f}万"

    return f"{num:,}"


def extract_urls(text: str) -> list[str]:
    """从文本中提取所有 HTTP/HTTPS URL。

    使用正则表达式匹配文本中的 URL，支持常见格式。
    会自动去除 URL 末尾可能误匹配的标点符号。

    Args:
        text: 待提取 URL 的文本。

    Returns:
        提取到的 URL 列表（保持出现顺序，自动去重）。
    """
    if not text:
        return []

    matches = _URL_PATTERN.findall(text)

    # 清理 URL 末尾可能误匹配的标点
    cleaned: list[str] = []
    seen: set[str] = set()
    trailing_punct = re.compile(r"[.,;:!?。，；：！？…）)》」』]+$")

    for url in matches:
        url = trailing_punct.sub("", url)
        if url and url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned
