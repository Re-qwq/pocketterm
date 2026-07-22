"""PocketTerm 协议适配器 — 根据版本自动适配协议差异。

本模块提供 :class:`ProtocolAdapter`, 在 :class:`~app.protocol.version_manager.MinecraftVersion`
之上封装一层「面向业务」的访问接口。它的职责是:

1. **隔离版本差异**: 上层业务代码 (connection.py / nbt_placer.py /
   rate_limiter.py 等) 不需要直接 if-else 3.8/3.9, 只需调用
   :class:`ProtocolAdapter` 的方法即可获取适配后的值。
2. **支持运行时切换**: 持有一个可变 ``_raknet_protocol_version``
   状态, 通过 :meth:`set_raknet_protocol_version` 在运行时切换
   RakNet 协议版本 (对应 Community-Bot 的
   ``SetRakNetProtocolVersion`` 函数)。
3. **暴露服务器地址**: 集中暴露 auth/api/lobby 三个服务器地址,
   方便上层 HTTP 客户端构造请求。

设计模式
========

- **Adapter / Facade**: :class:`ProtocolAdapter` 是
  :class:`~app.protocol.version_manager.VersionInfo` 的 facade,
  提供更细粒度的 getter。
- **Stateful**: 实例化后允许通过 setter 修改 ``raknet_protocol_version``
  (用于运行时协商), 但版本本身 (3.8/3.9) 不可变 — 需要切换版本应
  重新构造 adapter。

典型用法
========

::

    from app.protocol.protocol_adapter import ProtocolAdapter
    from app.protocol.version_manager import MinecraftVersion

    # 1. 用默认版本 (3.8) 构造 adapter
    adapter = ProtocolAdapter(MinecraftVersion.V3_8)
    print(adapter.get_engine_version())           # "1.21.80"
    print(adapter.get_raknet_protocol_version())   # 10
    print(adapter.get_auth_endpoint())             # "https://g79authobt.minecraft.cn"

    # 2. RakNet 握手失败时尝试切换协议版本 (对应 Community-Bot 的
    #    [Fail] Incompatible protocol, trying next version... 逻辑)
    adapter.set_raknet_protocol_version(11)

    # 3. 业务侧判断 replaceitem 是否可用
    if adapter.supports_replaceitem():
        ...  # 走 replaceitem 路径
    else:
        ...  # 走 STRUCTURE 平台模式 (网易 3.8 推荐)

依赖关系
========

- :mod:`app.protocol.version_manager` (必需): 提供版本元数据。
- :mod:`app.logger` (可选): 仅用于日志, 缺失时回退到标准 logging。

.. note::

    本模块严格遵守「不要修改已有文件」约束, 仅新增文件, 不修改
    :mod:`app.protocol.__init__` / :mod:`app.protocol.connection` 等
    既有模块。上层模块可按需自行调用本 adapter。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.protocol.version_manager import (
    MinecraftVersion,
    VersionInfo,
    VersionManager,
)

# ---------------------------------------------------------------------------
# Logger (用户指定命名空间)
# ---------------------------------------------------------------------------
_LOGGER_NAME: str = "pocketterm.protocol.protocol_adapter"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ---------------------------------------------------------------------------
# 结构方块模式枚举 (字符串常量, 不用 IntEnum 以便直接透传给上层)
# ---------------------------------------------------------------------------
#: STRUCTURE 模式 — NovaBuilder/NexusE 方案, 11x11 海晶灯平台 + structure save/load。
#: 网易 3.8 推荐方案 (replaceitem 阉割后)。
STRUCTURE_MODE: str = "STRUCTURE"

#: REPLACEITEM 模式 — PhoenixBuilder 方案, 直接 replaceitem + 数据包写入。
#: 网易 3.8 受限模式 (无法放附魔/自定义名字), 3.9 可能恢复完整能力。
REPLACEITEM_MODE: str = "REPLACEITEM"


class ProtocolAdapter:
    """根据版本自动适配协议差异的 facade。

    Parameters
    ----------
    version:
        目标版本枚举。若为 ``None``, 使用
        :meth:`~app.protocol.version_manager.VersionManager.get_default`
        返回的默认版本 (3.8)。
    version_info:
        可选的 :class:`VersionInfo` 实例。若提供, 直接使用该实例而不
        再向 :class:`VersionManager` 查询 (便于单元测试注入 mock 数据)。

    Attributes
    ----------
    version : MinecraftVersion
        构造时确定的版本 (不可变)。
    info : VersionInfo
        构造时确定的版本元数据快照 (不可变)。
        若需要热加载新配置, 应重新构造 adapter。
    """

    def __init__(
        self,
        version: Optional[MinecraftVersion] = None,
        version_info: Optional[VersionInfo] = None,
    ) -> None:
        if version_info is not None:
            self.info: VersionInfo = version_info
            self.version: MinecraftVersion = version_info.version
        else:
            self.version = version if version is not None else VersionManager.get_default()
            self.info = VersionManager.get_version_info(self.version)

        # 运行时可变状态 (对应 Community-Bot 的 SetRakNetProtocolVersion)
        self._raknet_protocol_version: int = self.info.protocol_version

        logger.debug(
            "ProtocolAdapter(version=%s, engine=%s, protocol=%d)",
            self.version,
            self.info.engine_version,
            self._raknet_protocol_version,
        )

    # ------------------------------------------------------------------
    # RakNet 协议版本 (运行时可变)
    # ------------------------------------------------------------------
    def get_raknet_protocol_version(self) -> int:
        """返回当前 RakNet 协议版本。

        对应 Community-Bot 的 ``GetRakNetProtocolVersion`` 函数。
        初值等于 :attr:`VersionInfo.protocol_version`, 但可通过
        :meth:`set_raknet_protocol_version` 在运行时修改
        (用于 RakNet 握手失败时的协议版本协商)。
        """
        return self._raknet_protocol_version

    def set_raknet_protocol_version(self, version: int) -> None:
        """设置当前 RakNet 协议版本。

        对应 Community-Bot 的 ``SetRakNetProtocolVersion`` 函数。
        用于在 RakNet 握手失败时尝试下一个协议版本 (参考
        ``[Fail] Incompatible protocol, trying next version...`` 逻辑)。

        Parameters
        ----------
        version:
            新的 RakNet 协议版本 (整数, Bedrock 1.21.x 通常为 ``10``)。
        """
        if not isinstance(version, int) or version < 0:
            raise ValueError(
                f"RakNet 协议版本必须是非负整数, 收到: {version!r}"
            )
        old = self._raknet_protocol_version
        self._raknet_protocol_version = version
        if old != version:
            logger.info(
                "RakNet 协议版本已切换: %d -> %d (version=%s)",
                old,
                version,
                self.version,
            )

    # ------------------------------------------------------------------
    # 引擎/补丁版本
    # ------------------------------------------------------------------
    def get_engine_version(self) -> str:
        """返回 Bedrock 引擎版本字符串。

        例如 ``"1.21.80"`` (3.8) / ``"1.21.90"`` (3.9)。
        用于 RakNet Login 包的 protocol_version 字段 (字符串形式)。
        """
        return self.info.engine_version

    def get_patch_version(self) -> str:
        """返回网易启动器补丁版本字符串。

        例如 ``"3.8.0.0"`` (占位值) / ``"3.9.0.0"`` (占位值)。
        用于 ``PEAURequest.patch_version`` 字段 (NEMCTOOLS 语义)。
        """
        return self.info.patch_version

    def get_min_engine_version(self) -> str:
        """返回服务器要求的最小 Bedrock 引擎版本。"""
        return self.info.min_engine_version

    def get_min_patch_version(self) -> str:
        """返回服务器要求的最小网易补丁版本。"""
        return self.info.min_patch_version

    # ------------------------------------------------------------------
    # 服务器地址
    # ------------------------------------------------------------------
    def get_auth_endpoint(self) -> str:
        """返回认证服务器地址 (chainInfo 获取)。

        例如 ``"https://g79authobt.minecraft.cn"``。
        若该地址不可达, 上层可尝试 :meth:`get_auth_endpoint_alt`。
        """
        return self.info.auth_server

    def get_auth_endpoint_alt(self) -> str:
        """返回认证服务器备用地址 (同服务不同域名)。

        例如 ``"https://g79authobt.nie.netease.com"``。
        Community_Bot.exe strings 与既有 constants.py 均使用此域名。
        """
        return self.info.auth_server_alt

    def get_api_endpoint(self) -> str:
        """返回 API 网关地址 (PE/g79 路径)。

        例如 ``"https://g79apigatewayobt.minecraft.cn"``。
        """
        return self.info.api_server

    def get_lobby_endpoint(self) -> str:
        """返回联机大厅服务器地址。

        例如 ``"https://g79mclobt.minecraft.cn"``。
        """
        return self.info.lobby_server

    # ------------------------------------------------------------------
    # 协议特性
    # ------------------------------------------------------------------
    def supports_replaceitem(self) -> bool:
        """返回 ``replaceitem`` 命令是否完整可用 (无阉割)。

        网易 3.8 阉割了 ``replaceitem`` (只能放耐久/特殊值/数量/NBT 标签,
        不能放附魔/自定义名字), 因此返回 ``False``。3.9 可能恢复完整能力
        (待 3.9 发布后实测确认)。

        来源: :mod:`app.protocol.nbt_placer` 的双模式说明。
        """
        return not self.info.replaceitem_limited

    def get_max_command_block_rate(self) -> int:
        """返回命令方块速率上限 (次/秒)。

        3.8 默认 20, 3.9 推测放宽到 30 (待实测)。
        用于 :mod:`app.auth.rate_limiter` 配置。
        """
        return self.info.max_command_block_rate

    def get_chunk_size(self) -> int:
        """返回区块边长 (方块数)。

        Bedrock 全版本统一为 16。
        """
        return self.info.chunk_size

    def get_structure_block_mode(self) -> str:
        """返回 NBT 放置默认模式 (``STRUCTURE`` / ``REPLACEITEM``)。

        3.8 默认 ``STRUCTURE`` (因 replaceitem 受限)。
        返回值与 :data:`STRUCTURE_MODE` / :data:`REPLACEITEM_MODE` 常量比较。
        """
        mode = self.info.default_structure_mode.upper()
        if mode not in (STRUCTURE_MODE, REPLACEITEM_MODE):
            logger.warning(
                "未知的 structure_block_mode=%r, 回退到 STRUCTURE",
                mode,
            )
            return STRUCTURE_MODE
        return mode

    # ------------------------------------------------------------------
    # 调试 / repr
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"ProtocolAdapter(version={self.version}, "
            f"engine={self.info.engine_version!r}, "
            f"protocol={self._raknet_protocol_version})"
        )


__all__ = [
    "ProtocolAdapter",
    "STRUCTURE_MODE",
    "REPLACEITEM_MODE",
]
