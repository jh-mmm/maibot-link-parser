"""媒体下载器 — 负责视频和图片的流式下载"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import aiohttp
import yt_dlp

logger = logging.getLogger("plugin.com.maibot.link-parser.downloader")


class MediaDownloader:
    """媒体文件下载器
    
    支持流式下载，提供大小限制和超时保护。
    """

    def __init__(self, runtime_dir: str | Path, max_size_mb: int = 50, timeout: int = 60, proxy: str = ""):
        self.runtime_dir = Path(runtime_dir)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.timeout = timeout
        self.proxy = proxy.strip()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    async def download_video(self, url: str, headers: dict | None = None, filename: str | None = None, proxy: str | None = None) -> Path | None:
        """下载视频文件
        
        Args:
            url: 视频直链
            headers: 自定义请求头
            filename: 保存文件名，为空则自动生成
            proxy: 自定义代理地址，为空使用默认代理
            
        Returns:
            下载成功返回文件路径，失败返回 None
        """
        if not filename:
            ext = self._guess_ext(url, default=".mp4")
            filename = f"video_{uuid4().hex[:8]}{ext}"
        
        return await self._download(url, filename, headers, proxy=proxy)

    async def download_youtube_video(
        self,
        url: str,
        max_duration: int,
        cookies: str = "",
    ) -> Path | None:
        """通过 yt-dlp 下载 YouTube 视频并合并为 MP4。"""
        file_stem = f"youtube_{uuid4().hex[:8]}"
        output_template = self.runtime_dir / f"{file_stem}.%(ext)s"

        def download() -> Path | None:
            headers = {
                "Referer": "https://www.youtube.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            }
            if cookies:
                headers["Cookie"] = cookies

            options = {
                "format": "bv*[height<=720]+ba/b[height<=720]",
                "http_headers": headers,
                "max_filesize": self.max_size_bytes,
                "merge_output_format": "mp4",
                "noplaylist": True,
                "outtmpl": str(output_template),
                "quiet": True,
                "no_warnings": True,
            }
            if self.proxy:
                options["proxy"] = self.proxy

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                if not isinstance(info, dict):
                    raise ValueError("yt-dlp 未返回视频信息")
                duration = int(info.get("duration") or 0)
                if duration and duration > max_duration:
                    logger.warning("YouTube 视频时长 %ds 超限 %ds，跳过", duration, max_duration)
                    return None
                ydl.download([url])

            candidates = sorted(self.runtime_dir.glob(f"{file_stem}*.mp4"))
            if candidates:
                return candidates[0]
            logger.warning("yt-dlp 未生成 MP4 文件: %s", url)
            return None

        try:
            return await asyncio.to_thread(download)
        except Exception as e:
            logger.warning("YouTube 视频下载失败: %s", e)
            for path in self.runtime_dir.glob(f"{file_stem}.*"):
                path.unlink(missing_ok=True)
            return None

    async def download_image(self, url: str, headers: dict | None = None, filename: str | None = None, proxy: str | None = None) -> Path | None:
        """下载图片文件"""
        if not filename:
            ext = self._guess_ext(url, default=".jpg")
            filename = f"img_{uuid4().hex[:8]}{ext}"
        
        return await self._download(url, filename, headers, proxy=proxy)

    async def download_file(self, url: str, filename: str | None = None, headers: dict | None = None, proxy: str | None = None) -> Path | None:
        """通用文件下载（用于 zip、bin 等任意文件）"""
        if not filename:
            ext = self._guess_ext(url, default=".bin")
            filename = f"file_{uuid4().hex[:8]}{ext}"
        
        return await self._download(url, filename, headers, proxy=proxy)

    async def _download(self, url: str, filename: str, headers: dict | None = None, proxy: str | None = None) -> Path | None:
        """通用流式下载"""
        save_path = self.runtime_dir / filename
        default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        if headers:
            default_headers.update(headers)
        
        target_proxy = proxy if proxy is not None else (self.proxy or None)

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=default_headers, allow_redirects=True, proxy=target_proxy) as resp:
                    if resp.status >= 400:
                        logger.warning(f"下载失败: HTTP {resp.status} — {url}")
                        return None
                    
                    # Check Content-Length if available
                    content_length = resp.content_length
                    if content_length and content_length > self.max_size_bytes:
                        logger.warning(f"文件过大 ({content_length / 1024 / 1024:.1f}MB > {self.max_size_bytes / 1024 / 1024:.0f}MB): {url}")
                        return None
                    
                    # Stream download with size check
                    downloaded = 0
                    with open(save_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            downloaded += len(chunk)
                            if downloaded > self.max_size_bytes:
                                logger.warning(f"下载中断：文件超过大小限制 ({self.max_size_bytes / 1024 / 1024:.0f}MB)")
                                f.close()
                                save_path.unlink(missing_ok=True)
                                return None
                            f.write(chunk)
                    
                    logger.info(f"下载完成: {save_path.name} ({downloaded / 1024:.1f}KB)")
                    return save_path
                    
        except aiohttp.ClientError as e:
            logger.warning(f"下载网络错误: {e} — {url}")
            save_path.unlink(missing_ok=True)
            return None
        except TimeoutError:
            logger.warning(f"下载超时: {url}")
            save_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            logger.error(f"下载异常: {e} — {url}", exc_info=True)
            save_path.unlink(missing_ok=True)
            return None

    @staticmethod
    def _guess_ext(url: str, default: str = "") -> str:
        """从 URL 猜测文件扩展名"""
        from urllib.parse import urlparse
        path = urlparse(url).path
        if "." in path.split("/")[-1]:
            ext = "." + path.split(".")[-1].lower()
            if ext in (".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv",
                       ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip", ".pdf", ".txt"):
                return ext
        return default

    def cleanup(self) -> None:
        """清理运行时目录中的临时文件"""
        if self.runtime_dir.exists():
            for f in self.runtime_dir.iterdir():
                if f.is_file() and f.suffix in (".mp4", ".webm", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".pdf", ".txt"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
