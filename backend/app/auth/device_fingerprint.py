"""PocketTerm 设备指纹管理器

本模块基于 NovaBuilder / NexusE 逆向结果实现设备指纹的统一管理。

逆向关键发现
------------

1. NovaBuilder 与 NexusE 都通过 ``uqholder.Player`` 维护以下身份字段::

       DeviceID          -- 设备唯一标识 (如 "amawufyaaxtu3ufq-d")
       ClientRandomID    -- 客户端随机数 (int64, 登录链中使用)
       ClientIdentity    -- 客户端身份串
       BuildPlatform     -- 平台编号 (0/1/2/7/11...)
       XUID              -- Xbox Live 用户 ID (登录成功后由服务器返回)
       UUID              -- 玩家 UUID (github.com/google/uuid v1.6.0)

2. NexusE 额外提供 ``PlayerKit.GetDeviceID`` 查询接口, 用于在运行时
   回查设备 ID, 说明设备指纹是与会话绑定的持久化状态。

3. 登录链 (JWT chain) 中的 identity data 必须包含:

       IdentityUUID / IdentityName / IdentityXUID (登录后填充)
       ClientRandomId         -- int64, 客户端随机数
       BuildPlatform          -- int, 平台编号
       DeviceId               -- 字符串, 设备唯一标识
       DeviceOS               -- 字符串, 操作系统
       LanguageCode           -- BCP-47 语言代码
       CurrentInputMode       -- 0/1/2/3/4 输入模式
       DefaultInputMode       -- 0/1/2/3/4 默认输入模式
       UIProfile              -- 0=Classic 1=Pocket
       IsEditorMode           -- bool

设计要点
--------

1. **持久化**: 所有指纹写入 ``backend/data/device_fingerprints.json``,
   下次启动自动加载, 保证同一账号每次上线设备指纹稳定。
2. **多账号隔离**: 每个账号 (account_id) 独立持有一份指纹, 不同账号
   之间互不串扰, 模拟“一人一机”真实使用模式。
3. **防封禁**: 指纹字段贴近真实设备特征 (随机机型/语言/输入模式),
   且 ``device_id`` 与 ``client_random_id`` 在账号生命周期内保持稳定,
   避免每次登录都换“新设备”导致反作弊告警。
4. **可观测**: 提供列表 / 详情 / 增删改查 API (见 ``app.api.devices``)。
"""
from __future__ import annotations

import copy
import json
import os
import random
import secrets
import string
import threading
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import DATA_DIR, get_config
from ..logger import get_logger

logger = get_logger("auth.device_fingerprint")


# ---------------------------------------------------------------------------
# 平台与机型常量 (逆向自 NovaBuilder / NexusE)
# ---------------------------------------------------------------------------
# BuildPlatform 编号, 取值 1..15 (与 DeviceOS 校验一致)
# 参考 strings: "DeviceOS must carry a value between 1 and 15, but got %v"
class BuildPlatform:
    """Bedrock ``BuildPlatform`` 枚举 (来自逆向 strings)。"""

    UNKNOWN = 0       # 未知 / 占位
    WIN32 = 1         # Windows 经典版 (Win32)
    MAC = 2          # macOS
    LINUX = 7         # Linux
    WINDOWS_10 = 11   # Windows 10 UWP 版 (网易 PC 版主流)
    ANDROID = 8       # Android (Google Play)
    IOS = 9          # iOS
    CONSOLE = 10      # 主机
    PLAYSTATION = 12  # PlayStation
    XBOX = 13         # Xbox
    SWITCH = 14       # Nintendo Switch


