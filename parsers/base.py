"""解析器基类与数据模型。

定义所有平台解析器共享的抽象基类 BaseParser 和统一的解析结果
数据类 ParseResult。每个具体平台的解析器都应继承 BaseParser
并实现 parse 方法。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class ParseResult:
    """链接解析结果的统一数据模型。

    所有平台解析器返回此类型，由卡片渲染器消费。
    字段均有默认值，解析器只需填充其获取到的信息。

    Attributes:
        title: 内容标题（文章标题、推文摘要等）。
        author: 作者/发布者名称。
        author_avatar: 作者头像 URL。
        content: 正文摘要（已清理 HTML、已截断）。
        cover_image: 封面图 URL。
        images: 内容中包含的图片 URL 列表。
        video_url: 视频直链 URL（用于视频类内容）。
        video_duration: 视频时长（秒）。
        url: 原始链接 URL。
        platform: 平台名称（如 "知乎"、"微博"）。
        platform_icon: 平台图标（emoji 或符号）。
        stats: 互动数据，如 likes、comments、reposts、views 等。
        extra: 额外的平台特定数据。
    """

    title: str = ""
    author: str = ""
    author_avatar: str = ""
    content: str = ""
    cover_image: str = ""
    images: list[str] = field(default_factory=list)
    video_url: str = ""
    video_duration: int = 0
    url: str = ""
    platform: str = ""
    platform_icon: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        """是否包含视频内容。"""
        return bool(self.video_url)

    @property
    def has_images(self) -> bool:
        """是否包含图片内容。"""
        return bool(self.images) or bool(self.cover_image)

    @property
    def unique_image_urls(self) -> list[str]:
        """去重后的图片 URL 列表（封面排在首位）。"""
        seen: set[str] = set()
        result: list[str] = []
        for u in ([self.cover_image] + self.images):
            if u and u not in seen:
                seen.add(u)
                result.append(u)
        return result

    def stats_text(self) -> str:
        """将互动数据格式化为可读文本。

        Returns:
            格式化的统计文本，如 "👍 1.2万  💬 386  🔄 520"。
        """
        from ..utils import format_number

        icon_map = {
            "likes": "👍",
            "comments": "💬",
            "reposts": "🔄",
            "views": "👁️",
            "favorites": "⭐",
            "followers": "👥",
            "shares": "↗️",
        }

        parts: list[str] = []
        for key, value in self.stats.items():
            icon = icon_map.get(key, "📊")
            parts.append(f"{icon} {format_number(value)}")

        return "  ".join(parts)


class BaseParser(ABC):
    """平台解析器抽象基类。

    所有具体平台解析器必须继承此类并实现以下内容：
    - url_patterns: 该平台支持的 URL 正则模式列表。
    - platform_name: 平台显示名称。
    - platform_icon: 平台图标 emoji。
    - parse(): 异步解析方法。

    使用示例::

        class ZhihuParser(BaseParser):
            url_patterns = [re.compile(r"zhihu\\.com/question/(\\d+)")]
            platform_name = "知乎"
            platform_icon = "🔵"

            async def parse(self, url, match):
                ...
    """

    url_patterns: ClassVar[list[re.Pattern]] = []
    platform_name: ClassVar[str] = ""
    platform_icon: ClassVar[str] = ""
    config_key: ClassVar[str] = ""

    @abstractmethod
    async def parse(self, url: str, match: re.Match) -> ParseResult:
        """解析给定 URL 并返回结构化结果。

        Args:
            url: 完整的原始 URL。
            match: URL 正则匹配结果，可从中提取 ID 等参数。

        Returns:
            填充了平台信息的 ParseResult 实例。

        Raises:
            Exception: 解析过程中发生的任何错误。
        """
        ...

    @classmethod
    def match_url(cls, text: str) -> re.Match | None:
        """尝试在文本中匹配该解析器支持的 URL 模式。

        按 url_patterns 列表顺序逐一尝试匹配，返回第一个成功的
        Match 对象；如果全部不匹配则返回 None。

        Args:
            text: 待匹配的文本（可包含非 URL 内容）。

        Returns:
            匹配成功时返回 re.Match 对象，否则返回 None。
        """
        for pattern in cls.url_patterns:
            m = pattern.search(text)
            if m:
                return m
        return None

    def _make_result(self, **kwargs) -> ParseResult:
        """创建预填充平台信息的 ParseResult 便捷方法。

        自动设置 platform 和 platform_icon 字段，子类调用时
        只需传入实际解析到的数据字段。

        Args:
            **kwargs: 传递给 ParseResult 的字段值。

        Returns:
            预填充了平台信息的 ParseResult 实例。
        """
        return ParseResult(
            platform=self.platform_name,
            platform_icon=self.platform_icon,
            **kwargs,
        )
