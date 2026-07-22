"""AES 加密辅助模块.

从 ``Noya.LocalServer.Common.Cryptography/AESHelper.cs`` 移植.

提供 AES-CBC / AES-ECB / AES-CFB 加解密, 以及 MclNetClient 使用的
128 位 AES (AESEncrypt128Ex / AESDecrypt128Ex) 工具函数.

注意
----
C# 原版 ``AES_CBC_Encrypt`` / ``AES_CBC_Decrypt`` 使用 ``TransformBlock``
(不做填充), 但此处按用户规格使用 **PKCS7** 填充, 以便处理非对齐数据.
``x19crypt`` 模块在调用前已手动将数据补齐至 16 字节倍数, PKCS7 会额外
追加一个完整块, 解密时再移除, 因此 roundtrip 不受影响.

对于 MclNetClient 的 token 加解密 (AESEncrypt128Ex / AESDecrypt128Ex),
使用标准的 AES-128-CBC + PKCS7 填充.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Union

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

__all__ = [
    "AES_CBC_Encrypt",
    "AES_CBC_Decrypt",
    "AES_ECB_Encrypt",
    "AES_ECB_Decrypt",
    "AES_CFB_Decrypt",
    "AESEncrypt128Ex",
    "AESDecrypt128Ex",
    "BytesToHex",
    "HexToBytes",
    "mclnet_get_encrypt_token",
    "mclnet_get_decrypt_token",
]

#: AES 块大小 (字节).
AES_BLOCK_SIZE: int = 16

#: MclNetClient token 加解密使用的固定密钥 (UTF-8 ASCII).
_MCLNET_TOKEN_KEY: bytes = b"debbde3548928fab"

#: MclNetClient token 加解密使用的固定 IV.
_MCLNET_TOKEN_IV: bytes = b"afd4c5c5a7c456a1"

#: 随机字符串字符集 (字母+数字).
_RAND_RUNES: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def AES_CBC_Encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 加密 (PKCS7 填充).

    对应 C# ``AESHelper.AES_CBC_Encrypt``.

    Parameters
    ----------
    data
        待加密数据.
    key
        AES 密钥 (16 / 24 / 32 字节).
    iv
        初始向量 (16 字节).

    Returns
    -------
    bytes
        加密后的数据.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(data, AES_BLOCK_SIZE)
    return cipher.encrypt(padded)


def AES_CBC_Decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 解密 (PKCS7 去填充).

    对应 C# ``AESHelper.AES_CBC_Decrypt``.

    Parameters
    ----------
    data
        待解密数据.
    key
        AES 密钥.
    iv
        初始向量.

    Returns
    -------
    bytes
        解密后的数据.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(data)
    return unpad(decrypted, AES_BLOCK_SIZE)


def AES_ECB_Encrypt(data: bytes, key: bytes) -> bytes:
    """AES-ECB 加密 (PKCS7 填充).

    对应 C# ``AESHelper.AES_ECB_Encrypt``.

    Parameters
    ----------
    data
        待加密数据.
    key
        AES 密钥.

    Returns
    -------
    bytes
        加密后的数据.
    """
    cipher = AES.new(key, AES.MODE_ECB)
    padded = pad(data, AES_BLOCK_SIZE)
    return cipher.encrypt(padded)


def AES_ECB_Decrypt(data: bytes, key: bytes) -> bytes:
    """AES-ECB 解密 (PKCS7 去填充).

    Parameters
    ----------
    data
        待解密数据.
    key
        AES 密钥.

    Returns
    -------
    bytes
        解密后的数据.
    """
    cipher = AES.new(key, AES.MODE_ECB)
    decrypted = cipher.decrypt(data)
    return unpad(decrypted, AES_BLOCK_SIZE)


def AES_CFB_Decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CFB 解密 (Zeros 填充, CFB-8 模式).

    对应 C# ``AESHelper.AES_CFB_Decrypt``.

    .NET 的 ``CipherMode.CFB`` 默认使用 8 位反馈 (CFB-8).
    pycryptodome 的 ``segment_size=8`` (位) 与之一致.

    Parameters
    ----------
    data
        待解密数据.
    key
        AES 密钥.
    iv
        初始向量.

    Returns
    -------
    bytes
        解密后的数据.
    """
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=8)
    return cipher.decrypt(data)


