"""平台解析器模块。

汇总导出所有已实现的平台解析器，以及基类和数据模型。
新增解析器后需在此处注册到 ALL_PARSERS 列表。
"""

from __future__ import annotations

from .base import BaseParser, ParseResult
from .pixiv import PixivParser
from .twitter import TwitterParser
from .weibo import WeiboParser
from .youtube import YouTubeParser
from .zhihu import ZhihuParser

ALL_PARSERS: list[type[BaseParser]] = [
    ZhihuParser,
    WeiboParser,
    YouTubeParser,
    TwitterParser,
    PixivParser,
]

__all__ = [
    "BaseParser",
    "ParseResult",
    "ALL_PARSERS",
    "ZhihuParser",
    "WeiboParser",
    "YouTubeParser",
    "TwitterParser",
    "PixivParser",
]

