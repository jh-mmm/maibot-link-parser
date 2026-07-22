"""知乎链接解析器"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import ClassVar

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .base import BaseParser, ParseResult
from ..utils import COMMON_HEADERS, clean_html, fetch_json, truncate_text

logger = logging.getLogger("plugin.com.maibot.link-parser.zhihu")


def _extract_content_images(html_text: str) -> list[str]:
    """从知乎正文 HTML 中提取全部图片地址。

    优先取 ``data-rawsrc`` / ``data-original``（原图），回退到 ``src``；
    将协议相对（``//``）与根相对路径补全为完整 https 链接，保持顺序去重。
    """
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    images: list[str] = []
    for img in soup.find_all("img"):
        url = img.get("data-rawsrc") or img.get("data-original") or img.get("src") or ""
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.zhihu.com" + url
        if url.startswith("http"):
            images.append(url)
    return list(dict.fromkeys(images))


class ZhihuParser(BaseParser):
    platform_name: ClassVar[str] = "知乎"
    platform_icon: ClassVar[str] = "💡"
    url_patterns: ClassVar[list[re.Pattern]] = [
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/question/(\d+)/answer/(\d+)"),
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/question/(\d+)(?:[?#]|$)"),
        re.compile(r"(?:https?://)?zhuanlan\.zhihu\.com/p/(\d+)"),
        re.compile(r"(?:https?://)?(?:www\.)?zhihu\.com/pin/(\d+)"),
    ]

    def __init__(self, cookies: str = "", timeout: int = 15, max_content_length: int = 500) -> None:
        """初始化知乎页面解析器。

        请求流程适配自 Zhalslar/astrbot_plugin_parser 的知乎解析器：使用
        curl_cffi 模拟浏览器，并从页面 ``js-initialData`` 中读取内容。

        Args:
            cookies: 知乎登录 Cookies，遇到风控/登录页时填写。
            timeout: HTTP 请求超时（秒）。
            max_content_length: 正文摘要最大字符数。
        """
        self._cookies = cookies.strip()
        self._timeout = timeout
        self._max_content_length = max_content_length

    async def parse(self, url: str, match: re.Match) -> ParseResult:
        full_url = match.group(0)
        if not full_url.startswith("http"):
            full_url = "https://" + full_url

        # Determine type based on which pattern matched
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
        """解析知乎回答。"""
        initial_data = await self._fetch_initial_data(
            url,
            question_id=qid,
            answer_id=aid,
        )
        entities = initial_data["initialState"]["entities"]
        answer = entities["answers"][aid]
        question = entities["questions"][qid]

        author = answer.get("author") or {}
        question_title = question.get("title", "知乎回答")
        author_name = author.get("name", "匿名用户")
        author_avatar = author.get("avatarUrl", "")
        content_html = answer.get("content", "")
        voteup = answer.get("voteupCount", 0)
        comment_count = answer.get("commentCount", 0)

        content_text = clean_html(content_html)
        content_text = truncate_text(content_text, self._max_content_length)

        content_images = _extract_content_images(content_html)
        cover = content_images[0] if content_images else ""

        return ParseResult(
            title=question_title,
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover,
            images=content_images,
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={"likes": voteup, "comments": comment_count},
        )

    async def _fetch_initial_data(
        self,
        url: str,
        *,
        question_id: str,
        answer_id: str,
    ) -> dict:
        """按上游的浏览器指纹请求策略读取知乎页面 initialData。"""
        headers = {
            "Accept": (
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
            headers["Cookie"] = self._cookies

        for profile in ("chrome", "safari_ios", "chrome_android"):
            response = await asyncio.to_thread(
                curl_requests.get,
                url,
                headers=headers,
                impersonate=profile,
                timeout=self._timeout,
            )
            initial_data = self._extract_initial_data(response.text)
            if self._is_expected_answer(initial_data, question_id, answer_id):
                logger.debug("知乎页面解析成功，浏览器指纹=%s", profile)
                return initial_data

        if self._cookies:
            raise RuntimeError("知乎页面请求被拒绝，请更新 Cookies 或检查其访问权限")
        raise RuntimeError("知乎页面请求被风控拦截，请在配置中填写有效的知乎 Cookies")

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
    def _is_expected_answer(data: dict, question_id: str, answer_id: str) -> bool:
        entities = data.get("initialState", {}).get("entities", {})
        if not isinstance(entities, dict):
            return False
        answers = entities.get("answers", {})
        questions = entities.get("questions", {})
        return isinstance(answers, dict) and isinstance(questions, dict) and (
            isinstance(answers.get(answer_id), dict)
            and isinstance(questions.get(question_id), dict)
        )

    async def _parse_question(self, qid: str, url: str) -> ParseResult:
        """解析知乎问题"""
        api_url = f"https://www.zhihu.com/api/v4/questions/{qid}"
        headers = {**COMMON_HEADERS, "Referer": "https://www.zhihu.com/"}

        try:
            data = await fetch_json(api_url, headers=headers)
        except Exception as e:
            logger.warning(f"知乎问题 API 请求失败: {e}")
            raise RuntimeError("知乎问题接口拒绝请求，无法获取内容") from e

        title = data.get("title", "")
        detail = clean_html(data.get("detail", ""))
        detail = truncate_text(detail, self._max_content_length)
        answer_count = data.get("answer_count", 0)
        follower_count = data.get("follower_count", 0)

        return ParseResult(
            title=title,
            content=detail if detail else f"{answer_count} 个回答 · {follower_count} 人关注",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={"comments": answer_count, "likes": follower_count},
        )

    async def _parse_article(self, article_id: str, url: str) -> ParseResult:
        """解析知乎专栏文章"""
        api_url = f"https://www.zhihu.com/api/v4/articles/{article_id}"
        headers = {**COMMON_HEADERS, "Referer": "https://www.zhihu.com/"}

        try:
            data = await fetch_json(api_url, headers=headers)
        except Exception as e:
            logger.warning(f"知乎文章 API 请求失败: {e}")
            raise RuntimeError("知乎专栏接口拒绝请求，无法获取内容") from e

        title = data.get("title", "")
        author_name = data.get("author", {}).get("name", "")
        author_avatar = data.get("author", {}).get("avatar_url", "")
        content_html = data.get("content", "")
        voteup = data.get("voteup_count", 0)
        comment_count = data.get("comment_count", 0)
        cover = data.get("image_url", "")

        content_text = clean_html(content_html)
        content_text = truncate_text(content_text, self._max_content_length)

        content_images = _extract_content_images(content_html)
        if not cover:
            cover = content_images[0] if content_images else ""

        return ParseResult(
            title=title,
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover,
            images=content_images,
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={"likes": voteup, "comments": comment_count},
        )

    async def _parse_pin(self, pin_id: str, url: str) -> ParseResult:
        """解析知乎想法"""
        api_url = f"https://www.zhihu.com/api/v4/pins/{pin_id}"
        headers = {**COMMON_HEADERS, "Referer": "https://www.zhihu.com/"}

        try:
            data = await fetch_json(api_url, headers=headers)
        except Exception as e:
            logger.warning(f"知乎想法 API 请求失败: {e}")
            raise RuntimeError("知乎想法接口拒绝请求，无法获取内容") from e

        author_name = data.get("author", {}).get("name", "")
        author_avatar = data.get("author", {}).get("avatar_url", "")

        # Pin content is in content list
        content_parts = []
        images = []
        for item in data.get("content", []):
            if item.get("type") == "text":
                content_parts.append(item.get("content", ""))
            elif item.get("type") == "image":
                images.append(item.get("url", ""))

        content_text = "\n".join(content_parts)
        content_text = truncate_text(content_text, self._max_content_length)
        like_count = data.get("like_count", 0)
        comment_count = data.get("comment_count", 0)

        return ParseResult(
            title="知乎想法",
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            images=images,
            cover_image=images[0] if images else "",
            url=url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={"likes": like_count, "comments": comment_count},
        )
