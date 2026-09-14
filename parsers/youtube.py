"""YouTube 链接解析器"""
from __future__ import annotations

import logging
import re
from typing import ClassVar

from .base import BaseParser, ParseResult
from ..utils import fetch_json

logger = logging.getLogger("plugin.com.maibot.link-parser.youtube")


class YouTubeParser(BaseParser):
    platform_name: ClassVar[str] = "YouTube"
    platform_icon: ClassVar[str] = "▶️"
    config_key: ClassVar[str] = "youtube"
    url_patterns: ClassVar[list[re.Pattern]] = [
        re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/watch\?(?:[^&\s]+&)*v=([A-Za-z0-9_-]+)"),
        re.compile(r"(?:https?://)?youtu\.be/([A-Za-z0-9_-]+)"),
        re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/(?:shorts|live)/([A-Za-z0-9_-]+)"),
    ]

    def __init__(self, api_key: str = "", cookies: str = "", timeout: int = 15, proxy: str = ""):
        self.api_key = api_key
        self.cookies = cookies.strip()
        self._timeout = timeout
        self.proxy = proxy.strip()

    async def parse(self, url: str, match: re.Match) -> ParseResult:
        full_url = match.group(0)
        if not full_url.startswith("http"):
            full_url = "https://" + full_url

        vid = match.group(1)

        if self.api_key:
            try:
                return await self._parse_with_api(vid, full_url)
            except Exception as e:
                logger.warning("YouTube Data API 请求失败: %s，将回退到无 Key 模式", e)
                
        return await self._parse_with_noembed(vid, full_url)

    async def _parse_with_api(self, vid: str, url: str) -> ParseResult:
        """使用 YouTube Data API 解析"""
        api_url = f"https://www.googleapis.com/youtube/v3/videos?id={vid}&key={self.api_key}&part=snippet,statistics,contentDetails"
        data = await fetch_json(api_url, timeout=self._timeout, proxy=self.proxy)
        
        items = data.get("items", [])
        if not items:
            return self._make_result(title="YouTube Video", content="视频不存在或被隐藏", url=url)
        
        item = items[0]
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        content_details = item.get("contentDetails", {})
        
        title = snippet.get("title", "")
        author = snippet.get("channelTitle", "")
        thumbnails = snippet.get("thumbnails", {})
        
        cover = (
            thumbnails.get("maxres")
            or thumbnails.get("standard")
            or thumbnails.get("high")
            or thumbnails.get("medium")
            or thumbnails.get("default")
            or {}
        ).get("url", "")
        
        view_count = int(statistics.get("viewCount", 0))
        like_count = int(statistics.get("likeCount", 0))
        comment_count = int(statistics.get("commentCount", 0))
        
        duration_str = content_details.get("duration", "PT0S")
        duration_sec = self._parse_iso8601_duration(duration_str)
        
        return self._make_result(
            title=title,
            author=author,
            cover_image=cover,
            images=[cover] if cover else [],
            url=url,
            video_duration=duration_sec,
            video_url=url,
            stats={"views": view_count, "likes": like_count, "comments": comment_count},
            extra={
                "video_downloader": "yt-dlp",
                "youtube_cookies": self.cookies,
                "proxy": self.proxy,
            },
        )

    async def _parse_with_noembed(self, vid: str, url: str) -> ParseResult:
        """使用 noembed API 免费解析"""
        api_url = f"https://noembed.com/embed?url=https://www.youtube.com/watch?v={vid}"
        
        try:
            data = await fetch_json(api_url, timeout=self._timeout, proxy=self.proxy)
            
            if "error" in data:
                return self._make_result(title="YouTube Video", content=data.get("error", ""), url=url)
                
            title = data.get("title", "")
            author = data.get("author_name", "")
            cover = data.get("thumbnail_url") or f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            
            return self._make_result(
                title=title,
                author=author,
                cover_image=cover,
                images=[cover] if cover else [],
                url=url,
                video_url=url,
                extra={
                    "video_downloader": "yt-dlp",
                    "youtube_cookies": self.cookies,
                    "proxy": self.proxy,
                },
            )
        except Exception as e:
            logger.warning("YouTube noembed API 请求失败: %s", e)
            return self._make_result(
                title="YouTube Video",
                content="获取视频信息失败",
                url=url,
            )

    def _parse_iso8601_duration(self, duration: str) -> int:
        """解析 ISO 8601 视频时长格式，如 PT1H2M10S 转换为秒"""
        if not duration.startswith("PT"):
            return 0
            
        duration = duration[2:]
        total_seconds = 0
        
        h_match = re.search(r"(\d+)H", duration)
        if h_match:
            total_seconds += int(h_match.group(1)) * 3600
            
        m_match = re.search(r"(\d+)M", duration)
        if m_match:
            total_seconds += int(m_match.group(1)) * 60
            
        s_match = re.search(r"(\d+)S", duration)
        if s_match:
            total_seconds += int(s_match.group(1))
            
        return total_seconds
