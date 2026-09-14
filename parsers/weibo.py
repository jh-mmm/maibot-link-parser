"""微博链接解析器"""
from __future__ import annotations

import json
import logging
import re
from time import time
from typing import ClassVar
from uuid import uuid4

import aiohttp
from bs4 import BeautifulSoup

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
    config_key: ClassVar[str] = "weibo"
    url_patterns: ClassVar[list[re.Pattern]] = [
        re.compile(r"(?:https?://)?(?:www\.)?weibo\.com/(?:u/)?(\d+)/([0-9a-zA-Z]+)"),
        re.compile(r"(?:https?://)?(?:m\.weibo\.cn|(?:www\.)?weibo\.com)/(?:status|detail|\d+)/([0-9a-zA-Z]+)"),
        re.compile(
            r"(?:https?://)?(?:www\.)?weibo\.com/tv/show/(\d+:\d+|\d+)(?:\?(?:.*&)?mid=(\d+))?"
        ),
        re.compile(r"(?:https?://)?(?:video|h5)\.weibo\.com/show\?(?:.*&)?fid=(\d+:\d+)"),
        re.compile(
            r"(?:https?://)?(?:(?:www\.)?weibo\.com/ttarticle/p/show\?(?:.*&)?id=|card\.weibo\.com/article/m/show/id/)(\d+)"
        ),
        re.compile(r"(?:https?://)?t\.cn/([0-9a-zA-Z]+)"),
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

        # 1. 微博官方短链 t.cn 处理：跟踪重定向到目标微博 URL
        if "t\\.cn" in pattern_str:
            resolved_url = await self._resolve_t_cn(full_url)
            if not resolved_url:
                raise RuntimeError("微博短链解析失败：未获取到重定向目标地址")
            # 在重定向后的 URL 中查找匹配的解析器
            for sub_pat in self.url_patterns:
                if "t\\.cn" in sub_pat.pattern:
                    continue
                sub_match = sub_pat.search(resolved_url)
                if sub_match:
                    return await self.parse(resolved_url, sub_match)
            raise RuntimeError(f"微博短链已重定向到非支持页面: {resolved_url}")

        # 2. 视频页面 video.weibo.com 处理
        if "video\\.weibo\\.com" in pattern_str:
            fid = match.group(1)
            return await self._parse_fid(fid, full_url)

        # 3. 专栏文章 ttarticle / card.weibo.com 处理
        if "ttarticle" in pattern_str or "card\\.weibo\\.com" in pattern_str:
            article_id = match.group(1)
            return await self._parse_article(article_id, full_url)

        # 4. 普通微博动态处理
        wid = ""
        if "tv/show" in pattern_str:
            mid = match.group(2) or match.group(1)
            wid = self._mid_to_wid(mid) if mid.isdigit() else mid
        elif "status" in pattern_str or "detail" in pattern_str or "weibo\\.cn" in pattern_str:
            wid = match.group(1)
        else:
            wid = match.group(2)

        if self._is_numeric_mid(wid):
            wid = self._mid_to_wid(wid)

        return await self._parse_status(wid, full_url)

    async def _resolve_t_cn(self, short_url: str) -> str:
        """解析 t.cn 短链重定向目标。"""
        headers = {"User-Agent": COMMON_HEADERS["User-Agent"]}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(short_url, headers=headers, allow_redirects=True) as resp:
                    return str(resp.url)
        except Exception as e:
            logger.warning("追踪 t.cn 重定向失败: %s", e)
            return ""

    async def _parse_fid(self, fid: str, url: str) -> ParseResult:
        """通过 H5 组件接口解析 video.weibo.com 视频。"""
        req_url = f"https://h5.video.weibo.com/api/component?page=/show/{fid}"
        headers = {
            **COMMON_HEADERS,
            "Referer": f"https://h5.video.weibo.com/show/{fid}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        post_content = {"data": json.dumps({"Component_Play_Playinfo": {"oid": fid}})}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(req_url, data=post_content, headers=headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"微博视频接口返回 HTTP {resp.status}")
                    json_data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("微博视频接口请求失败: %s", e)
            raise RuntimeError("微博视频接口请求失败") from e

        play_info = json_data.get("data", {}).get("Component_Play_Playinfo", {})
        if not play_info:
            raise RuntimeError("未获取到有效微博视频数据")

        user = play_info.get("reward", {}).get("user", {})
        author_name = user.get("name", "微博用户")
        avatar = user.get("profile_image_url", "")
        title = play_info.get("title", "")
        text = play_info.get("text", "")
        content_text = clean_html(text) if text else title
        content_text = truncate_text(content_text, self._max_content_length)

        cover_url = play_info.get("cover_image", "")
        if cover_url and not cover_url.startswith("http"):
            cover_url = "https:" + cover_url

        video_url = ""
        video_url_dict = play_info.get("urls")
        if isinstance(video_url_dict, dict) and video_url_dict:
            first_mp4 = next(iter(video_url_dict.values()), "")
            if first_mp4:
                video_url = first_mp4 if first_mp4.startswith("http") else f"https:{first_mp4}"
        if not video_url:
            video_url = play_info.get("stream_url", "") or ""
            if video_url and not video_url.startswith("http"):
                video_url = "https:" + video_url

        return self._make_result(
            title=title or "微博视频",
            author=author_name,
            author_avatar=avatar,
            content=content_text,
            cover_image=cover_url,
            images=[cover_url] if cover_url else [],
            video_url=video_url,
            url=url,
            extra={"media_headers": _WEIBO_MEDIA_HEADERS},
        )

    async def _parse_article(self, article_id: str, url: str) -> ParseResult:
        """解析微博专栏文章 (ttarticle / card.weibo.com)。"""
        api_url = "https://card.weibo.com/article/m/aj/detail"
        headers = {
            **COMMON_HEADERS,
            "Referer": f"https://card.weibo.com/article/m/show/id/{article_id}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        params = {
            "_rid": str(uuid4()),
            "id": article_id,
            "_t": str(int(time() * 1000)),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(api_url, data=params, headers=headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"微博文章接口返回 HTTP {resp.status}")
                    json_data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("微博文章接口请求失败: %s", e)
            raise RuntimeError("微博文章接口请求失败") from e

        data = json_data.get("data", {})
        if not data or json_data.get("msg") != "success":
            msg = json_data.get("msg") or "获取微博文章失败"
            raise RuntimeError(f"微博文章无法访问：{msg}")

        user_info = data.get("userinfo", {})
        author_name = user_info.get("screen_name", "微博用户")
        author_avatar = user_info.get("profile_image_url", "")
        title = data.get("title", "微博专栏")
        raw_html = data.get("content", "")
        content_text = clean_html(raw_html)
        content_text = truncate_text(content_text, self._max_content_length)

        # 提取文章内的正文配图
        soup = BeautifulSoup(raw_html, "html.parser")
        images: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and src.startswith("http"):
                images.append(src)
        images = list(dict.fromkeys(images))
        cover = images[0] if images else ""

        return self._make_result(
            title=title,
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover,
            images=images[:9],
            url=url,
            extra={"media_headers": _WEIBO_MEDIA_HEADERS},
        )

    async def _parse_status(self, wid: str, full_url: str) -> ParseResult:
        """通过 m.weibo.cn 移动端接口解析微博动态。"""
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
            logger.warning("微博 API 请求失败: %s", e)
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

        return self._make_result(
            title="微博动态",
            author=author_name,
            author_avatar=author_avatar,
            content=content_text,
            cover_image=cover,
            images=images,
            video_url=video_url,
            url=full_url,
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
        return wid.isdigit() and len(wid) >= 14

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
