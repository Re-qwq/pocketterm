"""版本自适应配置读取器。

从 ``version_config.json`` 读取版本相关参数, 实现 3.8/3.9 双版本自适应。
当网易升级版本时, 只需修改 JSON 文件, 无需改动 Python 源码。

配置文件路径: ``backend/data/version_config.json``

核心字段:
  - ``engine_version``: 基岩版版本号 (如 "1.21.120")
  - ``patch_version``: 网易引擎版本号 (如 "3.9.0")
  - ``protocol_version``: RakNet 协议版本 (如 10)
  - ``sdk_version``: SDK 版本号 (sauth_json.sdk_version 字段)
  - ``sdk_version_pe``: PE SDK 版本号
  - ``gameid``: 游戏 ID (如 "x19")
  - ``platform``: 平台标识 (如 "pc")
  - ``auth_server_pc``: PC 认证服务器 URL
  - ``api_server_pc``: PC API 网关 URL
  - ``launcher_version``: PC 启动器版本号
  - ``replaceitem_limited``: 是否限制 replaceitem 命令
  - ``max_command_block_rate``: 最大命令方块速率

用法:
  ::

      from app.protocol.version_adaptive import VersionConfig

      # 获取默认版本 (3.9) 配置
      config = VersionConfig.get_version("3.9")
      print(config.sdk_version)       # "3.9.0"
      print(config.engine_version)    # "1.21.120"
      print(config.auth_server_pc)    # "https://x19obtcore.nie.netease.com:8443"

      # 获取所有可用版本
      versions = VersionConfig.list_versions()  # ["3.8", "3.9"]

      # 获取默认版本
      default = VersionConfig.get_default_version()  # "3.9"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pocketterm.version_adaptive")

# 配置文件路径
_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "data" / "version_config.json"


@dataclass
class VersionInfo:
    """单个版本的配置信息。"""
    version: str
    engine_version: str
    patch_version: str
    protocol_version: int
    min_engine_version: str
    min_patch_version: str
    auth_server: str
    auth_server_alt: str
    api_server: str
    lobby_server: str
    replaceitem_limited: bool
    default_structure_mode: str
    max_command_block_rate: int
    chunk_size: int
    auth_server_pc: str
    auth_server_pe: str
    api_server_pc: str
    patch_list_url: str
    sdk_version: str
    sdk_version_pe: str
    gameid: str
    platform: str
    launcher_version: str
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "version": self.version,
            "engine_version": self.engine_version,
            "patch_version": self.patch_version,
            "protocol_version": self.protocol_version,
            "min_engine_version": self.min_engine_version,
            "min_patch_version": self.min_patch_version,
            "auth_server": self.auth_server,
            "auth_server_alt": self.auth_server_alt,
            "api_server": self.api_server,
            "lobby_server": self.lobby_server,
            "replaceitem_limited": self.replaceitem_limited,
            "default_structure_mode": self.default_structure_mode,
            "max_command_block_rate": self.max_command_block_rate,
            "chunk_size": self.chunk_size,
            "auth_server_pc": self.auth_server_pc,
            "auth_server_pe": self.auth_server_pe,
            "api_server_pc": self.api_server_pc,
            "patch_list_url": self.patch_list_url,
            "sdk_version": self.sdk_version,
            "sdk_version_pe": self.sdk_version_pe,
            "gameid": self.gameid,
            "platform": self.platform,
            "launcher_version": self.launcher_version,
            "notes": self.notes,
        }


class VersionConfig:
    """版本配置管理器 (单例, 惰性加载)。"""

    _config: Optional[Dict[str, Any]] = None
    _loaded_at: float = 0.0

    # 缓存时间 (秒)
    _CACHE_TTL = 60.0

    @classmethod
    def _load_config(cls) -> Dict[str, Any]:
        """加载并缓存 version_config.json。"""
        import time

        now = time.time()
        if cls._config is not None and (now - cls._loaded_at) < cls._CACHE_TTL:
            return cls._config

        try:
            if not _CONFIG_PATH.exists():
                logger.warning("version_config.json 不存在: %s", _CONFIG_PATH)
                return {}

            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            cls._config = data
            cls._loaded_at = now
            return data

        except json.JSONDecodeError as e:
            logger.error("version_config.json JSON 解析失败: %s", e)
            return {}
        except Exception as e:
            logger.error("加载 version_config.json 失败: %s", e)
            return {}

    @classmethod
    def get_version(cls, version: str) -> Optional[VersionInfo]:
        """获取指定版本的配置信息。

        Args:
            version: 版本号字符串 (如 "3.8" 或 "3.9")。

        Returns:
            VersionInfo 对象, 或 None (版本不存在时)。
        """
        data = cls._load_config()
        versions = data.get("versions", {})

        if version not in versions:
            logger.warning("版本 %s 不存在于配置文件中", version)
            return None

        v = versions[version]
        return VersionInfo(
            version=version,
            engine_version=v.get("engine_version", ""),
            patch_version=v.get("patch_version", ""),
            protocol_version=v.get("protocol_version", 10),
            min_engine_version=v.get("min_engine_version", ""),
            min_patch_version=v.get("min_patch_version", ""),
            auth_server=v.get("auth_server", ""),
            auth_server_alt=v.get("auth_server_alt", ""),
            api_server=v.get("api_server", ""),
            lobby_server=v.get("lobby_server", ""),
            replaceitem_limited=v.get("replaceitem_limited", True),
            default_structure_mode=v.get("default_structure_mode", "STRUCTURE"),
            max_command_block_rate=v.get("max_command_block_rate", 20),
            chunk_size=v.get("chunk_size", 16),
            auth_server_pc=v.get("auth_server_pc", ""),
            auth_server_pe=v.get("auth_server_pe", ""),
            api_server_pc=v.get("api_server_pc", ""),
            patch_list_url=v.get("patch_list_url", ""),
            sdk_version=v.get("sdk_version", ""),
            sdk_version_pe=v.get("sdk_version_pe", ""),
            gameid=v.get("gameid", "x19"),
            platform=v.get("platform", "pc"),
            launcher_version=v.get("launcher_version", ""),
            notes=v.get("_notes", ""),
        )

    @classmethod
    def get_default_version(cls) -> str:
        """获取默认版本号。"""
        data = cls._load_config()
        return data.get("default_version", "3.9")

    @classmethod
    def get_default(cls) -> Optional[VersionInfo]:
        """获取默认版本的配置信息。"""
        return cls.get_version(cls.get_default_version())

    @classmethod
    def list_versions(cls) -> List[str]:
        """列出所有可用版本。"""
        data = cls._load_config()
        versions = data.get("versions", {})
        return sorted(versions.keys())

    @classmethod
    def get_endpoint(cls, version: str, endpoint_name: str) -> Optional[str]:
        """获取指定版本的某个 Community-Bot 端点。

        Args:
            version: 版本号 (如 "3.9")。
            endpoint_name: 端点名 (如 "login_otp", "authentication_otp")。

        Returns:
            端点 URL, 或 None。
        """
        data = cls._load_config()
        endpoints = data.get("_community_bot_endpoints", {})
        pc_endpoints = endpoints.get("pc", {})
        return pc_endpoints.get(endpoint_name)

    @classmethod
    def reload(cls) -> None:
        """强制重新加载配置文件。"""
        cls._config = None
        cls._loaded_at = 0.0
        cls._load_config()


__all__ = [
    "VersionConfig",
    "VersionInfo",
]