def AESEncrypt128Ex(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 加密 (PKCS7 填充), 用于 MclNetClient.

    对应 C# ``AESHelper.AESEncrypt128Ex``. 使用 128 位密钥 (16 字节),
    PKCS7 填充, 输出长度为 ``ceil(len(data)/16)*16``.

    Parameters
    ----------
    data
        待加密数据.
    key
        16 字节密钥.
    iv
        16 字节初始向量.

    Returns
    -------
    bytes
        加密后的数据.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(data, AES_BLOCK_SIZE)
    return cipher.encrypt(padded)


def AESDecrypt128Ex(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密 (PKCS7 去填充), 用于 MclNetClient.

    对应 C# ``AESHelper.AESDecrypt128Ex``.

    Parameters
    ----------
    data
        待解密数据.
    key
        16 字节密钥.
    iv
        16 字节初始向量.

    Returns
    -------
    bytes
        解密后的数据.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(data)
    return unpad(decrypted, AES_BLOCK_SIZE)


def BytesToHex(data: bytes, to_upper: bool = False) -> str:
    """将字节数组转换为十六进制字符串.

    对应 C# ``ByteArrayExtensions.ToHex``.

    Parameters
    ----------
    data
        字节数据.
    to_upper
        是否输出大写十六进制.

    Returns
    -------
    str
        十六进制字符串.
    """
    return data.hex().upper() if to_upper else data.hex()


def HexToBytes(hex_str: str) -> bytes:
    """将十六进制字符串转换为字节数组.

    对应 C# ``StringExtensions.HexToBytes``.

    Parameters
    ----------
    hex_str
        十六进制字符串.

    Returns
    -------
    bytes
        字节数据.
    """
    if not hex_str:
        return b""
    return bytes.fromhex(hex_str)


def _get_random_string(length: int) -> str:
    """生成指定长度的随机字母数字字符串.

    对应 C# ``MclNetClient.GetRandomString``.
    """
    return "".join(secrets.choice(_RAND_RUNES) for _ in range(length))


def mclnet_get_encrypt_token(token: str) -> str:
    """MclNetClient token 加密.

    对应 C# ``MclNetClient.GetEncryptToken``.

    在 token 前后各添加 8 个随机字符, 然后用 AES-128-CBC 加密
    (key=``debbde3548928fab``, iv=``afd4c5c5a7c456a1``), 输出十六进制.

    Parameters
    ----------
    token
        原始 token 字符串.

    Returns
    -------
    str
        加密后的十六进制字符串.
    """
    prefix = _get_random_string(8)
    suffix = _get_random_string(8)
    plaintext = (prefix + token + suffix).encode("ascii")
    encrypted = AESEncrypt128Ex(plaintext, _MCLNET_TOKEN_KEY, _MCLNET_TOKEN_IV)
    return BytesToHex(encrypted)


def mclnet_get_decrypt_token(d_token: str) -> str:
    """MclNetClient token 解密.

    对应 C# ``MclNetClient.GetDecryptToken``.

    将十六进制字符串解码, 用 AES-128-CBC 解密, 然后跳过前 8 字节随机
    前缀和后 8 字节随机后缀, 取出原始 token.

    .. note::
        C# 原版使用 ``Skip(8).Take(16)`` 硬编码取 16 字节, 此处改为
        ``data[8:-8]`` 以支持任意长度的 token (16 字节 token 结果一致).

    Parameters
    ----------
    d_token
        加密后的十六进制字符串.

    Returns
    -------
    str
        原始 token 字符串.
    """
    raw = HexToBytes(d_token)
    decrypted = AESDecrypt128Ex(raw, _MCLNET_TOKEN_KEY, _MCLNET_TOKEN_IV)
    # 跳过前 8 字节随机前缀, 去掉后 8 字节随机后缀.
    token_bytes = decrypted[8:-8]
    return token_bytes.decode("ascii")
