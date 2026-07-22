"""微博链接解析器"""
from __future__ import annotations

import logging
import re
from time import time
from typing import ClassVar

from .base import BaseParser, ParseResult
from ..utils import COMMON_HEADERS, clean_html, fetch_json, truncate_text

logger = logging.getLogger("plugin.com.maibot.link-parser.weibo")

_WEIBO_MEDIA_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://weibo.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


class WeiboParser(BaseParser):
    platform_name: ClassVar[str] = "微博"
    platform_icon: ClassVar[str] = "🍊"
    url_patterns: ClassVar[list[re.Pattern]] = [
        re.compile(r"(?:https?://)?(?:www\.)?weibo\.com/(\d+)/([0-9a-zA-Z]+)"),
        re.compile(r"(?:https?://)?m\.weibo\.cn/(?:status|detail|\d+)/([0-9a-zA-Z]+)"),
        re.compile(
            r"(?:https?://)?(?:www\.)?weibo\.com/tv/show/\d+:\d+\?mid=(\d+)"
        ),
    ]

    # base62 characters for mid/wid conversion
    BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, timeout: int = 15, max_content_length: int = 500) -> None:
        self._timeout = timeout
        self._max_content_length = max_content_length

    async def parse(self, url: str, match: re.Match) -> ParseResult:
        full_url = match.group(0)
        if not full_url.startswith("http"):
            full_url = "https://" + full_url

        pattern_str = match.re.pattern
        wid = ""

        if "tv/show" in pattern_str:
            mid = match.group(1)
            wid = self._mid_to_wid(mid)
        elif "weibo\\.cn" in pattern_str:
            wid = match.group(1)
        else:
            wid = match.group(2)
            
        if self._is_numeric_mid(wid):
            wid = self._mid_to_wid(wid)

        # 请求策略适配自 Zhalslar/astrbot_plugin_parser：使用移动端 XHR
        # 请求头、时间戳参数且不跟随重定向，避免跳转到风控页后误判成功。
        api_url = f"https://m.weibo.cn/statuses/show?id={wid}&_={int(time() * 1000)}"
        headers = {
            **COMMON_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://m.weibo.cn",
            "Referer": f"https://m.weibo.cn/detail/{wid}",
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        try:
            data = await fetch_json(api_url, headers=headers, allow_redirects=False, timeout=self._timeout)
        except Exception as e:
            logger.warning(f"微博 API 请求失败: {e}")
            raise RuntimeError("微博接口请求失败，未获取到可解析数据") from e

        if data.get("ok") != 1:
            raise RuntimeError("微博不存在、无权访问或请求被风控拦截")

        mblog = data.get("data", {})
        
        user = mblog.get("user", {})
        author_name = user.get("screen_name", "微博用户")
        author_avatar = user.get("profile_image_url", "")
        
        content_html = mblog.get("text", "")
        content_text = clean_html(content_html)
        content_text = truncate_text(content_text, self._max_content_length)
        
        reposts = mblog.get("reposts_count", 0)
        comments = mblog.get("comments_count", 0)
        likes = mblog.get("attitudes_count", 0)

        images = self._extract_images(mblog)
        
        cover = images[0] if images else ""

        # Video
        video_url = ""
        page_info = mblog.get("page_info", {})
        if page_info:
            media_info = page_info.get("media_info", {})
            urls = page_info.get("urls", {})
            video_url = self._pick_video_url(media_info, urls)
            
            if not cover and page_info.get("page_pic", {}).get("url"):
                cover = page_info["page_pic"]["url"]

        return ParseResult(
            title="微博动态",
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover,
            images=images,
            video_url=video_url,
            url=full_url,
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            stats={"likes": likes, "comments": comments, "reposts": reposts},
            extra={"media_headers": _WEIBO_MEDIA_HEADERS},
        )

    @staticmethod
    def _pick_video_url(media_info: dict, urls: dict) -> str:
        """按清晰度选择微博视频直链。"""
        for source in (urls, media_info):
            if not isinstance(source, dict):
                continue
            for key in (
                "mp4_1080p_mp4",
                "mp4_hd_mp4",
                "mp4_720p_mp4",
                "mp4_ld_mp4",
                "mp4_hd_url",
                "mp4_720p_url",
                "video_url",
                "stream_url_hd",
                "stream_url",
            ):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value if value.startswith("http") else f"https:{value}"

        playback_list = media_info.get("playback_list", []) if isinstance(media_info, dict) else []
        if isinstance(playback_list, list):
            for item in playback_list:
                if not isinstance(item, dict):
                    continue
                play_info = item.get("play_info", item)
                if not isinstance(play_info, dict):
                    continue
                value = play_info.get("url")
                if isinstance(value, str) and value:
                    return value if value.startswith("http") else f"https:{value}"
        return ""

    @staticmethod
    def _extract_images(mblog: dict) -> list[str]:
        """提取微博旧版和新版媒体字段中的原图地址。"""
        images: list[str] = []

        def append_image(image_data: object) -> None:
            if isinstance(image_data, str):
                if image_data:
                    images.append(image_data)
                return
            if not isinstance(image_data, dict):
                return
            for key in ("largest", "large", "mw2000", "original", "url"):
                value = image_data.get(key)
                if isinstance(value, dict):
                    value = value.get("url")
                if isinstance(value, str) and value:
                    images.append(value)
                    return

        pics = mblog.get("pics", [])
        if isinstance(pics, list):
            for picture in pics:
                append_image(picture)

        pic_infos = mblog.get("pic_infos", {})
        if isinstance(pic_infos, dict):
            for picture in pic_infos.values():
                append_image(picture)

        mix_media_info = mblog.get("mix_media_info", {})
        mix_items = mix_media_info.get("items", []) if isinstance(mix_media_info, dict) else []
        if isinstance(mix_items, list):
            for item in mix_items:
                if not isinstance(item, dict) or item.get("type") not in {"pic", "image"}:
                    continue
                append_image(item.get("data", item))

        return list(dict.fromkeys(images))

    def _is_numeric_mid(self, wid: str) -> bool:
        """判断 wid 是否是需要转换的数字 mid"""
        return wid.isdigit() and len(wid) > 16

    def _mid_to_wid(self, mid: str) -> str:
        """将数字 mid 转换为 base62 编码的 wid"""
        result = ""
        while mid:
            segment = mid[-7:]
            mid = mid[:-7]
            num = int(segment)
            encoded = ""
            if num == 0:
                encoded = "0"
            else:
                while num > 0:
                    encoded = self.BASE62_CHARS[num % 62] + encoded
                    num //= 62
            if mid:
                encoded = encoded.zfill(4)
            result = encoded + result
        return result
