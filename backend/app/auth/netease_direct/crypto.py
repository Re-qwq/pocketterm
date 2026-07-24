"""网易认证加密模块 - AES-CBC 加密/解密 + 动态令牌计算。

从 Login.Core.dll 反编译实现:
- HttpEncrypt / HttpDecrypt: AES-128-CBC,密钥由末尾字节选择
- ComputeDynamicToken: MD5 + 二进制旋转 + XOR,用于 API 网关认证

加密格式 (来自 IL 代码分析):
    [16字节IV] [AES-CBC加密数据] [1字节密钥选择器]
    
    加密前数据 = 原始数据 + 16字节随机ASCII字符 + 零填充到块大小对齐
    
    解密时: 从末尾向前扫描,跳过零字节,找到16个非零字节(随机填充),
    取这16个非零字节之前的所有数据作为原始数据。
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
import string
from typing import Optional

from .constants import KEYS, KEYS_G79V3, KEYS_G79V12, DYNAMIC_TOKEN_SALT

# ---------------------------------------------------------------------------
# AES-CBC 加密/解密
# ---------------------------------------------------------------------------

# BUG-2.6 修复: 不同密钥集使用不同的标志字节后缀
# 来源: InfBotLobby openssl_.h 和 nemc_crypto/x19crypt.py 交叉验证
#   - x19 (KEYS):         后缀 0x02
#   - g79v3 (KEYS_G79V3): 后缀 0x03
#   - g79v12 (KEYS_G79V12): 后缀 0x0C
# 之前所有密钥集统一使用 0x0C, 导致 x19 加密的标志字节错误
KEY_SELECTOR_SUFFIX_X19: int = 0x02
KEY_SELECTOR_SUFFIX_G79V3: int = 0x03
KEY_SELECTOR_SUFFIX_G79V12: int = 0x0C

def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR 两个等长字节串"""
    return bytes(x ^ y for x, y in zip(a, b))


def _aes_cbc_encrypt(key: bytes, data: bytes, iv: bytes) -> bytes:
    """AES-CBC 加密"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_cbc_decrypt(key: bytes, data: bytes, iv: bytes) -> bytes:
    """AES-CBC 解密"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend(),
    )
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _pick_key(key_selector: int, key_set: Optional[list[str]] = None) -> bytes:
    """根据密钥选择字节获取 AES 密钥

    密钥选择逻辑 (来自 IL 代码 PickKey / PickKey_g79v3 / PickKey_g79v12):
        index = (key_selector >> 4) & 0x0F  (高4位)

    密钥编码方式:
        - KEYS / KEYS_G79V3: UTF-8 编码 (ASCII 字符串 → 16字节 → AES-128)
        - KEYS_G79V12: Hex 解码 (32字符hex → 16字节 → AES-128)

    Args:
        key_selector: 密钥选择字节 (末尾字节)
        key_set: 密钥集合 (默认使用 KEYS)

    Returns:
        AES 密钥字节
    """
    if key_set is None:
        key_set = KEYS
    index = (key_selector >> 4) & 0x0F
    key_str = key_set[index]

    # g79v12 密钥是 hex 编码的，需要用 HexToBytes 转换 (来自 IL: PickKey_g79v12)
    # P1 修复: 用 == 比较列表内容而非 is 比较身份 (is 对不同导入路径的同一列表可能失败)
    if key_set == KEYS_G79V12:
        return bytes.fromhex(key_str)
    # 普通密钥和 g79v3 密钥用 UTF-8 编码 (来自 IL: PickKey / PickKey_g79v3)
    return key_str.encode("utf-8")


