"""知乎链接解析器"""
from __future__ import annotations

import asyncio
from datetime import datetime
import html
import json
import logging
import re
from typing import Any, Callable, ClassVar
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from curl_cffi import requests as curl_requests

from .base import BaseParser, ParseResult
from ..utils import COMMON_HEADERS, clean_html, truncate_text

logger = logging.getLogger("plugin.com.maibot.link-parser.zhihu")

# 文本智能截断断句符号
_SENTENCE_MARKERS = ("。", "！", "？", "；", "…", "!", "?", ";", "\n")

# 视频与多媒体解析匹配键
_VIDEO_URL_KEYS = (
    "playurl",
    "playurlhd",
    "videourl",
    "originvideourl",
    "videoplayurl",
    "playlist",
    "src",
    "url",
)
_VIDEO_COVER_KEYS = (
    "cover",
    "coverurl",
    "thumbnail",
    "thumbnailurl",
    "imageurl",
    "poster",
)


def _normalize_text(text: str, *, keep_newlines: bool = False) -> str:
    """清理并规整文本，统一换行与空白字符。"""
    if not text:
        return ""
    value = html.unescape(text)
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    if keep_newlines:
        value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
    else:
        value = re.sub(r"\s+", " ", value)
    return value.strip()


def _smart_truncate(text: str, max_length: int = 500) -> str:
    """在最大长度限制内，尽量在句子终结标点处平滑截断文本。"""
    if not text or len(text) <= max_length:
        return text

    window = text[: max_length + 1]
    last_break = max((window.rfind(m) for m in _SENTENCE_MARKERS), default=-1)
    if last_break >= max_length // 3:
        cut = window[: last_break + 1].strip()
        if not cut[-1] in "。！？!?":
            return cut + "..."
        return cut
    return text[:max_length].rstrip(" ，,；;。！？!?、") + "..."


def _normalize_media_url(url: str | None, page_url: str | None = None) -> str:
    """补全与规整媒体资源 URL。"""
    if not url:
        return ""
    value = html.unescape(str(url)).strip().strip("\"'")
    value = value.replace("\\u002F", "/").replace("\\/", "/")
    value = value.replace("&amp;", "&")
    value = value.rstrip(".,);")
    if not value or value.startswith(("data:", "blob:")):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    elif page_url and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = urljoin(page_url, value)
    elif value.startswith("/"):
        value = "https://www.zhihu.com" + value
    if not value.startswith(("http://", "https://")):
        return ""
    return value


