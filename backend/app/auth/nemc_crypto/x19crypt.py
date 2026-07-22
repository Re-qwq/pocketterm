"""x19Crypt 核心加密模块.

从 ``Mark/x19Crypt.cs`` 完整移植.

提供网易 Minecraft 中国版 HTTP API 使用的加密/解密和动态令牌计算:

- :func:`HttpEncrypt_g79v12` — 加密 HTTP 请求体
- :func:`HttpDecrypt_g79v12` — 解密 HTTP 响应体
- :func:`ComputeDynamicToken` — 计算动态用户令牌

加密流程
--------
1. 将明文 + 16 字节随机 ASCII 填充至 16 字节倍数
2. 生成随机密钥索引字节 (高 4 位 = 密钥索引, 低 4 位 = 0xC)
3. 从 16 个 hex 密钥中选取对应密钥 (16 字节)
4. 生成随机 16 字节 IV
5. AES-CBC 加密 (无自动填充, 数据已对齐)
6. 输出 = IV(16) + 密文 + 密钥索引(1)

解密流程
--------
1. 从最后 1 字节取密钥索引
2. 取前 16 字节作为 IV
3. AES-CBC 解密中间部分
4. 从尾部扫描 16 个非零字节, 去除随机填充
"""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
from typing import List

from Crypto.Cipher import AES

__all__ = [
    "PickKey",
    "PickKey_g79v3",
    "PickKey_g79v12",
    "HttpEncrypt",
    "HttpDecrypt",
    "ParseLoginResponse",
    "HttpEncrypt_g79v3",
    "ParseLoginResponse_g79v3",
    "HttpEncrypt_g79v12",
    "HttpDecrypt_g79v12",
    "ParseLoginResponse_g79v12",
    "ComputeDynamicToken",
    "DecryptModJson",
]

#: 随机字符串字符集.
_RAND_RUNES: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: 动态令牌盐值.
_TOKEN_SALT: str = "0eGsBkhl"

#: 第一组密钥 (16 个 ASCII 密钥, 每个 16 字节).
_keys: List[str] = [
    "MK6mipwmOUedplb6", "OtEylfId6dyhrfdn", "VNbhn5mvUaQaeOo9",
    "bIEoQGQYjKd02U0J", "fuaJrPwaH2cfXXLP", "LEkdyiroouKQ4XN1",
    "jM1h27H4UROu427W", "DhReQada7gZybTDk", "ZGXfpSTYUvcdKqdY",
    "AZwKf7MWZrJpGR5W", "amuvbcHw38TcSyPU", "SI4QotspbjhyFdT0",
    "VP4dhjKnDGlSJtbB", "UXDZx4KhZywQ2tcn", "NIK73ZNvNqzva4kd",
    "WeiW7qU766Q1YQZI",
]

#: 第二组密钥 (g79v3, 16 个 ASCII 密钥).
_keys_g79v3: List[str] = [
    "75yWE1DMlhP6JZre", "NtDdtr7zaCO7MGqK", "5P3gbvwC2x2qVsXK",
    "Qgg0y2foklzV8W2P", "ItCyfnGMte15pFXe", "bp8UGVtOcS4Cc0VS",
    "ZRoxt2LItMBL2Rko", "EyVV2FUOWSU3pfEE", "L9molWm6kVuE6c6m",
    "oPDdpwvjN2YgZzE8", "K5rvy5Jb2S1J4SpX", "IYDhVUqFPlVjA7to",
    "LCR32BrjIVqkaYbS", "RWAss9Mri8bThLgF", "cdxDfuavFR1Frds5",
    "euKUQqtpUkUKF5aY",
]

