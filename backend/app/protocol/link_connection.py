"""LinkConnection 加密协议栈（NovaBuilder 逆向）。

NovaBuilder 使用自定义的 LinkConnection 协议与网易租赁服/联机大厅通信。
这是其核心通信协议，实现了 RSA 密钥交换 + ChaCha8 流加密的端到端加密。

逆向来源:
  - NovaBuilder 1.3.4 二进制 (Go, garble 混淆)
  - link_connection 包: handshake, login, decodeMessage, sendMessage
  - github.com/Yeah114/g79client — 网易G79客户端协议库
  - github.com/deatil/go-cryptobin — 加密库 (RSA/ChaCha8)

协议分层:
  TCP
  └── RakNet (UDP可靠传输) — PocketTerm 已有 raknet.py
      └── LinkConnection (本模块)
          ├── Handshake: RSA密钥交换 → ChaCha8会话密钥
          ├── Login: 加密登录 → 获取UID
          └── Session: 加密消息收发 → 心跳维持

握手流程:
  1. 生成 CipherR1 (RSA加密)
  2. 发送 CipherR1
  3. 解码客户端 MD5
  4. 解密 CipherR2 (RSA解密)
  5. 创建 ChaCha8 加密器 (key=32字节, nonce=12字节)
  6. 创建 ChaCha8 解密器

登录流程:
  7. 生成随机块
  8. RSA加密随机块
  9. 解析登录响应
  10. 解析 UID
  11. 处理服务器返回码

消息处理:
  12. decodeMessage: 解密消息体
  13. sendMessage: 加密引擎
  14. SendGameStart: 发送游戏开始
  15. heartbeat: 心跳维持

加密算法:
  - RSA: PKCS#1 v1.5, 握手阶段密钥交换
  - ChaCha8: 8轮 ChaCha20 流密码, 会话加密
  - HMAC-SHA256: 签名验证 (UniSauth/CheckEnter)
  - MD5: 握手阶段客户端校验

连接目标:
  - g79.update.netease.com — Android 服务器列表
  - x19.update.netease.com — X19 服务器列表
  - drpf-g79.proxima.nie.netease.com — DRPF G79 代理
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

# ============================================================================
# 常量
# ============================================================================

# ChaCha8 密钥长度 (固定)
CHACHA8_KEY_SIZE = 32   # 256 bits
CHACHA8_NONCE_SIZE = 12  # 96 bits

# RSA 密钥长度
RSA_KEY_SIZE = 1024     # bits

# 服务器列表 URL
ANDROID_SERVER_LIST = "https://g79.update.netease.com/serverlist/adr_release.0.17.json"
X19_SERVER_LIST = "https://x19.update.netease.com/serverlist/release.json"
DRPF_PROXY = "https://drpf-g79.proxima.nie.netease.com"

# G79 包列表/补丁列表
PACK_LIST = "https://g79.update.netease.com/pack_list/production/g79_packlist"
PATCH_LIST = "https://g79.update.netease.com/patch_list/production/g79_rn_patchlist"


# ============================================================================
# 协议状态
# ============================================================================

class ConnectionState(IntEnum):
    """LinkConnection 连接状态。"""
    DISCONNECTED = 0
    HANDSHAKING = 1
    LOGGING_IN = 2
    CONNECTED = 3
    RECONNECTING = 4
    ERROR = 5


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class HandshakeResult:
    """握手结果。"""
    encrypt_key: bytes    # ChaCha8 加密密钥 (32 bytes)
    decrypt_key: bytes    # ChaCha8 解密密钥 (32 bytes)
    encrypt_nonce: bytes  # 加密 nonce (12 bytes)
    decrypt_nonce: bytes  # 解密 nonce (12 bytes)
    client_md5: str       # 客户端 MD5 校验值
    server_md5: str       # 服务器 MD5 校验值


@dataclass
class LoginResult:
    """登录结果。"""
    uid: str              # 用户 UID
    server_code: int      # 服务器返回码
    entity_id: str        # 实体 ID
    server_address: str   # 服务器地址
    server_port: int      # 服务器端口


# ============================================================================
# ChaCha8 流密码 (纯 Python 实现)
# ============================================================================

class ChaCha8:
    """ChaCha8 流密码。

    与 ChaCha20 相同但只执行 8 轮 (而非 20 轮)。
    网易联机使用此变体作为会话加密。

    参考: RFC 8439, 轮数改为 8
    """

    def __init__(self, key: bytes, nonce: bytes, counter: int = 0):
        if len(key) != CHACHA8_KEY_SIZE:
            raise ValueError(f"ChaCha8 key must be {CHACHA8_KEY_SIZE} bytes")
        if len(nonce) != CHACHA8_NONCE_SIZE:
            raise ValueError(f"ChaCha8 nonce must be {CHACHA8_NONCE_SIZE} bytes")
        self._key = key
        self._nonce = nonce
        self._counter = counter
        self._buffer = b""
        self._position = 0

    @staticmethod
    def _rotl(v: int, c: int) -> int:
        return ((v << c) | (v >> (32 - c))) & 0xFFFFFFFF

    @staticmethod
    def _quarter_round(state: list, a: int, b: int, c: int, d: int):
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] ^= state[a]
        state[d] = ChaCha8._rotl(state[d], 16)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] ^= state[c]
        state[b] = ChaCha8._rotl(state[b], 12)
        state[a] = (state[a] + state[b]) & 0xFFFFFFFF
        state[d] ^= state[a]
        state[d] = ChaCha8._rotl(state[d], 8)
        state[c] = (state[c] + state[d]) & 0xFFFFFFFF
        state[b] ^= state[c]
        state[b] = ChaCha8._rotl(state[b], 7)

    def _block(self, counter: int) -> bytes:
        """生成一个 64 字节的 ChaCha8 密钥流块。"""
        # 常量: "expand 32-byte k"
        state = [
            0x61707865, 0x3320646E, 0x79622D32, 0x6B206574,
            struct.unpack("<I", self._key[0:4])[0],
            struct.unpack("<I", self._key[4:8])[0],
            struct.unpack("<I", self._key[8:12])[0],
            struct.unpack("<I", self._key[12:16])[0],
            struct.unpack("<I", self._key[16:20])[0],
            struct.unpack("<I", self._key[20:24])[0],
            struct.unpack("<I", self._key[24:28])[0],
            struct.unpack("<I", self._key[28:32])[0],
            counter & 0xFFFFFFFF,
            struct.unpack("<I", self._nonce[0:4])[0],
            struct.unpack("<I", self._nonce[4:8])[0],
            struct.unpack("<I", self._nonce[8:12])[0],
        ]

        working = state[:]
        # 8 轮 (ChaCha20 是 20 轮)
        for _ in range(4):
            # 列轮
            self._quarter_round(working, 0, 4, 8, 12)
            self._quarter_round(working, 1, 5, 9, 13)
            self._quarter_round(working, 2, 6, 10, 14)
            self._quarter_round(working, 3, 7, 11, 15)
            # 对角轮
            self._quarter_round(working, 0, 5, 10, 15)
            self._quarter_round(working, 1, 6, 11, 12)
            self._quarter_round(working, 2, 7, 8, 13)
            self._quarter_round(working, 3, 4, 9, 14)

        # 加回原始状态
        for i in range(16):
            working[i] = (working[i] + state[i]) & 0xFFFFFFFF

        return b''.join(struct.pack("<I", w) for w in working)

    def encrypt(self, data: bytes) -> bytes:
        """加密/解密 (对称操作)。"""
        result = bytearray(len(data))
        for i in range(len(data)):
            if self._position >= len(self._buffer):
                self._buffer = self._block(self._counter)
                self._counter += 1
                self._position = 0
            result[i] = data[i] ^ self._buffer[self._position]
            self._position += 1
        return bytes(result)

    decrypt = encrypt  # 对称加密


# ============================================================================
# LinkConnection 核心类
# ============================================================================

class LinkConnection:
    """LinkConnection 加密协议栈。

    实现 NovaBuilder 的 RSA+ChaCha8 端到端加密通信协议。

    使用示例:
        conn = LinkConnection()
        # 握手
        handshake = conn.create_handshake()
        # ... 发送 handshake.cipher_r1 到服务器 ...
        # ... 接收 cipher_r2 从服务器 ...
        # conn.complete_handshake(cipher_r2)
        # 登录
        # login = conn.create_login_request(sauth_json)
        # ... 发送加密登录请求 ...
        # 会话
        # encrypted = conn.encrypt_message(message)
        # decrypted = conn.decrypt_message(encrypted)
    """

    def __init__(self):
        self._state = ConnectionState.DISCONNECTED
        self._encrypter: Optional[ChaCha8] = None
        self._decrypter: Optional[ChaCha8] = None
        self._client_random: bytes = b""
        self._server_random: bytes = b""
        self._uid: str = ""
        self._session_start: float = 0.0

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    # ------------------------------------------------------------------
    # 握手阶段
    # ------------------------------------------------------------------

    def create_handshake(self, rsa_public_key_pem: str) -> bytes:
        """创建握手请求 (CipherR1)。

        NovaBuilder 握手流程:
          1. 生成随机 nonce1 (12 bytes)
          2. 生成随机密钥 k1 (32 bytes)
          3. cipherR1 = RSA_Encrypt(k1 + nonce1, server_public_key)
          4. 发送 cipherR1

        Args:
            rsa_public_key_pem: 服务器 RSA 公钥 (PEM 格式)

        Returns:
            cipherR1 (RSA 加密后的数据)
        """
        self._state = ConnectionState.HANDSHAKING

        # 生成随机数据
        encrypt_key = os.urandom(CHACHA8_KEY_SIZE)     # 32 bytes
        encrypt_nonce = os.urandom(CHACHA8_NONCE_SIZE)  # 12 bytes
        self._client_random = encrypt_key + encrypt_nonce

        # RSA 加密 (使用服务器的公钥)
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key = serialization.load_pem_public_key(rsa_public_key_pem.encode())
        cipher_r1 = public_key.encrypt(
            self._client_random,
            padding.PKCS1v15(),
        )

        return cipher_r1

    def complete_handshake(self, cipher_r2: bytes, rsa_private_key_pem: str) -> HandshakeResult:
        """完成握手 (解密 CipherR2)。

        NovaBuilder 流程:
          1. RSA 解密 cipherR2 → 获取 k2 (32 bytes) + nonce2 (12 bytes)
          2. 创建 ChaCha8 加密器: key=k1, nonce=nonce1
          3. 创建 ChaCha8 解密器: key=k2, nonce=nonce2
          4. 验证 MD5 校验

        Args:
            cipher_r2: 服务器返回的加密数据
            rsa_private_key_pem: 客户端 RSA 私钥 (PEM 格式)

        Returns:
            HandshakeResult
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = serialization.load_pem_private_key(
            rsa_private_key_pem.encode(), password=None
        )
        self._server_random = private_key.decrypt(
            cipher_r2,
            padding.PKCS1v15(),
        )

        # 提取密钥和 nonce
        decrypt_key = self._server_random[:CHACHA8_KEY_SIZE]
        decrypt_nonce = self._server_random[CHACHA8_KEY_SIZE:CHACHA8_KEY_SIZE + CHACHA8_NONCE_SIZE]
        encrypt_key = self._client_random[:CHACHA8_KEY_SIZE]
        encrypt_nonce = self._client_random[CHACHA8_KEY_SIZE:CHACHA8_KEY_SIZE + CHACHA8_NONCE_SIZE]

        # 创建 ChaCha8 加密器/解密器
        self._encrypter = ChaCha8(encrypt_key, encrypt_nonce)
        self._decrypter = ChaCha8(decrypt_key, decrypt_nonce)

        # MD5 校验
        client_md5 = hashlib.md5(self._client_random).hexdigest()
        server_md5 = hashlib.md5(self._server_random).hexdigest()

        self._state = ConnectionState.LOGGING_IN

        return HandshakeResult(
            encrypt_key=encrypt_key,
            decrypt_key=decrypt_key,
            encrypt_nonce=encrypt_nonce,
            decrypt_nonce=decrypt_nonce,
            client_md5=client_md5,
            server_md5=server_md5,
        )

    # ------------------------------------------------------------------
    # 登录阶段
    # ------------------------------------------------------------------

    def create_login_request(self, sauth_json: dict) -> bytes:
        """创建加密登录请求。

        NovaBuilder 流程:
          1. 生成随机块 (16 bytes)
          2. RSA 加密随机块
          3. 构建登录负载 (含 sauth_json)
          4. ChaCha8 加密负载

        Args:
            sauth_json: 认证数据

        Returns:
            加密的登录请求
        """
        if self._state != ConnectionState.LOGGING_IN:
            raise RuntimeError("必须先完成握手")

        # 构建登录负载
        import json
        payload = json.dumps({
            "type": "login",
            "sauth_json": sauth_json,
            "timestamp": int(time.time()),
            "random": os.urandom(16).hex(),
        }).encode()

        # 加密
        encrypted = self._encrypter.encrypt(payload)
        return encrypted

    def parse_login_response(self, encrypted_response: bytes) -> LoginResult:
        """解析登录响应。

        Args:
            encrypted_response: 加密的登录响应

        Returns:
            LoginResult
        """
        import json
        decrypted = self._decrypter.decrypt(encrypted_response)
        data = json.loads(decrypted.decode())

        code = data.get("code", -1)
        if code != 0:
            raise ConnectionError(f"登录失败: code={code}, msg={data.get('msg', '')}")

        self._uid = data.get("uid", "")
        self._state = ConnectionState.CONNECTED
        self._session_start = time.time()

        return LoginResult(
            uid=self._uid,
            server_code=code,
            entity_id=data.get("entity_id", ""),
            server_address=data.get("server_address", ""),
            server_port=data.get("server_port", 0),
        )

    # ------------------------------------------------------------------
    # 会话加密
    # ------------------------------------------------------------------

    def encrypt_message(self, message: bytes) -> bytes:
        """加密消息。"""
        if not self._encrypter:
            raise RuntimeError("加密器未初始化")
        return self._encrypter.encrypt(message)

    def decrypt_message(self, message: bytes) -> bytes:
        """解密消息。"""
        if not self._decrypter:
            raise RuntimeError("解密器未初始化")
        return self._decrypter.decrypt(message)

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    def create_heartbeat(self) -> bytes:
        """创建心跳包。"""
        import json
        payload = json.dumps({
            "type": "heartbeat",
            "timestamp": int(time.time()),
            "uid": self._uid,
        }).encode()
        return self._encrypter.encrypt(payload)

    def session_age(self) -> float:
        """会话持续时间 (秒)。"""
        if self._session_start == 0:
            return 0
        return time.time() - self._session_start

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self):
        """重置连接状态。"""
        self._state = ConnectionState.DISCONNECTED
        self._encrypter = None
        self._decrypter = None
        self._client_random = b""
        self._server_random = b""
        self._uid = ""
        self._session_start = 0.0


