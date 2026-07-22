"""从网易 Minecraft 启动器配置文件提取 sauth_json/cookie。

网易我的世界启动器在本地保存了登录凭证,我们可以从配置文件中提取
sauth_json 用于直接认证,避免手动复制 cookie。

启动器配置路径:
    Windows:
        C:\\Users\\<用户名>\\AppData\\Local\\Netease\\MCLauncher\\config\\
        C:\\Users\\<用户名>\\AppData\\Roaming\\Netease\\Minecraft\\
    macOS:
        ~/Library/Application Support/Netease/Minecraft/
    Linux:
        ~/.config/Netease/Minecraft/

关键文件:
    - config.json (启动器配置,包含 uid/token)
    - sauth.json (认证凭证,直接可用)
    - launcher_profiles.json (类似国际版的配置)

用法::

    from app.auth.netease_direct.cookie_extractor import CookieExtractor

    extractor = CookieExtractor()
    cookies = extractor.extract_all()
    for c in cookies:
        print(c["uid"], c["sauth_json"][:40])
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger("pocketterm.cookie_extractor")


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ExtractedCredential:
    """从启动器提取的凭证。"""
    source: str = ""  # 来源文件路径
    uid: str = ""
    player_name: str = ""
    sauth_json: str = ""  # 完整的 sauth_json (包装格式)
    sauth_json_inner: str = ""  # 内部 sauth_json
    sessionid: str = ""
    sdkuid: str = ""
    udid: str = ""
    deviceid: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """检查凭证是否有效 (至少有 sessionid 和 sdkuid)。"""
        return bool(self.sessionid and self.sdkuid)

    def to_wrapped(self) -> str:
        """返回包装格式的 sauth_json。"""
        if self.sauth_json:
            return self.sauth_json
        if self.sauth_json_inner:
            return json.dumps({"sauth_json": self.sauth_json_inner}, ensure_ascii=False)
        return ""

    def to_account_dict(self) -> dict:
        """转换为 accounts.json 格式。"""
        return {
            "uid": self.uid,
            "player_name": self.player_name,
            "sdkuid": self.sdkuid,
            "sessionid": self.sessionid,
            "udid": self.udid,
            "deviceid": self.deviceid,
            "sauth_json": self.to_wrapped(),
            "source": "launcher_extract",
            "extracted_at": int(time.time()),
        }


# ============================================================================
# 提取器
# ============================================================================

class CookieExtractor:
    """从网易 Minecraft 启动器配置文件提取 sauth_json。

    用法::

        extractor = CookieExtractor()
        creds = extractor.extract_all()
        if creds:
            print(f"找到 {len(creds)} 个凭证")
            for c in creds:
                print(f"  UID={c.uid}, name={c.player_name}")
    """

    # 启动器可能的配置路径 (按优先级)
    LAUNCHER_PATHS_WINDOWS = [
        # 网易启动器标准路径
        "~/AppData/Local/Netease/MCLauncher/config",
        "~/AppData/Roaming/Netease/MCLauncher/config",
        "~/AppData/Local/Netease/Minecraft",
        "~/AppData/Roaming/Netease/Minecraft",
        # 我的世界中国版路径
        "~/AppData/Local/Packages/Microsoft.MinecraftUWP_8wekyb3d8bbwe",
        # 网易我的世界 PC 版
        "~/Documents/Netease/Minecraft",
        # 其他可能路径
        "~/AppData/Local/Netease",
        "~/AppData/Roaming/Netease",
    ]

    LAUNCHER_PATHS_MACOS = [
        "~/Library/Application Support/Netease/Minecraft",
        "~/Library/Application Support/Netease/MCLauncher",
        "~/Library/Preferences/Netease",
    ]

    LAUNCHER_PATHS_LINUX = [
        "~/.config/Netease/Minecraft",
        "~/.config/Netease/MCLauncher",
        "~/.local/share/Netease/Minecraft",
    ]

    # 关键配置文件名
    CONFIG_FILES = [
        "sauth.json",
        "config.json",
        "launcher_profiles.json",
        "login.json",
        "account.json",
        "credentials.json",
        "cookie.json",
        "cookies.json",
        "auth.json",
    ]

    def __init__(self, custom_path: Optional[Union[str, Path]] = None):
        """初始化提取器。

        Args:
            custom_path: 自定义启动器配置路径 (可选)
        """
        self.custom_path = Path(custom_path) if custom_path else None
        self._found_paths: list[Path] = []

    @property
    def search_paths(self) -> list[Path]:
        """返回搜索路径列表。"""
        if self.custom_path:
            return [self.custom_path]

        system = platform.system()
        home = Path.home()

        if system == "Windows":
            paths = self.LAUNCHER_PATHS_WINDOWS
        elif system == "Darwin":
            paths = self.LAUNCHER_PATHS_MACOS
        else:
            paths = self.LAUNCHER_PATHS_LINUX

        return [Path(p).expanduser() for p in paths]

    def extract_all(self) -> list[ExtractedCredential]:
        """提取所有找到的凭证。

        Returns:
            凭证列表 (可能为空)
        """
        creds: list[ExtractedCredential] = []
        seen_sessionids: set[str] = set()

        for search_path in self.search_paths:
            if not search_path.exists():
                continue

            logger.info(f"搜索路径: {search_path}")

            # 递归搜索配置文件
            for root, dirs, files in os.walk(search_path):
                root_path = Path(root)
                for fname in files:
                    if fname.lower() in [c.lower() for c in self.CONFIG_FILES]:
                        file_path = root_path / fname
                        try:
                            cred = self._extract_from_file(file_path)
                            if cred and cred.is_valid():
                                if cred.sessionid not in seen_sessionids:
                                    creds.append(cred)
                                    seen_sessionids.add(cred.sessionid)
                                    self._found_paths.append(file_path)
                                    logger.info(
                                        f"从 {file_path} 提取凭证: "
                                        f"UID={cred.uid}, name={cred.player_name}"
                                    )
                        except Exception as e:
                            logger.warning(f"解析 {file_path} 失败: {e}")

        return creds

    def _extract_from_file(self, file_path: Path) -> Optional[ExtractedCredential]:
        """从单个文件提取凭证。"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug(f"读取 {file_path} 失败: {e}")
            return None

        if not content.strip():
            return None

        # 尝试解析为 JSON
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # 不是 JSON, 尝试解析为其他格式
            return self._extract_from_text(content, file_path)

        if not isinstance(data, dict):
            return None

        cred = ExtractedCredential(source=str(file_path))

        # 情况1: 直接是 sauth_json (内部格式)
        if "sdkuid" in data and "sessionid" in data:
            cred.sauth_json_inner = content
            cred.sdkuid = str(data.get("sdkuid", ""))
            cred.sessionid = str(data.get("sessionid", ""))
            cred.udid = str(data.get("udid", ""))
            cred.deviceid = str(data.get("deviceid", ""))
            cred.uid = str(data.get("uid", ""))
            cred.player_name = str(data.get("nickname", data.get("player_name", "")))
            cred.raw = data
            return cred

        # 情况2: 包装格式 {"sauth_json": "<inner>"}
        if "sauth_json" in data:
            inner = data["sauth_json"]
            if isinstance(inner, str):
                try:
                    inner_data = json.loads(inner)
                    if isinstance(inner_data, dict):
                        return self._extract_from_dict(inner_data, file_path, wrapped=content)
                except json.JSONDecodeError:
                    pass
            elif isinstance(inner, dict):
                return self._extract_from_dict(inner, file_path)

        # 情况3: 包含 uid/token 的配置文件
        if "uid" in data or "userId" in data:
            return self._extract_from_dict(data, file_path)

        # 情况4: 嵌套结构 (如 profiles 数组)
        for key in ("profiles", "accounts", "users", "credentials", "logins"):
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    if isinstance(item, dict):
                        cred = self._extract_from_dict(item, file_path)
                        if cred and cred.is_valid():
                            return cred

        return None

    def _extract_from_dict(
        self, data: dict, file_path: Path, wrapped: str = ""
    ) -> Optional[ExtractedCredential]:
        """从字典提取凭证。"""
        cred = ExtractedCredential(source=str(file_path))

        # 提取各种字段名
        cred.sdkuid = str(
            data.get("sdkuid", data.get("sdk_uid", data.get("userId", "")))
        )
        cred.sessionid = str(
            data.get("sessionid", data.get("sessionId", data.get("token", "")))
        )
        cred.udid = str(data.get("udid", data.get("device_id", "")))
        cred.deviceid = str(data.get("deviceid", data.get("deviceId", "")))
        cred.uid = str(data.get("uid", data.get("player_uid", "")))
        cred.player_name = str(
            data.get("nickname", data.get("player_name", data.get("displayName", "")))
        )

        if wrapped:
            cred.sauth_json = wrapped
        else:
            cred.sauth_json_inner = json.dumps(data, ensure_ascii=False)

        cred.raw = data

        if cred.is_valid():
            return cred
        return None

    def _extract_from_text(self, content: str, file_path: Path) -> Optional[ExtractedCredential]:
        """从文本格式提取凭证 (如 INI 或 properties 文件)。"""
        cred = ExtractedCredential(source=str(file_path))

        # 尝试 INI/properties 格式
        patterns = {
            "sdkuid": r"sdkuid[=:]\s*([^\s\n]+)",
            "sessionid": r"sessionid[=:]\s*([^\s\n]+)",
            "udid": r"udid[=:]\s*([^\s\n]+)",
            "deviceid": r"deviceid[=:]\s*([^\s\n]+)",
            "uid": r"(?:player_)?uid[=:]\s*([^\s\n]+)",
            "player_name": r"(?:nickname|player_name|displayName)[=:]\s*([^\s\n]+)",
        }

        found = False
        for field_name, pattern in patterns.items():
            m = re.search(pattern, content, re.IGNORECASE)
            if m:
                setattr(cred, field_name, m.group(1).strip('"\''))
                found = True

        if found and cred.is_valid():
            return cred
        return None

    def get_found_paths(self) -> list[Path]:
        """返回找到凭证的文件路径列表。"""
        return self._found_paths


# ============================================================================
# 便捷函数
# ============================================================================

def extract_launcher_cookies(custom_path: Optional[str] = None) -> list[ExtractedCredential]:
    """从网易启动器配置提取所有 cookie/sauth_json。

    Args:
        custom_path: 自定义启动器配置路径 (可选)

    Returns:
        凭证列表
    """
    extractor = CookieExtractor(custom_path=custom_path)
    return extractor.extract_all()


def find_launcher_config() -> Optional[Path]:
    """查找网易启动器配置目录。"""
    extractor = CookieExtractor()
    for path in extractor.search_paths:
        if path.exists():
            return path
    return None


__all__ = [
    "CookieExtractor",
    "ExtractedCredential",
    "extract_launcher_cookies",
    "find_launcher_config",
]
