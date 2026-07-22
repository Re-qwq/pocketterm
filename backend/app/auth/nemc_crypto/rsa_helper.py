"""RSA 加密辅助模块.

从 ``Login.NetEase.LinkService/RSAHelper.cs`` 移植.

提供基于 .NET XML 密钥格式的 RSA 加解密, 使用 **PKCS#1 v1.5** 填充
(对应 C# ``fOAEP: false``).

XML 密钥格式::

    <RSAKeyValue>
        <Modulus>...</Modulus>
        <Exponent>AQAB</Exponent>
        <D>...</D>           (仅私钥)
        <P>...</P>            (仅私钥)
        <Q>...</Q>            (仅私钥)
        <DP>...</DP>          (仅私钥)
        <DQ>...</DQ>          (仅私键)
        <InverseQ>...</InverseQ>  (仅私钥)
    </RSAKeyValue>
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from typing import Optional, Union

from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from Crypto.Util.number import bytes_to_long, long_to_bytes

__all__ = [
    "RSAEncrypt",
    "RSADecrypt",
    "parse_xml_public_key",
    "parse_xml_private_key",
]


def _b64_to_int(b64_str: str) -> int:
    """将 Base64 字符串转换为整数."""
    raw = base64.b64decode(b64_str)
    return bytes_to_long(raw)


def parse_xml_public_key(xml_key: str) -> RSA.RsaKey:
    """从 XML 字符串解析 RSA 公钥.

    Parameters
    ----------
    xml_key
        XML 格式的公钥字符串.

    Returns
    -------
    RSA.RsaKey
        pycryptodome 的 RSA 公钥对象.
    """
    root = ET.fromstring(xml_key)
    modulus_el = root.find("Modulus")
    exponent_el = root.find("Exponent")

    if modulus_el is None or exponent_el is None:
        raise ValueError("XML 公钥缺少 Modulus 或 Exponent 元素")

    n = _b64_to_int(modulus_el.text.strip())
    e = _b64_to_int(exponent_el.text.strip())

    return RSA.construct((n, e))


def parse_xml_private_key(xml_key: str) -> RSA.RsaKey:
    """从 XML 字符串解析 RSA 私钥.

    Parameters
    ----------
    xml_key
        XML 格式的私钥字符串.

    Returns
    -------
    RSA.RsaKey
        pycryptodome 的 RSA 私钥对象.
    """
    root = ET.fromstring(xml_key)
    modulus_el = root.find("Modulus")
    exponent_el = root.find("Exponent")

    if modulus_el is None or exponent_el is None:
        raise ValueError("XML 私钥缺少 Modulus 或 Exponent 元素")

    n = _b64_to_int(modulus_el.text.strip())
    e = _b64_to_int(exponent_el.text.strip())

    d_el = root.find("D")
    if d_el is not None:
        # 完整私钥.
        d = _b64_to_int(d_el.text.strip())
        p_el = root.find("P")
        q_el = root.find("Q")
        if p_el is not None and q_el is not None:
            p = _b64_to_int(p_el.text.strip())
            q = _b64_to_int(q_el.text.strip())
            # .NET 不保证 p > q, 而 pycryptodome 要求 p > q.
            # 如果 p < q, 交换 p↔q.
            if p < q:
                p, q = q, p
            # pycryptodome 的 construct 只接受 (n, e, d, p, q, u) 六元组,
            # 不支持 dp / dq (它们会被自动计算).
            # u = p^(-1) mod q (.NET 的 InverseQ = q^(-1) mod p, 方向相反).
            u = pow(p, -1, q)
            return RSA.construct((n, e, d, p, q, u))
        # 仅有 d, 无 p/q.
        return RSA.construct((n, e, d))

    raise ValueError("XML 私钥缺少 D 元素")


def RSAEncrypt(xml_public_key: str, data: bytes) -> bytes:
    """RSA 加密 (PKCS#1 v1.5 填充).

    对应 C# ``RSAHelper.RSAEncrypt``.

    Parameters
    ----------
    xml_public_key
        XML 格式的公钥字符串.
    data
        待加密数据 (长度不能超过密钥长度 - 11 字节).

    Returns
    -------
    bytes
        加密后的数据 (长度等于密钥长度, 如 2048 位密钥 → 256 字节).
    """
    key = parse_xml_public_key(xml_public_key)
    cipher = PKCS1_v1_5.new(key)
    return cipher.encrypt(data)


def RSADecrypt(xml_private_key: str, data: bytes) -> bytes:
    """RSA 解密 (PKCS#1 v1.5 填充).

    对应 C# ``RSADecrypt``.

    Parameters
    ----------
    xml_private_key
        XML 格式的私钥字符串.
    data
        待解密数据.

    Returns
    -------
    bytes
        解密后的数据.
    """
    key = parse_xml_private_key(xml_private_key)
    cipher = PKCS1_v1_5.new(key)
    return cipher.decrypt(data, sentinel=b"")