#: 第三组密钥 (g79v12, 16 个 hex 密钥, 每个 16 字节).
_keys_g79v12: List[str] = [
    "60F1E0D1FD635362430747215CF1C2FF", "EA5B62D27D0338374852C4B9469D7AC6",
    "17238D55501C5F020B155FB3303591E6", "8C5CEAE0F443E006A050266F73ADD5B0",
    "1C02CE22FB22F0E72060217418F351F3", "9A01773FEBB0CFE0EBDBF37F4D23C27F",
    "43F32300BF252CC320E2572ACE766367", "07F161011B3101F1ED0301735631E734",
    "0454E7707A5F37565601E100406060AF", "647554BAD3100C43C16660F002CC10F3",
    "E157213170F842382032564265B0B043", "914FC59311B04151393EF6896A847636",
    "0710C0205D224237025323265C145FA1", "054E6F01165267025C3111F562A921E9",
    "722D1789E792E2CA0D5322211FD0F5AE", "91F7C751FCF671F34943430772341799",
]


def _rand_runes(length: int) -> bytes:
    """生成指定长度的随机 ASCII 字节."""
    return bytes(
        ord(secrets.choice(_RAND_RUNES))
        for _ in range(length)
    )


def PickKey(query: int) -> bytes:
    """从第一组密钥中选取密钥.

    取 ``query`` 的高 4 位作为索引 (0-15).

    Parameters
    ----------
    query
        密钥索引字节.

    Returns
    -------
    bytes
        16 字节 ASCII 密钥.
    """
    return _keys[(query >> 4) & 0xF].encode("utf-8")


def PickKey_g79v3(query: int) -> bytes:
    """从第二组密钥 (g79v3) 中选取密钥."""
    return _keys_g79v3[(query >> 4) & 0xF].encode("utf-8")


def PickKey_g79v12(query: int) -> bytes:
    """从第三组密钥 (g79v12) 中选取密钥.

    Parameters
    ----------
    query
        密钥索引字节 (高 4 位为索引).

    Returns
    -------
    bytes
        16 字节二进制密钥 (从 hex 解码).
    """
    return bytes.fromhex(_keys_g79v12[(query >> 4) & 0xF])


def _manual_pad(body_in: bytes) -> bytes:
    """手动填充数据至 16 字节倍数.

    在原始数据后追加 16 字节随机 ASCII 字符, 不足 16 倍数的部分用
    零字节填充.
    """
    target_len = math.ceil((len(body_in) + 16) / 16.0) * 16
    array = bytearray(target_len)
    array[0:len(body_in)] = body_in
    random_bytes = _rand_runes(16)
    array[len(body_in):len(body_in) + 16] = random_bytes
    return bytes(array)


def _strip_manual_padding(data: bytes) -> bytes:
    """从尾部扫描去除 16 字节随机填充.

    从后向前扫描, 跳过零字节, 计数 16 个非零字节后截取.
    """
    num = 0
    num2 = len(data) - 1
    while num < 16 and num2 >= 0:
        if data[num2] != 0:
            num += 1
        num2 -= 1
    return data[:num2 + 1]


