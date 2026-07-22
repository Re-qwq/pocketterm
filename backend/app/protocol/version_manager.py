"""PocketTerm 网易我的世界双版本管理器 (3.8 / 3.9)。

本模块为 PocketTerm 提供网易我的世界 3.8 / 3.9 双版本的统一抽象层。
所有版本相关常量 (engine_version / patch_version / RakNet protocol_version /
认证服务器地址 / API 服务器地址 / 联机大厅地址) 都集中在此处管理,
便于在网易升级版本时只修改一处即可全局生效。

设计原则
========

1. **单一数据源 (Single Source of Truth)**: 所有版本元数据来源于
   ``backend/data/version_config.json``。Python 代码不再硬编码版本字符串,
   升级版本时只需修改 JSON 文件。

2. **协议协商 (Protocol Negotiation)**: 参考 Community-Bot 的
   ``[Fail] Incompatible protocol, trying next version...`` 逻辑,
   :meth:`VersionManager.try_negotiate_protocol` 在 RakNet 握手失败时
   按优先级尝试多个候选协议版本。

3. **与既有代码兼容**: 既有 :mod:`app.constants.minecraft` 的
   ``GameVersion`` / ``ProtocolVersion`` 不受影响, 本模块仅作为新增
   的双版本管理层, 不强制既有模块立即接入。

4. **零运行时副作用**: import 本模块不会发起任何网络请求或文件写入,
   仅在第一次访问 :data:`VersionManager` 时惰性加载 JSON 配置。

字段语义说明
============

.. important::

    ``engine_version`` / ``patch_version`` 的语义在用户规格与既有
    PocketTerm 代码之间存在差异, 本模块采用**用户规格**:

    - 用户规格: ``engine_version`` 指 **Bedrock 游戏版本**
      (如 ``"1.21.80"`` / ``"1.21.90"``)。
    - 既有 :mod:`app.auth.netease_direct.constants` 中
      ``ENGINE_VERSION`` 指 **网易启动器版本** (如 ``"3.8.25.293531"``)。

    在本模块中:

    - :attr:`VersionInfo.engine_version` — Bedrock 游戏版本字符串
      (用户规格, 用于 RakNet Login 包的 protocol_version 字段)。
    - :attr:`VersionInfo.patch_version` — 网易启动器版本字符串
      (NEMCTOOLS PEAURequest.patch_version 字段语义)。
    - :attr:`VersionInfo.protocol_version` — RakNet 协议版本
      (整数, Bedrock 1.21.x 全系列使用 ``10``)。
    - :attr:`VersionInfo.min_engine_version` /
      :attr:`VersionInfo.min_patch_version` — 认证服务器返回的最低
      允许版本 (来自 ``AuthenticationResponseEntity`` 字段)。

数据来源
========

- **3.9 引擎版本**: ``1.21.90`` — 经 ``Community_Bot.exe`` strings 验证
  (二进制中硬编码 ``1.21.90``, 该机器人目标版本即网易 3.9)。
- **3.8 引擎版本**: ``1.21.80`` — 用户规格指定 (既有 constants.py 中
  ``3.8 -> 1.21.90`` 与用户规格不一致, 此处以用户规格为准, 待 3.9 正式
  发布后再次校准)。
- **patch_version**: 3.8 / 3.9 均为占位值 (``3.x.0.0``)。3.9 尚未发布
  (预计 2026-07-24), 发布后需更新 :file:`version_config.json`。
- **RakNet protocol_version**: ``10`` — 来源
  :mod:`app.protocol.raknet` ``DEFAULT_PROTOCOL_VERSION``。
- **认证服务器**: 用户模板使用 ``https://g79authobt.minecraft.cn``;
  既有 :mod:`app.auth.netease_direct.constants` 与 Community_Bot.exe
  均使用 ``https://g79authobt.nie.netease.com`` (两域名解析同一服务)。
  本模块保留两份, 通过 :attr:`VersionInfo.auth_server` /
  :attr:`VersionInfo.auth_server_alt` 暴露。

典型用法
========

::

    from app.protocol.version_manager import (
        VersionManager, MinecraftVersion, VersionInfo,
    )

    # 1. 获取默认版本 (3.8)
    default = VersionManager.get_default()
    info = VersionManager.get_version_info(default)
    print(info.engine_version, info.protocol_version)

    # 2. 解析用户输入的版本字符串
    v = VersionManager.parse_version_string("3.9")
    assert v is MinecraftVersion.V3_9

    # 3. RakNet 握手失败时尝试协商
    negotiated = VersionManager.try_negotiate_protocol([10, 11, 9])
    print(negotiated.version, negotiated.protocol_version)

逆向来源
========

- ``Community_Bot.exe`` (用户上传) — strings 分析:
  ``1.21.90`` / ``GetRakNetProtocolVersion`` / ``SetRakNetProtocolVersion``
  / ``[Fail] Incompatible protocol, trying next version...`` /
  ``min_engine_version`` / ``min_patch_version``。
- NEMCTOOLS ``查UID源码(1.3.8)`` — ``PEAURequest.cs`` /
  ``AuthenticationResponseEntity.cs`` / ``Http.cs``。
- 既有 PocketTerm ``app/auth/netease_direct/constants.py`` /
  ``app/protocol/raknet.py`` / ``app/constants/minecraft.py``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# 路径与日志
# ---------------------------------------------------------------------------
# 本文件位于:  <PROJECT_ROOT>/backend/app/protocol/version_manager.py
# 因此:
#   _THIS_DIR    = backend/app/protocol/
#   _APP_DIR     = backend/app/
#   _BACKEND_DIR = backend/
_THIS_DIR = Path(__file__).resolve().parent
_APP_DIR = _THIS_DIR.parent
_BACKEND_DIR = _APP_DIR.parent

#: 版本配置文件路径: ``backend/data/version_config.json``。
VERSION_CONFIG_FILE: Path = _BACKEND_DIR / "data" / "version_config.json"

#: Logger 命名空间 (用户指定)。
#:
#: 通过 ``logging.getLogger("pocketterm.protocol.version_manager")`` 获取,
#: 所有日志都汇入 PocketTerm 主 logger 的 handler。
_LOGGER_NAME: str = "pocketterm.protocol.version_manager"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ---------------------------------------------------------------------------
# 枚举与数据类
# ---------------------------------------------------------------------------
class MinecraftVersion(Enum):
    """网易我的世界 (Bedrock 中国版) 主版本枚举。

    每个枚举成员的 ``value`` 是用户可见的版本字符串 (如 ``"3.8"``)，
    与 :file:`version_config.json` 中的 ``versions`` 字典 key 对应。
    """

    V3_8 = "3.8"
    """网易我的世界 3.8 (Bedrock 1.21.80 系列)。"""

    V3_9 = "3.9"
    """网易我的世界 3.9 (Bedrock 1.21.90 系列, Community-Bot 目标版本)。"""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class VersionInfo:
    """单个版本的完整元数据 (不可变)。

    使用 ``frozen=True`` 以保证版本信息在运行时不被意外篡改;
    如需切换版本应重新调用 :meth:`VersionManager.get_version_info`。
    """

    version: MinecraftVersion
    """该 :class:`VersionInfo` 对应的枚举成员。"""

    engine_version: str
    """Bedrock 游戏版本字符串 (用户规格)。

    例如 ``"1.21.80"`` (3.8) / ``"1.21.90"`` (3.9)。
    用于 RakNet Login 包的 protocol_version 字段 (字符串形式)。
    """

    patch_version: str
    """网易启动器版本字符串 (NEMCTOOLS 语义)。

    例如 ``"3.8.0.0"`` / ``"3.9.0.0"`` (当前为占位值)。
    用于 ``PEAURequest.patch_version`` 字段。
    """

    protocol_version: int
    """RakNet 协议版本 (整数)。

    Bedrock 1.21.x 全系列使用 ``10``
    (来源: :mod:`app.protocol.raknet` ``DEFAULT_PROTOCOL_VERSION``)。
    """

    min_engine_version: str
    """服务器要求的最小 Bedrock 引擎版本 (来自认证响应)。

    对应 :class:`AuthenticationResponseEntity.min_engine_version` 字段。
    """

    min_patch_version: str
    """服务器要求的最小网易补丁版本 (来自认证响应)。

    对应 :class:`AuthenticationResponseEntity.min_patch_version` 字段。
    """

    auth_server: str
    """认证服务器地址 (chainInfo 获取)。

    例如 ``"https://g79authobt.minecraft.cn"``。
    """

    auth_server_alt: str = ""
    """认证服务器备用地址 (同服务不同域名)。

    例如 ``"https://g79authobt.nie.netease.com"``。
    Community_Bot.exe strings 与既有 constants.py 均使用此域名。
    """

    api_server: str = ""
    """API 网关 (PE/g79 路径)。

    例如 ``"https://g79apigatewayobt.minecraft.cn"``。
    """

    lobby_server: str = ""
    """联机大厅服务器地址。

    例如 ``"https://g79mclobt.minecraft.cn"``。
    """

    replaceitem_limited: bool = True
    """``replaceitem`` 命令是否受限。

    网易 3.8 阉割了 ``replaceitem`` (只能放耐久/特殊值/数量/NBT 标签,
    不能放附魔/自定义名字), 因此默认 ``True``。3.9 可能恢复完整能力
    (待 3.9 发布后实测确认)。来源: :mod:`app.protocol.nbt_placer`。
    """

    default_structure_mode: str = "STRUCTURE"
    """NBT 放置默认模式 (``STRUCTURE`` / ``REPLACEITEM``)。

    3.8 默认 ``STRUCTURE`` (因 replaceitem 受限)。
    """

    max_command_block_rate: int = 20
    """命令方块速率上限 (次/秒, 来自实测)。

    3.8 默认 20, 3.9 可能放宽到 30 (待实测)。
    """

    chunk_size: int = 16
    """区块边长 (方块数)。

    Bedrock 全版本统一为 16。
    """


# ---------------------------------------------------------------------------
# VersionManager
# ---------------------------------------------------------------------------
class VersionManager:
    """网易 3.8 / 3.9 双版本管理器。

    所有方法均为 ``@staticmethod``, 不需要实例化即可使用。
    版本元数据在首次访问时惰性加载自 :data:`VERSION_CONFIG_FILE`,
    后续访问走内存缓存 (除非调用 :meth:`reload`)。

    升级指南 (网易发布新版本时)
    ---------------------------
    1. 修改 :file:`backend/data/version_config.json` 中对应版本的
       ``engine_version`` / ``patch_version`` / ``protocol_version``。
    2. 调用 :meth:`VersionManager.reload` 或重启 PocketTerm。
    3. 运行验证测试 (见模块文档底部)。
    """

    # 静态缓存 (惰性加载)
    _cache: Dict[str, Any] = {}
    _loaded: bool = False

    # -- 内部: 加载 JSON 配置 ----------------------------------------------
    @classmethod
    def _load_config(cls) -> Dict[str, Any]:
        """读取并缓存 :data:`VERSION_CONFIG_FILE`。

        若文件不存在或解析失败, 回退到 :data:`_BUILTIN_DEFAULTS`
        (硬编码兜底值), 并打印警告到日志。
        """
        if cls._loaded:
            return cls._cache
        try:
            with open(VERSION_CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("version_config.json 顶层不是 JSON 对象")
            cls._cache = data
            cls._loaded = True
            logger.debug(
                "已加载版本配置 %s (默认版本=%s, 版本数=%d)",
                VERSION_CONFIG_FILE,
                data.get("default_version", "?"),
                len(data.get("versions", {})),
            )
            return cls._cache
        except FileNotFoundError:
            logger.warning(
                "版本配置文件不存在: %s, 使用内置默认值",
                VERSION_CONFIG_FILE,
            )
            cls._cache = _BUILTIN_DEFAULTS
            cls._loaded = True
            return cls._cache
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "版本配置文件解析失败: %s (%s), 使用内置默认值",
                VERSION_CONFIG_FILE,
                exc,
            )
            cls._cache = _BUILTIN_DEFAULTS
            cls._loaded = True
            return cls._cache

    @classmethod
    def reload(cls) -> Dict[str, Any]:
        """强制重新加载 :data:`VERSION_CONFIG_FILE`。

        适用于用户在运行时修改了 JSON 配置后希望立即生效的场景。
        """
        cls._loaded = False
        cls._cache = {}
        return cls._load_config()

    # -- 内部: 从 JSON 字典构造 VersionInfo --------------------------------
    @staticmethod
    def _build_version_info(
        version: MinecraftVersion,
        raw: Dict[str, Any],
    ) -> VersionInfo:
        """从 JSON 字典构造 :class:`VersionInfo`。

        缺失字段使用 :class:`VersionInfo` 的默认值 (dataclass 默认)。
        """
        return VersionInfo(
            version=version,
            engine_version=str(raw.get("engine_version", "")),
            patch_version=str(raw.get("patch_version", "")),
            protocol_version=int(raw.get("protocol_version", 10)),
            min_engine_version=str(raw.get("min_engine_version", "")),
            min_patch_version=str(raw.get("min_patch_version", "")),
            auth_server=str(raw.get("auth_server", "")),
            auth_server_alt=str(raw.get("auth_server_alt", "")),
            api_server=str(raw.get("api_server", "")),
            lobby_server=str(raw.get("lobby_server", "")),
            replaceitem_limited=bool(raw.get("replaceitem_limited", True)),
            default_structure_mode=str(
                raw.get("default_structure_mode", "STRUCTURE")
            ).upper(),
            max_command_block_rate=int(raw.get("max_command_block_rate", 20)),
            chunk_size=int(raw.get("chunk_size", 16)),
        )

    # -- 公开 API ----------------------------------------------------------
    @staticmethod
    def get_version_info(version: MinecraftVersion) -> VersionInfo:
        """返回指定版本的完整 :class:`VersionInfo`。

        Parameters
        ----------
        version:
            目标版本枚举 (``MinecraftVersion.V3_8`` / ``V3_9``)。

        Returns
        -------
        VersionInfo
            该版本的元数据。若配置文件中缺失该版本, 回退到
            :data:`_BUILTIN_DEFAULTS["versions"][version.value]`。
        """
        data = VersionManager._load_config()
        versions = data.get("versions", {})
        raw = versions.get(version.value)
        if raw is None:
            # 回退到内置默认值
            builtin = _BUILTIN_DEFAULTS.get("versions", {}).get(version.value)
            if builtin is None:
                raise KeyError(
                    f"版本 {version.value} 既不在配置文件中也不在内置默认值中"
                )
            logger.warning(
                "版本 %s 不在配置文件中, 使用内置默认值",
                version.value,
            )
            raw = builtin
        return VersionManager._build_version_info(version, raw)

    @staticmethod
    def get_all_versions() -> List[VersionInfo]:
        """返回所有已注册版本的 :class:`VersionInfo` 列表。

        列表按枚举定义顺序返回 (3.8 在前, 3.9 在后)。
        """
        return [VersionManager.get_version_info(v) for v in MinecraftVersion]

    @staticmethod
    def parse_version_string(s: Union[str, MinecraftVersion, None]) -> Optional[MinecraftVersion]:
        """将用户输入的字符串解析为 :class:`MinecraftVersion`。

        支持以下格式 (大小写不敏感, 去除空白):

        - ``"3.8"`` / ``"3.8.0"`` / ``"v3.8"`` → :data:`MinecraftVersion.V3_8`
        - ``"3.9"`` / ``"3.9.0"`` / ``"v3.9"`` → :data:`MinecraftVersion.V3_9`
        - 直接传入 :class:`MinecraftVersion` 实例时原样返回
        - ``None`` 或无法识别时返回 ``None``
        """
        if s is None:
            return None
        if isinstance(s, MinecraftVersion):
            return s
        if not isinstance(s, str):
            return None
        s_norm = s.strip().lower().lstrip("v")
        # 取主版本前缀: "3.8.25.293531" -> "3.8"
        parts = s_norm.split(".")
        if len(parts) < 2:
            return None
        major_minor = f"{parts[0]}.{parts[1]}"
        for v in MinecraftVersion:
            if v.value == major_minor:
                return v
        return None

    @staticmethod
    def get_default() -> MinecraftVersion:
        """返回默认版本 (默认 3.8)。

        优先读取 :file:`version_config.json` 的 ``default_version`` 字段;
        若缺失或无法识别, 回退到 :data:`MinecraftVersion.V3_8`。
        """
        data = VersionManager._load_config()
        default_str = data.get("default_version", "3.8")
        v = VersionManager.parse_version_string(default_str)
        if v is None:
            logger.warning(
                "default_version 配置项无法识别: %r, 回退到 3.8",
                default_str,
            )
            return MinecraftVersion.V3_8
        return v

    @staticmethod
    def try_negotiate_protocol(supported: List[int]) -> VersionInfo:
        """根据 RakNet 服务器返回的协议版本列表协商最合适的版本。

        参考 Community-Bot 的 ``[Fail] Incompatible protocol, trying next
        version...`` 逻辑: RakNet 握手失败时服务器可能返回它支持的多个
        协议版本, 本方法在 PocketTerm 已注册版本中找出第一个匹配的。

        Parameters
        ----------
        supported:
            服务器声明支持的 RakNet 协议版本列表 (如 ``[10, 11, 9]``)。
            列表顺序通常即服务器优先级, 本方法按列表顺序尝试。

        Returns
        -------
        VersionInfo
            第一个 ``protocol_version`` 在 ``supported`` 中的版本信息。

        Raises
        ------
        ValueError
            所有已注册版本的 ``protocol_version`` 都不在 ``supported`` 中
            (对应 Community-Bot 的 ``[Error] Failed to connect, all
            protocol versions tried.``)。
        """
        if not supported:
            # 服务器未返回协议版本列表 -> 返回默认版本
            logger.info(
                "服务器未返回协议版本列表, 使用默认版本 %s",
                VersionManager.get_default(),
            )
            return VersionManager.get_version_info(VersionManager.get_default())

        all_versions = VersionManager.get_all_versions()
        for proto in supported:
            for info in all_versions:
                if info.protocol_version == proto:
                    logger.info(
                        "协议协商成功: 服务器协议=%d, 选中版本=%s (engine=%s)",
                        proto,
                        info.version,
                        info.engine_version,
                    )
                    return info

        # 所有协议版本均不匹配
        tried_protos = sorted({v.protocol_version for v in all_versions})
        raise ValueError(
            f"协议协商失败: 服务器支持 {supported}, "
            f"PocketTerm 已注册 {tried_protos} "
            f"(对应 Community-Bot '[Error] Failed to connect, "
            f"all protocol versions tried.')"
        )


# ---------------------------------------------------------------------------
# 内置默认值 (配置文件丢失时兜底)
# ---------------------------------------------------------------------------
#: 当 :file:`version_config.json` 缺失或损坏时的内置默认配置。
#:
#: 此处的值与 :file:`version_config.json` 内容保持一致, 仅作为兜底,
#: 不应作为版本升级时的修改入口 (应修改 JSON 文件)。
_BUILTIN_DEFAULTS: Dict[str, Any] = {
    "default_version": "3.8",
    "versions": {
        "3.8": {
            "engine_version": "1.21.80",
            "patch_version": "3.8.0.0",
            "protocol_version": 10,
            "min_engine_version": "1.21.80",
            "min_patch_version": "3.8.0.0",
            "auth_server": "https://g79authobt.minecraft.cn",
            "auth_server_alt": "https://g79authobt.nie.netease.com",
            "api_server": "https://g79apigatewayobt.minecraft.cn",
            "lobby_server": "https://g79mclobt.minecraft.cn",
            "replaceitem_limited": True,
            "default_structure_mode": "STRUCTURE",
            "max_command_block_rate": 20,
            "chunk_size": 16,
        },
        "3.9": {
            "engine_version": "1.21.90",
            "patch_version": "3.9.0.0",
            "protocol_version": 10,
            "min_engine_version": "1.21.90",
            "min_patch_version": "3.9.0.0",
            "auth_server": "https://g79authobt.minecraft.cn",
            "auth_server_alt": "https://g79authobt.nie.netease.com",
            "api_server": "https://g79apigatewayobt.minecraft.cn",
            "lobby_server": "https://g79mclobt.minecraft.cn",
            "replaceitem_limited": False,
            "default_structure_mode": "STRUCTURE",
            "max_command_block_rate": 30,
            "chunk_size": 16,
        },
    },
}


# ---------------------------------------------------------------------------
# 兼容既有代码: 暴露 3.8 / 3.9 的 VersionInfo 常量
# ---------------------------------------------------------------------------
#: 3.8 版本信息常量 (等价于 ``VersionManager.get_version_info(V3_8)``)。
#:
#: .. note::
#:     此常量在模块 import 时即计算, 若用户修改了 :file:`version_config.json`
#:     并希望立即生效, 应调用 :meth:`VersionManager.reload` 后重新取值
#:     (而非使用此常量)。
V3_8_INFO: VersionInfo = VersionManager.get_version_info(MinecraftVersion.V3_8)

#: 3.9 版本信息常量 (等价于 ``VersionManager.get_version_info(V3_9)``)。
V3_9_INFO: VersionInfo = VersionManager.get_version_info(MinecraftVersion.V3_9)


__all__ = [
    "MinecraftVersion",
    "VersionInfo",
    "VersionManager",
    "V3_8_INFO",
    "V3_9_INFO",
    "VERSION_CONFIG_FILE",
]