# 真实设备机型表 (build_platform -> [(device_os, model), ...])
# 选择项贴近主流网易 MC 玩家真实设备分布, 避免冷门机型触发反作弊。
_REAL_DEVICE_MODELS: Dict[int, List[tuple[str, str]]] = {
    BuildPlatform.WINDOWS_10: [
        ("Windows10", "Windows 10 / Intel i5-10400 / 16GB"),
        ("Windows10", "Windows 10 / AMD Ryzen 5 5600 / 32GB"),
        ("Windows10", "Windows 10 / Intel i7-12700 / 32GB"),
        ("Windows11", "Windows 11 / AMD Ryzen 7 5800X / 32GB"),
        ("Windows11", "Windows 11 / Intel i5-12400 / 16GB"),
    ],
    BuildPlatform.ANDROID: [
        ("Android", "Xiaomi 13"),
        ("Android", "Xiaomi 14 Pro"),
        ("Android", "HUAWEI Mate 60 Pro"),
        ("Android", "HUAWEI P60 Pro"),
        ("Android", "OPPO Find X7"),
        ("Android", "vivo X100 Pro"),
        ("Android", "Samsung Galaxy S24"),
        ("Android", "OnePlus 12"),
        ("Android", "Redmi K70 Pro"),
    ],
    BuildPlatform.IOS: [
        ("iOS", "iPhone 13"),
        ("iOS", "iPhone 14 Pro"),
        ("iOS", "iPhone 15"),
        ("iOS", "iPhone 15 Pro Max"),
    ],
    BuildPlatform.MAC: [
        ("MacOS", "MacBook Pro (M1)"),
        ("MacOS", "MacBook Air (M2)"),
        ("MacOS", "Mac mini (M2)"),
    ],
}

# 主流默认平台: 网易 PC 版 = Win10 (BuildPlatform 11)
DEFAULT_BUILD_PLATFORM: int = BuildPlatform.WINDOWS_10

# 默认游戏版本 / 协议版本 (与 mc_auth.sauth 保持一致)
DEFAULT_GAME_VERSION: str = "1.21.93"

# 默认语言代码 (BCP-47)
DEFAULT_LANGUAGE_CODE: str = "zh_CN"

# PlayerAuthInput 输入模式 (与协议常量保持一致)
class InputMode:
    """PlayerAuthInput 输入模式 (来自 strings_source)。"""

    UNSPECIFIED = 0
    MOUSE = 1        # PC 键鼠
    TOUCH = 2        # 触摸屏
    GAME_PAD = 3     # 手柄
    MOTION_CONTROLLER = 4


# UI 配置文件 (0=Classic / 1=Pocket)
class UIProfile:
    CLASSIC = 0
    POCKET = 1


# device_id 字符集: 小写字母 + 数字 (与逆向 strings 中 "amawufyaaxtu3ufq-d" 风格一致)
_DEVICE_ID_ALPHABET = string.ascii_lowercase + string.digits

# device_id 默认长度 (NovaBuilder 风格: 16 字符随机串 + "-d" 后缀)
_DEVICE_ID_RANDOM_LEN = 16
_DEVICE_ID_SUFFIX = "-d"

