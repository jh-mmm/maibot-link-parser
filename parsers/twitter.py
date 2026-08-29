"""Twitter/X 平台链接解析器

支持 twitter.com 和 x.com 两种域名的推文链接解析。
提供两种 API 策略：
1. 自定义 API 端点（需配置 api_key 和 api_base_url）
2. fxtwitter 公共 API（免费回退方案）
"""

import ipaddress
import logging
import re
import socket
from typing import Any, ClassVar
from urllib.parse import urlparse

import aiohttp

from ..utils import truncate_text
from .base import BaseParser, ParseResult

logger = logging.getLogger("plugin.com.maibot.link-parser.twitter")

_FXTWITTER_API = "https://api.fxtwitter.com/{username}/status/{tweet_id}"


class TwitterParser(BaseParser):
    """Twitter/X 推文解析器

    支持解析推文正文、作者信息、互动数据以及媒体附件（图片/视频）。
    """

    platform_name: ClassVar[str] = "Twitter"
    platform_icon: ClassVar[str] = "🐦"
    config_key: ClassVar[str] = "twitter"
    url_patterns: ClassVar[list[re.Pattern]] = [
        # 标准格式: twitter.com/username/status/ID 或 x.com/username/status/ID
        re.compile(r"https?://(?:www\.)?twitter\.com/(\w+)/status/(\d+)"),
        re.compile(r"https?://(?:www\.)?x\.com/(\w+)/status/(\d+)"),
    ]

    def __init__(self, api_key: str = "", api_base_url: str = "", timeout: int = 15, max_content_length: int = 500) -> None:
        """初始化 Twitter 解析器

        Args:
            api_key: 自定义 API 密钥，为空则使用 fxtwitter 公共 API
            api_base_url: 自定义 API 基础 URL，为空则使用 fxtwitter
            timeout: HTTP 请求超时（秒）
            max_content_length: 正文摘要最大字符数
        """
        self._api_key = api_key.strip()
        self._api_base_url = api_base_url.strip().rstrip("/")
        self._timeout = timeout
        self._max_content_length = max_content_length


    def _validate_api_url(self, url: str) -> None:
        """
        防止 SSRF(Server-Side Request Forgery)

        禁止访问:
        - localhost
        - 本机回环地址
        - RFC1918 内网地址
        - 链路本地地址
        - 云服务器 metadata 地址
        """

        parsed = urlparse(url)


        # 只允许 HTTP / HTTPS
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"禁止的 API 协议: {parsed.scheme}"
            )


        hostname = parsed.hostname

        if not hostname:
            raise ValueError(
                "API 地址缺少 hostname"
            )


        # localhost
        if hostname.lower() in (
            "localhost",
            "localhost.localdomain",
        ):
            raise ValueError(
                "禁止访问 localhost"
            )


        try:
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)

        except Exception as e:
            raise ValueError(
                f"API 地址解析失败: {hostname}"
            ) from e


        # 阻止:
        # 127.0.0.0/8
        # 10.0.0.0/8
        # 172.16.0.0/12
        # 192.168.0.0/16
        # 169.254.0.0/16
        # 保留地址

        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
        ):
            raise ValueError(
                f"禁止访问内部地址: {ip}"
            )


        # 云厂商 metadata 服务
        if ip == "169.254.169.254":
            raise ValueError(
                "禁止访问云元数据地址"
            )


    @property
    def _use_custom_api(self) -> bool:
        """是否使用自定义 API 端点"""
        return bool(
            self._api_key
            and self._api_base_url
        )

    # ------------------------------------------------------------------
    # 解析入口
    # ------------------------------------------------------------------

    async def parse(self, raw_text: str, match: re.Match[str]) -> ParseResult:
        """解析推文链接，获取推文详情

        Args:
            raw_text: 原始消息文本
            match: URL 正则匹配对象

        Returns:
            解析结果 ParseResult

        Raises:
            ValueError: 推文数据为空或 API 返回异常
            aiohttp.ClientError: 网络请求失败
        """
        username: str = match.group(1)
        tweet_id: str = match.group(2)
        original_url: str = match.group(0)

        logger.info("开始解析推文: @%s/status/%s", username, tweet_id)

        if self._use_custom_api:
            tweet_data = await self._fetch_via_custom_api(username, tweet_id)
        else:
            tweet_data = await self._fetch_via_fxtwitter(username, tweet_id)

        return self._build_result(tweet_data, original_url)

    # ------------------------------------------------------------------
    # 自定义 API
    # ------------------------------------------------------------------

    async def _fetch_via_custom_api(
            self,
            username: str,
            tweet_id: str
    ) -> dict[str, Any]:

        base_url = self._api_base_url.rstrip("/")

        url = (
            f"{base_url}/"
            f"{username}/status/{tweet_id}"
        )

        self._validate_api_url(url)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

        logger.debug(
            "使用自定义 API: %s",
            url
        )

        async with aiohttp.ClientSession() as session:

            async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()

                    raise ValueError(
                        f"自定义 API 请求失败 "
                        f"(HTTP {resp.status}): "
                        f"{body[:200]}"
                    )

                data: dict[str, Any] = await resp.json()

        tweet = (
                data.get("tweet")
                or data.get("data")
        )

        if not tweet:
            raise ValueError(
                "自定义 API 返回的推文数据为空"
            )

        return tweet

    # ------------------------------------------------------------------
    # fxtwitter 公共 API
    # ------------------------------------------------------------------

    async def _fetch_via_fxtwitter(self, username: str, tweet_id: str) -> dict[str, Any]:
        """通过 fxtwitter 公共 API 获取推文数据

        Args:
            username: 推文作者用户名
            tweet_id: 推文 ID

        Returns:
            fxtwitter API 响应中的 tweet 字段

        Raises:
            ValueError: API 返回错误码或推文数据缺失
            aiohttp.ClientError: 网络请求失败
        """
        url = _FXTWITTER_API.format(username=username, tweet_id=tweet_id)

        logger.debug("使用 fxtwitter API: %s", url)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=self._timeout)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(
                        f"fxtwitter API 请求失败 (HTTP {resp.status}): {body[:200]}"
                    )
                data: dict[str, Any] = await resp.json()

        # fxtwitter 返回格式: {"code": 200, "message": "OK", "tweet": {...}}
        api_code = data.get("code", 0)
        if api_code != 200:
            api_msg = data.get("message", "未知错误")
            raise ValueError(f"fxtwitter API 业务错误 (code={api_code}): {api_msg}")

        tweet = data.get("tweet")
        if not tweet:
            raise ValueError("fxtwitter API 返回的推文数据为空")

        return tweet

    # ------------------------------------------------------------------
    # 结果构建
    # ------------------------------------------------------------------

    def _build_result(self, tweet: dict[str, Any], original_url: str) -> ParseResult:
        """将原始推文数据转换为统一 ParseResult

        Args:
            tweet: 推文数据字典（来自任一 API）
            original_url: 原始推文链接

        Returns:
            标准化的 ParseResult 对象
        """
        # ---- 作者信息 ----
        author_info = tweet.get("author", {})
        display_name = author_info.get("name", "")
        screen_name = author_info.get("screen_name", "")
        author_avatar = author_info.get("avatar_url", "")

        if display_name and screen_name:
            author = f"{display_name} (@{screen_name})"
        elif screen_name:
            author = f"@{screen_name}"
        else:
            author = display_name or "未知用户"

        # ---- 正文 ----
        text = tweet.get("text", "")
        content = truncate_text(text, self._max_content_length) if text else ""

        # ---- 互动数据 ----
        likes = tweet.get("likes", 0)
        retweets = tweet.get("retweets", 0)
        replies = tweet.get("replies", 0)

        stats: dict[str, int] = {}
        if likes is not None:
            stats["likes"] = int(likes)
        if retweets is not None:
            stats["reposts"] = int(retweets)
        if replies is not None:
            stats["comments"] = int(replies)

        # ---- 媒体附件 ----
        images: list[str] = []
        video_url: str = ""
        video_thumbnail: str = ""

        media = tweet.get("media") or {}

        # 图片
        photos = media.get("photos") or []
        for photo in photos:
            photo_url = photo.get("url", "")
            if photo_url:
                images.append(photo_url)

        # 视频（取第一个）
        videos = media.get("videos") or []
        if videos:
            first_video = videos[0]
            video_url = first_video.get("url", "")
            video_thumbnail = first_video.get("thumbnail_url", "")

        # ---- 发布时间 ----
        created_at = tweet.get("created_at", "")

        logger.info(
            "推文解析完成: @%s | 图片=%d 视频=%s | ❤️%s 🔄%s 💬%s",
            screen_name,
            len(images),
            "有" if video_url else "无",
            stats.get("likes", "0"),
            stats.get("reposts", "0"),
            stats.get("comments", "0"),
        )

        return self._make_result(
            url=original_url,
            title="",  # 推文通常没有标题
            author=author,
            author_avatar=author_avatar,
            content=content,
            cover_image=video_thumbnail or (images[0] if images else ""),
            images=images,
            video_url=video_url,
            video_duration=0,  # fxtwitter 不提供时长
            stats=stats,
            extra={"created_at": created_at} if created_at else {},
        )