def _aes_cbc_encrypt_raw(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 加密 (无自动填充, 数据须为 16 字节倍数).

    对应 C# ``TransformBlock`` 行为.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(data)


def _aes_cbc_decrypt_raw(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 解密 (无自动去填充).

    对应 C# ``TransformBlock`` 行为.
    """
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)


def HttpEncrypt(body_in: bytes) -> bytes:
    """HTTP 请求体加密 (第一组密钥).

    对应 C# ``x19Crypt.HttpEncrypt``.

    Parameters
    ----------
    body_in
        待加密的明文数据.

    Returns
    -------
    bytes
        加密后的数据: IV(16) + 密文 + 密钥索引(1).
    """
    try:
        padded = _manual_pad(body_in)
        # 密钥索引: 高 4 位随机 0-15, 低 4 位 = 2.
        idx = secrets.randbelow(16)
        b = (idx << 4) | 2
        key = PickKey(b)
        iv = _rand_runes(16)
        encrypted = _aes_cbc_encrypt_raw(padded, key, iv)
        return iv + encrypted + bytes([b])
    except Exception:
        return b""


def HttpDecrypt(body: bytes) -> str:
    """HTTP 响应体解密 (第一组密钥).

    对应 C# ``x19Crypt.ParseLoginResponse``.
    """
    if len(body) < 18:
        raise ValueError("输入数据太短")
    key_index = body[-1]
    key = PickKey(key_index)
    iv = body[:16]
    ciphertext = body[16:-1]
    decrypted = _aes_cbc_decrypt_raw(ciphertext, key, iv)
    result = _strip_manual_padding(decrypted)
    return result.decode("utf-8")


#: ``ParseLoginResponse`` 是 ``HttpDecrypt`` 的别名, 对应 C#
#: ``CoreNative.ParseLoginResponse``. 保持与 nemc_client 调用一致.
ParseLoginResponse = HttpDecrypt


def HttpEncrypt_g79v3(body_in: bytes) -> bytes:
    """HTTP 请求体加密 (g79v3 密钥组).

    对应 C# ``x19Crypt.HttpEncrypt_g79v3``.
    """
    try:
        padded = _manual_pad(body_in)
        idx = secrets.randbelow(16)
        b = (idx << 4) | 3
        key = PickKey_g79v3(b)
        iv = _rand_runes(16)
        encrypted = _aes_cbc_encrypt_raw(padded, key, iv)
        return iv + encrypted + bytes([b])
    except Exception:
        return b""


def ParseLoginResponse_g79v3(body: bytes) -> str:
    """登录响应解析 (g79v3).

    对应 C# ``x19Crypt.ParseLoginResponse_g79v3``.
    """
    if len(body) < 18:
        raise ValueError("输入数据太短")
    key_index = body[-1]
    key = PickKey_g79v3(key_index)
    iv = body[:16]
    ciphertext = body[16:-1]
    decrypted = _aes_cbc_decrypt_raw(ciphertext, key, iv)
    result = _strip_manual_padding(decrypted)
    return result.decode("utf-8")


def HttpEncrypt_g79v12(body_in: bytes) -> bytes:
    """HTTP 请求体加密 (g79v12 密钥组).

    对应 C# ``x19Crypt.HttpEncrypt_g79v12``.

    加密步骤:
    1. 将明文 + 16 字节随机 ASCII 填充至 16 字节倍数
    2. 随机选择密钥索引 (高 4 位 0-15, 低 4 位 = 0xC)
    3. 从 16 个 hex 密钥中选取 16 字节密钥
    4. 生成 16 字节随机 IV
    5. AES-CBC 加密
    6. 输出 = IV(16) + 密文 + 索引(1)

    Parameters
    ----------
    body_in
        待加密的明文数据.

    Returns
    -------
    bytes
        加密数据: IV(16) + 密文 + 密钥索引(1).
    """
    try:
        padded = _manual_pad(body_in)
        # 密钥索引: 高 4 位随机 0-15, 低 4 位 = 0xC.
        idx = secrets.randbelow(16)
        b = (idx << 4) | 0xC
        key = PickKey_g79v12(b)
        iv = _rand_runes(16)
        encrypted = _aes_cbc_encrypt_raw(padded, key, iv)
        return iv + encrypted + bytes([b])
    except Exception:
        return b""


def HttpDecrypt_g79v12(body: bytes) -> bytes:
    """HTTP 响应体解密 (g79v12 密钥组).

    对应 C# ``x19Crypt.HttpDecrypt_g79v12``.

    解密步骤:
    1. 从最后 1 字节取密钥索引
    2. 前 16 字节为 IV
    3. 中间部分为 AES-CBC 密文
    4. 解密后从尾部去除 16 字节随机填充

    Parameters
    ----------
    body
        加密数据: IV(16) + 密文 + 索引(1).

    Returns
    -------
    bytes
        解密后的原始数据.
    """
    if len(body) < 18:
        raise ValueError("输入数据太短")
    key_index = body[-1]
    key = PickKey_g79v12(key_index)
    iv = body[:16]
    ciphertext = body[16:-1]
    decrypted = _aes_cbc_decrypt_raw(ciphertext, key, iv)
    result = _strip_manual_padding(decrypted)
    return result


def ParseLoginResponse_g79v12(body: bytes) -> str:
    """登录响应解析 (g79v12, 不去随机填充).

    对应 C# ``x19Crypt.ParseLoginResponse_g79v12``.
    """
    if len(body) < 18:
        raise ValueError("输入数据太短")
    key_index = body[-1]
    key = PickKey_g79v12(key_index)
    iv = body[:16]
    ciphertext = body[16:-1]
    decrypted = _aes_cbc_decrypt_raw(ciphertext, key, iv)
    return decrypted.decode("utf-8")


def ComputeDynamicToken(url: str, body: str, token: str) -> str:
    """计算动态用户令牌.

    对应 C# ``x19Crypt.ComputeDynamicToken``.

    算法:
    1. MD5(token) → hex
    2. 拼接: md5_hex + body + "0eGsBkhl" + url(去尾部?)
    3. MD5(拼接结果) → hex → UTF-8 bytes (32 字节)
    4. 转 256 位二进制, 循环左移 6 位
    5. 每字节: 反转对应 8 位, 与原字节异或
    6. Base64 取前 16 字符, 替换 +→m /→o, 追加 "1"

    Parameters
    ----------
    url
        请求路径 (如 ``/online-lobby-room-enter``).
    body
        请求体内容.
    token
        用户令牌 (LoginSRCToken).

    Returns
    -------
    str
        17 字符的动态令牌.
    """
    # Step 1: MD5(token) hex.
    md5_token = hashlib.md5(token.encode("utf-8")).hexdigest()

    # Step 2: 拼接.
    combined = md5_token + body + _TOKEN_SALT + url.rstrip("?")

    # Step 3: MD5(combined) hex → bytes.
    md5_combined = hashlib.md5(combined.encode("utf-8")).hexdigest()
    data_bytes = md5_combined.encode("utf-8")  # 32 字节

    # Step 4: 转二进制 (256 位), 循环左移 6 位.
    binary_str = "".join(format(b, "08b") for b in data_bytes)
    rotated = binary_str[6:] + binary_str[:6]

    # Step 5: 每字节反转 8 位后与原字节异或.
    result = bytearray(len(data_bytes))
    for i in range(len(data_bytes)):
        bits = rotated[i * 8: i * 8 + 8]
        reversed_byte = 0
        for j in range(8):
            if bits[7 - j] == "1":
                reversed_byte |= (1 << j)
        result[i] = reversed_byte ^ data_bytes[i]

    # Step 6: Base64 编码, 取前 16 字符, 替换, 追加 "1".
    b64 = base64.b64encode(bytes(result)).decode("ascii")
    token_str = b64[:16].replace("+", "m").replace("/", "o")
    return token_str + "1"


def DecryptModJson(array: bytes, key: bytes, uuid: str) -> bytes:
    """解密 ModJson 文件.

    对应 C# ``x19Crypt.DecryptModJson``.

    Parameters
    ----------
    array
        加密数据 (前 4 字节跳过, 4-40 字节为 UUID).
    key
        AES 密钥.
    uuid
        预期 UUID 字符串 (36 字符).

    Returns
    -------
    bytes
        解密后的数据, 如果 UUID 不匹配返回 ``None``.
    """
    from .aes_helper import AES_CFB_Decrypt

    uuid_bytes = uuid.encode("ascii")
    if array[4:40] != uuid_bytes:
        return None

    data = array[64:]

    if len(data) % 16 == 0:
        return AES_CFB_Decrypt(data, key, key)

    if len(data) < 16:
        pad_len = 16 - len(data)
        padded = data + b"\x00" * pad_len
        result = AES_CFB_Decrypt(padded, key, key)
        return result[:len(data)]

    pad_len = 16 - (len(data) % 16)
    padded = data + b"\x00" * pad_len
    result = AES_CFB_Decrypt(padded, key, key)
    return result[:len(data)]
