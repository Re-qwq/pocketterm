"""sauth_json 构建器 — 对应 Community-Bot 的 PC/PE 认证凭证格式。

本模块构建网易 Minecraft 客户端登录时携带的 ``sauth_json`` 凭证字符串,
逆向自 ``Community_Bot.exe`` strings 中提取的完整 sauth_json 示例。

Community-Bot 提取的完整 sauth_json 示例
========================================

::

    {
      "sauth_json": "{
        \\"gameid\\":\\"x19\\",
        \\"login_channel\\":\\"netease\\",
        \\"app_channel\\":\\"netease\\",
        \\"platform\\":\\"pc\\",
        \\"sdkuid\\":\\"aebghyp62fz2pwms\\",
        \\"sessionid\\":\\"1-eyJzaSI6Ij...\\",
        \\"sdk_version\\":\\"3.4.0\\",
        \\"udid\\":\\"d1a91970b6aa41e59a0aeaea42c55abf\\",
        \\"deviceid\\":\\"amawhyiaanju3rfe-d\\",
        \\"aim_info\\":\\"{\\\\\\"aim\\\\\\":\\\\\\"100.100.100.100\\\\\\",
                        \\\\\\"country\\\\\\":\\\\\\"CN\\\\\\",
                        \\\\\\"tz\\\\\\":\\\\\\"+0800\\\\\\",
                        \\\\\\"tzid\\\\\\":\\\\\\"\\\\\\"}\\"
      }"
    }

关键字段说明
============

- ``gameid``: 游戏 ID, PC=x19 / PE=g79
- ``login_channel`` / ``app_channel``: 渠道, 均为 ``netease``
- ``platform``: 平台标识, ``pc`` / ``pe``
- ``sdkuid``: SDK 用户 ID (登录后由网易服务器返回)
- ``sessionid``: 会话 ID (``1-`` 前缀 + base64 payload)
- ``sdk_version``: SDK 版本号, PC=3.4.0 / PE=5.2.0
- ``udid``: 设备唯一标识 (32 字符 hex)
- ``deviceid``: 设备 ID (含 ``-d`` 后缀)
- ``aim_info``: AIM 信息 JSON 字符串 (内嵌的转义 JSON, 含 aim/country/tz/tzid)

设计原则
========

- **可独立 import**: 仅依赖标准库 (``json`` / ``logging``), 不依赖其他 PocketTerm 模块。
- **不修改既有模块**: 本模块与既有 :mod:`app.auth.mc_auth.sauth` (设备指纹格式)
  并存, 两者格式不同, 互不干扰。
- **双版本兼容**: 通过 :class:`~app.protocol.version_manager.MinecraftVersion`
  关联版本配置中的 ``sdk_version`` / ``gameid`` / ``platform``。
- **PC/PE 双模式**: :meth:`SAuthBuilder.build_pc_sauth` 与
  :meth:`SAuthBuilder.build_pe_sauth` 分别构造 PC (x19) 与 PE (g79) 凭证。

逆向来源
========

- ``Community_Bot.exe`` (用户上传) — strings 分析:
  - 完整 ``sauth_json`` 示例 (gameid=x19, platform=pc, sdk_version=3.4.0)
  - ``sdkuid`` / ``sessionid`` / ``udid`` / ``deviceid`` / ``aim_info`` 字段名
- PocketTerm ``app/auth/netease_direct/constants.py``:
  - ``SDK_VERSION_PC = "3.4.0"`` (与 Community-Bot 一致)
  - ``SDK_VERSION_PE = "5.2.0"``
- PocketTerm ``backend/data/version_config.json``:
  - ``sdk_version`` / ``gameid`` / ``platform`` / ``sdk_version_pe`` 字段

典型用法
========

::

    from app.auth.sauth_builder import SAuthBuilder

    # 1. 构建 PC 端 sauth_json
    sauth = SAuthBuilder.build_pc_sauth(
        sdkuid="aebghyp62fz2pwms",
        sessionid="1-eyJzaSI6Ij...",
        udid="d1a91970b6aa41e59a0aeaea42c55abf",
        deviceid="amawhyiaanju3rfe-d",
    )
    # sauth 是一个 JSON 字符串, 可直接作为登录请求的 sauth_json 字段

    # 2. 构建 PE 端 sauth_json
    sauth_pe = SAuthBuilder.build_pe_sauth(
        sdkuid="aebghyp62fz2pwms",
        sessionid="1-eyJzaSI6Ij...",
        udid="d1a91970b6aa41e59a0aeaea42c55abf",
        deviceid="amawhyiaanju3rfe-d",
    )

    # 3. 解析验证
    import json
    d = json.loads(sauth)
    assert d["gameid"] == "x19"
    assert d["platform"] == "pc"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Logger (用户指定命名空间, sauth_builder 属于 auth 但也按 pocketterm 命名)
# ---------------------------------------------------------------------------
_LOGGER_NAME: str = "pocketterm.auth.sauth_builder"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ===========================================================================
# 协议常量 (来自 Community-Bot.exe strings + 既有 constants.py)
# ===========================================================================
#: PC (x19) 游戏 ID (sauth_json.gameid 字段)。
GAMEID_PC: str = "x19"

#: PE (g79) 游戏 ID。
GAMEID_PE: str = "g79"

#: PC 登录/应用渠道。
LOGIN_CHANNEL: str = "netease"
APP_CHANNEL: str = "netease"

#: PC 平台标识。
PLATFORM_PC: str = "pc"

#: PE 平台标识。
PLATFORM_PE: str = "pe"

#: PC SDK 版本号 (sauth_json.sdk_version 字段)。
#: 来源: Community-Bot.exe strings sauth_json 示例 (3.4.0) +
#: 既有 constants.py SDK_VERSION_PC (3.4.0), 二者一致。
SDK_VERSION_PC: str = "3.4.0"

#: PE SDK 版本号。
#: 来源: 既有 constants.py SDK_VERSION_PE。
SDK_VERSION_PE: str = "5.2.0"

#: 默认 AIM 信息 (aim_info 字段内嵌 JSON)。
DEFAULT_AIM: str = "100.100.100.100"
DEFAULT_COUNTRY: str = "CN"
DEFAULT_TZ: str = "+0800"
DEFAULT_TZID: str = ""


# ===========================================================================
# 异常
# ===========================================================================
class SAuthBuildError(ValueError):
    """sauth_json 构建失败 (必填字段缺失或格式非法)。"""


# ===========================================================================
# SAuthBuilder
# ===========================================================================
class SAuthBuilder:
    """构建 sauth_json 认证凭证 (对应 Community-Bot 的 sauth_json 格式)。

    本类提供 PC (x19) 与 PE (g79) 两种平台的 sauth_json 构建方法,
    所有方法均为 ``@staticmethod``, 无需实例化。

    字段格式严格遵循 Community-Bot.exe strings 提取的示例,
    ``aim_info`` 字段为内嵌的转义 JSON 字符串 (即 ``json.dumps`` 后的字符串
    作为 ``aim_info`` 的值, 外层再做一次 ``json.dumps``)。
    """

    # ------------------------------------------------------------------
    # PC 端 sauth_json (platform=pc, gameid=x19)
    # ------------------------------------------------------------------
    @staticmethod
    def build_pc_sauth(
        sdkuid: str,
        sessionid: str,
        udid: str,
        deviceid: str,
        *,
        sdk_version: str = SDK_VERSION_PC,
        gameid: str = GAMEID_PC,
        aim: str = DEFAULT_AIM,
        country: str = DEFAULT_COUNTRY,
        tz: str = DEFAULT_TZ,
        tzid: str = DEFAULT_TZID,
    ) -> str:
        """构建 PC 端 sauth_json (platform=pc, gameid=x19)。

        对应 Community-Bot.exe strings 提取的完整 sauth_json 示例。

        Parameters
        ----------
        sdkuid:
            SDK 用户 ID (登录后由网易服务器返回, 如 ``aebghyp62fz2pwms``)。
        sessionid:
            会话 ID (``1-`` 前缀 + base64 payload, 如 ``1-eyJzaSI6Ij...``)。
        udid:
            设备唯一标识 (32 字符 hex, 如 ``d1a91970b6aa41e59a0aeaea42c55abf``)。
        deviceid:
            设备 ID (含 ``-d`` 后缀, 如 ``amawhyiaanju3rfe-d``)。
        sdk_version:
            SDK 版本号 (默认 ``3.4.0``, 来自 Community-Bot)。
        gameid:
            游戏 ID (默认 ``x19``)。
        aim:
            AIM 地址 (默认 ``100.100.100.100``)。
        country:
            国家代码 (默认 ``CN``)。
        tz:
            时区 (默认 ``+0800``)。
        tzid:
            时区 ID (默认空字符串)。

        Returns
        -------
        str
            sauth_json 字符串 (JSON 序列化, 可直接作为登录请求字段)。

        Raises
        ------
        SAuthBuildError
            必填字段 (sdkuid/sessionid/udid/deviceid) 为空时抛出。
        """
        SAuthBuilder._validate_required(sdkuid, sessionid, udid, deviceid)
        aim_info = SAuthBuilder._build_aim_info(aim, country, tz, tzid)
        payload = {
            "gameid": gameid,
            "login_channel": LOGIN_CHANNEL,
            "app_channel": APP_CHANNEL,
            "platform": PLATFORM_PC,
            "sdkuid": sdkuid,
            "sessionid": sessionid,
            "sdk_version": sdk_version,
            "udid": udid,
            "deviceid": deviceid,
            "aim_info": aim_info,
        }
        result = json.dumps(payload, ensure_ascii=False)
        logger.debug(
            "已构建 PC sauth_json (gameid=%s, platform=%s, sdk_version=%s)",
            gameid,
            PLATFORM_PC,
            sdk_version,
        )
        return result

    # ------------------------------------------------------------------
    # PE 端 sauth_json (platform=pe, gameid=g79)
    # ------------------------------------------------------------------
    @staticmethod
    def build_pe_sauth(
        sdkuid: str,
        sessionid: str,
        udid: str,
        deviceid: str,
        *,
        sdk_version: str = SDK_VERSION_PE,
        gameid: str = GAMEID_PE,
        aim: str = DEFAULT_AIM,
        country: str = DEFAULT_COUNTRY,
        tz: str = DEFAULT_TZ,
        tzid: str = DEFAULT_TZID,
    ) -> str:
        """构建 PE 端 sauth_json (platform=pe, gameid=g79)。

        PE 模式对应 Community-Bot 的 ``--g79`` 命令行参数路径,
        与 PC 模式的区别仅在 ``platform`` (pe) / ``gameid`` (g79) /
        ``sdk_version`` (5.2.0)。

        Parameters
        ----------
        sdkuid:
            SDK 用户 ID。
        sessionid:
            会话 ID。
        udid:
            设备唯一标识。
        deviceid:
            设备 ID。
        sdk_version:
            SDK 版本号 (默认 ``5.2.0``, PE 模式)。
        gameid:
            游戏 ID (默认 ``g79``)。
        aim, country, tz, tzid:
            AIM 信息字段 (与 PC 相同语义)。

        Returns
        -------
        str
            sauth_json 字符串。
        """
        SAuthBuilder._validate_required(sdkuid, sessionid, udid, deviceid)
        aim_info = SAuthBuilder._build_aim_info(aim, country, tz, tzid)
        payload = {
            "gameid": gameid,
            "login_channel": LOGIN_CHANNEL,
            "app_channel": APP_CHANNEL,
            "platform": PLATFORM_PE,
            "sdkuid": sdkuid,
            "sessionid": sessionid,
            "sdk_version": sdk_version,
            "udid": udid,
            "deviceid": deviceid,
            "aim_info": aim_info,
        }
        result = json.dumps(payload, ensure_ascii=False)
        logger.debug(
            "已构建 PE sauth_json (gameid=%s, platform=%s, sdk_version=%s)",
            gameid,
            PLATFORM_PE,
            sdk_version,
        )
        return result

    # ------------------------------------------------------------------
    # 从版本配置构建 (双版本适配)
    # ------------------------------------------------------------------
    @staticmethod
    def build_from_version(
        version,
        sdkuid: str,
        sessionid: str,
        udid: str,
        deviceid: str,
        *,
        is_pc: bool = True,
        aim: str = DEFAULT_AIM,
        country: str = DEFAULT_COUNTRY,
        tz: str = DEFAULT_TZ,
        tzid: str = DEFAULT_TZID,
    ) -> str:
        """根据 :class:`~app.protocol.version_manager.MinecraftVersion` 构建 sauth_json。

        从 :file:`version_config.json` 读取 ``sdk_version`` / ``gameid`` /
        ``platform`` 字段, 适配网易 3.8 / 3.9 双版本。

        Parameters
        ----------
        version:
            目标版本枚举 (``MinecraftVersion.V3_8`` / ``V3_9``),
            或版本字符串 (``"3.8"`` / ``"3.9"``)。
        sdkuid, sessionid, udid, deviceid:
            认证字段 (同 :meth:`build_pc_sauth`)。
        is_pc:
            ``True`` 构建 PC (x19) 凭证; ``False`` 构建 PE (g79) 凭证。
        aim, country, tz, tzid:
            AIM 信息字段。

        Returns
        -------
        str
            sauth_json 字符串。

        Raises
        ------
        SAuthBuildError
            版本配置缺失 ``sdk_version`` / ``gameid`` / ``platform`` 字段时抛出。
        """
        # 惰性导入 version_manager (避免顶层循环依赖, 保证独立 import)
        try:
            from app.protocol.version_manager import (
                MinecraftVersion,
                VersionManager,
            )
        except Exception as exc:  # noqa: BLE001
            raise SAuthBuildError(
                f"无法导入 version_manager, 请检查 app.protocol.version_manager: {exc}"
            ) from exc

        # 解析版本
        if isinstance(version, MinecraftVersion):
            mv = version
        elif isinstance(version, str):
            mv = VersionManager.parse_version_string(version)
            if mv is None:
                raise SAuthBuildError(f"无法识别的版本字符串: {version!r}")
        else:
            raise SAuthBuildError(
                f"version 参数类型不支持: {type(version).__name__}"
            )

        info = VersionManager.get_version_info(mv)
        # 版本配置中的字段 (build_pc_sauth / build_pe_sauth 的默认值兜底)
        sdk_version = getattr(info, "sdk_version", None) or (
            SDK_VERSION_PC if is_pc else SDK_VERSION_PE
        )
        gameid = getattr(info, "gameid", None) or (
            GAMEID_PC if is_pc else GAMEID_PE
        )
        platform = getattr(info, "platform", None)
        if not platform:
            platform = PLATFORM_PC if is_pc else PLATFORM_PE

        # 若配置中的 platform 与 is_pc 不一致, 以 is_pc 为准 (显式参数优先)
        if is_pc and platform != PLATFORM_PC:
            logger.warning(
                "版本配置 platform=%r 与 is_pc=True 不一致, 已强制使用 pc",
                platform,
            )
            platform = PLATFORM_PC
        elif not is_pc and platform != PLATFORM_PE:
            logger.warning(
                "版本配置 platform=%r 与 is_pc=False 不一致, 已强制使用 pe",
                platform,
            )
            platform = PLATFORM_PE

        aim_info = SAuthBuilder._build_aim_info(aim, country, tz, tzid)
        payload = {
            "gameid": gameid,
            "login_channel": LOGIN_CHANNEL,
            "app_channel": APP_CHANNEL,
            "platform": platform,
            "sdkuid": sdkuid,
            "sessionid": sessionid,
            "sdk_version": sdk_version,
            "udid": udid,
            "deviceid": deviceid,
            "aim_info": aim_info,
        }
        result = json.dumps(payload, ensure_ascii=False)
        logger.debug(
            "已构建 sauth_json (version=%s, gameid=%s, platform=%s, sdk=%s)",
            mv,
            gameid,
            platform,
            sdk_version,
        )
        return result

    # ------------------------------------------------------------------
    # 解析 / 验证
    # ------------------------------------------------------------------
    @staticmethod
    def parse_sauth(sauth_json: str) -> Dict[str, Any]:
        """解析 sauth_json 字符串并校验关键字段。

        Parameters
        ----------
        sauth_json:
            sauth_json 字符串 (由 :meth:`build_pc_sauth` /
            :meth:`build_pe_sauth` 生成, 或从服务器响应中获取)。

        Returns
        -------
        dict
            解析后的字典。

        Raises
        ------
        SAuthBuildError
            JSON 解析失败或缺少关键字段时抛出。
        """
        try:
            data = json.loads(sauth_json)
        except json.JSONDecodeError as exc:
            raise SAuthBuildError(f"sauth_json JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise SAuthBuildError("sauth_json 顶层不是 JSON 对象")
        for key in ("gameid", "platform", "sdkuid", "sessionid", "sdk_version"):
            if key not in data:
                raise SAuthBuildError(f"sauth_json 缺少关键字段: {key}")
        return data

    @staticmethod
    def validate_pc_sauth(sauth_json: str) -> bool:
        """校验 sauth_json 是否为合法的 PC (x19) 凭证。

        Parameters
        ----------
        sauth_json:
            待校验的 sauth_json 字符串。

        Returns
        -------
        bool
            ``True`` 表示合法的 PC 凭证 (gameid=x19, platform=pc)。
        """
        try:
            data = SAuthBuilder.parse_sauth(sauth_json)
        except SAuthBuildError:
            return False
        return data.get("gameid") == GAMEID_PC and data.get("platform") == PLATFORM_PC

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_required(
        sdkuid: str, sessionid: str, udid: str, deviceid: str
    ) -> None:
        """校验必填字段非空。"""
        for name, value in (
            ("sdkuid", sdkuid),
            ("sessionid", sessionid),
            ("udid", udid),
            ("deviceid", deviceid),
        ):
            if not isinstance(value, str) or not value:
                raise SAuthBuildError(f"必填字段 {name} 不能为空")

    @staticmethod
    def _build_aim_info(
        aim: str, country: str, tz: str, tzid: str
    ) -> str:
        """构建 aim_info 字段 (内嵌的转义 JSON 字符串)。

        对应 Community-Bot sauth_json 示例中的::

            "aim_info": "{\\"aim\\":\\"100.100.100.100\\",
                          \\"country\\":\\"CN\\",
                          \\"tz\\":\\"+0800\\",
                          \\"tzid\\":\\"\\"}"

        即 ``aim_info`` 的值是一个 JSON 字符串 (经 ``json.dumps`` 序列化)。
        """
        aim_obj = {
            "aim": aim,
            "country": country,
            "tz": tz,
            "tzid": tzid,
        }
        return json.dumps(aim_obj, ensure_ascii=False)


__all__ = [
    "SAuthBuilder",
    "SAuthBuildError",
    # 常量
    "GAMEID_PC",
    "GAMEID_PE",
    "PLATFORM_PC",
    "PLATFORM_PE",
    "SDK_VERSION_PC",
    "SDK_VERSION_PE",
    "LOGIN_CHANNEL",
    "APP_CHANNEL",
    "DEFAULT_AIM",
    "DEFAULT_COUNTRY",
    "DEFAULT_TZ",
    "DEFAULT_TZID",
]
