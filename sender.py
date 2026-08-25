# -*- coding: utf-8 -*-
"""OneBot HTTP sender for the MaiBot multi-platform parser plugin."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("plugin.com.maibot.link-parser.sender")


class ApiSettings:
    host: str
    port: int
    token: str
    bot_uin: str


MessageSegment = dict[str, Any]


async def send_text(message: dict[str, Any], text: str, api: ApiSettings) -> bool:
    return await _send_message(message, [{"type": "text", "data": {"text": text}}], api, timeout=30)


async def send_image(message: dict[str, Any], path: Path, api: ApiSettings) -> bool:
    return await _send_message(message, [image_segment(path)], api, timeout=120)


async def send_video(message: dict[str, Any], path: Path, api: ApiSettings) -> bool:
    return await _send_message(message, [{"type": "video", "data": {"file": _file_uri(path)}}], api, timeout=300)


async def send_forward(
    message: dict[str, Any],
    nodes: list[list[MessageSegment]],
    api: ApiSettings,
) -> bool:
    """以合并转发方式发送多个消息节点（群聊与私聊均支持）。

    Args:
        message: 触发解析的原消息，用于路由到对应群/私聊。
        nodes: 每个元素是一条消息的所有 segment 列表，对应一个转发节点。
        api: OneBot HTTP 连接信息。
    """
    if not nodes:
        return True

    forward_nodes = [
        {
            "type": "node",
            "data": {
                "name": "麦麦解析",
                "uin": api.bot_uin or "10000",
                "content": node,
            },
        }
        for node in nodes
    ]

    if is_private_message(message):
        user_id = get_user_id(message)
        if not user_id:
            logger.error("无法发送私聊合并转发: 缺少 user_id | message: %s", message)
            return False
        url = f"http://{api.host}:{api.port}/send_private_forward_msg"
        payload = {"user_id": int(user_id) if user_id.isdigit() else user_id, "messages": forward_nodes}
    else:
        group_id = get_group_id(message)
        if not group_id:
            logger.error("无法发送群聊合并转发: 缺少 group_id | message: %s", message)
            return False
        url = f"http://{api.host}:{api.port}/send_group_forward_msg"
        payload = {"group_id": int(group_id) if group_id.isdigit() else group_id, "messages": forward_nodes}

    return await _post_onebot(url, payload, api, timeout=120)


def text_segment(text: str) -> MessageSegment:
    return {"type": "text", "data": {"text": text}}


def image_segment(path: Path) -> MessageSegment:
    """构建 OneBot 图片消息段，优先转为 base64 提高跨环境/容器兼容性。"""
    try:
        if path.exists() and path.is_file():
            b64_str = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"type": "image", "data": {"file": f"base64://{b64_str}"}}
    except Exception as e:
        logger.warning("图片转 base64 失败，降级为 file_uri: %s", e)
    return {"type": "image", "data": {"file": _file_uri(path)}}


async def _send_message(
    message: dict[str, Any],
    segments: list[MessageSegment],
    api: ApiSettings,
    *,
    timeout: int,
) -> bool:
    if is_private_message(message):
        user_id = get_user_id(message)
        if not user_id:
            logger.error("无法发送私聊消息: 缺少 user_id | message: %s", message)
            return False
        url = f"http://{api.host}:{api.port}/send_private_msg"
        payload = {"user_id": int(user_id) if user_id.isdigit() else user_id, "message": segments}
    else:
        group_id = get_group_id(message)
        if not group_id:
            logger.error("无法发送群消息: 缺少 group_id | message: %s", message)
            return False
        url = f"http://{api.host}:{api.port}/send_group_msg"
        payload = {"group_id": int(group_id) if group_id.isdigit() else group_id, "message": segments}

    return await _post_onebot(url, payload, api, timeout=timeout)


def _check_onebot_json(response_text: str, url: str, payload: dict[str, Any]) -> bool:
    try:
        data = json.loads(response_text)
        if isinstance(data, dict):
            status = data.get("status")
            retcode = data.get("retcode")
            if status == "failed" or (retcode is not None and retcode != 0):
                msg = data.get("message") or data.get("wording") or data.get("msg") or response_text
                logger.error("OneBot 接口拒绝发送 (retcode=%s): %s | URL: %s", retcode, msg, url)
                return False
            return True
    except Exception:
        pass
    return True


async def _post_onebot(url: str, payload: dict[str, Any], api: ApiSettings, *, timeout: int) -> bool:
    token = str(getattr(api, "token", "") or "").strip()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
                text = await response.text()
                if response.status != 200:
                    if response.status in (401, 403) and token:
                        retry_url = f"{url}?access_token={urllib.parse.quote(token)}"
                        async with session.post(retry_url, json=payload, headers=headers, timeout=timeout) as retry:
                            retry_text = await retry.text()
                            if retry.status == 200:
                                return _check_onebot_json(retry_text, retry_url, payload)
                            logger.error("OneBot 重试失败: HTTP %s %s", retry.status, retry_text)
                            return False
                    logger.error("OneBot 请求失败: HTTP %s %s", response.status, text)
                    return False

                return _check_onebot_json(text, url, payload)
    except asyncio.TimeoutError:
        logger.error("OneBot 请求超时: %s", url)
        return False
    except Exception as exc:
        logger.error("OneBot 请求异常: %s (请检查 config.toml 中 [onebot] 的 host 和 port 是否正确连接到 NapCat/go-cqhttp)", exc)
        return False


def _file_uri(path: Path) -> str:
    value = str(path)
    if value.startswith(("http://", "https://", "file://")):
        return value
    return "file:///" + urllib.request.pathname2url(value).lstrip("/")


def is_private_message(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return True
    m_type = str(message.get("message_type") or message.get("chat_type") or "").strip().lower()
    if m_type == "group":
        return False
    if m_type == "private":
        return True
    return get_group_id(message) is None


_is_private_message = is_private_message


def get_user_id(message: dict[str, Any]) -> str | None:
    if not isinstance(message, dict):
        return None

    # 1. message_info.user_info
    message_info = message.get("message_info")
    if isinstance(message_info, dict):
        user_info = message_info.get("user_info")
        if isinstance(user_info, dict):
            uid = user_info.get("user_id") or user_info.get("userId")
            if uid is not None and str(uid).strip():
                return str(uid).strip()

    # 2. 顶层直接字段
    for key in ("user_id", "userId", "sender_id", "sender_uin", "qq"):
        val = message.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()

    # 3. sender 对象
    sender = message.get("sender")
    if isinstance(sender, dict):
        uid = sender.get("user_id") or sender.get("userId")
        if uid is not None and str(uid).strip():
            return str(uid).strip()

    # 4. session 对象
    session = message.get("session")
    if isinstance(session, dict):
        uid = session.get("user_id") or session.get("userId")
        if uid is not None and str(uid).strip():
            return str(uid).strip()

    return None


_get_user_id = get_user_id


def get_group_id(message: dict[str, Any]) -> str | None:
    if not isinstance(message, dict):
        return None

    # 1. 顶层直接字段
    for key in ("group_id", "groupId", "group_uin", "target_id", "peer_id"):
        val = message.get(key)
        if val is not None and str(val).strip() and str(val).strip() != "0":
            return str(val).strip()

    # 2. message_info.group_info 结构 (MaiBot 原结构)
    message_info = message.get("message_info")
    if isinstance(message_info, dict):
        group_info = message_info.get("group_info")
        if isinstance(group_info, dict):
            gid = group_info.get("group_id") or group_info.get("groupId")
            if gid is not None and str(gid).strip() and str(gid).strip() != "0":
                return str(gid).strip()

    # 3. session / context / chat_info 结构
    for parent_key in ("session", "context", "chat_info"):
        parent = message.get(parent_key)
        if isinstance(parent, dict):
            gid = parent.get("group_id") or parent.get("groupId")
            if gid is not None and str(gid).strip() and str(gid).strip() != "0":
                return str(gid).strip()

    # 4. session_id / chat_id / stream_id (如 "group_123456" 或 "group:123456")
    for key in ("session_id", "chat_id", "stream_id"):
        val = str(message.get(key) or "")
        if val.startswith("group_") or val.startswith("group:"):
            extracted = val.split("_", 1)[-1].split(":", 1)[-1]
            if extracted.isdigit():
                return extracted

    return None


_get_group_id = get_group_id

