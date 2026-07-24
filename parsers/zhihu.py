"""知乎链接解析器"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable, ClassVar

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

    # 浏览器指纹配置：(名称, curl_cffi impersonate 值, 匹配的 User-Agent)。
    # 每个指纹使用各自的 UA，保持 TLS 指纹与 UA 一致，降低被识别为非真实浏览器的概率。
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
        """初始化知乎页面解析器。

        回答 / 专栏 / 问题统一通过页面 ``js-initialData`` 读取内容，想法仍走
        JSON 接口。请求流程适配自 Zhalslar/astrbot_plugin_parser 的知乎解析器：
        使用 curl_cffi 模拟浏览器指纹，并用 validator 校验拿到的实体是否为目标页。
        问题页会在问题实体的基础上补取"默认排序首条回答"的完整正文。

        请求层按 desktop / ios / mobile 三套指纹分别构造请求头轮询，并识别反爬
        挑战页与登录引导页，命中时给出更具体的错误提示；支持可选代理。

        Args:
            cookies: 知乎登录 Cookies，遇到风控/登录页时填写。
            timeout: HTTP 请求超时（秒）。
            max_content_length: 正文摘要最大字符数。
            proxy: HTTP/HTTPS 代理地址（如 ``http://127.0.0.1:7890``），留空不使用。
        """
        self._cookies = cookies.strip()
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._proxy = proxy.strip()

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
        """解析知乎回答。

        优先走 JSON 接口（``api/v4/answers/{aid}``）：HTML 页面常被 zse-ck
        风控挑战拦截（即便携带登录 cookies），而 JSON 接口在携带登录 cookies
        时更稳定。接口失败时回退到页面 initialData 解析。
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

    async def _parse_article(self, article_id: str, url: str) -> ParseResult:
        """解析知乎专栏文章（走页面 initialData）。"""
        initial_data = await self._fetch_initial_data(
            url,
            validator=lambda data: self._has_article_entity(data, article_id),
        )
        entities = initial_data["initialState"]["entities"]
        article = (entities.get("articles") or {}).get(article_id)
        if not isinstance(article, dict):
            raise RuntimeError("知乎专栏数据不存在")

        author = article.get("author") or {}
        title = article.get("title", "知乎文章")
        author_name = author.get("name", "")
        author_avatar = author.get("avatarUrl") or author.get("avatar_url") or ""
        content_html = article.get("content", "") or ""
        voteup = article.get("voteupCount", article.get("voteup_count", 0))
        comment_count = article.get("commentCount", article.get("comment_count", 0))
        cover = article.get("image_url") or article.get("imageUrl") or ""

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

    async def _parse_question(self, qid: str, url: str) -> ParseResult:
        """解析知乎问题页：取问题实体 + 默认排序首条回答全文。

        优先走 JSON 接口（``api/v4/questions/{qid}/answers``，默认排序首条），
        规避 HTML 页面的 zse-ck 风控挑战；接口失败时回退到页面 initialData 解析。

        正文仍以纯文本形式发送（``clean_html`` + ``truncate_text``），不渲染 HTML。
        若问题页未加载到首条回答，则退化为只展示问题摘要。
        """
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
        detail_text = clean_html(detail_html)
        detail_text = truncate_text(detail_text, self._max_content_length)

        answer_count = question.get("answerCount", question.get("answer_count", 0))
        follower_count = question.get("followerCount", question.get("follower_count", 0))

        answer_id = self._pick_first_answer_id(initial_data, qid)
        if not answer_id:
            # 问题页未加载到首条回答，只展示问题摘要
            return ParseResult(
                title=question_title,
                content=detail_text or f"{answer_count} 个回答 · {follower_count} 人关注",
                url=url,
                platform=self.platform_name,
                platform_icon=self.platform_icon,
                stats={"comments": answer_count, "likes": follower_count},
            )

        answer = await self._load_answer_for_question(
            qid=qid,
            answer_id=answer_id,
            question_data=initial_data,
        )

        author = answer.get("author") or {}
        author_name = author.get("name", "匿名用户")
        author_avatar = author.get("avatarUrl", "")
        content_html = answer.get("content", "") or ""
        voteup = answer.get("voteupCount", 0)
        comment_count = answer.get("commentCount", 0)

        answer_text = clean_html(content_html)
        answer_text = truncate_text(answer_text, self._max_content_length)

        content_images = _extract_content_images(content_html)
        cover = content_images[0] if content_images else ""

        if detail_text and answer_text:
            content = f"{detail_text}\n\n-- 首条回答 --\n{answer_text}"
        elif answer_text:
            content = answer_text
        else:
            content = detail_text

        return ParseResult(
            title=question_title,
            author=author_name,
            author_avatar=author_avatar,
            content=content,
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

    def _build_from_api_answer(
        self,
        answer: dict,
        url: str,
        *,
        title: str | None = None,
    ) -> ParseResult:
        """由 JSON 接口的回答实体（snake_case 字段）构造 ParseResult。

        JSON 接口字段与页面 initialData 的驼峰字段不同：作者头像为
        ``avatar_url``，点赞/评论为 ``voteup_count`` / ``comment_count``，
        问题标题在嵌套的 ``question.title`` 里。
        """
        author = answer.get("author") or {}
        question = answer.get("question") or {}
        question_title = title or question.get("title", "知乎回答")
        author_name = author.get("name", "匿名用户")
        author_avatar = author.get("avatar_url", "")
        content_html = answer.get("content", "") or ""
        voteup = answer.get("voteup_count", 0)
        comment_count = answer.get("comment_count", 0)

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
        validator: Callable[[dict], bool],
    ) -> dict:
        """按上游的浏览器指纹请求策略读取知乎页面 initialData。

        依次用 desktop / ios / mobile 三套指纹（各自带匹配的 UA）请求页面：

        - 命中反爬挑战页（``zse-ck``）或登录引导页时跳过该指纹；
        - 解析出 initialData 并通过 ``validator``（确认实体就是目标页）即返回；
        - 三套指纹全部失败时，依据见到的页面类型给出更具体的错误提示。
        """
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
        """按浏览器指纹请求知乎 JSON 接口，返回通过校验的 payload。

        与 ``_fetch_initial_data`` 对应的 JSON 接口版本：依次用 desktop / ios /
        mobile 三套指纹请求接口（Accept 覆盖为 JSON），识别反爬挑战页、登录页
        与 4xx 后跳过；解析出 JSON 并通过 ``validator`` 即返回。全部失败时抛出
        RuntimeError。适配自上游 ``_fetch_json_data``，用于回答/问题等接口路径。
        """
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
                        name, url, final_url, status_code,
                    )
                    continue

                if status_code >= 400:
                    saw_invalid_target = True
                    logger.debug(
                        "知乎 %s JSON 接口状态异常: %s -> %s, status=%s",
                        name, url, final_url, status_code,
                    )
                    continue

                payload = self._extract_json_payload(body_text, content_type=content_type)
                if payload is None:
                    logger.debug(
                        "知乎 %s JSON 接口未返回可解析 JSON: %s -> %s, ct=%s",
                        name, url, final_url, content_type,
                    )
                    continue

                saw_json_payload = True
                if validator(payload):
                    logger.debug("知乎 %s JSON 接口请求成功: %s -> %s", name, url, final_url)
                    return payload

                saw_invalid_target = True
                logger.debug(
                    "知乎 %s JSON 接口拿到的不是目标数据: %s -> %s",
                    name, url, final_url,
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
        """构造三套指纹各自的请求头（共享基础头，UA 按指纹区分）。

        Args:
            accept: 自定义 Accept 头；留空时使用浏览器导航的 HTML Accept，
                供 JSON 接口请求覆盖为 ``application/json, text/plain, */*``。
        """
        base: dict[str, str] = {
            "Accept": accept or (
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
        """获取问题页首条回答的完整正文。

        问题页 ``initialData`` 里的回答正文可能被知乎截断（``contentNeedTruncated``
        为真），此时单独请求回答页拿全文；否则直接复用问题页里已有的回答实体。
        """
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
        include = "content,excerpt,voteup_count,comment_count,thanks_count,author,question"
        return f"https://www.zhihu.com/api/v4/answers/{aid}?include={include}"

    @staticmethod
    def _question_answers_api_url(qid: str) -> str:
        """问题下默认排序首条回答的 JSON 列表接口（limit=1）。"""
        include = "content,excerpt,voteup_count,comment_count,thanks_count,author,question"
        return (
            f"https://www.zhihu.com/api/v4/questions/{qid}/answers"
            f"?include={include}&limit=1&offset=0&sort_by=default"
        )

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
