"""设备指纹模块 - 生成伪造的网易 Minecraft 客户端设备身份。

``DeviceFingerprint`` 仅保留与认证协议相关的核心字段,便于序列化与跨模块传递。
"""
from __future__ import annotations

import random
import secrets
import string
from dataclasses import dataclass, asdict
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# 默认常量
# ---------------------------------------------------------------------------
DEFAULT_PLATFORM: str = "android"
DEFAULT_GAME_VERSION: str = "1.21.93"
DEFAULT_CLIENT_VERSION: str = "1.0.0"

# device_id 字符集(小写字母 + 数字)
_DEVICE_ID_ALPHABET = string.ascii_lowercase + string.digits

# 常见 Android 设备型号,用于随机选取
ANDROID_MODELS = [
    "Xiaomi 13", "Xiaomi 14", "Xiaomi 14 Pro",
    "HUAWEI Mate 60 Pro", "HUAWEI P60 Pro",
    "OPPO Find X7", "vivo X100 Pro",
    "Samsung Galaxy S24", "Samsung Galaxy S24 Ultra",
    "OnePlus 12", "iQOO 12 Pro",
    "Redmi K70 Pro", "realme GT5 Pro",
]

# 设备 ID 默认长度
_DEVICE_ID_LENGTH = 32


def _random_device_id(length: int = _DEVICE_ID_LENGTH) -> str:
    """生成随机 device_id(小写字母 + 数字)。"""
    return "".join(secrets.choice(_DEVICE_ID_ALPHABET) for _ in range(length))


@dataclass
class DeviceFingerprint:
    """设备指纹信息。

    Attributes:
        device_id: 设备唯一标识(随机生成)
        device_model: 设备型号(如 "Xiaomi 13")
        platform: 平台(如 "android")
        game_version: 游戏版本(如 "1.21.93")
        client_version: 客户端版本(如 "1.0.0")
    """
    device_id: str
    device_model: str = ANDROID_MODELS[0]
    platform: str = DEFAULT_PLATFORM
    game_version: str = DEFAULT_GAME_VERSION
    client_version: str = DEFAULT_CLIENT_VERSION

    @classmethod
    def generate(
        cls,
        *,
        device_model: Optional[str] = None,
        platform: str = DEFAULT_PLATFORM,
        game_version: str = DEFAULT_GAME_VERSION,
        client_version: str = DEFAULT_CLIENT_VERSION,
        rng: Optional[random.Random] = None,
    ) -> "DeviceFingerprint":
        """生成全新的随机设备指纹。

        Args:
            device_model: 指定设备型号;为 ``None`` 时随机选取
            platform: 平台,默认 ``android``
            game_version: 游戏版本,默认 ``1.21.93``
            client_version: 客户端版本,默认 ``1.0.0``
            rng: 可选的随机数生成器(用于测试可复现)
        """
        if device_model is None:
            device_model = (rng or secrets).choice(ANDROID_MODELS)

        if rng is None:
            device_id = _random_device_id()
        else:
            device_id = "".join(rng.choice(_DEVICE_ID_ALPHABET) for _ in range(_DEVICE_ID_LENGTH))

        return cls(
            device_id=device_id,
            device_model=device_model,
            platform=platform,
            game_version=game_version,
            client_version=client_version,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeviceFingerprint":
        """从字典反序列化(忽略未知字段)。"""
        kwargs: dict[str, Any] = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                kwargs[f] = data[f]
        return cls(**kwargs)

    def short_summary(self) -> str:
        """返回简短描述,便于日志输出。"""
        head = self.device_id[:4] if len(self.device_id) >= 4 else self.device_id
        tail = self.device_id[-4:] if len(self.device_id) >= 4 else ""
        return (
            f"Model={self.device_model} "
            f"Platform={self.platform} "
            f"GameVer={self.game_version} "
            f"DevId={head}..{tail}"
        )