# client_random_id 取值范围 (int64 正数, 真实客户端通常为 10^17 量级)
_CLIENT_RANDOM_ID_MIN = 10**17
_CLIENT_RANDOM_ID_MAX = 10**18 - 1


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class DeviceFingerprint:
    """设备指纹。

    字段与 NovaBuilder / NexusE 的 ``uqholder.Player`` 中维护的字段对齐,
    保证登录链 (JWT identity data) 能直接序列化使用。

    Attributes:
        device_id: 设备唯一标识 (如 ``"amawufyaaxtu3ufq-d"``)。
        client_random_id: 客户端随机 ID (int64, 登录链 ClientRandomId 字段)。
        uuid: 玩家 UUID (github.com/google/uuid v4 风格)。
        build_platform: 平台编号 (见 :class:`BuildPlatform`)。
        device_os: 操作系统字符串 (登录链 DeviceOS 字段)。
        game_version: 游戏版本字符串 (如 ``"1.21.93"``)。
        language_code: BCP-47 语言代码 (如 ``"zh_CN"``)。
        current_input_mode: 当前输入模式 (见 :class:`InputMode`)。
        default_input_mode: 默认输入模式。
        ui_profile: UI 配置文件 (0=Classic / 1=Pocket)。
        is_editor_mode: 是否编辑器模式。
        device_model: 设备型号描述 (用于日志展示, 非协议字段)。
        account_id: 所属账号 ID (用于多账号隔离)。
        created_at: 创建时间戳。
        last_used_at: 最近一次使用时间戳。
    """

    device_id: str = ""
    client_random_id: int = 0
    uuid: str = ""
    build_platform: int = DEFAULT_BUILD_PLATFORM
    device_os: str = "Windows10"
    game_version: str = DEFAULT_GAME_VERSION
    language_code: str = DEFAULT_LANGUAGE_CODE
    current_input_mode: int = InputMode.MOUSE
    default_input_mode: int = InputMode.MOUSE
    ui_profile: int = UIProfile.CLASSIC
    is_editor_mode: bool = False
    device_model: str = ""
    account_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    # -- uqholder 扩展信息 (NexusE 风格) --
    # 登录成功后由服务器返回, 用于保持多登录一致性
    xuid: str = ""                # Xbox Live 用户 ID (登录后填充)
    identity_name: str = ""       # 玩家名 (登录后填充)
    last_login_at: float = 0.0    # 上次登录时间戳
    last_login_host: str = ""     # 上次登录服务器地址
    login_count: int = 0          # 累计登录次数
    extend_info: Dict[str, Any] = field(default_factory=dict)  # 自定义扩展信息

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------
    @classmethod
    def generate(
        cls,
        *,
        account_id: str = "",
        build_platform: Optional[int] = None,
        device_model: Optional[str] = None,
        game_version: Optional[str] = None,
        language_code: Optional[str] = None,
        rng: Optional[random.Random] = None,
    ) -> "DeviceFingerprint":
        """生成一份全新的随机设备指纹。

        Args:
            account_id: 所属账号 ID (用于多账号隔离)。
            build_platform: 指定平台编号; 为 ``None`` 时按权重随机选取。
            device_model: 指定设备型号描述; 为 ``None`` 时按平台随机选取。
            game_version: 游戏版本, 默认取配置中的 ``bot.game_version``。
            language_code: 语言代码, 默认 ``zh_CN``。
            rng: 可选随机数生成器 (用于测试可复现)。
        """
        rng = rng or random.Random(secrets.randbits(64))

        # 1. 平台选取: 默认按真实玩家分布加权 (PC 70% / Android 25% / iOS 5%)
        if build_platform is None:
            build_platform = rng.choices(
                [
                    BuildPlatform.WINDOWS_10,
                    BuildPlatform.ANDROID,
                    BuildPlatform.IOS,
                    BuildPlatform.MAC,
                ],
                weights=[70, 25, 4, 1],
                k=1,
            )[0]

        # 2. 机型选取
        # 默认 device_os (按平台推断, 后续若选取到机型则覆盖)
        if build_platform in (BuildPlatform.WINDOWS_10, BuildPlatform.WIN32):
            device_os = "Windows10"
        elif build_platform == BuildPlatform.MAC:
            device_os = "MacOS"
        elif build_platform == BuildPlatform.LINUX:
            device_os = "Linux"
        elif build_platform == BuildPlatform.ANDROID:
            device_os = "Android"
        elif build_platform == BuildPlatform.IOS:
            device_os = "iOS"
        else:
            device_os = "Windows10"

        if device_model is None:
            models = _REAL_DEVICE_MODELS.get(build_platform, [])
            if models:
                device_os, device_model = rng.choice(models)
            else:
                device_model = "Unknown Device"
        # 若调用方指定了 device_model 但未指定 device_os, 则保留上面推断的 device_os

        # 3. 输入模式: PC -> Mouse, Mobile -> Touch
        if build_platform in (BuildPlatform.WINDOWS_10, BuildPlatform.WIN32,
                              BuildPlatform.MAC, BuildPlatform.LINUX):
            current_input_mode = InputMode.MOUSE
            default_input_mode = InputMode.MOUSE
            ui_profile = UIProfile.CLASSIC
        else:
            current_input_mode = InputMode.TOUCH
            default_input_mode = InputMode.TOUCH
            ui_profile = UIProfile.POCKET

        # 4. 游戏版本 (优先使用配置)
        if game_version is None:
            try:
                game_version = get_config().get(
                    "bot", "game_version", default=DEFAULT_GAME_VERSION
                )
            except Exception:  # noqa: BLE001
                game_version = DEFAULT_GAME_VERSION

        if language_code is None:
            language_code = DEFAULT_LANGUAGE_CODE

        return cls(
            device_id=_generate_device_id(rng),
            client_random_id=_generate_client_random_id(rng),
            uuid=str(_uuid.uuid4()),
            build_platform=build_platform,
            device_os=device_os,
            game_version=game_version,
            language_code=language_code,
            current_input_mode=current_input_mode,
            default_input_mode=default_input_mode,
            ui_profile=ui_profile,
            is_editor_mode=False,
            device_model=device_model,
            account_id=account_id,
            created_at=time.time(),
            last_used_at=0.0,
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典 (供 API 返回 / 持久化 / 协议字段使用)。"""
        return asdict(self)

    def to_login_chain_identity(self) -> Dict[str, Any]:
        """构造登录链 (JWT identity data) 所需的字段子集。

        与 NovaBuilder / NexusE 在 ``build_login_chain`` 中使用的字段名一致,
        可直接合并入 JWT payload。

        若已登录过 (XUID 已填充), 会包含 XUID 以保持多登录一致性
        (NexusE 的 ``uqholder`` 风格)。
        """
        identity = {
            "IdentityUUID": self.uuid,
            "IdentityName": self.identity_name,  # 优先使用已登录的玩家名
            "IdentityXUID": self.xuid,           # 已登录则填充, 否则空
            "ClientRandomId": self.client_random_id,
            "BuildPlatform": self.build_platform,
            "DeviceId": self.device_id,
            "DeviceOS": self.device_os,
            "LanguageCode": self.language_code,
            "CurrentInputMode": self.current_input_mode,
            "DefaultInputMode": self.default_input_mode,
            "UIProfile": self.ui_profile,
            "IsEditorMode": self.is_editor_mode,
            "GameVersion": self.game_version,
        }
        # 合并扩展信息 (如果有)
        if self.extend_info:
            identity.update(self.extend_info)
        return identity

    def update_login_info(
        self,
        *,
        xuid: Optional[str] = None,
        identity_name: Optional[str] = None,
        login_host: Optional[str] = None,
        extend_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """登录成功后更新扩展信息 (uqholder 风格)。

        NexusE 在登录成功后会将服务器返回的 XUID / IdentityName 等
        持久化到 ``uqholder``, 下次登录时复用, 保持多登录一致性。

        Args:
            xuid: 服务器返回的 XUID。
            identity_name: 服务器返回的玩家名。
            login_host: 登录的服务器地址。
            extend_info: 自定义扩展信息 (会合并入现有 extend_info)。
        """
        if xuid is not None:
            self.xuid = xuid
        if identity_name is not None:
            self.identity_name = identity_name
        if login_host is not None:
            self.last_login_host = login_host
        if extend_info is not None:
            self.extend_info.update(extend_info)
        self.last_login_at = time.time()
        self.login_count += 1
        self.touch()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeviceFingerprint":
        """从字典反序列化 (忽略未知字段, 缺失字段使用默认值)。"""
        kwargs: Dict[str, Any] = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                kwargs[f] = data[f]
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def short_summary(self) -> str:
        """返回简短描述 (日志展示用)。"""
        head = self.device_id[:8] if len(self.device_id) >= 8 else self.device_id
        return (
            f"Platform={self.build_platform} "
            f"OS={self.device_os} "
            f"Model={self.device_model or 'N/A'} "
            f"DevId={head}.. "
            f"UUID={self.uuid[:8]}.. "
            f"Ver={self.game_version}"
        )

    def touch(self) -> None:
        """更新最近使用时间。"""
        self.last_used_at = time.time()


# ---------------------------------------------------------------------------
# 随机字段生成器
# ---------------------------------------------------------------------------
def _generate_device_id(rng: random.Random) -> str:
    """生成随机 device_id (NovaBuilder 风格: 16 字符随机串 + ``-d`` 后缀)。

    示例: ``amawufyaaxtu3ufq-d``
    """
    random_part = "".join(rng.choice(_DEVICE_ID_ALPHABET) for _ in range(_DEVICE_ID_RANDOM_LEN))
    return f"{random_part}{_DEVICE_ID_SUFFIX}"


def _generate_client_random_id(rng: random.Random) -> int:
    """生成随机 client_random_id (int64, 真实客户端量级 ~10^17)。"""
    return rng.randint(_CLIENT_RANDOM_ID_MIN, _CLIENT_RANDOM_ID_MAX)


# ---------------------------------------------------------------------------
# 持久化管理器
# ---------------------------------------------------------------------------
#: 默认指纹存储路径: ``backend/data/device_fingerprints.json``
DEFAULT_FINGERPRINT_FILE: Path = DATA_DIR / "device_fingerprints.json"


class DeviceFingerprintManager:
    """设备指纹管理器。

    负责:
        1. 多账号独立的设备指纹管理 (按 ``account_id`` 索引)
        2. 持久化到 JSON 文件 (启动加载 / 变更保存)
        3. 提供 CRUD 接口供 API 与机器人使用
        4. 线程安全 (使用 ``threading.RLock`` 保护内存结构)

    典型用法::

        mgr = DeviceFingerprintManager()
        mgr.load()
        fp = mgr.get_or_create(account_id="acc-123")
        fp_dict = fp.to_login_chain_identity()
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._file_path: Path = Path(file_path) if file_path else DEFAULT_FINGERPRINT_FILE
        self._fingerprints: Dict[str, DeviceFingerprint] = {}
        self._lock = threading.RLock()
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    def load(self) -> None:
        """从磁盘加载所有设备指纹。

        首次运行或文件不存在时, 自动创建空指纹集 (不报错)。
        """
        with self._lock:
            if not self._file_path.exists():
                logger.info(f"设备指纹文件不存在, 将新建: {self._file_path}")
                self._fingerprints = {}
                self._loaded = True
                return

            try:
                with open(self._file_path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error(f"设备指纹文件加载失败, 将重建: {exc}")
                self._fingerprints = {}
                self._loaded = True
                return

            if not isinstance(raw, dict):
                logger.error("设备指纹文件顶层结构非对象, 将重建")
                self._fingerprints = {}
                self._loaded = True
                return

            fingerprints: Dict[str, DeviceFingerprint] = {}
            # 兼容两种文件格式:
            #   1. { "account_id": {fingerprint_dict}, ... }
            #   2. { "fingerprints": [ {fingerprint_dict}, ... ], "version": "1" }
            items: List[Mapping[str, Any]]
            if "fingerprints" in raw and isinstance(raw["fingerprints"], list):
                items = raw["fingerprints"]
            else:
                items = list(raw.values())

            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    fp = DeviceFingerprint.from_dict(item)
                except TypeError as exc:
                    logger.warning(f"跳过无法解析的指纹项: {exc}")
                    continue
                key = self._index_for(fp)
                fingerprints[key] = fp

            self._fingerprints = fingerprints
            self._loaded = True
            logger.info(f"已加载 {len(fingerprints)} 条设备指纹 (来自 {self._file_path})")

    def save(self) -> None:
        """持久化所有设备指纹到磁盘 (原子写入)。"""
        with self._lock:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1",
                "updated_at": time.time(),
                "fingerprints": [fp.to_dict() for fp in self._fingerprints.values()],
            }
            tmp_path = self._file_path.with_suffix(self._file_path.suffix + ".tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                # 原子替换: os.replace 在同一文件系统上是原子的
                os.replace(tmp_path, self._file_path)
                logger.debug(f"设备指纹已保存: {len(self._fingerprints)} 条")
            except (OSError, TypeError, ValueError) as exc:
                # BUG-6.3 修复: 之前仅捕获 OSError, 但 json.dump 可能抛出
                # TypeError (不可序列化对象) 或 ValueError (循环引用等),
                # 这些异常会导致 save 方法向上抛出而非优雅降级。
                logger.error(f"设备指纹保存失败: {exc}")
                # 清理临时文件
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _account_key(account_id: str) -> str:
        """账号索引键 (主键)。"""
        return f"acct:{account_id}"

    @staticmethod
    def _device_key(device_id: str) -> str:
        """设备索引键 (仅用于回退查找, 不直接存储)。"""
        return f"dev:{device_id}"

    def _index_for(self, fp: DeviceFingerprint) -> str:
        """计算指纹在字典中的存储键 (优先 account_id)。"""
        if fp.account_id:
            return self._account_key(fp.account_id)
        return self._device_key(fp.device_id)

    def _ensure_loaded(self) -> None:
        """确保已从磁盘加载 (懒加载)。

        BUG-6.1 修复: 之前 _loaded 检查和 load() 调用都在锁外执行,
        多线程并发调用时可能同时触发 load() 导致重复加载或数据竞争。
        现在在锁内进行检查和加载 (RLock 允许重入, load() 内部也加锁)。
        """
        with self._lock:
            if not self._loaded:
                self.load()

    def _find_by_device_id(self, device_id: str) -> Optional[DeviceFingerprint]:
        """按 device_id 遍历查找指纹 (内部辅助方法, 必须在锁内调用)。"""
        for fp in self._fingerprints.values():
            if fp.device_id == device_id:
                return fp
        return None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def get_by_account(self, account_id: str) -> Optional[DeviceFingerprint]:
        """按账号 ID 查询设备指纹。"""
        self._ensure_loaded()
        with self._lock:
            fp = self._fingerprints.get(self._account_key(account_id))
            # BUG-6.2 修复: 返回深拷贝而非缓存中的实际对象引用,
            # 防止调用方意外修改返回值而污染内部缓存。
            return copy.deepcopy(fp) if fp is not None else None

    def get_by_device_id(self, device_id: str) -> Optional[DeviceFingerprint]:
        """按 device_id 查询设备指纹。"""
        self._ensure_loaded()
        with self._lock:
            fp = self._find_by_device_id(device_id)
            # BUG-6.2 修复: 同 get_by_account, 返回深拷贝防止缓存污染。
            return copy.deepcopy(fp) if fp is not None else None

    def list_all(self) -> List[DeviceFingerprint]:
        """返回所有已存储的设备指纹 (浅拷贝列表)。"""
        self._ensure_loaded()
        with self._lock:
            return list(self._fingerprints.values())

    def get_or_create(
        self,
        account_id: str,
        *,
        build_platform: Optional[int] = None,
        device_model: Optional[str] = None,
        game_version: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> DeviceFingerprint:
        """获取或创建账号绑定的设备指纹。

        - 若该账号已有指纹, 直接返回 (保证“一人一机”稳定)。
        - 若无, 生成新指纹并持久化。

        Args:
            account_id: 账号 ID (不可为空)。
            build_platform / device_model / game_version / language_code:
                创建新指纹时使用的可选覆盖值。
        """
        if not account_id:
            raise ValueError("account_id 不能为空")

        self._ensure_loaded()
        with self._lock:
            key = self._account_key(account_id)
            fp = self._fingerprints.get(key)
            if fp is None:
                fp = DeviceFingerprint.generate(
                    account_id=account_id,
                    build_platform=build_platform,
                    device_model=device_model,
                    game_version=game_version,
                    language_code=language_code,
                )
                self._fingerprints[key] = fp
                self.save()
                logger.info(
                    f"为账号 {account_id} 生成新设备指纹: {fp.short_summary()}"
                )
            else:
                fp.touch()
                logger.debug(f"账号 {account_id} 复用设备指纹: {fp.short_summary()}")
            return fp

    def create(
        self,
        *,
        account_id: str = "",
        build_platform: Optional[int] = None,
        device_model: Optional[str] = None,
        game_version: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> DeviceFingerprint:
        """强制创建一份新设备指纹 (即使 account_id 已存在也会覆盖)。

        主要用于 API ``POST /api/devices`` 端点, 允许运维主动重置指纹。
        """
        self._ensure_loaded()
        with self._lock:
            fp = DeviceFingerprint.generate(
                account_id=account_id,
                build_platform=build_platform,
                device_model=device_model,
                game_version=game_version,
                language_code=language_code,
            )
            key = self._index_for(fp)
            self._fingerprints[key] = fp
            self.save()
            logger.info(f"创建新设备指纹 (account={account_id}): {fp.short_summary()}")
            return fp

    def update(self, device_id: str, updates: Mapping[str, Any]) -> Optional[DeviceFingerprint]:
        """按 device_id 更新指纹字段 (部分更新)。

        Args:
            device_id: 目标设备 ID。
            updates: 待覆盖的字段字典。

        Returns:
            更新后的 :class:`DeviceFingerprint`; 不存在时返回 ``None``。
        """
        self._ensure_loaded()
        with self._lock:
            fp = self._find_by_device_id(device_id)
            if fp is None:
                return None
            for key, value in updates.items():
                if key in DeviceFingerprint.__dataclass_fields__:
                    setattr(fp, key, value)
            fp.touch()
            self.save()
            logger.info(f"更新设备指纹 {device_id}: fields={list(updates.keys())}")
            return fp

    def update_login_info(
        self,
        account_id: str,
        *,
        xuid: Optional[str] = None,
        identity_name: Optional[str] = None,
        login_host: Optional[str] = None,
        extend_info: Optional[Dict[str, Any]] = None,
    ) -> Optional[DeviceFingerprint]:
        """更新账号的登录信息并持久化 (uqholder 风格)。

        登录成功后调用, 将服务器返回的 XUID / IdentityName 等持久化,
        下次登录时复用, 保持多登录一致性。

        Args:
            account_id: 账号 ID。
            xuid: 服务器返回的 XUID。
            identity_name: 服务器返回的玩家名。
            login_host: 登录的服务器地址。
            extend_info: 自定义扩展信息。

        Returns:
            更新后的 :class:`DeviceFingerprint`; 不存在时返回 ``None``。
        """
        self._ensure_loaded()
        with self._lock:
            fp = self._fingerprints.get(self._account_key(account_id))
            if fp is None:
                logger.warning(
                    f"账号 {account_id} 无设备指纹, 无法更新登录信息"
                )
                return None
            fp.update_login_info(
                xuid=xuid,
                identity_name=identity_name,
                login_host=login_host,
                extend_info=extend_info,
            )
            self.save()
            logger.info(
                f"账号 {account_id} 登录信息已更新: "
                f"xuid={xuid or '(unchanged)'} "
                f"name={identity_name or '(unchanged)'} "
                f"login_count={fp.login_count}"
            )
            return fp

    def delete(self, device_id: str) -> bool:
        """按 device_id 删除指纹。

        Returns:
            ``True`` 删除成功; ``False`` 不存在。
        """
        self._ensure_loaded()
        with self._lock:
            fp = self._find_by_device_id(device_id)
            if fp is None:
                return False
            key = self._index_for(fp)
            self._fingerprints.pop(key, None)
            self.save()
            logger.info(f"删除设备指纹: {device_id} (account={fp.account_id})")
            return True

    def delete_by_account(self, account_id: str) -> int:
        """删除某账号下的所有指纹。

        Returns:
            实际删除的条目数。
        """
        self._ensure_loaded()
        with self._lock:
            removed = 0
            for key in list(self._fingerprints.keys()):
                fp = self._fingerprints[key]
                if fp.account_id == account_id:
                    self._fingerprints.pop(key, None)
                    removed += 1
            if removed:
                self.save()
                logger.info(f"清理账号 {account_id} 的 {removed} 条设备指纹")
            return removed

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """返回指纹集合的统计信息 (供 API 使用)。"""
        self._ensure_loaded()
        with self._lock:
            total = len(self._fingerprints)
            by_platform: Dict[int, int] = {}
            by_os: Dict[str, int] = {}
            for fp in self._fingerprints.values():
                by_platform[fp.build_platform] = by_platform.get(fp.build_platform, 0) + 1
                by_os[fp.device_os] = by_os.get(fp.device_os, 0) + 1
            return {
                "total": total,
                "by_platform": by_platform,
                "by_os": by_os,
                "file_path": str(self._file_path),
            }


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
#: 进程级全局设备指纹管理器 (惰性加载)
_global_manager: Optional[DeviceFingerprintManager] = None
_global_lock = threading.Lock()


def get_fingerprint_manager() -> DeviceFingerprintManager:
    """返回全局 :class:`DeviceFingerprintManager` 单例。

    首次调用时惰性加载磁盘上的指纹文件。
    """
    global _global_manager
    with _global_lock:
        if _global_manager is None:
            _global_manager = DeviceFingerprintManager()
            _global_manager.load()
        return _global_manager


def reset_fingerprint_manager() -> None:
    """重置全局单例 (主要用于测试)。"""
    global _global_manager
    with _global_lock:
        _global_manager = None


__all__ = [
    # 常量
    "BuildPlatform",
    "InputMode",
    "UIProfile",
    "DEFAULT_BUILD_PLATFORM",
    "DEFAULT_GAME_VERSION",
    "DEFAULT_LANGUAGE_CODE",
    "DEFAULT_FINGERPRINT_FILE",
    # 数据结构
    "DeviceFingerprint",
    # 管理器
    "DeviceFingerprintManager",
    "get_fingerprint_manager",
    "reset_fingerprint_manager",
]