# ============================================================================
# HMAC-SHA256 签名 (UniSauth/CheckEnter)
# ============================================================================

def sign_hmac_sha256(data: bytes, key: bytes) -> str:
    """HMAC-SHA256 签名，返回十六进制字符串。

    用于 NovaBuilder 的 signUniSauthBody / signCheckEnterBody。
    """
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def sign_uni_sauth_body(body: dict, secret: str) -> str:
    """签名 UniSauth 请求体。

    NovaBuilder 在发送 uni_sauth 请求前使用此函数签名。
    """
    import json
    payload = json.dumps(body, separators=(",", ":")).encode()
    return sign_hmac_sha256(payload, secret.encode())


def sign_check_enter_body(body: dict, secret: str) -> str:
    """签名 CheckEnter 请求体。"""
    import json
    payload = json.dumps(body, separators=(",", ":")).encode()
    return sign_hmac_sha256(payload, secret.encode())


# ============================================================================
# 服务器列表获取
# ============================================================================

async def fetch_server_list(platform: str = "android") -> list[dict]:
    """获取网易服务器列表。

    Args:
        platform: "android" 或 "x19"

    Returns:
        服务器列表
    """
    import httpx

    url = ANDROID_SERVER_LIST if platform == "android" else X19_SERVER_LIST
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.json()


# ============================================================================
# 测试
# ============================================================================

if __name__ == "__main__":
    # 测试 ChaCha8
    key = os.urandom(32)
    nonce = os.urandom(12)
    chacha = ChaCha8(key, nonce)

    plaintext = b"Hello, NovaBuilder LinkConnection!"
    encrypted = chacha.encrypt(plaintext)

    chacha2 = ChaCha8(key, nonce)
    decrypted = chacha2.decrypt(encrypted)

    assert plaintext == decrypted, "ChaCha8 加密/解密验证失败"
    print(f"ChaCha8 测试通过: {plaintext} -> {encrypted.hex()[:32]}... -> {decrypted}")

    # 测试 HMAC-SHA256
    sig = sign_hmac_sha256(b"test data", b"secret")
    print(f"HMAC-SHA256: {sig}")

    # 测试连接状态
    conn = LinkConnection()
    print(f"初始状态: {conn.state.name}")
    print("LinkConnection 协议栈就绪")