def _rand_ascii_string(length: int = 16) -> bytes:
    """生成随机 ASCII 字符串 (用于 IV 和填充)"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length)).encode("ascii")


def http_encrypt(body: bytes, key_set: Optional[list[str]] = None) -> bytes:
    """HttpEncrypt - 加密 HTTP 请求体

    格式 (来自 IL 代码 HttpEncrypt):
        1. 计算 padded_size = ceil((len(body) + 16) / 16) * 16
        2. 创建 padded_size 字节数组 (默认全零)
        3. 复制 body 到开头
        4. 在 body 之后填充 16 字节随机 ASCII 字符
        5. 剩余位置为零 (已由 bytearray 初始化)
        6. 生成随机 16 字节 ASCII IV
        7. 生成随机密钥选择器: (rand(0,15) << 4) | 2
        8. AES-CBC 加密 padded 数据
        9. 输出: [IV (16)] [encrypted] [key_selector (1)]

    Args:
        body: 要加密的原始数据 (UTF-8 字节)
        key_set: 密钥集合 (默认 KEYS)

    Returns:
        加密后的字节串
    """
    if key_set is None:
        key_set = KEYS

    # BUG-2.5 修复: import math 已移至模块顶部, 避免每次调用都执行 import 语句。
    body_len = len(body)

    # 1. 计算对齐大小
    padded_size = math.ceil((body_len + 16) / 16.0) * 16

    # 2. 创建填充数组 (全零)
    padded = bytearray(padded_size)

    # 3. 复制 body 到开头
    padded[:body_len] = body

    # 4. 在 body 之后填充 16 字节随机 ASCII 字符
    random_padding = _rand_ascii_string(16)
    for i in range(16):
        if body_len + i < padded_size:
            padded[body_len + i] = random_padding[i]

    # 5. 剩余位置已经是零 (bytearray 默认)

    # 6. 生成随机 IV (16 字节 ASCII)
    iv = _rand_ascii_string(16)

    # 7. 生成随机密钥选择器
    # BUG 修复: x19 用 0x02, g79v3 用 0x03, g79v12 用 0x0C
    # 来源: InfBotLobby openssl_.h + nemc_crypto/x19crypt.py 交叉验证
    key_index = random.randint(0, 15)
    if key_set == KEYS_G79V12:
        key_selector = (key_index << 4) | KEY_SELECTOR_SUFFIX_G79V12
    elif key_set == KEYS_G79V3:
        key_selector = (key_index << 4) | KEY_SELECTOR_SUFFIX_G79V3
    else:
        key_selector = (key_index << 4) | KEY_SELECTOR_SUFFIX_X19

    # 8. 获取密钥并加密 (g79v12 用 hex 解码, 其他用 UTF-8)
    key_str = key_set[key_index]
    # P1 修复: 用 == 比较列表内容而非 is 比较身份
    if key_set == KEYS_G79V12:
        key = bytes.fromhex(key_str)
    else:
        key = key_str.encode("utf-8")
    encrypted = _aes_cbc_encrypt(key, bytes(padded), iv)

    # 9. 构造输出: [IV] [encrypted] [key_selector]
    return iv + encrypted + bytes([key_selector])


def http_decrypt(body: bytes, key_set: Optional[list[str]] = None) -> bytes:
    """HttpDecrypt - 解密 HTTP 响应体

    格式 (来自 IL 代码 HttpDecrypt):
        1. 从末尾字节获取密钥选择器
        2. 提取 IV (前16字节)
        3. 提取加密数据 (中间部分, len-17 字节)
        4. AES-CBC 解密
        5. 从末尾向前扫描:
           - 跳过零字节
           - 计数非零字节,直到找到16个
           - 取这16个非零字节之前的所有数据

    Args:
        body: 加密数据
        key_set: 密钥集合 (默认 KEYS)

    Returns:
        解密后的原始字节
    """
    if key_set is None:
        key_set = KEYS

    if len(body) < 33:
        # BUG-2.2 修复: 之前 < 18 只检查了 IV(16) + selector(1), 没有确保
        # 至少有一个 AES 加密块(16)。有效密文至少需要 16(IV) + 16(一个AES块)
        # + 1(selector) = 33 字节。len(body) - 17 也必须是 16 的倍数。
        raise ValueError("Input body too short")
    if (len(body) - 17) % 16 != 0:
        raise ValueError("Encrypted data length is not a multiple of AES block size")

    # 1. 从末尾获取密钥选择器
    key_selector = body[-1]
    key = _pick_key(key_selector, key_set)

    # 2. 提取 IV (前16字节)
    iv = body[:16]

    # 3. 提取加密数据 (跳过前16字节IV,去掉末尾1字节选择器)
    encrypted = body[16:-1]

    # 4. AES-CBC 解密
    decrypted = _aes_cbc_decrypt(key, encrypted, iv)

    # 5. 从末尾向前扫描,找到16个非零字节的位置
    #    (这些是随机填充的ASCII字符,之前是零填充,再之前是原始数据)
    count = 0  # 非零字节计数
    pos = len(decrypted) - 1  # 从末尾开始

    while count < 16 and pos >= 0:
        if decrypted[pos] > 0:
            count += 1
        pos -= 1

    # pos 现在指向第16个非零字节的前一个位置
    # 原始数据是 decrypted[0 : pos+1]
    result_end = pos + 1

    # 如果找不到16个非零字节,返回全部解密数据
    if count < 16:
        result_end = len(decrypted)

    return decrypted[:result_end]


def http_encrypt_g79v3(body: bytes) -> bytes:
    """g79 v3 版加密"""
    return http_encrypt(body, key_set=KEYS_G79V3)


def http_decrypt_g79v3(body: bytes) -> bytes:
    """g79 v3 版解密"""
    return http_decrypt(body, key_set=KEYS_G79V3)


def http_encrypt_g79v12(body: bytes) -> bytes:
    """g79 v12 版加密 (AES-128, 密钥由32字符hex解码为16字节)"""
    return http_encrypt(body, key_set=KEYS_G79V12)


def http_decrypt_g79v12(body: bytes) -> bytes:
    """g79 v12 版解密 (AES-128, 密钥由32字符hex解码为16字节)"""
    return http_decrypt(body, key_set=KEYS_G79V12)


# ---------------------------------------------------------------------------
# ComputeDynamicToken - API 网关动态令牌
# ---------------------------------------------------------------------------

def _md5_hex(data: bytes) -> str:
    """计算 MD5 并返回十六进制字符串"""
    return hashlib.md5(data).hexdigest()


def _to_binary_string(data: bytes) -> str:
    """将字节串转换为二进制字符串 (每字节8位)"""
    return "".join(format(b, "08b") for b in data)


def _from_binary_string(binary: str) -> bytes:
    """将二进制字符串转换回字节串"""
    result = bytearray()
    for i in range(0, len(binary), 8):
        chunk = binary[i:i + 8]
        result.append(int(chunk, 2))
    return bytes(result)


def compute_dynamic_token(path: str, body: str, token: str) -> str:
    """计算 API 网关动态令牌

    算法 (来自 IL 代码 ComputeDynamicToken, 逐行反编译):
        1. combined = MD5Hex(token) + body + salt + path.TrimEnd('?')
           (注意: MD5 是对 token 计算,不是 body; 顺序也与之前不同)
        2. md5_hex = MD5Hex(UTF8.GetBytes(combined))  # 32字符小写hex
        3. V_1 = UTF8.GetBytes(md5_hex)  # 32字节 (hex字符的ASCII码)
        4. binary = ToBinary(V_1)  # 256位 (32字节 × 8位)
        5. rotated = binary[6:] + binary[:6]  # 左旋6位
        6. 对每个字节 i: V_1[i] ^= int(rotated[i*8:i*8+8], 2)
        7. result = "1" + Base64(V_1)[:16].Replace("+","m").Replace("/","o")

    Args:
        path: API 路径 (如 "/rental-server/query/search-by-name")
        body: 请求体 JSON 字符串
        token: 登录令牌 (LoginSRCToken)

    Returns:
        17字符动态令牌 (以 "1" 开头)
    """
    # 1. 构造组合字符串: MD5Hex(token) + body + salt + path.TrimEnd('?')
    token_md5 = _md5_hex(token.encode("utf-8"))
    path_trimmed = path.rstrip("?")
    combined = token_md5 + body + DYNAMIC_TOKEN_SALT + path_trimmed

    # 2. 计算 MD5,得到 hex 字符串 (32字符小写)
    md5_hex = _md5_hex(combined.encode("utf-8"))

    # 3. 将 hex 字符串转为字节 (32字节, 每个hex字符的ASCII码)
    #    例如 '0'=0x30, 'a'=0x61, 'f'=0x66
    V_1 = bytearray(md5_hex.encode("utf-8"))  # 32字节

    # 4. 转换为二进制字符串 (256位)
    binary = _to_binary_string(V_1)  # 256字符

    # 5. 左旋 6 位
    rotated = binary[6:] + binary[:6]  # 256字符

    # 6. 逐字节 XOR: V_1[i] ^= 从 rotated 中提取的8位二进制对应的字节
    for i in range(len(V_1)):
        chunk = rotated[i * 8: i * 8 + 8]
        byte_val = int(chunk, 2)
        V_1[i] ^= byte_val

    # 7. Base64 编码, 截取前16字符, 替换特殊字符, 末尾追加 "1"
    # BUG 修复: "1" 应在末尾追加, 而非开头前缀
    # 来源: InfBotLobby openssl_.h (d = d + "1") 和 nemc_crypto/x19crypt.py 一致
    b64 = base64.b64encode(bytes(V_1)).decode("ascii")
    token_result = b64[:16].replace("+", "m").replace("/", "o") + "1"

    return token_result


def compute_dynamic_token_auth(path: str, body: str, token_bytes: bytes) -> str:
    """计算认证服务器动态令牌 (FastBuilder 版本)。

    与 compute_dynamic_token 的区别:
        1. token 是 byte 数组 (H5Token = Base64Decode(LoginSRCToken))
        2. 使用 ToHex(token_bytes) 而非 MD5Hex(token_string)
        3. 结果需要 hex 编码后作为 user-token 头

    算法 (来自 FastBuilder IL 代码 ComputeDynamicToken(string, string, byte[])):
        1. combined = ToHex(token_bytes, lowercase) + body + salt + path.TrimEnd('?')
        2. md5_hex = MD5Hex(UTF8.GetBytes(combined))  # 32字符小写hex
        3. V_1 = UTF8.GetBytes(md5_hex)  # 32字节
        4. binary = ToBinary(V_1)  # 256位
        5. rotated = binary[6:] + binary[:6]  # 左旋6位
        6. 对每个字节 i: V_1[i] ^= int(rotated[i*8:i*8+8], 2)
        7. result = "1" + Base64(V_1)[:16].Replace("+","m").Replace("/","o")
        8. user_token = ToHex(ASCII.GetBytes(result), uppercase)

    Args:
        path: API 路径 (如 "/authentication-v2")
        body: 请求体 JSON 字符串 (加密前的原始 JSON)
        token_bytes: H5Token 字节数组 (Base64Decode(LoginSRCToken))

    Returns:
        hex 编码的动态令牌字符串 (大写 hex)
    """
    # 1. 构造组合字符串: ToHex(token_bytes) + body + salt + path.TrimEnd('?')
    token_hex = token_bytes.hex()  # lowercase hex (对应 ToHex(bytes, false))
    path_trimmed = path.rstrip("?")
    combined = token_hex + body + DYNAMIC_TOKEN_SALT + path_trimmed

    # 2. 计算 MD5,得到 hex 字符串 (32字符小写)
    md5_hex = _md5_hex(combined.encode("utf-8"))

    # 3. 将 hex 字符串转为字节 (32字节)
    V_1 = bytearray(md5_hex.encode("utf-8"))

    # 4. 转换为二进制字符串 (256位)
    binary = _to_binary_string(V_1)

    # 5. 左旋 6 位
    rotated = binary[6:] + binary[:6]

    # 6. 逐字节 XOR
    for i in range(len(V_1)):
        chunk = rotated[i * 8: i * 8 + 8]
        byte_val = int(chunk, 2)
        V_1[i] ^= byte_val

    # 7. Base64 编码, 截取前16字符, 替换特殊字符, 末尾追加 "1"
    # BUG 修复: "1" 应在末尾追加 (与 compute_dynamic_token 一致)
    b64 = base64.b64encode(bytes(V_1)).decode("ascii")
    token_result = b64[:16].replace("+", "m").replace("/", "o") + "1"

    # 8. 将结果 hex 编码 (对应 ToHex(ASCII.GetBytes(result), true))
    #    FastBuilder: user-token = ToHex(Encoding.ASCII.GetBytes(token_result), uppercase)
    hex_token = token_result.encode("ascii").hex().upper()

    return hex_token


def generate_trace_id() -> str:
    """生成 32 位 X_TRACE_ID"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(32))