def _looks_like_image_url(url: str | None) -> bool:
    """判断链接是否为静态图片。"""
    if not url:
        return False
    val = url.lower()
    if not val.startswith(("http://", "https://")):
        return False
    if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", val):
        return False
    if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|avif)(?:$|[?#])", val):
        return True
    return any(
        host in val
        for host in (
            "picx.zhimg.com",
            "pic1.zhimg.com",
            "pic2.zhimg.com",
            "zhimg.com",
        )
    )


def _looks_like_video_url(url: str | None) -> bool:
    """判断链接是否为视频流或视频文件。"""
    if not url:
        return False
    val = url.lower()
    if not val.startswith(("http://", "https://")):
        return False
    if _looks_like_image_url(val):
        return False
    if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", val):
        return True
    return any(
        marker in val
        for marker in (
            "video.zhihu.com",
            "/playlist.m3u8",
            "/playback/",
            "/stream/",
            ".vod.",
        )
    )


def _media_key(url: str | None) -> str:
    """计算用于媒体去重的唯一 Key。"""
    normalized = _normalize_media_url(url)
    if not normalized:
        return ""
    val = normalized.split("#", 1)[0]
    if _looks_like_image_url(val) or _looks_like_video_url(val):
        val = val.split("?", 1)[0]
    return val.replace("http://", "https://")


def _format_timestamp(value: Any) -> str | None:
    """将时间戳转换为格式化时间字符串。"""
    if value is None:
        return None
    try:
        ts = int(value)
        if ts <= 0:
            return None
        if ts >= 10**11:
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _extract_code_block(pre_tag: Tag) -> tuple[str, str]:
    """从 pre/code 标签中提取代码内容与语言标识。"""
    code_tag = pre_tag.find("code")
    source = code_tag if isinstance(code_tag, Tag) else pre_tag
    code = html.unescape(source.get_text("", strip=False))
    lang = ""
    for tag in (pre_tag, code_tag):
        if not isinstance(tag, Tag):
            continue
        for key in ("data-language", "data-lang", "lang"):
            v = tag.get(key)
            if isinstance(v, str) and v.strip():
                lang = v.strip()
                break
        if lang:
            break
        classes = tag.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        for item in classes:
            m = re.search(r"^(?:language|lang)[-_]([A-Za-z0-9_+#.-]+)$", item, re.I)
            if m:
                lang = m.group(1)
                break
        if lang:
            break
    lang = re.sub(r"[^A-Za-z0-9_+#.-]+", "", lang)
    return code.strip("\n"), lang


def _format_list_node(list_node: Tag) -> str:
    """递归格式化 ul/ol 列表，保持嵌套缩进与序号。"""
    items = []
    ordered = (list_node.name or "").lower() == "ol"
    for li in list_node.find_all("li", recursive=False):
        li_copy = BeautifulSoup(str(li), "html.parser").find("li")
        if li_copy is None:
            continue
        nested_texts = []
        for nested in li.find_all(["ul", "ol"], recursive=False):
            sub_text = _format_list_node(nested)
            if sub_text:
                nested_texts.append(
                    "\n".join(f"  {line}" for line in sub_text.splitlines())
                )
        for nested in li_copy.find_all(["ul", "ol"]):
            nested.decompose()
        base_text = _normalize_text(li_copy.get_text(" ", strip=True))
        combined = "\n".join([part for part in [base_text, *nested_texts] if part])
        if combined:
            items.append(combined)
    lines = []
    for idx, itm in enumerate(items, start=1):
        prefix = f"{idx}. " if ordered else "- "
        parts = itm.splitlines() or [itm]
        lines.append(prefix + parts[0])
        indent = " " * len(prefix)
        for p in parts[1:]:
            lines.append(indent + p)
    return "\n".join(lines).strip()


def _extract_content_and_media(
    html_text: str, page_url: str = ""
) -> tuple[str, list[str], str | None, str | None]:
    """从知乎正文 HTML 中结构化提取 Markdown 格式文本、图片列表、视频直链及封面。

    Returns:
        (markdown_text, image_urls, video_url, cover_image_url)
    """
    if not html_text or not html_text.strip():
        return "", [], None, None

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()

    images: list[str] = []
    seen_images: set[str] = set()
    videos: list[str] = []
    seen_videos: set[str] = set()
    cover_url: str | None = None

    # 1. 扫描媒体标签（图片、视频、封面）
    for img in soup.find_all("img"):
        url = (
            img.get("data-rawsrc")
            or img.get("data-original")
            or img.get("data-actualsrc")
            or img.get("data-default-watermark-src")
            or img.get("src")
            or ""
        )
        norm = _normalize_media_url(url, page_url)
        if norm and _looks_like_image_url(norm):
            k = _media_key(norm)
            if k and k not in seen_images:
                seen_images.add(k)
                images.append(norm)

    for node in soup.find_all(["video", "source", "iframe"]):
        vurl = (
            node.get("src")
            or node.get("data-src")
            or node.get("data-original")
            or node.get("playUrl")
            or ""
        )
        norm_v = _normalize_media_url(vurl, page_url)
        if norm_v and _looks_like_video_url(norm_v):
            vk = _media_key(norm_v)
            if vk and vk not in seen_videos:
                seen_videos.add(vk)
                videos.append(norm_v)
        poster = node.get("poster") or node.get("cover")
        if poster and not cover_url:
            norm_c = _normalize_media_url(poster, page_url)
            if norm_c and _looks_like_image_url(norm_c):
                cover_url = norm_c

    # 2. 递归遍历 DOM 树生成结构化 Markdown 块
    blocks: list[str] = []

    def process_node(node: Tag | NavigableString):
        if isinstance(node, NavigableString):
            t = _normalize_text(str(node))
            if t:
                blocks.append(t)
            return
        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if name in {
            "script",
            "style",
            "noscript",
            "svg",
            "video",
            "source",
            "iframe",
            "img",
        }:
            return

        if name == "br":
            if blocks:
                blocks[-1] = blocks[-1].rstrip() + "\n"
            return

        if name == "pre":
            code, lang = _extract_code_block(node)
            if code:
                blocks.append(
                    f"```{lang}\n{code}\n```" if lang else f"```\n{code}\n```"
                )
            return

        if name == "blockquote":
            q_text = _normalize_text(
                node.get_text("\n", strip=True), keep_newlines=True
            )
            if q_text:
                blocks.append(
                    "\n".join(
                        f"> {line}" if line else ">" for line in q_text.splitlines()
                    )
                )
            return

        if name in {"ul", "ol"}:
            l_text = _format_list_node(node)
            if l_text:
                blocks.append(l_text)
            return

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            lvl = int(name[1])
            prefix = "#" * lvl + " "
            t = _normalize_text(node.get_text(" ", strip=True))
            if t:
                blocks.append(f"{prefix}{t}")
            return

        if name == "hr":
            blocks.append("---")
            return

        if name in {
            "p",
            "div",
            "section",
            "article",
            "figure",
            "figcaption",
            "table",
            "tr",
        }:
            has_sub_blocks = any(
                isinstance(c, Tag)
                and (c.name or "").lower()
                in {
                    "p",
                    "div",
                    "section",
                    "article",
                    "blockquote",
                    "ul",
                    "ol",
                    "pre",
                    "figure",
                    "table",
                    "hr",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                }
                for c in node.children
            )
            if has_sub_blocks:
                for c in node.children:
                    if isinstance(c, (Tag, NavigableString)):
                        process_node(c)
                return
            t = _normalize_text(node.get_text(" ", strip=True))
            if t:
                blocks.append(t)
            return

        for c in node.children:
            if isinstance(c, (Tag, NavigableString)):
                process_node(c)

    for ch in soup.children:
        if isinstance(ch, (Tag, NavigableString)):
            process_node(ch)

    compacted: list[str] = []
    for b in blocks:
        v = b.strip()
        if not v:
            continue
        compacted.append(v)

    markdown_text = "\n\n".join(compacted).strip()
    primary_video = videos[0] if videos else None
    if not cover_url and images:
        cover_url = images[0]

    return markdown_text, images, primary_video, cover_url


def _extract_videos_from_state(
    initial_data: dict[str, Any], page_url: str = ""
) -> list[dict[str, str | None]]:
    """递归遍历知乎页面的 initialState 状态树，提取视频直链与封面。"""
    entries: list[dict[str, str | None]] = []
    seen: set[int] = set()
    queue: list[Any] = [initial_data.get("initialState") or initial_data]

    while queue:
        val = queue.pop()
        if isinstance(val, dict):
            mid = id(val)
            if mid in seen:
                continue
            seen.add(mid)

            lowered = {str(k).lower(): (k, v) for k, v in val.items()}
            vurl = None
            for vk in _VIDEO_URL_KEYS:
                if vk in lowered:
                    _, cand = lowered[vk]
                    if isinstance(cand, str) and _looks_like_video_url(cand):
                        vurl = _normalize_media_url(cand, page_url)
                        break
                    elif isinstance(cand, dict):
                        for sub_k in _VIDEO_URL_KEYS:
                            for nested_v in cand.values():
                                if (
                                    isinstance(nested_v, dict)
                                    and sub_k in nested_v
                                    and isinstance(nested_v[sub_k], str)
                                    and _looks_like_video_url(nested_v[sub_k])
                                ):
                                    vurl = _normalize_media_url(
                                        nested_v[sub_k], page_url
                                    )
                                    break
                                elif (
                                    isinstance(nested_v, str)
                                    and _looks_like_video_url(nested_v)
                                ):
                                    vurl = _normalize_media_url(
                                        nested_v, page_url
                                    )
                                    break
                            if vurl:
                                break
                    if vurl:
                        break

            if vurl:
                c_url = None
                for ck in _VIDEO_COVER_KEYS:
                    if ck in lowered:
                        _, cand = lowered[ck]
                        if isinstance(cand, str) and _looks_like_image_url(cand):
                            c_url = _normalize_media_url(cand, page_url)
                            break
                title = val.get("title") or val.get("name")
                entries.append(
                    {
                        "url": vurl,
                        "cover_url": c_url,
                        "title": str(title) if title else None,
                    }
                )

            queue.extend(val.values())
        elif isinstance(val, list):
            mid = id(val)
            if mid in seen:
                continue
            seen.add(mid)
            queue.extend(val)

    unique_entries: list[dict[str, str | None]] = []
    seen_keys: set[str] = set()
    for e in entries:
        k = _media_key(e.get("url"))
        if k and k not in seen_keys:
            seen_keys.add(k)
            unique_entries.append(e)
    return unique_entries


class ZhihuParser(BaseParser):
    platform_name: ClassVar[str] = "知乎"
    platform_icon: ClassVar[str] = "💡"
    config_key: ClassVar[str] = "zhihu"
    url_patterns: ClassVar[list[re.Pattern]] = [
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/question/(\d+)/answer/(\d+)"),
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/question/(\d+)(?:[?#]|$)"),
        re.compile(r"(?:https?://)?zhuanlan\.zhihu\.com/p/(\d+)"),
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/pin/(\d+)"),
    ]

    # 浏览器指纹配置：(名称, curl_cffi impersonate 值, 匹配的 User-Agent)。
    _PROFILES: ClassVar[tuple[tuple[str, str, str], ...]] = (
        (
            "desktop",
            "chrome",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ),
        (
            "ios",
            "safari_ios",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
            "Mobile/15E148 Safari/604.1",
        ),
        (
            "mobile",
            "chrome_android",
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        ),
    )

    def __init__(
        self,
        cookies: str = "",
        timeout: int = 15,
        max_content_length: int = 500,
        proxy: str = "",
    ) -> None:
        """初始化知乎链接解析器。

        Args:
            cookies: 知乎登录 Cookies（遇到风控/登录页时填写）。
            timeout: HTTP 请求超时（秒）。
            max_content_length: 正文摘要最大字符数。
            proxy: HTTP/HTTPS 代理地址（如 http://127.0.0.1:7890），留空不使用。
        """
        self._cookies = cookies.strip()
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._proxy = proxy.strip()

    def _media_headers(self) -> dict[str, str]:
        """构造用于下载知乎防盗链图片和视频的请求头。"""
        headers = {
            "Referer": "https://www.zhihu.com/",
            "User-Agent": self._PROFILES[0][2],
        }
        if self._cookies:
            headers["Cookie"] = self._cookies
        return headers

    def _extra_payload(self) -> dict[str, Any]:
        """构造附带下载凭据与代理的 extra 字段。"""
        return {
            "media_headers": self._media_headers(),
            "proxy": self._proxy,
        }

    async def parse(self, url: str, match: re.Match) -> ParseResult:
        full_url = match.group(0)
        if not full_url.startswith("http"):
            full_url = "https://" + full_url

        pattern_str = match.re.pattern
        if "answer" in pattern_str:
            qid = match.group(1)
            aid = match.group(2)
            return await self._parse_answer(qid, aid, full_url)
        elif "zhuanlan" in pattern_str:
            article_id = match.group(1)
            return await self._parse_article(article_id, full_url)
        elif "pin" in pattern_str:
            pin_id = match.group(1)
            return await self._parse_pin(pin_id, full_url)
        else:
            qid = match.group(1)
            return await self._parse_question(qid, full_url)

    async def _parse_answer(self, qid: str, aid: str, url: str) -> ParseResult:
        """解析知乎回答。

        优先走官方 JSON 接口规避 HTML 反爬，接口失败时回退到页面 initialData 解析。
        """
        try:
            payload = await self._fetch_json_data(
                self._answer_api_url(aid),
                validator=lambda data: self._has_answer_api_payload(data, aid),
            )
            return self._build_from_api_answer(payload, url)
        except Exception as exc:
            logger.debug("知乎回答 JSON 接口失败，回退到页面解析: %s", exc)

        initial_data = await self._fetch_initial_data(
            url,
            validator=lambda data: self._has_answer_entities(data, qid, aid),
        )
        entities = initial_data["initialState"]["entities"]
        answer = entities["answers"][aid]
        question = entities["questions"][qid]

        author = answer.get("author") or {}
        question_title = question.get("title", "知乎回答")
        author_name = author.get("name", "匿名用户")
        headline = author.get("headline", "")
        author_avatar = author.get("avatarUrl", "")
        content_html = answer.get("content", "") or ""
        voteup = answer.get("voteupCount", 0)
        comment_count = answer.get("commentCount", 0)
        fav_count = answer.get("favlistsCount") or answer.get("favoriteCount", 0)

        # 结构化提取正文 Markdown、图片与视频
        content_md, content_images, video_url, cover = _extract_content_and_media(
            content_html, url
        )

        # 状态树深度提取视频
        if not video_url:
            state_videos = _extract_videos_from_state(initial_data, url)
            if state_videos:
                video_url = state_videos[0].get("url")
                if not cover and state_videos[0].get("cover_url"):
                    cover = state_videos[0].get("cover_url")

        content_text = _smart_truncate(content_md, self._max_content_length)
        if not cover and content_images:
            cover = content_images[0]

        author_display = (
            f"{author_name} ({headline})"
            if headline and author_name != "匿名用户"
            else author_name
        )

        return ParseResult(
            title=question_title,
            author=author_display,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover or "",
            images=content_images,
            video_url=video_url or "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={
                "likes": voteup,
                "comments": comment_count,
                "favorites": fav_count,
            },
            extra=self._extra_payload(),
        )

    async def _parse_article(self, article_id: str, url: str) -> ParseResult:
        """解析知乎专栏文章。

        优先走专栏 JSON 接口（zhuanlan.zhihu.com/api/articles/{article_id}）规避页面
        反爬与 JS 挑战，接口失败时回退到页面 initialData 解析。
        """
        try:
            payload = await self._fetch_json_data(
                self._article_api_url(article_id),
                validator=lambda data: self._has_article_api_payload(data, article_id),
            )
            return self._build_from_api_article(payload, url)
        except Exception as exc:
            logger.debug("知乎专栏 JSON 接口失败，回退到页面解析: %s", exc)

        initial_data = await self._fetch_initial_data(
            url,
            validator=lambda data: self._has_article_entity(data, article_id),
        )
        entities = initial_data["initialState"]["entities"]
        article = (entities.get("articles") or {}).get(article_id)
        if not isinstance(article, dict):
            raise RuntimeError("知乎专栏数据不存在")

        return self._build_from_api_article(article, url, initial_data=initial_data)

    def _build_from_api_article(
        self,
        article: dict,
        url: str,
        *,
        initial_data: dict | None = None,
    ) -> ParseResult:
        """由专栏文章实体（来自 JSON API 或 initialData）构造 ParseResult。"""
        author = article.get("author") or {}
        title = article.get("title", "知乎文章")
        author_name = author.get("name", "")
        headline = author.get("headline", "")
        author_avatar = (
            author.get("avatar_url")
            or author.get("avatarUrl")
            or author.get("avatar_url_template")
            or ""
        )
        content_html = article.get("content", "") or ""
        voteup = article.get("voteup_count", article.get("voteupCount", 0))
        comment_count = article.get("comment_count", article.get("commentCount", 0))
        fav_count = article.get(
            "favlists_count",
            article.get("favlistsCount", article.get("favoriteCount", 0)),
        )
        cover = (
            article.get("image_url")
            or article.get("title_image")
            or article.get("imageUrl")
            or ""
        )

        column = article.get("column") or {}
        column_title = column.get("title") if isinstance(column, dict) else ""
        if column_title:
            title = f"[{column_title}] {title}"

        content_md, content_images, video_url, media_cover = _extract_content_and_media(
            content_html, url
        )

        if not video_url:
            source_state = initial_data or article
            raw_videos = _extract_videos_from_state(source_state, url)
            if raw_videos:
                video_url = raw_videos[0].get("url")
                if not cover and raw_videos[0].get("cover_url"):
                    cover = raw_videos[0].get("cover_url")

        content_text = _smart_truncate(content_md, self._max_content_length)
        if not cover:
            cover = media_cover or (content_images[0] if content_images else "")

        author_display = (
            f"{author_name} ({headline})" if headline else author_name
        )

        return ParseResult(
            title=title,
            author=author_display,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover or "",
            images=content_images,
            video_url=video_url or "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={
                "likes": voteup,
                "comments": comment_count,
                "favorites": fav_count,
            },
            extra=self._extra_payload(),
        )

    async def _parse_question(self, qid: str, url: str) -> ParseResult:
        """解析知乎问题页：取问题实体 + 默认排序首条回答全文。"""
        try:
            payload = await self._fetch_json_data(
                self._question_answers_api_url(qid),
                validator=lambda data: self._has_question_answers_payload(data, qid),
            )
            answers = payload.get("data") or []
            if answers and isinstance(answers[0], dict):
                first = answers[0]
                title = (first.get("question") or {}).get("title", "知乎问题")
                return self._build_from_api_answer(first, url, title=title)
        except Exception as exc:
            logger.debug("知乎问题 JSON 接口失败，回退到页面解析: %s", exc)

        initial_data = await self._fetch_initial_data(
            url,
            validator=lambda data: self._has_question_entity(data, qid),
        )
        entities = initial_data["initialState"]["entities"]
        question = (entities.get("questions") or {}).get(qid)
        if not isinstance(question, dict):
            raise RuntimeError("知乎问题数据不存在")

        question_title = question.get("title", "知乎问题")
        detail_html = question.get("detail", "") or ""
        detail_md, detail_images, _, _ = _extract_content_and_media(
            detail_html, url
        )
        detail_text = _smart_truncate(detail_md, self._max_content_length)

        answer_count = question.get("answerCount", question.get("answer_count", 0))
        follower_count = question.get(
            "followerCount", question.get("follower_count", 0)
        )

        answer_id = self._pick_first_answer_id(initial_data, qid)
        if not answer_id:
            return ParseResult(
                title=question_title,
                content=detail_text
                or f"{answer_count} 个回答 · {follower_count} 人关注",
                url=url,
                platform=self.platform_name,
                platform_icon=self.platform_icon,
                stats={"comments": answer_count, "likes": follower_count},
                extra=self._extra_payload(),
            )

        answer = await self._load_answer_for_question(
            qid=qid,
            answer_id=answer_id,
            question_data=initial_data,
        )

        author = answer.get("author") or {}
        author_name = author.get("name", "匿名用户")
        headline = author.get("headline", "")
        author_avatar = author.get("avatarUrl", "")
        content_html = answer.get("content", "") or ""
        voteup = answer.get("voteupCount", 0)
        comment_count = answer.get("commentCount", 0)
        fav_count = answer.get("favlistsCount") or answer.get("favoriteCount", 0)

        # 结构化提取首条回答
        ans_md, ans_images, video_url, cover = _extract_content_and_media(
            content_html, url
        )

        if not video_url:
            state_videos = _extract_videos_from_state(initial_data, url)
            if state_videos:
                video_url = state_videos[0].get("url")
                if not cover and state_videos[0].get("cover_url"):
                    cover = state_videos[0].get("cover_url")

        ans_text = _smart_truncate(ans_md, self._max_content_length)
        content_images = list(dict.fromkeys([*detail_images, *ans_images]))
        if not cover and content_images:
            cover = content_images[0]

        author_display = (
            f"{author_name} ({headline})"
            if headline and author_name != "匿名用户"
            else author_name
        )

        if detail_text and ans_text:
            content = f"【问题描述】\n{detail_text}\n\n-- 首条回答（答主: {author_name}）--\n{ans_text}"
        elif ans_text:
            content = ans_text
        else:
            content = detail_text

        return ParseResult(
            title=question_title,
            author=author_display,
            author_avatar=author_avatar,
            content=content,
            cover_image=cover or "",
            images=content_images,
            video_url=video_url or "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={
                "likes": voteup,
                "comments": comment_count,
                "favorites": fav_count,
            },
            extra=self._extra_payload(),
        )

    async def _parse_pin(self, pin_id: str, url: str) -> ParseResult:
        """解析知乎想法。

        使用基于 curl_cffi 的浏览器指纹轮询请求 Pin API，包含完整字段参数。
        """
        payload = await self._fetch_json_data(
            self._pin_api_url(pin_id),
            validator=lambda data: self._has_pin_payload(data, pin_id),
        )

        author = payload.get("author") or {}
        author_name = author.get("name", "知乎用户")
        headline = author.get("headline", "")
        author_avatar = author.get("avatar_url", "")

        # 想法内容可能在 content_html 或 content 列表中
        content_html = payload.get("content_html") or payload.get("contentHtml") or ""
        content_md, images, video_url, cover = "", [], None, None

        if content_html:
            content_md, images, video_url, cover = _extract_content_and_media(
                content_html, url
            )

        # 兜底从 content 列表提取图文和视频
        content_list = payload.get("content")
        if isinstance(content_list, list):
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "text" and not content_md:
                    content_md = _normalize_text(
                        item.get("content", ""), keep_newlines=True
                    )
                elif itype == "image":
                    img_url = _normalize_media_url(item.get("url"), url)
                    if img_url and img_url not in images:
                        images.append(img_url)
                elif itype == "video" and not video_url:
                    v_url = (
                        item.get("playlist", {}).get("hd", {}).get("url")
                        or item.get("video_url")
                        or item.get("url")
                    )
                    norm_v = _normalize_media_url(v_url, url)
                    if norm_v:
                        video_url = norm_v
                    poster = _normalize_media_url(
                        item.get("cover_url") or item.get("thumbnail"), url
                    )
                    if poster and not cover:
                        cover = poster

        content_text = _smart_truncate(content_md, self._max_content_length)
        if not cover and images:
            cover = images[0]

        like_count = payload.get("like_count") or payload.get("voteup_count", 0)
        comment_count = payload.get("comment_count", 0)
        fav_count = payload.get("favlists_count") or payload.get(
            "favorite_count", 0
        )

        author_display = (
            f"{author_name} ({headline})" if headline else author_name
        )

        return ParseResult(
            title="知乎想法",
            author=author_display,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover or "",
            images=images,
            video_url=video_url or "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={
                "likes": like_count,
                "comments": comment_count,
                "favorites": fav_count,
            },
            extra=self._extra_payload(),
        )

    def _build_from_api_answer(
        self,
        answer: dict,
        url: str,
        *,
        title: str | None = None,
    ) -> ParseResult:
        """由 JSON 接口的回答实体构造 ParseResult。"""
        author = answer.get("author") or {}
        question = answer.get("question") or {}
        question_title = title or question.get("title", "知乎回答")
        author_name = author.get("name", "匿名用户")
        headline = author.get("headline", "")
        author_avatar = author.get("avatar_url", "")
        content_html = answer.get("content", "") or ""
        voteup = answer.get("voteup_count", 0)
        comment_count = answer.get("comment_count", 0)
        fav_count = answer.get("favlists_count") or answer.get("favorite_count", 0)

        # 结构化提取
        content_md, content_images, video_url, cover = _extract_content_and_media(
            content_html, url
        )

        # 兜底从 answer dict 提取视频
        if not video_url:
            raw_videos = _extract_videos_from_state(answer, url)
            if raw_videos:
                video_url = raw_videos[0].get("url")
                if not cover and raw_videos[0].get("cover_url"):
                    cover = raw_videos[0].get("cover_url")

        content_text = _smart_truncate(content_md, self._max_content_length)
        if not cover and content_images:
            cover = content_images[0]

        author_display = (
            f"{author_name} ({headline})"
            if headline and author_name != "匿名用户"
            else author_name
        )

        return ParseResult(
            title=question_title,
            author=author_display,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover or "",
            images=content_images,
            video_url=video_url or "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={
                "likes": voteup,
                "comments": comment_count,
                "favorites": fav_count,
            },
            extra=self._extra_payload(),
        )

    async def _fetch_initial_data(
        self,
        url: str,
        *,
        validator: Callable[[dict], bool],
    ) -> dict:
        """按浏览器指纹请求策略读取知乎页面 initialData。"""
        last_error: Exception | None = None
        saw_challenge = False
        saw_login = False
        saw_invalid_target = False
        saw_initial_data = False

        for name, headers, impersonate in self._request_profiles():
            try:
                response_ctx = await self._request_text(
                    url,
                    headers=headers,
                    impersonate=impersonate,
                )
                html_text = response_ctx["text"]
                final_url = response_ctx["final_url"]
                status_code = response_ctx["status_code"]

                if self._is_challenge_page(html_text, status_code=status_code):
                    saw_challenge = True
                    logger.debug("知乎 %s 命中反爬挑战页: %s -> %s", name, url, final_url)
                    continue

                if self._is_login_page(final_url, html_text):
                    saw_login = True
                    logger.debug("知乎 %s 命中登录页: %s -> %s", name, url, final_url)
                    continue

                initial_data = self._extract_initial_data(html_text)
                if not initial_data:
                    logger.debug(
                        "知乎 %s 未找到可解析 initialData: %s -> %s, status=%s",
                        name,
                        url,
                        final_url,
                        status_code,
                    )
                    continue

                saw_initial_data = True
                if validator(initial_data):
                    logger.debug("知乎 %s 请求成功: %s -> %s", name, url, final_url)
                    return initial_data

                saw_invalid_target = True
                logger.debug(
                    "知乎 %s 拿到的页面不是目标页: %s -> %s, status=%s",
                    name,
                    url,
                    final_url,
                    status_code,
                )
            except Exception as exc:
                last_error = exc
                logger.debug("知乎 %s 请求失败: %s, error=%s", name, url, exc)

        if saw_challenge:
            if self._cookies:
                raise RuntimeError("知乎抓取失败：当前 cookies 可能失效，或请求仍被风控拦截")
            raise RuntimeError("知乎抓取失败：站点返回反爬挑战页，请配置有效 cookies")

        if saw_login:
            if self._cookies:
                raise RuntimeError("知乎抓取失败：当前 cookies 可能失效，或权限不足")
            raise RuntimeError("知乎抓取失败：当前请求被引导到登录页，请配置有效 cookies")

        if saw_invalid_target or saw_initial_data:
            raise RuntimeError("知乎抓取失败：未拿到目标知乎页面")

        raise RuntimeError("知乎页面抓取失败") from last_error

    async def _fetch_json_data(
        self,
        url: str,
        *,
        validator: Callable[[dict], bool],
    ) -> dict:
        """按浏览器指纹请求知乎 JSON 接口，返回通过校验的 payload。"""
        last_error: Exception | None = None
        saw_challenge = False
        saw_forbidden = False
        saw_login = False
        saw_invalid_target = False
        saw_json_payload = False

        for name, headers, impersonate in self._request_profiles(
            accept="application/json, text/plain, */*"
        ):
            try:
                response_ctx = await self._request_text(
                    url,
                    headers=headers,
                    impersonate=impersonate,
                )
                body_text = response_ctx["text"]
                final_url = response_ctx["final_url"]
                status_code = response_ctx["status_code"]
                content_type = response_ctx.get("content_type") or ""

                if self._is_challenge_page(body_text, status_code=status_code):
                    saw_challenge = True
                    logger.debug("知乎 %s JSON 接口命中反爬挑战: %s -> %s", name, url, final_url)
                    continue

                if self._is_login_page(final_url, body_text):
                    saw_login = True
                    logger.debug("知乎 %s JSON 接口命中登录页: %s -> %s", name, url, final_url)
                    continue

                if status_code in (401, 403):
                    saw_forbidden = True
                    logger.debug(
                        "知乎 %s JSON 接口无权限: %s -> %s, status=%s",
                        name,
                        url,
                        final_url,
                        status_code,
                    )
                    continue

                if status_code >= 400:
                    saw_invalid_target = True
                    logger.debug(
                        "知乎 %s JSON 接口状态异常: %s -> %s, status=%s",
                        name,
                        url,
                        final_url,
                        status_code,
                    )
                    continue

                payload = self._extract_json_payload(
                    body_text, content_type=content_type
                )
                if payload is None:
                    logger.debug(
                        "知乎 %s JSON 接口未返回可解析 JSON: %s -> %s, ct=%s",
                        name,
                        url,
                        final_url,
                        content_type,
                    )
                    continue

                saw_json_payload = True
                if validator(payload):
                    logger.debug("知乎 %s JSON 接口请求成功: %s -> %s", name, url, final_url)
                    return payload

                saw_invalid_target = True
                logger.debug(
                    "知乎 %s JSON 接口拿到的不是目标数据: %s -> %s",
                    name,
                    url,
                    final_url,
                )
            except Exception as exc:
                last_error = exc
                logger.debug("知乎 %s JSON 接口请求失败: %s, error=%s", name, url, exc)

        if saw_challenge or saw_forbidden:
            raise RuntimeError("知乎 JSON 接口被风控拦截")
        if saw_login:
            raise RuntimeError("知乎 JSON 接口被引导到登录页")
        if saw_invalid_target or saw_json_payload:
            raise RuntimeError("知乎 JSON 接口未拿到目标数据")
        raise RuntimeError("知乎 JSON 接口请求失败") from last_error

    @staticmethod
    def _extract_json_payload(raw_text: str, *, content_type: str) -> dict | None:
        """从响应文本中解析 JSON 对象，非 JSON 或非对象时返回 None。"""
        text = raw_text.strip()
        if not text:
            return None
        if "application/json" not in content_type.lower() and not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _request_profiles(
        self, *, accept: str | None = None
    ) -> list[tuple[str, dict[str, str], str]]:
        """构造三套指纹各自的请求头（共享基础头，UA 按指纹区分）。"""
        base: dict[str, str] = {
            "Accept": accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.zhihu.com/",
            "Origin": "https://www.zhihu.com",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if self._cookies:
            base["Cookie"] = self._cookies

        profiles: list[tuple[str, dict[str, str], str]] = []
        for name, impersonate, ua in self._PROFILES:
            profiles.append((name, {**base, "User-Agent": ua}, impersonate))
        return profiles

    async def _request_text(
        self,
        url: str,
        *,
        headers: dict[str, str],
        impersonate: str,
    ) -> dict:
        """用 curl_cffi 发起请求，返回状态码、最终 URL、响应文本与内容类型。"""

        def do_request():
            return curl_requests.get(
                url,
                headers=headers,
                impersonate=impersonate,
                proxies={"http": self._proxy, "https": self._proxy}
                if self._proxy
                else None,
                timeout=self._timeout,
                allow_redirects=True,
            )

        response = await asyncio.to_thread(do_request)
        return {
            "status_code": int(response.status_code),
            "final_url": str(response.url),
            "text": str(response.text),
            "content_type": str(response.headers.get("content-type", "")),
        }

    @staticmethod
    def _is_challenge_page(html_text: str, *, status_code: int) -> bool:
        """识别知乎反爬挑战页（zse-ck 安全校验）。"""
        lowered = html_text.lower()
        return (
            'id="zh-zse-ck"' in lowered
            or "static.zhihu.com/zse-ck/" in lowered
            or 'appname":"zse_ck"' in lowered
            or (status_code == 403 and "zse-ck" in lowered)
        )

    @staticmethod
    def _is_login_page(final_url: str, html_text: str) -> bool:
        """识别被引导到登录/注册页的情况。"""
        lowered_url = final_url.lower()
        lowered_html = html_text.lower()
        return (
            "/signin" in lowered_url
            or "/signup" in lowered_url
            or "<title>知乎 - 有问题，就会有答案</title>" in lowered_html
        )

    async def _load_answer_for_question(
        self,
        *,
        qid: str,
        answer_id: str,
        question_data: dict,
    ) -> dict:
        """获取问题页首条回答的完整正文。"""
        entities = question_data.get("initialState", {}).get("entities", {})
        answer = (entities.get("answers") or {}).get(answer_id) or {}
        if (
            isinstance(answer, dict)
            and answer.get("content")
            and not answer.get("contentNeedTruncated")
        ):
            return answer

        answer_data = await self._fetch_initial_data(
            self._answer_url(qid, answer_id),
            validator=lambda data: self._has_answer_entities(data, qid, answer_id),
        )
        entities = answer_data.get("initialState", {}).get("entities", {})
        answer = (entities.get("answers") or {}).get(answer_id) or {}
        if not isinstance(answer, dict):
            raise RuntimeError("知乎首条回答数据不存在")
        return answer

    @staticmethod
    def _answer_url(qid: str, aid: str) -> str:
        return f"https://www.zhihu.com/question/{qid}/answer/{aid}"

    @staticmethod
    def _answer_api_url(aid: str) -> str:
        """回答 JSON 接口；include 覆盖正文、摘要、互动数据、作者与问题标题。"""
        include = "content,excerpt,voteup_count,comment_count,favlists_count,thanks_count,author,question,created_time,updated_time"
        return f"https://www.zhihu.com/api/v4/answers/{aid}?include={include}"

    @staticmethod
    def _question_answers_api_url(qid: str) -> str:
        """问题下默认排序首条回答的 JSON 列表接口（limit=1）。"""
        include = "content,excerpt,voteup_count,comment_count,favlists_count,thanks_count,author,question,created_time"
        return (
            f"https://www.zhihu.com/api/v4/questions/{qid}/answers"
            f"?include={include}&limit=1&offset=0&sort_by=default"
        )

    @staticmethod
    def _pin_api_url(pin_id: str) -> str:
        """想法 JSON 接口；include 覆盖正文、HTML、时间与作者信息。"""
        include = "content,content_html,created_time,updated_time,author,origin_pin"
        return f"https://www.zhihu.com/api/v4/pins/{pin_id}?include={include}"

    @staticmethod
    def _article_api_url(article_id: str) -> str:
        """专栏文章 JSON 接口。"""
        return f"https://zhuanlan.zhihu.com/api/articles/{article_id}"

    @staticmethod
    def _has_article_api_payload(payload: dict, article_id: str) -> bool:
        """专栏文章 JSON 接口返回的是否为有效文章数据。"""
        if str(payload.get("id")) == article_id:
            return True
        return payload.get("content") is not None and payload.get("title") is not None

    @staticmethod
    def _has_answer_api_payload(payload: dict, aid: str) -> bool:
        """回答 JSON 接口返回的是否为目标回答。"""
        if str(payload.get("id")) == aid:
            return True
        return payload.get("content") is not None and isinstance(
            payload.get("question"), dict
        )

    @staticmethod
    def _has_question_answers_payload(payload: dict, qid: str) -> bool:
        """问题回答列表接口是否返回了首条回答。"""
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return False
        first = data[0]
        if not isinstance(first, dict):
            return False
        question = first.get("question") or {}
        return str(question.get("id")) == qid or question.get("title") is not None

    @staticmethod
    def _has_pin_payload(payload: dict, pin_id: str) -> bool:
        """想法 JSON 接口返回的是否为有效想法数据。"""
        current_id = (
            payload.get("id") or payload.get("pin_id") or payload.get("pinId")
        )
        if current_id is not None and str(current_id) == pin_id:
            return True
        return any(
            payload.get(k) is not None
            for k in (
                "content_html",
                "contentHtml",
                "content",
                "author",
                "created_time",
            )
        )

    @staticmethod
    def _extract_initial_data(html_text: str) -> dict:
        soup = BeautifulSoup(html_text, "html.parser")
        node = soup.select_one('script#js-initialData[type="text/json"]')
        if node is None:
            return {}
        try:
            data = json.loads(node.get_text(strip=True))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _has_answer_entities(data: dict, qid: str, aid: str) -> bool:
        """页面 initialData 中是否同时包含目标问题与回答实体。"""
        entities = data.get("initialState", {}).get("entities", {})
        if not isinstance(entities, dict):
            return False
        answers = entities.get("answers", {})
        questions = entities.get("questions", {})
        return (
            isinstance(answers, dict)
            and isinstance(questions, dict)
            and isinstance(answers.get(aid), dict)
            and isinstance(questions.get(qid), dict)
        )

    @staticmethod
    def _has_article_entity(data: dict, article_id: str) -> bool:
        """页面 initialData 中是否包含目标专栏文章实体。"""
        entities = data.get("initialState", {}).get("entities", {})
        if not isinstance(entities, dict):
            return False
        article = (entities.get("articles") or {}).get(article_id)
        return isinstance(article, dict)

    @staticmethod
    def _has_question_entity(data: dict, qid: str) -> bool:
        """页面 initialData 中是否包含目标问题实体。"""
        entities = data.get("initialState", {}).get("entities", {})
        if not isinstance(entities, dict):
            return False
        question = (entities.get("questions") or {}).get(qid)
        return isinstance(question, dict)

    @staticmethod
    def _pick_first_answer_id(data: dict, qid: str) -> str | None:
        """从问题页 initialData 中取默认排序首条回答 ID。"""
        initial_state = data.get("initialState") or {}
        answers = (
            ((initial_state.get("question") or {}).get("answers") or {}).get(qid)
            or {}
        )
        ids = answers.get("ids") or []
        if not ids or not isinstance(ids[0], dict):
            return None
        target = ids[0].get("target")
        return str(target) if target else None
