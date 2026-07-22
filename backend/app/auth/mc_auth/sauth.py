"""sauth_json 构造模块。

``sauth_json`` 是网易 Minecraft 客户端登录时携带的设备/会话身份描述,
由设备指纹与会话上下文拼接而成,再被外层登录请求封装为 ``FBToken`` 字段。
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from .device import DeviceFingerprint
from .exceptions import InvalidSauthJsonError


# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
DEFAULT_PROTOCOL_VERSION: int = 685
DEFAULT_GAME_VERSION: str = "1.21.93"


def build_sessionid(device_id: str) -> str:
    """根据 device_id 生成 sessionid(随机 32 字符十六进制)。

    sessionid 用于在单次登录流程中关联设备与服务器会话。
    """
    return secrets.token_hex(16)  # 32 个 hex 字符


@dataclass
class SauthSession:
    """sauth_json 构造所需的运行时上下文。

    Attributes:
        fingerprint: 设备指纹
        sessionid: 会话 ID
        protocol_version: 协议版本(默认 685)
    """
    fingerprint: DeviceFingerprint
    sessionid: str
    protocol_version: int = DEFAULT_PROTOCOL_VERSION

    @classmethod
    def from_fingerprint(
        cls,
        fp: DeviceFingerprint,
        *,
        protocol_version: int = DEFAULT_PROTOCOL_VERSION,
        sessionid: str | None = None,
    ) -> "SauthSession":
        """从设备指纹构造会话上下文。

        Args:
            fp: 设备指纹
            protocol_version: 协议版本,默认 685
            sessionid: 可选的会话 ID;未指定则自动生成
        """
        return cls(
            fingerprint=fp,
            sessionid=sessionid or build_sessionid(fp.device_id),
            protocol_version=protocol_version,
        )


def build_sauth_json(session: SauthSession) -> str:
    """构造 sauth_json 字符串。

    将设备指纹与会话信息序列化为紧凑 JSON。字段包括:
    ``device_id`` / ``device_model`` / ``platform`` / ``game_version`` /
    ``client_version`` / ``protocol_version`` / ``sessionid``。

    Raises:
        InvalidSauthJsonError: 设备指纹缺少必要字段时抛出。
    """
    fp = session.fingerprint

    # 字段合法性检查
    required_fields = ("device_id", "device_model", "platform", "game_version", "client_version")
    for field_name in required_fields:
        value = getattr(fp, field_name, None)
        if not value:
            raise InvalidSauthJsonError(f"字段 {field_name} 不能为空")
        if not isinstance(value, str):
            raise InvalidSauthJsonError(f"字段 {field_name} 必须为字符串")

    if not session.sessionid:
        raise InvalidSauthJsonError("字段 sessionid 不能为空")

    payload: dict[str, Any] = {
        "device_id": fp.device_id,
        "device_model": fp.device_model,
        "platform": fp.platform,
        "game_version": fp.game_version,
        "client_version": fp.client_version,
        "protocol_version": session.protocol_version,
        "sessionid": session.sessionid,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_sauth_json(raw: str) -> dict[str, Any]:
    """解析 sauth_json 字符串并做关键字段校验。

    Raises:
        InvalidSauthJsonError: JSON 解析失败或缺少关键字段时抛出。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidSauthJsonError(f"JSON 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise InvalidSauthJsonError("sauth_json 顶层不是对象")

    for key in ("device_id", "platform", "game_version", "sessionid"):
        if key not in data:
            raise InvalidSauthJsonError(f"sauth_json 缺少关键字段: {key}")
    return data
