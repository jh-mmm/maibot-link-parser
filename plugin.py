"""MaiBot 多平台链接解析器插件入口"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .downloader import MediaDownloader
from .parsers.base import BaseParser, ParseResult
from .sender import (
    ApiSettings,
    MessageSegment,
    image_segment,
    send_forward,
    send_image,
    send_text,
    send_video,
    text_segment,
)

logger = logging.getLogger("plugin.com.maibot.link-parser")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "extension"
    __ui_order__ = 0

    name: str = Field(default="maibot-link-parser", description="插件名称")
    config_version: str = Field(default="1.2.0", description="配置文件版本")
    version: str = Field(default="1.2.0", description="插件版本")
    enabled: bool = Field(default=True, description="是否启用插件")


class GeneralSectionConfig(PluginConfigBase):
    __ui_label__ = "通用设置"
    __ui_icon__ = "settings"
    __ui_order__ = 1

    timeout: int = Field(default=15, description="HTTP 请求超时（秒）")
    max_content_length: int = Field(default=500, description="内容摘要最大字符数")
    max_video_size_mb: int = Field(default=50, description="视频下载最大体积（MB）")
    max_video_duration: int = Field(default=300, description="视频最大时长（秒）")


class PlatformSectionConfig(PluginConfigBase):
    __ui_label__ = "平台开关"
    __ui_icon__ = "link"
    __ui_order__ = 2

    zhihu: bool = Field(default=True, description="启用知乎解析")
    weibo: bool = Field(default=True, description="启用微博解析")
    youtube: bool = Field(default=True, description="启用 YouTube 解析")
    twitter: bool = Field(default=True, description="启用 Twitter/X 解析")


class YouTubeSectionConfig(PluginConfigBase):
    __ui_label__ = "YouTube"
    __ui_icon__ = "smart_display"
    __ui_order__ = 3

    youtube_api_key: str = Field(default="", description="YouTube Data API v3 Key（可选）")
    cookies: str = Field(
        default="",
        description="YouTube 登录 Cookies（下载受限视频时可选）",
    )


class ZhihuSectionConfig(PluginConfigBase):
    __ui_label__ = "知乎"
    __ui_icon__ = "lightbulb"
    __ui_order__ = 4

    cookies: str = Field(
        default="",
        description="知乎登录 Cookies（遇到风控或登录页时需要填写）",
    )
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS 代理地址（如 http://127.0.0.1:7890，留空不使用）",
    )


class TwitterSectionConfig(PluginConfigBase):
    __ui_label__ = "Twitter/X"
    __ui_icon__ = "alternate_email"
    __ui_order__ = 5

    twitter_api_key: str = Field(default="", description="Twitter API Key（可选）")
    twitter_api_base_url: str = Field(
        default="", description="Twitter 自定义 API 基础 URL（可选）"
    )


class OneBotSectionConfig(PluginConfigBase):
    __ui_label__ = "OneBot"
    __ui_icon__ = "cable"
    __ui_order__ = 6

    host: str = Field(default="127.0.0.1", description="OneBot 实现（如 NapCat/go-cqhttp）HTTP 地址")
    port: int = Field(default=3000, description="OneBot HTTP 端口")
    token: str = Field(default="", description="OneBot access_token（可选）")
    bot_uin: str = Field(default="", description="机器人 QQ 号（合并转发节点使用，可选）")
    merge_send: bool = Field(
        default=True,
        description="对知乎解析与多图结果使用合并转发发送（失败时自动降级为逐条发送）",
    )


class ParserConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralSectionConfig = Field(default_factory=GeneralSectionConfig)
    platforms: PlatformSectionConfig = Field(default_factory=PlatformSectionConfig)
    youtube: YouTubeSectionConfig = Field(default_factory=YouTubeSectionConfig)
    zhihu: ZhihuSectionConfig = Field(default_factory=ZhihuSectionConfig)
    twitter: TwitterSectionConfig = Field(default_factory=TwitterSectionConfig)
    onebot: OneBotSectionConfig = Field(default_factory=OneBotSectionConfig)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 插件主类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LinkParserPlugin(MaiBotPlugin):
    """多平台链接解析器插件"""

    config_model = ParserConfig

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        self.ctx.logger.info("多平台链接解析器已加载")
        self._init_parsers()

    async def on_unload(self) -> None:
        self.ctx.logger.info("多平台链接解析器已卸载")
        if hasattr(self, "_downloader"):
            self._downloader.cleanup()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("配置已更新，重新初始化解析器")
            self._init_parsers()

    # ── 解析器初始化 ────────────────────────────────────────────────────

    def _init_parsers(self) -> None:
        # 获取运行时临时目录（兼容不同 SDK 版本）
        try:
            runtime_dir = self.ctx.paths.runtime_dir / "link-parser"
        except AttributeError:
            runtime_dir = Path(__file__).resolve().parent / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        self._downloader = MediaDownloader(
            runtime_dir=runtime_dir,
            max_size_mb=self.config.general.max_video_size_mb,
            timeout=self.config.general.timeout,
        )

        # OneBot HTTP 发送接口所需的连接信息
        self._api = ApiSettings()
        self._api.host = self.config.onebot.host
        self._api.port = self.config.onebot.port
        self._api.token = self.config.onebot.token
        self._api.bot_uin = self.config.onebot.bot_uin

        self._parsers: list[BaseParser] = []

        from .parsers.zhihu import ZhihuParser
        from .parsers.weibo import WeiboParser
        from .parsers.youtube import YouTubeParser
        from .parsers.twitter import TwitterParser

        common_timeout = self.config.general.timeout
        max_content_length = self.config.general.max_content_length

        if self.config.platforms.zhihu:
            self._parsers.append(
                ZhihuParser(
                    cookies=self.config.zhihu.cookies,
                    timeout=common_timeout,
                    max_content_length=max_content_length,
                    proxy=self.config.zhihu.proxy,
                )
            )
        if self.config.platforms.weibo:
            self._parsers.append(
                WeiboParser(timeout=common_timeout, max_content_length=max_content_length)
            )
        if self.config.platforms.youtube:
            self._parsers.append(
                YouTubeParser(
                    api_key=self.config.youtube.youtube_api_key,
                    cookies=self.config.youtube.cookies,
                    timeout=common_timeout,
                )
            )
        if self.config.platforms.twitter:
            self._parsers.append(
                TwitterParser(
                    api_key=self.config.twitter.twitter_api_key,
                    api_base_url=self.config.twitter.twitter_api_base_url,
                    timeout=common_timeout,
                    max_content_length=max_content_length,
                )
            )

        self.ctx.logger.info(
            "已启用 %d 个解析器: %s",
            len(self._parsers),
            [p.platform_name for p in self._parsers],
        )

    # ── 消息钩子 ────────────────────────────────────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="link_parser",
        description="检测并解析多平台链接",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        # 解析+下载+发送在钩子内同步完成（成功才 abort），需留足时间，
        # 避免被 before_process 默认超时取消导致「部分结果 + AI 双回复」。
        timeout_ms=120000,
    )
    async def handle_message(self, **kwargs) -> dict[str, str]:
        """拦截入站消息，检测链接并解析。

        链接解析结果发送成功后中止该消息的后续处理，避免机器人重复回复。
        """
        try:
            if await self._do_handle_message(kwargs):
                return {"action": "abort"}
        except Exception:
            self.ctx.logger.error(
                "link_parser 未捕获异常:\n%s", traceback.format_exc()
            )
        return {"action": "continue"}

    async def _do_handle_message(self, kwargs: dict) -> bool:
        """处理消息；仅在解析文本发送成功时返回 True。"""
        if not self.config.plugin.enabled:
            return False

        message = kwargs.get("message", {})

        # ── 1. 提取纯文本 ──────────────────────────────────────────────
        raw_text = self._extract_text(message)
        if not raw_text:
            return False

        # ── 2. 获取 stream_id（仅用于日志，发送已改为按 message 路由） ──
        stream_id = self._extract_stream_id(kwargs, message)
        if not stream_id:
            self.ctx.logger.warning(
                "link_parser: 无法获取 stream_id | kwargs_keys=%s | msg_keys=%s",
                list(kwargs.keys()),
                list(message.keys()) if isinstance(message, dict) else type(message).__name__,
            )
            return False

        self.ctx.logger.info(
            "link_parser | stream=%s | text=%s",
            stream_id[:16],
            raw_text[:80],
        )

        # ── 3. 匹配解析器 ─────────────────────────────────────────────
        for parser in self._parsers:
            match = parser.match_url(raw_text)
            if match:
                self.ctx.logger.info(
                    "link_parser | 匹配 %s | url=%s",
                    parser.platform_name,
                    match.group(0)[:80],
                )
                # 识别到链接即先告知用户，避免解析期间无任何反馈
                await self._send_status(
                    message, f"🔗 识别到{parser.platform_name}链接，开始解析..."
                )
                try:
                    result = await parser.parse(raw_text, match)
                    self.ctx.logger.info(
                        "link_parser | 解析成功 | title=%s author=%s",
                        (result.title or "(无)")[:40],
                        (result.author or "(无)")[:20],
                    )
                    return await self._send_result(result, message)
                except Exception as e:
                    self.ctx.logger.error(
                        "link_parser | %s 解析失败: %s\n%s",
                        parser.platform_name,
                        e,
                        traceback.format_exc(),
                    )
                    # 解析失败时把原因反馈给用户（而非静默吞掉）
                    await self._send_status(
                        message,
                        f"❌ {parser.platform_name}解析失败：{self._friendly_error(e)}",
                    )
                # 只处理第一条匹配的链接
                break
        return False

    # ── 状态提示与错误归因 ──────────────────────────────────────────────

    async def _send_status(self, message: dict, text: str) -> None:
        """发送一条简短状态提示（开始解析 / 解析失败原因等）。

        仅用于反馈进度与失败原因，不影响主流程；发送失败只记日志，不抛异常。
        """
        try:
            ok = await send_text(message, text, self._api)
            if not ok:
                self.ctx.logger.warning("link_parser | 状态提示发送失败: OneBot 接口返回失败")
        except Exception as e:
            self.ctx.logger.warning("link_parser | 状态提示发送失败: %s", e)

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        """把解析异常转成面向用户的简短原因说明。

        优先使用解析器主动抛出的文案（如知乎的风控/登录页提示）；
        对于未知异常给出兜底说明，避免把堆栈直接丢给用户。
        """
        message = str(exc).strip()
        if message:
            # 限制长度，防止个别异常携带超长信息刷屏
            return message if len(message) <= 200 else message[:200] + "…"
        return f"{type(exc).__name__}（未知错误）"

    # ── 消息文本提取 ────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(message: object) -> str:
        """从 MaiBot 消息 kwargs 中提取纯文本。

        MaiBot hook 的 message 结构 (实测)：
        {
            "message_id": "...",
            "processed_plain_text": "纯文本",      ← 优先使用
            "raw_message": [                        ← segment 列表
                {"type": "text", "data": "文本内容"},
                ...
            ],
            "session_id": "...",
            ...
        }
        """
        if not isinstance(message, dict):
            return str(message) if message else ""

        # 1) processed_plain_text — MaiBot 已处理好的纯文本，最可靠
        ppt = message.get("processed_plain_text")
        if isinstance(ppt, str) and ppt.strip():
            return ppt.strip()

        # 2) raw_message — 可能是字符串或 segment 列表
        raw = message.get("raw_message")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, list):
            parts: list[str] = []
            for seg in raw:
                if isinstance(seg, str):
                    parts.append(seg)
                elif isinstance(seg, dict):
                    # MaiBot 实际格式: {"type": "text", "data": "文本"}
                    # 兼容格式: {"type": "text", "content": "文本"}
                    # 兼容格式: {"type": "text", "data": {"text": "文本"}}
                    seg_type = seg.get("type", "")
                    if seg_type == "text":
                        data = seg.get("data")
                        if isinstance(data, str):
                            parts.append(data)
                        elif isinstance(data, dict):
                            parts.append(data.get("text", ""))
                        else:
                            content = seg.get("content", "")
                            if isinstance(content, str):
                                parts.append(content)
            text = " ".join(p for p in parts if p)
            if text.strip():
                return text.strip()

        # 3) 其他可能的字段名
        for key in ("plain_text", "text", "content"):
            val = message.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        return ""

    # ── stream_id 提取 ─────────────────────────────────────────────────

    @staticmethod
    def _extract_stream_id(kwargs: dict, message: object) -> str:
        """从 hook kwargs 和 message 中提取 stream_id。

        MaiBot hook 实测 kwargs 中只有 ['hook_name', 'message']，
        stream_id 在 message dict 的 session_id 字段里。
        """
        # 优先从 kwargs 顶层取
        for key in ("stream_id", "session_id", "chat_id"):
            val = kwargs.get(key, "")
            if val:
                return str(val)

        # 从 message dict 中取
        if isinstance(message, dict):
            for key in ("stream_id", "session_id", "chat_id"):
                val = message.get(key, "")
                if val:
                    return str(val)

        return ""

    # ── 结果发送（OneBot HTTP 接口） ──────────────────────────────────────

    async def _send_result(self, result: ParseResult, message: dict) -> bool:
        """发送解析结果。

        知乎解析与多图结果优先使用合并转发；未命中或失败时降级为逐条发送。
        发送成功时返回 True。
        """
        image_urls = [u for u in dict.fromkeys([*result.images, result.cover_image]) if u]
        use_forward = self.config.onebot.merge_send and (
            result.platform == "知乎" or len(image_urls) >= 2
        )

        if use_forward:
            if await self._send_forward_result(result, image_urls, message):
                # 合并转发已包含文本与图片；视频仍单独补发（知乎解析器不产生视频）。
                if result.video_url:
                    await self._send_video(result, message)
                return True
            self.ctx.logger.warning("link_parser | 合并转发失败，降级为逐条发送")

        # 逐条发送（默认或降级路径）
        text = self._format_text_result(result)
        try:
            ok = await send_text(message, text, self._api)
        except Exception as e:
            self.ctx.logger.error("link_parser | 文本发送失败: %s", e)
            return False

        if not ok:
            self.ctx.logger.error("link_parser | 文本发送失败: OneBot 接口返回失败")
            return False
        self.ctx.logger.info("link_parser | 文本已发送")

        # 文本已发送后，再补发解析到的原始图片和视频附件。
        await self._send_images(result, message)
        if result.video_url:
            await self._send_video(result, message)
        return True

    async def _send_forward_result(
        self,
        result: ParseResult,
        image_urls: list[str],
        message: dict,
    ) -> bool:
        """将文本与图片组装为合并转发消息发送，成功返回 True。"""
        nodes: list[list[MessageSegment]] = []

        text = self._format_text_result(result)
        if text:
            nodes.append([text_segment(text)])

        media_headers = result.extra.get("media_headers")
        downloaded = await self._download_images(image_urls, media_headers)
        try:
            for image_path in downloaded:
                nodes.append([image_segment(image_path)])

            # 知乎：即使只有文字也走合并转发；非知乎多图：若无图片成功则降级。
            if result.platform != "知乎" and not downloaded:
                self.ctx.logger.warning("link_parser | 无可用图片节点，放弃合并转发")
                return False
            if not nodes:
                return False

            ok = await send_forward(message, nodes, self._api)
            if ok:
                self.ctx.logger.info(
                    "link_parser | 合并转发已发送（%d 文本 + %d 图片节点）",
                    1 if text else 0,
                    len(downloaded),
                )
            else:
                self.ctx.logger.warning("link_parser | 合并转发失败: OneBot 接口返回失败")
            return ok
        finally:
            for path in downloaded:
                path.unlink(missing_ok=True)

    async def _download_images(
        self,
        image_urls: list[str],
        media_headers: dict | None,
    ) -> list[Path]:
        """下载图片列表，返回下载成功的本地路径（调用方负责清理）。"""
        downloaded: list[Path] = []
        for image_url in image_urls:
            if not image_url:
                continue
            try:
                image_path = await self._downloader.download_image(
                    image_url,
                    headers=media_headers,
                )
                if image_path is None or not image_path.exists():
                    self.ctx.logger.warning("link_parser | 图片下载失败: %s", image_url)
                    continue
                downloaded.append(image_path)
            except Exception as e:
                self.ctx.logger.warning("link_parser | 图片下载失败: %s", e)
        return downloaded

    async def _send_images(self, result: ParseResult, message: dict) -> None:
        """下载并发送解析结果中的原始图片。"""
        image_urls = [u for u in dict.fromkeys([*result.images, result.cover_image]) if u]
        media_headers = result.extra.get("media_headers")
        downloaded = await self._download_images(image_urls, media_headers)
        for image_path in downloaded:
            try:
                ok = await send_image(message, image_path, self._api)
                if ok:
                    self.ctx.logger.info("link_parser | 原始图片已发送")
                else:
                    self.ctx.logger.warning("link_parser | 图片发送失败: OneBot 接口返回失败")
            except Exception as e:
                self.ctx.logger.warning("link_parser | 图片发送失败: %s", e)
            finally:
                image_path.unlink(missing_ok=True)

    async def _send_video(self, result: ParseResult, message: dict) -> None:
        """下载并发送视频附件。"""
        if (
            result.video_duration > 0
            and result.video_duration > self.config.general.max_video_duration
        ):
            self.ctx.logger.info(
                "link_parser | 视频时长 %ds 超限 %ds，跳过",
                result.video_duration,
                self.config.general.max_video_duration,
            )
            return

        video_path = None
        try:
            media_headers = result.extra.get("media_headers")
            if result.extra.get("video_downloader") == "yt-dlp":
                video_path = await self._downloader.download_youtube_video(
                    result.video_url,
                    max_duration=self.config.general.max_video_duration,
                    cookies=result.extra.get("youtube_cookies", ""),
                )
            else:
                video_path = await self._downloader.download_video(
                    result.video_url,
                    headers=media_headers,
                )
            if video_path and video_path.exists():
                ok = await send_video(message, video_path, self._api)
                if ok:
                    self.ctx.logger.info("link_parser | 视频已发送")
                else:
                    self.ctx.logger.warning("link_parser | 视频发送失败: OneBot 接口返回失败")
                    await send_text(message, f"📹 视频链接: {result.video_url}", self._api)
        except Exception as e:
            self.ctx.logger.warning("link_parser | 视频发送失败: %s", e)
            try:
                await send_text(message, f"📹 视频链接: {result.video_url}", self._api)
            except Exception:
                pass
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)

    # ── 文本降级格式化 ──────────────────────────────────────────────────

    @staticmethod
    def _format_text_result(result: ParseResult) -> str:
        """格式化要直接发送的链接解析文本。"""
        sep = "=" * 20
        parts: list[str] = [sep]
        parts.append(f"{result.platform_icon} {result.platform}")

        if result.title:
            parts.append(f"📌 {result.title}")
        if result.author:
            parts.append(f"👤 {result.author}")
        if result.content:
            parts.append(f"\n{result.content}")

        if result.stats:
            stats_text = result.stats_text()
            if stats_text:
                parts.append(stats_text)

        parts.append(f"🔗 {result.url}")
        parts.append(sep)
        return "\n".join(parts)


def create_plugin():
    return LinkParserPlugin()
