"""MaiBot 多平台链接解析器插件入口"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import ClassVar, Literal

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .downloader import MediaDownloader
from .parsers.base import BaseParser, ParseResult
from .sender import (
    ApiSettings,
    MessageSegment,
    get_group_id,
    get_user_id,
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
    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    name: str = Field(
        default="maibot-link-parser",
        description="插件唯一标识名",
        json_schema_extra={"label": "插件标识", "disabled": True, "hidden": True},
    )
    config_version: str = Field(
        default="1.4.2",
        description="配置文件版本号",
        json_schema_extra={"label": "配置版本", "disabled": True, "hidden": True},
    )
    version: str = Field(
        default="1.4.2",
        description="插件发布版本号",
        json_schema_extra={"label": "插件版本", "disabled": True, "hidden": True},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用多平台链接解析插件",
        json_schema_extra={"label": "启用插件"},
    )


class GeneralSectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "通用设置"
    __ui_icon__: ClassVar[str] = "settings"
    __ui_order__: ClassVar[int] = 1

    timeout: int = Field(
        default=15,
        description="全局网络请求超时时间（单位：秒）",
        json_schema_extra={"label": "请求超时时间 (秒)"},
    )
    max_content_length: int = Field(
        default=500,
        description="正文摘要最大字符数（超过限制自动在句末截断并显示省略号）",
        json_schema_extra={"label": "正文摘要最大字数"},
    )
    max_video_size_mb: int = Field(
        default=50,
        description="允许下载并发送的最大视频体积（单位：MB，超过限制则不发送视频文件）",
        json_schema_extra={"label": "视频最大体积 (MB)"},
    )
    max_video_duration: int = Field(
        default=300,
        description="允许下载并发送的最大视频时长（单位：秒，300 即 5 分钟，超过限制则跳过）",
        json_schema_extra={"label": "视频最大时长 (秒)"},
    )


class PlatformSectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "平台开关"
    __ui_icon__: ClassVar[str] = "link"
    __ui_order__: ClassVar[int] = 2

    zhihu: bool = Field(
        default=True,
        description="是否启用知乎链接解析",
        json_schema_extra={"label": "启用知乎解析"},
    )
    weibo: bool = Field(
        default=True,
        description="是否启用微博链接解析",
        json_schema_extra={"label": "启用微博解析"},
    )
    youtube: bool = Field(
        default=False,
        description="是否启用 YouTube 视频解析",
        json_schema_extra={"label": "启用 YouTube 解析"},
    )
    twitter: bool = Field(
        default=False,
        description="是否启用 Twitter/X 推文解析",
        json_schema_extra={"label": "启用 Twitter(X) 解析"},
    )
    pixiv: bool = Field(
        default=True,
        description="是否启用 Pixiv 插画/漫画/动图/小说解析",
        json_schema_extra={"label": "启用 Pixiv 解析"},
    )


class AccessControlConfig(PluginConfigBase):
    """访问控制基础配置（各平台独立继承）。

    群维度与用户维度各自独立判定，两者都通过才会解析（AND 关系）。
    被拦截的消息不会阻止 AI 回复，插件对该消息完全隐形。
    """

    group_mode: Literal["off", "whitelist", "blacklist"] = Field(
        default="off",
        description="群名单模式：off=不限制 | whitelist=仅名单内群 | blacklist=名单内群不解析",
        json_schema_extra={"label": "群名单模式", "x-widget": "select"},
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群白名单（群号列表），group_mode=whitelist 时生效；留空表示所有群都不解析",
        json_schema_extra={"label": "群号白名单"},
    )
    group_blacklist: list[str] = Field(
        default_factory=list,
        description="群黑名单（群号列表），group_mode=blacklist 时生效",
        json_schema_extra={"label": "群号黑名单"},
    )
    user_mode: Literal["off", "whitelist", "blacklist"] = Field(
        default="off",
        description="用户名单模式：off=不限制 | whitelist=仅名单内用户 | blacklist=名单内用户不解析",
        json_schema_extra={"label": "用户名单模式", "x-widget": "select"},
    )
    user_whitelist: list[str] = Field(
        default_factory=list,
        description="用户白名单（QQ 号列表），user_mode=whitelist 时生效；留空表示所有人都不解析",
        json_schema_extra={"label": "用户 QQ 白名单"},
    )
    user_blacklist: list[str] = Field(
        default_factory=list,
        description="用户黑名单（QQ 号列表），user_mode=blacklist 时生效",
        json_schema_extra={"label": "用户 QQ 黑名单"},
    )


class ZhihuSectionConfig(AccessControlConfig):
    __ui_label__: ClassVar[str] = "知乎"
    __ui_icon__: ClassVar[str] = "lightbulb"
    __ui_order__: ClassVar[int] = 3

    cookies: str = Field(
        default="",
        description="知乎登录 Cookies（遇到反爬验证或风控时需要填写，包含 z_c0 等）",
        json_schema_extra={"label": "知乎 Cookies", "input_type": "textarea"},
    )
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7890，留空则直连）",
        json_schema_extra={"label": "知乎代理地址"},
    )


class WeiboSectionConfig(AccessControlConfig):
    __ui_label__: ClassVar[str] = "微博"
    __ui_icon__: ClassVar[str] = "chat"
    __ui_order__: ClassVar[int] = 4


class YouTubeSectionConfig(AccessControlConfig):
    __ui_label__: ClassVar[str] = "YouTube"
    __ui_icon__: ClassVar[str] = "smart_display"
    __ui_order__: ClassVar[int] = 5

    youtube_api_key: str = Field(
        default="",
        description="Google YouTube Data API v3 密钥（用于获取播放量/点赞数/时长等数据，可选）",
        json_schema_extra={"label": "YouTube API Key", "input_type": "password"},
    )
    cookies: str = Field(
        default="",
        description="YouTube 登录 Cookies（用于 yt-dlp 下载年龄受限或受限视频时填写，可选）",
        json_schema_extra={"label": "YouTube Cookies", "input_type": "textarea"},
    )


class TwitterSectionConfig(AccessControlConfig):
    __ui_label__: ClassVar[str] = "Twitter/X"
    __ui_icon__: ClassVar[str] = "alternate_email"
    __ui_order__: ClassVar[int] = 6

    twitter_api_key: str = Field(
        default="",
        description="Twitter/X 自定义 API 密钥（使用自建/反代 fxtwitter 实例时填写，可选）",
        json_schema_extra={"label": "Twitter API Key", "input_type": "password"},
    )
    twitter_api_base_url: str = Field(
        default="",
        description="Twitter/X 自定义 API 根地址（例如 https://fxtwitter.example.com，留空使用公共接口）",
        json_schema_extra={"label": "Twitter API 地址"},
    )


class PixivSectionConfig(AccessControlConfig):
    __ui_label__: ClassVar[str] = "Pixiv"
    __ui_icon__: ClassVar[str] = "palette"
    __ui_order__: ClassVar[int] = 7

    cookies: str = Field(
        default="",
        description="Pixiv 登录 Cookies（用于访问 R18 或登录限定内容，支持填写整行 Cookie 或仅填 PHPSESSID）",
        json_schema_extra={"label": "Pixiv Cookies", "input_type": "textarea"},
    )
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS 代理地址（国内服务器推荐配置，例如 http://127.0.0.1:7890）",
        json_schema_extra={"label": "Pixiv 代理地址"},
    )
    img_proxy: str = Field(
        default="",
        description="图片反代域名（如 i.pixiv.re，免代理直连图片服务器，留空则直连原图站）",
        json_schema_extra={"label": "图片反代域名"},
    )
    nsfw: Literal["send", "blur", "ignore"] = Field(
        default="blur",
        description="R18 内容策略：blur=高斯模糊打码封面 | ignore=忽略并拦截 | send=正常发送",
        json_schema_extra={"label": "R18 内容策略", "x-widget": "select"},
    )
    image_quality: Literal["regular", "original"] = Field(
        default="regular",
        description="图片清晰度：regular=标准清晰度大图 | original=原图",
        json_schema_extra={"label": "图片清晰度", "x-widget": "select"},
    )
    max_manga_pages: int = Field(
        default=20,
        description="漫画最大下载解析页数（超过该限制仅发送封面并提示，0 为不限制）",
        json_schema_extra={"label": "漫画最大下载页数"},
    )


class OneBotSectionConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "OneBot"
    __ui_icon__: ClassVar[str] = "cable"
    __ui_order__: ClassVar[int] = 8

    host: str = Field(
        default="127.0.0.1",
        description="OneBot 实现（如 NapCat/go-cqhttp/LLOneBot）HTTP 监听地址",
        json_schema_extra={"label": "OneBot HTTP 地址"},
    )
    port: int = Field(
        default=3000,
        description="OneBot HTTP 服务端口",
        json_schema_extra={"label": "OneBot HTTP 端口"},
    )
    token: str = Field(
        default="",
        description="OneBot access_token（未开启 Token 请保持为空）",
        json_schema_extra={"label": "OneBot Access Token", "input_type": "password"},
    )
    bot_uin: str = Field(
        default="",
        description="机器人 QQ 号（用于合并转发节点展示，普通消息发送无需填写）",
        json_schema_extra={"label": "机器人 QQ 号"},
    )
    merge_send: bool = Field(
        default=True,
        description="是否对知乎解析与多图画集使用合并转发发送（失败时自动降级为逐条发送）",
        json_schema_extra={"label": "合并转发发送"},
    )


class ParserConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralSectionConfig = Field(default_factory=GeneralSectionConfig)
    platforms: PlatformSectionConfig = Field(default_factory=PlatformSectionConfig)
    zhihu: ZhihuSectionConfig = Field(default_factory=ZhihuSectionConfig)
    weibo: WeiboSectionConfig = Field(default_factory=WeiboSectionConfig)
    youtube: YouTubeSectionConfig = Field(default_factory=YouTubeSectionConfig)
    twitter: TwitterSectionConfig = Field(default_factory=TwitterSectionConfig)
    pixiv: PixivSectionConfig = Field(default_factory=PixivSectionConfig)
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
        from .parsers.pixiv import PixivParser

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
        if self.config.platforms.pixiv:
            self._parsers.append(
                PixivParser(
                    cookies=self.config.pixiv.cookies,
                    proxy=self.config.pixiv.proxy,
                    img_proxy=self.config.pixiv.img_proxy,
                    nsfw=self.config.pixiv.nsfw,
                    image_quality=self.config.pixiv.image_quality,
                    max_manga_pages=self.config.pixiv.max_manga_pages,
                    runtime_dir=runtime_dir,
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
                # 针对匹配到的平台执行独立的访问控制检查
                platform_access = self._get_platform_access_config(parser.platform_name)
                if isinstance(message, dict) and not self._is_access_allowed(
                    message, platform_access
                ):
                    self.ctx.logger.info(
                        "link_parser | 匹配到 %s 链接，但未通过该平台的访问控制规则，跳过解析",
                        parser.platform_name,
                    )
                    return False

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

    # ── 访问控制 ────────────────────────────────────────────────────────

    def _get_platform_access_config(
        self, platform_name: str
    ) -> AccessControlConfig | None:
        """根据平台显示名称获取对应的独立访问控制配置。"""
        mapping: dict[str, AccessControlConfig | None] = {
            "知乎": getattr(self.config, "zhihu", None),
            "微博": getattr(self.config, "weibo", None),
            "YouTube": getattr(self.config, "youtube", None),
            "Twitter": getattr(self.config, "twitter", None),
            "Pixiv": getattr(self.config, "pixiv", None),
        }
        return mapping.get(platform_name)

    @staticmethod
    def _match_access_mode(
        mode: str,
        value: str | None,
        whitelist: list[str],
        blacklist: list[str],
    ) -> bool:
        """按 mode 判断单维度是否放行。

        - value 为 None（如私聊消息没有 group_id）时该维度不作判断，直接放行。
        - mode = "off" 时不作限制，直接放行。
        - mode = "whitelist" 时，仅名单内的号码放行；名单为空时全部拒绝。
        - mode = "blacklist" 时，名单内的号码拦截，其余放行。
        - 自动将列表项与待匹配值统一转为去除两端空白的字符串，兼容数字与字符串配置。
        """
        if mode == "off":
            return True
        if value is None:
            return True
        val_str = str(value).strip()
        if mode == "whitelist":
            return val_str in {str(item).strip() for item in whitelist}
        if mode == "blacklist":
            return val_str not in {str(item).strip() for item in blacklist}
        return True

    def _is_access_allowed(
        self, message: dict, access: AccessControlConfig | None = None
    ) -> bool:
        """群维度与用户维度都通过才放行（两个维度是「与」关系）。"""
        if access is None:
            return True

        group_id = get_group_id(message)
        if not self._match_access_mode(
            access.group_mode, group_id, access.group_whitelist, access.group_blacklist
        ):
            self.ctx.logger.debug(
                "link_parser | 访问控制拦截：群 %s 未通过 group_mode=%s",
                group_id,
                access.group_mode,
            )
            return False

        user_id = get_user_id(message)
        if not self._match_access_mode(
            access.user_mode, user_id, access.user_whitelist, access.user_blacklist
        ):
            self.ctx.logger.debug(
                "link_parser | 访问控制拦截：用户 %s 未通过 user_mode=%s",
                user_id,
                access.user_mode,
            )
            return False

        return True

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
        proxy = result.extra.get("proxy")
        downloaded = await self._download_images(image_urls, media_headers, proxy=proxy)
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
        proxy: str | None = None,
    ) -> list[Path]:
        """下载图片列表，返回下载成功的本地路径（调用方负责清理）。"""
        downloaded: list[Path] = []
        for image_url in image_urls:
            if not image_url:
                continue
            # 若已经是本地已存在的文件路径（如已生成的动图 GIF 或高斯模糊封面）
            local_p = Path(image_url)
            if local_p.exists() and local_p.is_file():
                downloaded.append(local_p)
                continue
            try:
                image_path = await self._downloader.download_image(
                    image_url,
                    headers=media_headers,
                    proxy=proxy,
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
        proxy = result.extra.get("proxy")
        downloaded = await self._download_images(image_urls, media_headers, proxy=proxy)
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
