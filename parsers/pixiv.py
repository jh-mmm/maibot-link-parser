"""Pixiv 平台链接解析器

支持插画 (Illustration)、多图画集、漫画 (Manga)、动图 (Ugoira) 以及小说 (Novel) 解析。
功能特性：
1. 动图 (Ugoira) 提取帧序列自动合成为动态 GIF，在 QQ 中可原生播放；
2. 漫画支持系列信息提取，支持最大页数限制；
3. R18 / NSFW 内容支持 send（正常发送）、blur（Pillow 高斯模糊封面打码）、ignore（拦截忽略）；
4. 支持 HTTP/HTTPS 代理与图片反代域名（如 i.pixiv.re）；
5. 支持链接触发与 PID 指令触发（如 pid 123456 / pixivid 123456）。
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
from pathlib import Path
import re
from typing import Any, ClassVar
from uuid import uuid4
import zipfile

from curl_cffi import requests as curl_requests
from PIL import Image, ImageFilter

try:
    from ..utils import clean_html, truncate_text
    from .base import BaseParser, ParseResult
except (ImportError, ValueError):
    from utils import clean_html, truncate_text
    from parsers.base import BaseParser, ParseResult

logger = logging.getLogger("plugin.com.maibot.link-parser.pixiv")

PIXIV_BASE = "https://www.pixiv.net"

# Pixiv Web API 请求头（模拟真实浏览器）
_PIXIV_HEADERS: dict[str, str] = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
}

# 图片下载防盗链请求头
_PIXIV_MEDIA_HEADERS: dict[str, str] = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


class PixivParser(BaseParser):
    """Pixiv 链接解析器"""

    platform_name: ClassVar[str] = "Pixiv"
    platform_icon: ClassVar[str] = "🎨"
    url_patterns: ClassVar[list[re.Pattern]] = [
        # 1. 插画 / 漫画 artworks 链接（支持多语言子路径如 /en/artworks/..., /zh/artworks/...）
        re.compile(
            r"(?:https?://)?(?:www\.)?pixiv\.net/(?:(?:en|zh|zh-tw|ja)/)?artworks/(\d+)"
        ),
        # 2. 旧版 member_illust.php 链接
        re.compile(
            r"(?:https?://)?(?:www\.)?pixiv\.net/member_illust\.php\?(?:.*&)?illust_id=(\d+)"
        ),
        # 3. 短链 /i/{id}
        re.compile(r"(?:https?://)?(?:www\.)?pixiv\.net/i/(\d+)"),
        # 4. 小说链接
        re.compile(
            r"(?:https?://)?(?:www\.)?pixiv\.net/(?:(?:en|zh|zh-tw|ja)/)?novel/show\.php\?id=(\d+)"
        ),
        re.compile(
            r"(?:https?://)?(?:www\.)?pixiv\.net/(?:(?:en|zh|zh-tw|ja)/)?novel/(\d+)"
        ),
        # 5. 纯文本 PID / PixivID 指令触发 (如 pid 123456, PID: 123456, pixivid 123456)
        re.compile(r"(?i)(?<![a-zA-Z0-9_-])(?:pid|pixivid)\s*[:=]?\s*(\d+)"),
    ]

    def __init__(
        self,
        cookies: str = "",
        proxy: str = "",
        img_proxy: str = "",
        nsfw: str = "blur",
        image_quality: str = "regular",
        max_manga_pages: int = 20,
        runtime_dir: str | Path | None = None,
        timeout: int = 15,
        max_content_length: int = 500,
    ) -> None:
        """初始化 Pixiv 解析器。

        Args:
            cookies: Pixiv 登录 Cookies（用于访问 R18 或登录限定内容）。
            proxy: HTTP/HTTPS 代理地址（如 http://127.0.0.1:7890）。
            img_proxy: 图片反代域名（如 i.pixiv.re，留空则直连 i.pximg.net）。
            nsfw: R18 内容策略：send=正常发送 | blur=封面模糊 | ignore=忽略并提示。
            image_quality: 图片质量：regular=标准清晰度 | original=原图。
            max_manga_pages: 漫画最大下载页数（0 为不限制）。
            runtime_dir: 临时文件保存目录。
            timeout: HTTP 请求超时（秒）。
            max_content_length: 正文摘要最大字符数。
        """
        cookies_str = cookies.strip()
        if cookies_str and "=" not in cookies_str:
            cookies_str = f"PHPSESSID={cookies_str}"
        self._cookies = cookies_str
        self._proxy = proxy.strip()
        self._img_proxy = img_proxy.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self._nsfw = nsfw.strip().lower() or "blur"
        self._image_quality = image_quality.strip().lower() or "regular"
        self._max_manga_pages = max_manga_pages
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._runtime_dir = Path(runtime_dir) if runtime_dir else Path(__file__).resolve().parent.parent / "runtime" / "link-parser"
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 解析入口
    # ------------------------------------------------------------------

    async def parse(self, url: str, match: re.Match) -> ParseResult:
        full_url = match.group(0)
        if not full_url.startswith("http") and not full_url.startswith("pid") and not full_url.startswith("PID") and not full_url.startswith("pixivid") and not full_url.startswith("PIXIVID"):
            full_url = "https://" + full_url

        pattern_str = match.re.pattern
        matched_id = match.group(1)

        # 判断是否为小说链接
        if "novel" in pattern_str:
            canonical_url = f"{PIXIV_BASE}/novel/show.php?id={matched_id}"
            return await self._parse_novel(matched_id, canonical_url)

        # 判断是否为纯 PID / PixivID 文本指令
        if "pid" in pattern_str.lower() and "artworks" not in pattern_str and "member_illust" not in pattern_str:
            return await self._parse_pid_trigger(matched_id)

        # 默认为插画/漫画/动图
        canonical_url = f"{PIXIV_BASE}/artworks/{matched_id}"
        return await self._parse_artwork(matched_id, canonical_url)

    # ------------------------------------------------------------------
    # PID 指令与作品解析
    # ------------------------------------------------------------------

    async def _parse_pid_trigger(self, pid: str) -> ParseResult:
        """处理纯 PID 触发：先尝试插画/漫画接口，失败则尝试小说接口。"""
        try:
            body = await self._fetch_json(f"{PIXIV_BASE}/ajax/illust/{pid}")
            if body:
                return await self._dispatch_artwork(pid, body, f"{PIXIV_BASE}/artworks/{pid}")
        except Exception as exc:
            logger.debug("PID %s 插画接口请求未命中或失败: %s，尝试小说接口", pid, exc)

        try:
            body = await self._fetch_json(f"{PIXIV_BASE}/ajax/novel/{pid}")
            if body:
                return await self._handle_novel(pid, body, f"{PIXIV_BASE}/novel/show.php?id={pid}")
        except Exception as exc:
            logger.warning("PID %s 小说接口同样失败: %s", pid, exc)

        raise RuntimeError(f"未找到 PID {pid} 对应的 Pixiv 插画、漫画或小说作品")

    async def _parse_artwork(self, pid: str, original_url: str) -> ParseResult:
        """解析插画/漫画/动图链接。"""
        body = await self._fetch_json(f"{PIXIV_BASE}/ajax/illust/{pid}")
        if not body:
            raise RuntimeError(f"获取 Pixiv 作品 (PID: {pid}) 详情失败")
        return await self._dispatch_artwork(pid, body, original_url)

    async def _dispatch_artwork(self, pid: str, body: dict[str, Any], original_url: str) -> ParseResult:
        """根据 illustType 分发到具体处理器。

        illustType:
            0: 插画 (Illust)
            1: 漫画 (Manga)
            2: 动图 (Ugoira)
        """
        illust_type = int(body.get("illustType", 0))
        if illust_type == 1:
            return await self._handle_manga(pid, body, original_url)
        if illust_type == 2:
            return await self._handle_ugoira(pid, body, original_url)
        return await self._handle_illust(pid, body, original_url)

    # ------------------------------------------------------------------
    # 1. 插画处理 (illustType == 0)
    # ------------------------------------------------------------------

    async def _handle_illust(self, pid: str, body: dict[str, Any], original_url: str) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        if self._is_nsfw_ignored(x_restrict):
            return self._make_nsfw_ignored_result(pid, original_url)

        should_blur = self._should_blur_nsfw(x_restrict)

        title = body.get("title", "") or body.get("illustTitle", "Pixiv 插画")
        author_name = body.get("userName", "未知画师")
        user_id = str(body.get("userId", ""))
        author_avatar = await self._get_author_avatar(user_id)

        description = clean_html(body.get("description", ""))
        tags_text = self._format_tags(body.get("tags", {}))
        page_count = int(body.get("pageCount", 1))

        content_parts: list[str] = []
        if description:
            content_parts.append(description)
        if tags_text:
            content_parts.append(f"🏷️ 标签: {tags_text}")
        if page_count > 1:
            content_parts.append(f"📄 图集共 {page_count} 张")
        if x_restrict > 0:
            content_parts.append("🔞 提示：本作为 R-18 限制级内容" + (" (已自动打码)" if should_blur else ""))

        content = "\n\n".join(content_parts)
        content = truncate_text(content, self._max_content_length)

        images: list[str] = []
        cover_image: str = ""

        # 获取图片 URL 列表
        quality_key = "original" if self._image_quality == "original" else "regular"
        if page_count > 1:
            pages = await self._fetch_pages(pid)
            for page in pages:
                urls_map = page.get("urls", {})
                img_url = urls_map.get(quality_key) or urls_map.get("regular") or urls_map.get("original") or ""
                if img_url:
                    images.append(self._apply_img_proxy(img_url))
            cover_image = images[0] if images else ""
        else:
            urls_map = body.get("urls", {})
            img_url = urls_map.get(quality_key) or urls_map.get("regular") or urls_map.get("original") or ""
            if img_url:
                img_url = self._apply_img_proxy(img_url)
                images.append(img_url)
                cover_image = img_url

        # R18 模糊打码处理
        if should_blur and cover_image:
            # 优先使用 regular 或 small 版本进行打码（体积适中，避免因下载数兆原图导致超时）
            blur_source = urls_map.get("regular") or urls_map.get("small") or cover_image
            blur_source = self._apply_img_proxy(blur_source)
            blurred_path = await self._download_and_blur(blur_source, f"pixiv_{pid}_cover_blur.jpg")
            if blurred_path:
                cover_image = str(blurred_path)
                images = [str(blurred_path)]
            else:
                # 模糊打码失败时，严禁回退发送原图，清空图片列表并补充提示
                cover_image = ""
                images = []
                content_parts.append("⚠️ R-18 封面打码处理超时/失败，为保护群聊安全已取消发送原图")
                content = "\n\n".join(content_parts)
                content = truncate_text(content, self._max_content_length)

        stats = {
            "favorites": int(body.get("bookmarkCount", 0)),
            "likes": int(body.get("likeCount", 0)),
            "views": int(body.get("viewCount", 0)),
            "comments": int(body.get("commentCount", 0)),
        }

        return self._make_result(
            title=title,
            author=f"{author_name} (UID: {user_id})" if user_id else author_name,
            author_avatar=author_avatar,
            content=content,
            cover_image=cover_image,
            images=images,
            url=original_url,
            stats=stats,
            extra={
                "media_headers": _PIXIV_MEDIA_HEADERS,
                "proxy": self._proxy,
            },
        )

    # ------------------------------------------------------------------
    # 2. 漫画处理 (illustType == 1)
    # ------------------------------------------------------------------

    async def _handle_manga(self, pid: str, body: dict[str, Any], original_url: str) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        if self._is_nsfw_ignored(x_restrict):
            return self._make_nsfw_ignored_result(pid, original_url)

        should_blur = self._should_blur_nsfw(x_restrict)

        title = body.get("title", "") or body.get("illustTitle", "Pixiv 漫画")
        author_name = body.get("userName", "未知画师")
        user_id = str(body.get("userId", ""))
        author_avatar = await self._get_author_avatar(user_id)

        description = clean_html(body.get("description", ""))
        tags_text = self._format_tags(body.get("tags", {}))
        page_count = int(body.get("pageCount", 1))

        # 系列信息提取
        series_info = self._extract_series_info(body)

        content_parts: list[str] = []
        if series_info:
            content_parts.append(f"📚 {series_info}")
        if description:
            content_parts.append(description)
        if tags_text:
            content_parts.append(f"🏷️ 标签: {tags_text}")
        content_parts.append(f"📖 漫画共 {page_count} 页")
        if x_restrict > 0:
            content_parts.append("🔞 提示：本作为 R-18 限制级内容" + (" (已自动打码)" if should_blur else ""))

        # 检查是否超过最大页数限制
        exceeds_max_pages = self._max_manga_pages > 0 and page_count > self._max_manga_pages
        if exceeds_max_pages:
            content_parts.append(f"⚠️ 超过最大解析页数限制 ({self._max_manga_pages} 页)，仅展示封面")

        content = "\n\n".join(content_parts)
        content = truncate_text(content, self._max_content_length)

        images: list[str] = []
        cover_image: str = ""

        quality_key = "original" if self._image_quality == "original" else "regular"
        urls_map = body.get("urls", {})
        first_cover_url = urls_map.get(quality_key) or urls_map.get("regular") or urls_map.get("original") or ""

        if not exceeds_max_pages and page_count > 1:
            pages = await self._fetch_pages(pid)
            for page in pages:
                p_urls = page.get("urls", {})
                img_url = p_urls.get(quality_key) or p_urls.get("regular") or p_urls.get("original") or ""
                if img_url:
                    images.append(self._apply_img_proxy(img_url))
            cover_image = images[0] if images else ""
        elif first_cover_url:
            first_cover_url = self._apply_img_proxy(first_cover_url)
            cover_image = first_cover_url
            images.append(first_cover_url)

        # R18 模糊打码
        if should_blur and cover_image:
            blur_source = urls_map.get("regular") or urls_map.get("small") or cover_image
            blur_source = self._apply_img_proxy(blur_source)
            blurred_path = await self._download_and_blur(blur_source, f"pixiv_{pid}_manga_blur.jpg")
            if blurred_path:
                cover_image = str(blurred_path)
                images = [str(blurred_path)]
            else:
                # 模糊打码失败时，严禁回退发送原图，清空图片列表
                cover_image = ""
                images = []
                content_parts.append("⚠️ R-18 封面打码处理超时/失败，为保护群聊安全已取消发送原图")
                content = "\n\n".join(content_parts)
                content = truncate_text(content, self._max_content_length)

        stats = {
            "favorites": int(body.get("bookmarkCount", 0)),
            "likes": int(body.get("likeCount", 0)),
            "views": int(body.get("viewCount", 0)),
            "comments": int(body.get("commentCount", 0)),
        }

        return self._make_result(
            title=title,
            author=f"{author_name} (UID: {user_id})" if user_id else author_name,
            author_avatar=author_avatar,
            content=content,
            cover_image=cover_image,
            images=images,
            url=original_url,
            stats=stats,
            extra={
                "media_headers": _PIXIV_MEDIA_HEADERS,
                "proxy": self._proxy,
            },
        )

    # ------------------------------------------------------------------
    # 3. 动图处理 (illustType == 2, Ugoira 合成 GIF)
    # ------------------------------------------------------------------

    async def _handle_ugoira(self, pid: str, body: dict[str, Any], original_url: str) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        if self._is_nsfw_ignored(x_restrict):
            return self._make_nsfw_ignored_result(pid, original_url)

        should_blur = self._should_blur_nsfw(x_restrict)

        title = body.get("title", "") or body.get("illustTitle", "Pixiv 动图")
        author_name = body.get("userName", "未知画师")
        user_id = str(body.get("userId", ""))
        author_avatar = await self._get_author_avatar(user_id)

        description = clean_html(body.get("description", ""))
        tags_text = self._format_tags(body.get("tags", {}))

        content_parts: list[str] = []
        if description:
            content_parts.append(description)
        if tags_text:
            content_parts.append(f"🏷️ 标签: {tags_text}")
        content_parts.append("🎞️ Pixiv 动态插画 (Ugoira)")
        if x_restrict > 0:
            content_parts.append("🔞 提示：本作为 R-18 限制级内容" + (" (已自动打码)" if should_blur else ""))

        # 尝试获取动图元数据并合成 GIF
        gif_file: Path | None = None
        try:
            ugoira_meta = await self._fetch_json(f"{PIXIV_BASE}/ajax/illust/{pid}/ugoira_meta")
            if ugoira_meta:
                gif_file = await self._synthesize_ugoira_gif(pid, ugoira_meta, blur=should_blur)
        except Exception as exc:
            logger.warning("Pixiv PID %s 动图元数据获取或合成失败: %s，回退为静态封面", pid, exc)
            content_parts.append("⚠️ 动图帧下载失败，已降级为静态图片展示")

        images: list[str] = []
        cover_image: str = ""

        if gif_file and gif_file.exists():
            cover_image = str(gif_file)
            images.append(str(gif_file))
        else:
            # 回退为普通静态封面
            urls_map = body.get("urls", {})
            img_url = urls_map.get("regular") or urls_map.get("small") or urls_map.get("original") or ""
            if img_url:
                img_url = self._apply_img_proxy(img_url)
                if should_blur:
                    blurred = await self._download_and_blur(img_url, f"pixiv_{pid}_ugoira_blur.jpg")
                    if blurred:
                        cover_image = str(blurred)
                        images.append(cover_image)
                    else:
                        content_parts.append("⚠️ R-18 封面打码处理超时/失败，为保护群聊安全已取消发送原图")
                else:
                    cover_image = img_url
                    images.append(cover_image)

        content = "\n\n".join(content_parts)
        content = truncate_text(content, self._max_content_length)

        stats = {
            "favorites": int(body.get("bookmarkCount", 0)),
            "likes": int(body.get("likeCount", 0)),
            "views": int(body.get("viewCount", 0)),
            "comments": int(body.get("commentCount", 0)),
        }

        return self._make_result(
            title=title,
            author=f"{author_name} (UID: {user_id})" if user_id else author_name,
            author_avatar=author_avatar,
            content=content,
            cover_image=cover_image,
            images=images,
            url=original_url,
            stats=stats,
            extra={
                "media_headers": _PIXIV_MEDIA_HEADERS,
                "proxy": self._proxy,
            },
        )

    # ------------------------------------------------------------------
    # 4. 小说处理 (Novel)
    # ------------------------------------------------------------------

    async def _parse_novel(self, nid: str, original_url: str) -> ParseResult:
        body = await self._fetch_json(f"{PIXIV_BASE}/ajax/novel/{nid}")
        if not body:
            raise RuntimeError(f"获取 Pixiv 小说 (NID: {nid}) 详情失败")
        return await self._handle_novel(nid, body, original_url)

    async def _handle_novel(self, nid: str, body: dict[str, Any], original_url: str) -> ParseResult:
        x_restrict = int(body.get("xRestrict", 0))
        if self._is_nsfw_ignored(x_restrict):
            return self._make_nsfw_ignored_result(nid, original_url, is_novel=True)

        should_blur = self._should_blur_nsfw(x_restrict)

        title = body.get("title", "Pixiv 小说")
        author_name = body.get("userName", "未知作者")
        user_id = str(body.get("userId", ""))
        author_avatar = await self._get_author_avatar(user_id)

        description = clean_html(body.get("description", ""))
        tags_text = self._format_tags(body.get("tags", {}))
        character_count = int(body.get("characterCount", 0))

        # 小说系列信息
        series_info = self._extract_series_info(body)

        # 正文内容清理与摘要
        novel_raw = body.get("content", "")
        novel_clean = self._clean_novel_text(novel_raw)

        content_parts: list[str] = []
        if series_info:
            content_parts.append(f"📚 {series_info}")
        if description:
            content_parts.append(f"📝 简介:\n{description}")
        if tags_text:
            content_parts.append(f"🏷️ 标签: {tags_text}")
        if character_count > 0:
            content_parts.append(f"📊 字数: {character_count:,} 字")
        if x_restrict > 0:
            content_parts.append("🔞 提示：本作为 R-18 限制级内容" + (" (已自动打码)" if should_blur else ""))

        if novel_clean:
            content_parts.append(f"\n📖 【正文摘录】\n{novel_clean}")

        # 封面图
        cover_url = body.get("coverUrl", "") or ""
        cover_image: str = ""
        images: list[str] = []
        if cover_url:
            cover_url = self._apply_img_proxy(cover_url)
            if should_blur:
                blurred = await self._download_and_blur(cover_url, f"pixiv_novel_{nid}_blur.jpg")
                if blurred:
                    cover_image = str(blurred)
                    images.append(cover_image)
                else:
                    content_parts.append("⚠️ R-18 封面打码处理超时/失败，为保护群聊安全已取消发送原图")
            else:
                cover_image = cover_url
                images.append(cover_image)

        content = "\n\n".join(content_parts)
        content = truncate_text(content, self._max_content_length)

        stats = {
            "favorites": int(body.get("bookmarkCount", 0)),
            "likes": int(body.get("likeCount", 0)),
            "views": int(body.get("viewCount", 0)),
            "comments": int(body.get("commentCount", 0)),
        }

        return self._make_result(
            title=f"📖 {title}",
            author=f"{author_name} (UID: {user_id})" if user_id else author_name,
            author_avatar=author_avatar,
            content=content,
            cover_image=cover_image,
            images=images,
            url=original_url,
            stats=stats,
            extra={
                "media_headers": _PIXIV_MEDIA_HEADERS,
                "proxy": self._proxy,
            },
        )

    # ------------------------------------------------------------------
    # 辅助方法：Ugoira 动图下载与合成
    # ------------------------------------------------------------------

    async def _synthesize_ugoira_gif(
        self, pid: str, meta: dict[str, Any], blur: bool = False
    ) -> Path:
        """下载 Ugoira zip 包并将其合成为动态 GIF。"""
        zip_url = meta.get("originalSrc") or meta.get("src", "")
        if not zip_url:
            raise ValueError("Ugoira 元数据中缺少 zip 下载链接")

        zip_url = self._apply_img_proxy(zip_url)
        frames = meta.get("frames", [])
        if not frames:
            raise ValueError("Ugoira 帧序列数据为空")

        # 下载 zip 文件
        zip_path = self._runtime_dir / f"ugoira_{pid}_{uuid4().hex[:6]}.zip"
        gif_path = self._runtime_dir / f"ugoira_{pid}_{uuid4().hex[:6]}.gif"

        def download_zip() -> None:
            proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
            headers = {**_PIXIV_MEDIA_HEADERS}
            if self._cookies:
                headers["Cookie"] = self._cookies
            resp = curl_requests.get(
                zip_url,
                headers=headers,
                impersonate="chrome",
                proxies=proxies,
                timeout=max(self._timeout * 3, 45),
            )
            if resp.status_code != 200:
                raise RuntimeError(f"下载 Ugoira zip 失败 (HTTP {resp.status_code})")
            with open(zip_path, "wb") as f:
                f.write(resp.content)

        await asyncio.to_thread(download_zip)

        try:
            def process_frames() -> None:
                images: list[Image.Image] = []
                durations: list[int] = []

                with zipfile.ZipFile(zip_path, "r") as zf:
                    for frame_info in frames:
                        frame_file = frame_info.get("file", "")
                        delay = int(frame_info.get("delay", 50))
                        if not frame_file or frame_file not in zf.namelist():
                            continue

                        with zf.open(frame_file) as ff:
                            img = Image.open(io.BytesIO(ff.read()))
                            img.load()
                            if blur:
                                img = img.filter(ImageFilter.GaussianBlur(radius=15))
                            images.append(img.convert("RGB"))
                            durations.append(delay)

                if not images:
                    raise ValueError("解压 Ugoira 帧图片失败")

                # 将第一帧作为基底保存为带有循环播放的 GIF
                images[0].save(
                    gif_path,
                    "GIF",
                    save_all=True,
                    append_images=images[1:],
                    duration=durations,
                    loop=0,
                )

            await asyncio.to_thread(process_frames)
            return gif_path
        finally:
            zip_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 辅助方法：图片下载与高斯模糊
    # ------------------------------------------------------------------

    async def _download_and_blur(self, image_url: str, filename: str) -> Path | None:
        """下载单张图片并生成高斯模糊打码版本。"""
        save_path = self._runtime_dir / filename

        def do_download_and_blur() -> Path | None:
            proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
            headers = {**_PIXIV_MEDIA_HEADERS}
            if self._cookies:
                headers["Cookie"] = self._cookies
            resp = curl_requests.get(
                image_url,
                headers=headers,
                impersonate="chrome",
                proxies=proxies,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.warning("下载图片进行模糊处理失败: HTTP %s", resp.status_code)
                return None

            raw_path = self._runtime_dir / f"temp_{filename}"
            with open(raw_path, "wb") as f:
                f.write(resp.content)

            try:
                with Image.open(raw_path) as img:
                    blurred = img.convert("RGB").filter(ImageFilter.GaussianBlur(radius=18))
                    blurred.save(save_path, "JPEG", quality=85)
                return save_path
            finally:
                raw_path.unlink(missing_ok=True)

        try:
            return await asyncio.to_thread(do_download_and_blur)
        except Exception as exc:
            logger.warning("图片模糊打码失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 辅助方法：API 请求与数据提取
    # ------------------------------------------------------------------

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        """向 Pixiv 发起带有浏览器指纹模拟的 JSON 请求。"""

        def do_request() -> dict[str, Any]:
            headers = {**_PIXIV_HEADERS}
            if self._cookies:
                headers["Cookie"] = self._cookies
            proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
            resp = curl_requests.get(
                url,
                headers=headers,
                impersonate="chrome",
                proxies=proxies,
                timeout=self._timeout,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Pixiv API 请求失败 (HTTP {resp.status_code})")
            data = resp.json()
            if data.get("error"):
                msg = data.get("message", "未知错误")
                raise RuntimeError(f"Pixiv API 返回错误: {msg}")
            return data.get("body") or {}

        return await asyncio.to_thread(do_request)

    async def _fetch_pages(self, pid: str) -> list[dict[str, Any]]:
        """获取多图插画的所有分页列表。"""
        try:
            body = await self._fetch_json(f"{PIXIV_BASE}/ajax/illust/{pid}/pages")
            if isinstance(body, list):
                return body
        except Exception as exc:
            logger.debug("获取多图列表失败 (PID: %s): %s", pid, exc)
        return []

    async def _get_author_avatar(self, uid: str) -> str:
        """获取画师头像 URL。"""
        if not uid:
            return ""
        try:
            body = await self._fetch_json(f"{PIXIV_BASE}/ajax/user/{uid}")
            avatar = body.get("imageBig") or body.get("image") or ""
            return self._apply_img_proxy(avatar) if avatar else ""
        except Exception:
            return ""

    def _apply_img_proxy(self, url: str) -> str:
        """如果配置了图片反向代理域名，替换 i.pximg.net。"""
        if not url:
            return ""
        if self._img_proxy and "i.pximg.net" in url:
            return url.replace("i.pximg.net", self._img_proxy)
        return url

    def _is_nsfw_ignored(self, x_restrict: int) -> bool:
        """判断是否应根据配置忽略 R18 内容。"""
        return x_restrict > 0 and self._nsfw == "ignore"

    def _should_blur_nsfw(self, x_restrict: int) -> bool:
        """判断是否需要对 R18 内容进行高斯模糊打码。"""
        return x_restrict > 0 and self._nsfw == "blur"

    def _make_nsfw_ignored_result(self, target_id: str, url: str, is_novel: bool = False) -> ParseResult:
        """生成 R18 拦截提示结果。"""
        kind = "小说" if is_novel else "作品"
        return self._make_result(
            title=f"Pixiv {kind} (PID: {target_id})",
            content="🔞 该作品包含 R-18 / R-18G 限制级内容，已根据插件配置策略忽略解析。",
            url=url,
        )

    @staticmethod
    def _format_tags(tags_data: dict[str, Any]) -> str:
        """格式化标签列表，支持原文加翻译。"""
        tags = tags_data.get("tags", [])
        if not isinstance(tags, list):
            return ""
        parts: list[str] = []
        for tag_info in tags:
            if not isinstance(tag_info, dict):
                continue
            tag = tag_info.get("tag", "")
            if not tag:
                continue
            trans_obj = tag_info.get("translation")
            trans_text = ""
            if isinstance(trans_obj, dict):
                trans_text = trans_obj.get("en", "") or trans_obj.get("zh", "")
            elif isinstance(trans_obj, str):
                trans_text = trans_obj
            if trans_text:
                parts.append(f"#{tag}({trans_text})")
            else:
                parts.append(f"#{tag}")
        return "  ".join(parts)

    @staticmethod
    def _extract_series_info(body: dict[str, Any]) -> str:
        """提取漫画或小说系列导航信息。"""
        series_nav = body.get("seriesNavData")
        if not isinstance(series_nav, dict):
            return ""
        series_title = series_nav.get("title", "")
        order = series_nav.get("order", 0)
        series_type = series_nav.get("seriesType", "manga")
        type_label = "小说系列" if series_type == "novel" else "漫画系列"
        if series_title:
            return f"{type_label}《{series_title}》第 {order} 话"
        return ""

    @staticmethod
    def _clean_novel_text(text: str) -> str:
        """清理 Pixiv 小说标记语法。"""
        if not text:
            return ""
        # 章节分页
        text = text.replace("[newpage]", "\n\n━━━━━━━━ 分页 ━━━━━━━━\n\n")
        # 超链接 [[jumpuri:标题>链接]] -> 标题 (链接)
        text = re.sub(r"\[\[jumpuri:\s*([^>]+?)\s*>\s*([^\]]+?)\s*\]\]", r"\1 (\2)", text)
        text = re.sub(r"\[\[jumpuri:\s*([^\]]+?)\s*\]\]", r"\1", text)
        # 注音 [[rb:文字>注音]] -> 文字(注音)
        text = re.sub(r"\[\[rb:\s*([^>]+?)\s*>\s*([^\]]+?)\s*\]\]", r"\1(\2)", text)
        text = re.sub(r"\[\[rb:\s*([^\]]+?)\s*\]\]", r"\1", text)
        # 页码跳转 [[jump:页码]]
        text = re.sub(r"\[\[jump:[^\]]*\]\]", "", text)
        # 解码 HTML 实体
        return html_module.unescape(text).strip()
