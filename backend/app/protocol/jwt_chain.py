"""JWT 登录链构建 — Minecraft Bedrock Edition 身份认证链。

Minecraft Bedrock Edition 使用 JWT (JSON Web Token) 登录链进行客户端身份认证。
本模块构造 Bedrock 协议 ``Login`` 数据包中的 ``ConnectionRequest`` 字段,
逆向自 neomega 与 NovaBuilder (PhoenixBuilder/StarShuttler)。

登录链 wire 格式::

    +--------------------------+-------------------------------+
    | chain JSON (chain 数组)  | raw token (ClientData JWT)    |
    +--------------------------+-------------------------------+

字节布局::

    [int32 LE: chain_json_len] [chain_json_bytes]
    [int32 LE: raw_token_len]  [raw_token_bytes]

chain JSON 格式::

    {"chain": ["<jwt1>", "<jwt2>", ...]}

JWT 结构 (header.payload.signature, 使用 ES384 / ECDSA P-384 签名):

JWT header::

    {"alg": "ES384", "x5u": "<base64_der_public_key>"}

``x5u`` 字段是 ECDSA 公钥的 SubjectPublicKeyInfo DER 编码后,使用 **标准
Base64** (含 padding, 而非 URL-safe) 编码得到的字符串。Bedrock 协议全部
使用标准 Base64, 这与 RFC 7517 中 x5u 字段建议的 base64url 不同, 但与
go-raknet / gophertunnel / neomega / NovaBuilder 的实现保持一致。

chain 中各 JWT 的语义:

    1. **identity claims** (自签名或由认证服务器签发):
       - 包含 ``extraData`` 字段 (即 :class:`IdentityData`, 含 XUID/displayName)
       - 包含 ``identityPublicKey`` 字段 (Base64 编码的公钥)
       - 可选包含 ``certificateAuthority: true``

    2. **client data JWT** (单独作为 RawToken, 不在 chain 数组中):
       - payload 是完整的 :class:`ClientData` 对象 (设备信息 + 皮肤)
       - 由客户端 ECDSA 私钥签名

关键设计:
    - 使用 ``secp384r1`` (NIST P-384) 曲线
    - ES384 算法 (ECDSA + SHA-384)
    - JWT 默认过期时间 6 小时, ``nbf`` 提前 6 小时
    - 公钥编码: SubjectPublicKeyInfo DER → 标准 Base64

基本用法::

    from app.protocol.jwt_chain import build_login_chain

    raw = build_login_chain(
        sauth_json=sauth_str,
        device_fingerprint=fp.to_dict(),
        server_address="1.2.3.4:19132",
    )
    # raw 可直接作为 Login 数据包的 ConnectionRequest 字段

逆向来源:
    - neomega ``minecraft/protocol/login`` (Go)
    - gophertunnel ``minecraft/protocol/login`` (Go, EncodeOffline)
    - NovaBuilder ``BedrockLoginChain`` (C#)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: ECDSA 曲线 — NIST P-384 (secp384r1)
ECDSA_CURVE = ec.SECP384R1()

#: JWT 签名算法 (ECDSA + SHA-384)
JWT_ALGORITHM: str = "ES384"

#: JWT 默认有效期 (6 小时, 与 gophertunnel/neomega 一致)
DEFAULT_JWT_LIFETIME: int = 6 * 3600

#: JWT ``nbf`` 字段提前量 (6 小时, 容忍时钟漂移)
DEFAULT_JWT_NOT_BEFORE_OFFSET: int = 6 * 3600

#: JWT 默认签发者
DEFAULT_ISSUER: str = "NetEase"

#: 默认游戏版本 (与 Bedrock 1.21.93 对应)
DEFAULT_GAME_VERSION: str = "1.21.93"

#: 默认皮肤几何引擎版本 (与游戏版本一致)
DEFAULT_GEOMETRY_ENGINE_VERSION: str = "1.16.0"

#: 默认皮肤资源 patch (geometry.humanoid.custom)
DEFAULT_SKIN_RESOURCE_PATCH: str = json.dumps(
    {"geometry": {"default": "geometry.humanoid.custom"}},
    separators=(",", ":"),
)

#: 默认皮肤几何数据 (Steve 64x64 标准模型)
DEFAULT_SKIN_GEOMETRY: str = json.dumps(
    {
        "format_version": "1.12.0",
        "minecraft:geometry": [
            {
                "description": {
                    "identifier": "geometry.humanoid.custom",
                    "texture_height": 64,
                    "texture_width": 64,
                    "visible_bounds_height": 2,
                    "visible_bounds_offset": [0, 1, 0],
                    "visible_bounds_width": 1,
                }
            }
        ],
    },
    separators=(",", ":"),
)

#: 默认皮肤宽度 (像素)
DEFAULT_SKIN_WIDTH: int = 64

#: 默认皮肤高度 (像素)
DEFAULT_SKIN_HEIGHT: int = 64

#: 默认语言代码
DEFAULT_LANGUAGE_CODE: str = "zh_CN"

#: 默认手臂粗细 ("wide" 或 "slim")
DEFAULT_ARM_SIZE: str = "wide"

#: 默认皮肤颜色 (Steve 头部)
DEFAULT_SKIN_COLOR: str = "#b37b62"

logger = logging.getLogger("pocketterm.jwt_chain")


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass
class IdentityData:
    """身份数据 (JWT chain 中 identity claims 的 ``extraData`` 字段)。

    Bedrock 协议中, ``IdentityData`` 由 Mojang / 网易认证服务器签发,
    客户端不能修改; 但在离线 (无 XBOX Live / 网易认证) 模式下,
    客户端可以自签名一个 ``IdentityData``。

    Attributes:
        XUID: XBOX Live 用户 ID (十六进制字符串); 离线模式下为空字符串。
        displayName: 玩家显示名称 (用户名)。
        identity: 玩家 UUID (带连字符的标准格式)。
        titleId: 标题 ID (XBOX Live 相关); 离线模式下为空字符串。
    """

    XUID: str = ""
    displayName: str = "Steve"
    identity: str = ""
    titleId: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JWT payload 中 ``extraData`` 的字典。"""
        return {
            "XUID": self.XUID,
            "displayName": self.displayName,
            "identity": self.identity,
            "titleId": self.titleId,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IdentityData":
        """从字典反序列化 (忽略未知字段)。"""
        return cls(
            XUID=str(data.get("XUID", "")),
            displayName=str(data.get("displayName", "Steve")),
            identity=str(data.get("identity", "")),
            titleId=str(data.get("titleId", "")),
        )


@dataclass
class ClientData:
    """客户端数据 (单独作为 RawToken 的 JWT payload)。

    ``ClientData`` 携带设备信息、皮肤、UI 偏好等数据, 由客户端自由构造,
    并用客户端的 ECDSA 私钥签名。Bedrock 服务器会校验其完整性, 但不会
    信任其中的身份字段 (身份字段来自 ``IdentityData``)。

    Attributes:
        GameVersion: 游戏版本字符串 (如 ``"1.21.93"``)。
        DeviceOS: 设备操作系统 ID (1=Android, 7=Win64)。
        DeviceModel: 设备型号字符串。
        DeviceId: 设备唯一 ID (UUID 格式)。
        ClientRandomId: 客户端随机 ID (int64, 跨会话保持一致)。
        SelfSignedId: 自签名 ID (UUID 格式)。
        ServerAddress: 服务器地址 (host:port 格式)。
        SkinId: 皮肤唯一 ID (UUID 格式)。
        SkinData: base64 编码的皮肤像素数据 (RGBA)。
        SkinGeometry: base64 编码的皮肤几何 JSON。
        SkinGeometryDataEngineVersion: 皮肤几何引擎版本。
        PlayFabId: PlayFab ID (皮肤市场相关)。
    """

    # 设备信息
    GameVersion: str = DEFAULT_GAME_VERSION
    DeviceOS: int = 1  # Android
    DeviceModel: str = ""
    DeviceId: str = ""
    ClientRandomId: int = 0
    SelfSignedId: str = ""
    ServerAddress: str = ""

    # 语言/UI
    LanguageCode: str = DEFAULT_LANGUAGE_CODE
    CurrentInputMode: int = 1
    DefaultInputMode: int = 1
    GuiScale: int = 0
    UIProfile: int = 1  # Pocket

    # 平台 ID
    PlatformOfflineId: str = ""
    PlatformOnlineId: str = ""
    PlatformUserId: str = ""

    # 皮肤基础字段
    SkinId: str = ""
    SkinData: str = ""
    SkinGeometry: str = ""
    SkinGeometryDataEngineVersion: str = DEFAULT_GEOMETRY_ENGINE_VERSION
    SkinResourcePatch: str = DEFAULT_SKIN_RESOURCE_PATCH
    SkinColor: str = DEFAULT_SKIN_COLOR
    SkinImageWidth: int = DEFAULT_SKIN_WIDTH
    SkinImageHeight: int = DEFAULT_SKIN_HEIGHT
    PlayFabId: str = ""

    # 披风
    CapeId: str = ""
    CapeData: str = ""
    CapeImageWidth: int = 0
    CapeImageHeight: int = 0
    CapeOnClassicSkin: bool = False

    # Persona 皮肤
    PersonaSkin: bool = False
    PremiumSkin: bool = False
    ArmSize: str = DEFAULT_ARM_SIZE
    AnimatedImageData: list = field(default_factory=list)
    PersonaPieces: list = field(default_factory=list)
    PieceTintColors: list = field(default_factory=list)

    # 其他
    ThirdPartyName: str = "Steve"
    ThirdPartyNameOnly: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JWT payload 字典 (使用 Bedrock 协议字段名)。"""
        # asdict 会递归展开嵌套 dataclass; 这里所有字段都是基础类型或 list
        data = asdict(self)
        return data


@dataclass
class LoginChain:
    """完整 Bedrock 登录链。

    包含 chain 数组 (identity JWTs) 与单独的 ClientData RawToken,
    能序列化为 Bedrock ``Login`` 数据包的 ``ConnectionRequest`` 字段。

    Attributes:
        chain: JWT 字符串列表 (identity claims)。
        clientData: 客户端数据 (用于构造 RawToken)。
        clientDataJwt: 客户端数据序列化后的 JWT 字符串 (单独作为 RawToken)。
    """

    chain: list[str]
    clientData: ClientData
    clientDataJwt: str = ""

    def to_bytes(self) -> bytes:
        """序列化为 Bedrock ``Login`` 数据包的 ``ConnectionRequest`` 字段。

        wire 格式::

            [int32 LE: chain_json_len] [chain_json_bytes]
            [int32 LE: raw_token_len]  [raw_token_bytes]

        其中::

            chain_json  = {"chain": ["<jwt1>", "<jwt2>", ...]}  (UTF-8)
            raw_token   = "<client_data_jwt>"  (UTF-8)

        Returns:
            序列化后的字节串。

        Raises:
            ValueError: ``clientDataJwt`` 为空。
        """
        if not self.clientDataJwt:
            raise ValueError(
                "clientDataJwt 为空, 请先调用 build_login_chain 构造"
            )

        chain_json = json.dumps(
            {"chain": self.chain}, separators=(",", ":"),
        ).encode("utf-8")
        raw_token_bytes = self.clientDataJwt.encode("utf-8")

        buf = bytearray()
        # chain JSON 长度 + 内容
        buf += struct.pack("<i", len(chain_json))
        buf += chain_json
        # RawToken 长度 + 内容
        buf += struct.pack("<i", len(raw_token_bytes))
        buf += raw_token_bytes
        return bytes(buf)


# ----------------------------------------------------------------------
# 内部: ECDSA 密钥与公钥序列化
# ----------------------------------------------------------------------

def generate_ecdsa_keypair() -> ec.EllipticCurvePrivateKey:
    """生成 ECDSA (secp384r1) 密钥对。

    Returns:
        :class:`~cryptography...EllipticCurvePrivateKey` 实例。
    """
    return ec.generate_private_key(ECDSA_CURVE)


def marshal_public_key(public_key: ec.EllipticCurvePublicKey) -> str:
    """将 ECDSA 公钥序列化为 Bedrock 协议使用的 ``x5u`` 字符串。

    序列化过程:
        1. 使用 ``SubjectPublicKeyInfo`` 格式 DER 编码公钥
        2. 使用标准 Base64 (含 padding) 编码 DER 字节

    Args:
        public_key: ECDSA 公钥。

    Returns:
        Base64 编码的公钥字符串, 可作为 JWT header 的 ``x5u`` 字段。
    """
    der_bytes = public_key.public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der_bytes).decode("ascii")


def parse_public_key(b64_data: str) -> ec.EllipticCurvePublicKey:
    """从 Base64 编码的 ``x5u`` 字符串解析 ECDSA 公钥。

    Args:
        b64_data: Base64 编码的 SubjectPublicKeyInfo DER 公钥。

    Returns:
        ECDSA 公钥对象。

    Raises:
        ValueError: Base64 解码失败或 DER 解析失败。
    """
    try:
        der_bytes = base64.b64decode(b64_data)
    except Exception as exc:
        raise ValueError(f"公钥 Base64 解码失败: {exc}") from exc

    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(der_bytes)
    except Exception as exc:
        raise ValueError(f"DER 公钥解析失败: {exc}") from exc

    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError(
            f"期望 ECDSA 公钥, 实际得到 {type(key).__name__}"
        )
    return key


# ----------------------------------------------------------------------
# 内部: JWT 签名
# ----------------------------------------------------------------------

def _sign_jwt(
    payload: dict[str, Any],
    private_key: ec.EllipticCurvePrivateKey,
    x5u: str,
) -> str:
    """使用 ECDSA (ES384) 签名一个 JWT。

    Args:
        payload: JWT payload 字典。
        private_key: ECDSA 私钥。
        x5u: Base64 编码的公钥 (作为 JWT header 的 ``x5u`` 字段)。

    Returns:
        紧凑序列化的 JWT 字符串 (header.payload.signature)。
    """
    headers = {"alg": JWT_ALGORITHM, "x5u": x5u}
    # PyJWT 2.x 会自动添加 typ=JWT; 我们显式覆盖以保持紧凑
    token = pyjwt.encode(
        payload,
        private_key,
        algorithm=JWT_ALGORITHM,
        headers=headers,
    )
    # PyJWT 2.x 返回 str, 1.x 返回 bytes
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


def _build_time_claims(
    lifetime: int = DEFAULT_JWT_LIFETIME,
    not_before_offset: int = DEFAULT_JWT_NOT_BEFORE_OFFSET,
) -> dict[str, int]:
    """构造 JWT 时间相关 claims (``exp`` / ``nbf`` / ``iat``)。"""
    now = int(time.time())
    return {
        "exp": now + lifetime,
        "nbf": now - not_before_offset,
        "iat": now,
    }


def _build_identity_claims(
    identity_data: IdentityData,
    x5u: str,
    issuer: str = DEFAULT_ISSUER,
) -> dict[str, Any]:
    """构造 identity claims JWT 的 payload。

    包含:
        - ``exp`` / ``nbf`` / ``iat``: 时间 claims
        - ``iss``: 签发者
        - ``extraData``: :class:`IdentityData` 字典
        - ``identityPublicKey``: 与 ``x5u`` 相同的 Base64 公钥
        - ``certificateAuthority``: ``True`` (标记自签名证书)

    Args:
        identity_data: 身份数据。
        x5u: Base64 编码的公钥 (与 header ``x5u`` 相同)。
        issuer: JWT 签发者字符串。

    Returns:
        identity claims 字典。
    """
    claims: dict[str, Any] = _build_time_claims()
    claims["iss"] = issuer
    claims["extraData"] = identity_data.to_dict()
    claims["identityPublicKey"] = x5u
    claims["certificateAuthority"] = True
    return claims


# ----------------------------------------------------------------------
# 内部: sauth_json 与设备指纹解析
# ----------------------------------------------------------------------

def _parse_sauth_json(sauth_json: str) -> dict[str, Any]:
    """解析 sauth_json 字符串, 容错处理。

    接受两种格式:
        - ``{"sauth_json": "<inner>"}`` (外层包装)
        - ``<inner>`` (直接是 inner JSON)

    Args:
        sauth_json: sauth_json 字符串。

    Returns:
        内部 sauth 字典。

    Raises:
        ValueError: JSON 解析失败。
    """
    text = sauth_json.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sauth_json 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("sauth_json 顶层不是对象")

    # 解包 {"sauth_json": "<inner>"}
    inner = data.get("sauth_json")
    if isinstance(inner, str):
        try:
            inner_data = json.loads(inner)
        except json.JSONDecodeError as exc:
            raise ValueError(f"sauth_json inner 解析失败: {exc}") from exc
        if isinstance(inner_data, dict):
            return inner_data

    return data


def _build_default_identity_data(
    sauth_data: Mapping[str, Any],
    display_name: str = "",
) -> IdentityData:
    """从 sauth_json 解析结果构造默认 :class:`IdentityData`。

    优先级:
        - ``display_name`` 参数 > sauth 中的 ``sdkuid`` / ``player_name`` > 默认 "Steve"

    Args:
        sauth_data: 解析后的 sauth 字典。
        display_name: 显式指定的显示名称 (覆盖 sauth 中的值)。

    Returns:
        :class:`IdentityData` 实例。
    """
    name = display_name or str(
        sauth_data.get("player_name")
        or sauth_data.get("sdkuid")
        or sauth_data.get("display_name")
        or "Steve"
    )

    # 网易 UID 作为 XUID (离线模式 XUID 通常为空)
    xuid = str(sauth_data.get("uid") or sauth_data.get("xuid") or "")

    # Bug 10.2 修复: identity 之前每次调用都随机生成 (uuid.uuid4()), 但防作弊
    # 系统会检测 identity 频繁变化。改为基于 sdkuid 生成确定性 UUID (uuid5),
    # 保证同一账号每次连接的 identity 稳定不变。sdkuid 为空时回退到 displayName。
    identity_seed = str(
        sauth_data.get("sdkuid") or sauth_data.get("player_name") or name
    )
    identity = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"netease:{identity_seed}"))

    return IdentityData(
        XUID=xuid,
        displayName=name,
        identity=identity,
        titleId="",
    )


# ----------------------------------------------------------------------
# 内部: 默认皮肤数据生成
# ----------------------------------------------------------------------

def _default_skin_pixels(width: int = DEFAULT_SKIN_WIDTH,
                         height: int = DEFAULT_SKIN_HEIGHT) -> bytes:
    """生成默认皮肤像素数据 (RGBA, 透明黑)。

    Args:
        width: 皮肤宽度 (像素)。
        height: 皮肤高度 (像素)。

    Returns:
        ``width * height * 4`` 字节的 RGBA 数据。
    """
    # 每个 RGBA 像素 = (0, 0, 0, 255) — 不透明黑色
    return bytes([0, 0, 0, 255]) * (width * height)


def _build_default_client_data(
    device_fingerprint: Mapping[str, Any],
    server_address: str,
    game_version: str = DEFAULT_GAME_VERSION,
    skin_data: Optional[Mapping[str, Any]] = None,
) -> ClientData:
    """构造默认 :class:`ClientData`。

    从设备指纹字典与可选皮肤数据构造 ClientData, 字段缺失时使用合理默认值。

    Args:
        device_fingerprint: 设备指纹字典 (含 ``device_id`` / ``device_model`` /
            ``platform`` / ``game_version`` 等字段)。
        server_address: 服务器地址 (host:port)。
        game_version: 游戏版本字符串。
        skin_data: 可选的自定义皮肤数据 (字段名与 :class:`ClientData` 一致)。

    Returns:
        :class:`ClientData` 实例。
    """
    # 解析设备指纹
    device_id = str(device_fingerprint.get("device_id") or str(uuid.uuid4()))
    device_model = str(
        device_fingerprint.get("device_model") or "Samsung Galaxy S21"
    )
    platform = str(device_fingerprint.get("platform") or "android").lower()

    # 平台 → DeviceOS
    if platform in ("android", "g79"):
        device_os = 1
    elif platform in ("windows", "pc", "x19", "win64"):
        device_os = 7
    elif platform == "ios":
        device_os = 2
    elif platform in ("osx", "macos"):
        device_os = 3
    else:
        device_os = 1  # 默认 Android

    # 客户端随机 ID (跨会话保持一致)
    # 注意: 不能使用 Python 内置 hash(), 因为 Python 3.3+ 对字符串启用了
    # PYTHONHASHSEED 随机化, 同一 device_id 在不同进程会产生不同 hash 值,
    # 导致 ClientRandomId 每次进程启动都变化 (登录链不一致)。
    # 改用确定性哈希 (SHA256), 确保同一 device_id 每次生成相同的 ClientRandomId。
    try:
        h = hashlib.sha256(device_id.encode("utf-8")).digest()
        client_random_id = (
            int.from_bytes(h[:8], byteorder="big", signed=False)
            & 0x7FFFFFFFFFFFFFFF
        )
    except Exception:
        client_random_id = secrets.randbits(63)

    # 默认皮肤
    skin_pixels = _default_skin_pixels()
    skin_id = str(uuid.uuid4())
    playfab_id = uuid.uuid4().hex[:16]

    client_data = ClientData(
        GameVersion=str(
            device_fingerprint.get("game_version") or game_version
        ),
        DeviceOS=device_os,
        DeviceModel=device_model,
        DeviceId=device_id,
        ClientRandomId=client_random_id,
        SelfSignedId=str(uuid.uuid4()),
        ServerAddress=server_address,
        SkinId=skin_id,
        SkinData=base64.b64encode(skin_pixels).decode("ascii"),
        SkinGeometry=base64.b64encode(
            DEFAULT_SKIN_GEOMETRY.encode("utf-8")
        ).decode("ascii"),
        SkinGeometryDataEngineVersion=DEFAULT_GEOMETRY_ENGINE_VERSION,
        PlayFabId=playfab_id,
        SkinResourcePatch=DEFAULT_SKIN_RESOURCE_PATCH,
        SkinImageWidth=DEFAULT_SKIN_WIDTH,
        SkinImageHeight=DEFAULT_SKIN_HEIGHT,
        PlatformOfflineId=str(uuid.uuid4()),
        PlatformOnlineId=str(secrets.randbits(63)),
        ThirdPartyName=str(device_fingerprint.get("player_name") or device_fingerprint.get("display_name") or "Steve"),
    )

    # 应用自定义皮肤数据 (覆盖默认值)
    if skin_data:
        for key, value in skin_data.items():
            if hasattr(client_data, key):
                setattr(client_data, key, value)

    return client_data


# ----------------------------------------------------------------------
# 公开 API: build_login_chain
# ----------------------------------------------------------------------

def build_login_chain(
    sauth_json: str,
    device_fingerprint: dict,
    server_address: str,
    skin_data: dict | None = None,
    *,
    identity_data: IdentityData | None = None,
    existing_chain: list[str] | None = None,
    issuer: str = DEFAULT_ISSUER,
    game_version: str = DEFAULT_GAME_VERSION,
    private_key: Optional[ec.EllipticCurvePrivateKey] = None,
) -> bytes:
    """构建完整的 Bedrock 登录数据包。

    根据 sauth_json、设备指纹、服务器地址构造 Bedrock ``Login`` 数据包的
    ``ConnectionRequest`` 字段。完整流程:

        1. 解析 sauth_json (可选, 用于获取 display_name 等)
        2. 构造 :class:`ClientData` (设备信息 + 皮肤)
        3. 生成 ECDSA (secp384r1) 密钥对
        4. 构造 :class:`IdentityData` (含 XUID / displayName / identity)
        5. 构造 identity claims JWT (chain[0])
        6. 用同一私钥签名 client data JWT (RawToken)
        7. 序列化为 Bedrock wire 格式字节串

    支持两种模式:
        - **离线模式** (默认): 自签名 identity claims, chain 中只有 1 个 JWT。
        - **在线模式**: 传入 ``existing_chain`` (认证服务器返回的 JWT 列表),
          会自签名一个新的 identityPublicKey JWT 并 ``prepend`` 到链首。

    Args:
        sauth_json: sauth_json 字符串 (网易认证会话 JSON)。
            可包含 ``display_name`` / ``sdkuid`` / ``uid`` 等字段。
        device_fingerprint: 设备指纹字典, 至少应包含:
            ``device_id`` / ``device_model`` / ``platform`` / ``game_version``。
        server_address: 服务器地址 (host:port), 如 ``"1.2.3.4:19132"``。
        skin_data: 可选的自定义皮肤数据, 字段名与 :class:`ClientData` 一致。
        identity_data: 可选的 :class:`IdentityData`。为 ``None`` 时从
            sauth_json 自动构造默认值。
        existing_chain: 可选的已有 chain (认证服务器返回的 JWT 列表)。
            传入时会自签名一个 identityPublicKey JWT 并 prepend 到链首。
        issuer: JWT ``iss`` 字段 (默认 ``"NetEase"``)。
        game_version: 游戏版本字符串 (默认 ``"1.21.93"``)。
        private_key: 可选的 ECDSA 私钥。为 ``None`` 时自动生成新的密钥对。

    Returns:
        序列化后的字节串, 可直接作为 ``Login`` 数据包的
        ``ConnectionRequest`` 字段。

    Raises:
        ValueError: sauth_json 解析失败或参数非法。
        RuntimeError: JWT 签名失败。

    Note:
        生成 ECDSA 密钥对是耗时操作 (约 50ms), 在频繁登录场景下可复用
        ``private_key`` 参数。
    """
    # 1. 解析 sauth_json (容错: 解析失败不阻止登录)
    try:
        sauth_data = _parse_sauth_json(sauth_json)
    except ValueError as exc:
        logger.warning("sauth_json 解析失败, 使用空字典: %s", exc)
        sauth_data = {}

    # 2. 构造 ClientData
    client_data = _build_default_client_data(
        device_fingerprint=device_fingerprint,
        server_address=server_address,
        game_version=game_version,
        skin_data=skin_data,
    )

    # 3. 生成 ECDSA 密钥对 (或使用传入的私钥)
    if private_key is None:
        private_key = generate_ecdsa_keypair()
    public_key = private_key.public_key()
    x5u = marshal_public_key(public_key)

    # 4. 构造 IdentityData
    if identity_data is None:
        identity_data = _build_default_identity_data(
            sauth_data,
            display_name=str(device_fingerprint.get("display_name") or ""),
        )

    # 5. 构造 identity claims JWT (chain[0])
    identity_claims = _build_identity_claims(identity_data, x5u, issuer)
    identity_jwt = _sign_jwt(identity_claims, private_key, x5u)

    # 6. 构造 client data JWT (RawToken)
    client_data_payload = client_data.to_dict()
    client_data_jwt = _sign_jwt(client_data_payload, private_key, x5u)

    # 7. 组装 chain
    chain: list[str] = []
    if existing_chain:
        # 在线模式: 自签名 identityPublicKey JWT prepend 到链首
        # 此 JWT 只包含 identityPublicKey (指向原 chain[0] 的 x5u) 与时间 claims
        # 参考 gophertunnel Encode() 的实现
        identity_pub_claims = _build_time_claims()
        identity_pub_claims["identityPublicKey"] = x5u
        identity_pub_claims["certificateAuthority"] = True
        identity_pub_jwt = _sign_jwt(identity_pub_claims, private_key, x5u)
        chain.append(identity_pub_jwt)
        chain.extend(existing_chain)
    else:
        # 离线模式: chain 中只有 1 个 identity JWT
        chain.append(identity_jwt)

    # 8. 序列化为字节
    login_chain = LoginChain(
        chain=chain,
        clientData=client_data,
        clientDataJwt=client_data_jwt,
    )
    return login_chain.to_bytes()


# ----------------------------------------------------------------------
# 公开 API: parse_login_chain (反向解析, 调试用)
# ----------------------------------------------------------------------

def parse_login_chain(data: bytes) -> LoginChain:
    """解析 Bedrock ``Login`` 数据包的 ``ConnectionRequest`` 字段。

    反向操作 :func:`build_login_chain`, 用于调试与验证。

    Args:
        data: 序列化后的 ConnectionRequest 字节串。

    Returns:
        :class:`LoginChain` 实例。``clientData`` 字段会从 RawToken 中解码。

    Raises:
        ValueError: 数据不完整或格式错误。
    """
    if len(data) < 4:
        raise ValueError("数据过短, 至少需要 4 字节 chain 长度前缀")

    offset = 0
    # 1. chain JSON 长度
    (chain_json_len,) = struct.unpack_from("<i", data, offset)
    offset += 4
    if chain_json_len < 0 or offset + chain_json_len > len(data):
        raise ValueError(
            f"chain JSON 长度非法: {chain_json_len} (剩余 {len(data) - offset} 字节)"
        )

    chain_json_bytes = data[offset:offset + chain_json_len]
    offset += chain_json_len

    try:
        chain_json = json.loads(chain_json_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"chain JSON 解析失败: {exc}") from exc

    if not isinstance(chain_json, dict) or "chain" not in chain_json:
        raise ValueError("chain JSON 缺少 'chain' 字段")
    chain_list = chain_json["chain"]
    if not isinstance(chain_list, list):
        raise ValueError("'chain' 字段不是列表")

    # 2. RawToken 长度
    if offset + 4 > len(data):
        raise ValueError("数据不完整, 缺少 RawToken 长度前缀")
    (raw_token_len,) = struct.unpack_from("<i", data, offset)
    offset += 4
    if raw_token_len < 0 or offset + raw_token_len > len(data):
        raise ValueError(
            f"RawToken 长度非法: {raw_token_len} (剩余 {len(data) - offset} 字节)"
        )

    raw_token_bytes = data[offset:offset + raw_token_len]
    offset += raw_token_len
    client_data_jwt = raw_token_bytes.decode("utf-8")

    # 3. 解码 ClientData (不验证签名, 仅解析 payload)
    client_data = _decode_client_data_jwt(client_data_jwt)

    return LoginChain(
        chain=chain_list,
        clientData=client_data,
        clientDataJwt=client_data_jwt,
    )


def _decode_client_data_jwt(token: str) -> ClientData:
    """从 ClientData JWT (RawToken) 解码 payload, 不验证签名。

    Args:
        token: 紧凑序列化的 JWT 字符串。

    Returns:
        :class:`ClientData` 实例。

    Raises:
        ValueError: JWT 解析失败。
    """
    try:
        # options=["verify_signature"] 关闭签名验证 (仅用于调试解析)
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False},
        )
    except pyjwt.PyJWTError as exc:
        raise ValueError(f"ClientData JWT 解码失败: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("ClientData JWT payload 不是对象")

    # 仅取 ClientData 已知字段 (忽略未知字段)
    kwargs: dict[str, Any] = {}
    for f in ClientData.__dataclass_fields__:
        if f in payload:
            kwargs[f] = payload[f]
    return ClientData(**kwargs)


__all__ = [
    # 常量
    "ECDSA_CURVE",
    "JWT_ALGORITHM",
    "DEFAULT_JWT_LIFETIME",
    "DEFAULT_JWT_NOT_BEFORE_OFFSET",
    "DEFAULT_ISSUER",
    "DEFAULT_GAME_VERSION",
    "DEFAULT_GEOMETRY_ENGINE_VERSION",
    "DEFAULT_SKIN_RESOURCE_PATCH",
    "DEFAULT_SKIN_GEOMETRY",
    "DEFAULT_SKIN_WIDTH",
    "DEFAULT_SKIN_HEIGHT",
    "DEFAULT_LANGUAGE_CODE",
    "DEFAULT_ARM_SIZE",
    "DEFAULT_SKIN_COLOR",
    # 数据结构
    "IdentityData",
    "ClientData",
    "LoginChain",
    # 密钥
    "generate_ecdsa_keypair",
    "marshal_public_key",
    "parse_public_key",
    # 主函数
    "build_login_chain",
    "parse_login_chain",
]
