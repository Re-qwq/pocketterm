"""TCP 连接到游戏服务器模块.

从 ``Login.NetEase.LinkService/LinkConnection.cs`` 移植.

提供与网易 Minecraft 中国版联机服务器的 TCP 连接, 包含:

- RSA 密钥交换 (R1/R2 密钥协商)
- ChaCha8 加密通道
- LoginV2 登录协议
- 心跳保活 (每 30 秒发送 2 字节空数据)

连接流程
--------

1. GET ``https://g79.update.netease.com/linkserver_obt.list`` 获取服务器列表
2. TCP 连接到第 2 个服务器 (index=1)
3. 生成 16 字节随机 R1_KEY (32 hex chars), 用服务器 RSA 公钥加密发送 (256 bytes)
4. 发送客户端公钥 MD5 (16 bytes)
5. 接收 256 字节, 用客户端 RSA 私钥解密得到 R2_KEY (16 bytes)
6. 加密密钥 = R1+R2 (32 bytes), 解密密钥 = R2+R1 (32 bytes)
7. 创建 ChaCha8 加密器/解密器

LoginV2 协议
------------

1. AES-ECB 加密 login_token (用随机 16 字节 key)
2. 构建 JSON: ``{"s2": encrypted_hex, "s1": random_key_hex, "is_zip": true, "uid": uid}``
3. 前置 ``[0x00, 0x00, 0x07]`` + ``"LoginV2"`` + JSON
4. ChaCha8 加密 + VarInt 长度前缀 + 发送
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import List, Optional, Tuple

import httpx

from ..nemc_crypto import aes_helper, chacha8, rsa_helper

__all__ = [
    "LinkConnection",
    "SERVER_PUBLIC_KEY_XML",
    "CLIENT_PUBLIC_KEY_MD5",
    "CLIENT_PRIVATE_KEY_XML",
    "LINKSERVER_LIST_URL",
]

logger = logging.getLogger("pocketterm.auth.nemc.link_connection")

# --- RSA 密钥 (从 LinkConnection.cs 硬编码) --------------------------------

#: 服务器 RSA 公钥 (XML 格式, 用于加密 R1_KEY).
SERVER_PUBLIC_KEY_XML: str = (
    "<RSAKeyValue><Exponent>AQAB</Exponent>"
    "<Modulus>rht9ioo6tc3Z7On/80iYjNI+HpxnEpSc0tXC9JLykvwkxluZiLPrlvO6sgkkPsQMBXudGRu335dBCVdwfMefY7wswrQG51U+Nw3xfSSRgSptNV8PcmNjh6EYAluRSy7AZLcWc6+qJ6fJFOeABGYxNwMVvbpDC0R+t7BtcmQCk+4uXP2dsRjJSs6ALlfT7iEs8IL7iRfu1IvomTAc6eJarStgxEBTWdV/d2XfoIshbNYQ9ziBk0iWzHoI15UXFWLL+jZwhQYwzB0f+ilckgFT0IFKU4msUUQ7io4CBY2iI1G0BnSQYwpm84WR0HrgKL7uoxtJQ98iPk2GbZgWFv1OTw==</Modulus>"
    "</RSAKeyValue>"
)

#: 客户端公钥 MD5 (hex 字符串, 发送给服务器用于身份验证).
CLIENT_PUBLIC_KEY_MD5: str = "2ffdc15cb5e3790b92ff549f31390442"

#: 客户端 RSA 私钥 (XML 格式, 用于解密 R2_KEY).
CLIENT_PRIVATE_KEY_XML: str = (
    "<RSAKeyValue><Exponent>AQAB</Exponent>"
    "<Modulus>zBHNrnH++2A7LQch+AOcJRYpTxo5f9rOlZOsWbbG+SYbfGxFUgatBdJ67vLQV4EQbUv++GyNlKMa79l+0kIJTG2FIveFgBpzLTBIvsNsJiVsWpmWZsFzUo0HMmd0JnZszJq/OqTm87PPpfj5RA8ydUDFq0YXIcZy4XZqHmXPfS2EcZ40OcTKHg5DRnxegHM5Avhq8rhSdUQzr7BVs2Im2Z83ePhk3lWxvhxLURtHq0A6BcAsR6cMCx2uKhnddbqPWmABsRAcvGfzKwdat3QXBsxRuTSbsXgznlM8AM52DC4TazGhesqwwsyKnhQZKqi0nLGuu9vgyX13ca2No0mu0w==</Modulus>"
    "<D>m26QB+fB+7tfNzuwjtRJESJhEmP6Gb0SDnGtG6QQx2JUGx/oaMK29LFNe0SslYmzdlwk9xjPecAF21wAsasko/bjKi/3mgwLYAbf0ZTNgfyNHDDRkrCT4vOR4L1VhZo74leXgdZqJoL1jQgm68TbfN158atwIQSjKcFksISBVmiBGcQ8XVka6yg6D4Qob7DMHUCt8XFM5P50CQdqvUiq6oPWEoIy3nWVsrQDA9B+p0SXrDBFN0gqRIvc96wBbaAtgbHCVFGFMA+5xc+75ZZcN4de0aqFxOph2NjdcR3JdmZjCSw/6iEXIhV2dP3zIUulJA4geNiDL9SsD6Zw8WR7UQ==</D>"
    "<P>8xLLsWCv+DZZjxYsjIxeCQjU8UZPqoRKYiGTctjcl0FK3yq4YGk0zoWUgqjKX10QXTmG6G5cF92DiAzLg5gnLm6P8euT0/JPgGZl9sVkvEecntig7HEiqs9yqWA3GDb150FqsPBBSIPANFZUZXVTk68qGVIKiG9rzCu3axV1bFs=</P>"
    "<Q>1uwCMLOa2N5HZ2P6/qjTZQv0wjc19XzxA0S6A8UaW8vOPdc8hJB97FKMgOUkicxjXlMZSBO1sNT8Y1dPR15ufl/2QjcP88AliYFSO7nhhE8RojRLAZULzuC4hyEYYv8QUQy6TXE2Ta4AIi64OAcg20i/xij2cIKwBH9cwjSbMOk=</Q>"
    "<DP>bpBUGsCyCiMepZkedme6tj1QLtcekZ9O/kfre8fsvtgyKESUTTZNkMrt/GiudKYuNVlfZgYc2bYmiBHZ2GezGsmrrAzN1xBW3T62joLHCWVBdndu612iuTNXInfjV55YR/JXh1ghOczD9op2JRgzBfAdJBtPMzQLQnl4GrtOCBU=</DP>"
    "<DQ>t/5wmZUJWeRhqMfVVzLdV0J3FdYCYdnG04+A2D1jpXbDZ/neG3c/9pNtKeQB9d5+q3/kwunswCh2se1LN8RGP/aTcniFNZ4oBKIr7mniAU1XwU+XbxFUfJWyJC1XHVlTdK+6xxXG8ZWnE5x/paekn1aWp2TmJcgcPJ10oeY7fhE=</DQ>"
    "<InverseQ>Uz+ZaNougfcTIXZcHyA5VFX28tSp640PeNy/5en3Pd9J5ocFel2VNlTBgy5AesON4uUK6bSehXL6d8ZK3KoxV5Bcr2zAttil07/KSg4gOfj0tqlsCVf044JmFuVBloFGLyj4fUQBA4YoGMHi2eDvh4bTZlhjeCRdnABlomhJTDE=</InverseQ>"
    "</RSAKeyValue>"
)

#: LinkServer 列表 URL.
LINKSERVER_LIST_URL: str = "https://g79.update.netease.com/linkserver_obt.list"

#: ChaCha8 默认 IV (ASCII "163 NetEase\\n").
_CHACHA_DEFAULT_IV: bytes = bytes([49, 54, 51, 32, 78, 101, 116, 69, 97, 115, 101, 10])

#: 心跳间隔 (秒).
HEARTBEAT_INTERVAL: int = 30

#: RSA 密钥长度 (字节, 2048 位 = 256 字节).
_RSA_BLOCK_SIZE: int = 256


class LinkConnection:
    """联机服务器 TCP 连接.

    从 ``Login.NetEase.LinkService/LinkConnection.cs`` 移植.

    提供基于 RSA 密钥交换 + ChaCha8 加密的 TCP 通信通道.

    Parameters
    ----------
    server_public_key
        服务器 RSA 公钥 (XML 格式). 默认使用硬编码值.
    client_public_key_md5
        客户端公钥 MD5 (hex 字符串). 默认使用硬编码值.
    client_private_key
        客户端 RSA 私钥 (XML 格式). 默认使用硬编码值.
    """

    def __init__(
        self,
        server_public_key: str = SERVER_PUBLIC_KEY_XML,
        client_public_key_md5: str = CLIENT_PUBLIC_KEY_MD5,
        client_private_key: str = CLIENT_PRIVATE_KEY_XML,
    ) -> None:
        self._server_public_key: str = server_public_key
        self._client_public_key_md5: str = client_public_key_md5
        self._client_private_key: str = client_private_key

        # 连接状态.
        self.is_connected: bool = False
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

        # 密钥.
        self._r1_key: str = ""
        self._r2_key: str = ""
        self._encrypt_key: bytes = b""
        self._decrypt_key: bytes = b""

        # ChaCha8 加密器/解密器.
        self.encryption: Optional[chacha8.ChaCha8] = None
        self.decryption: Optional[chacha8.ChaCha8] = None

        # 心跳任务.
        self._heartbeat_task: Optional[asyncio.Task] = None

        # HTTP 客户端 (用于获取服务器列表).
        self._http_client: Optional[httpx.AsyncClient] = None

    # --- 连接 ---------------------------------------------------------------

    async def start_connect(self) -> None:
        """启动连接.

        从 ``LinkConnection.StartConnect`` 移植.

        流程:
        1. 获取服务器列表
        2. TCP 连接到第 2 个服务器
        3. RSA 密钥交换 (R1/R2)
        4. 创建 ChaCha8 加密器/解密器
        """
        try:
            self.is_connected = True

            # Step 1: 获取服务器列表.
            server_list = await self._get_link_host()
            if not server_list or len(server_list) < 2:
                raise ValueError("服务器列表为空或不足 2 个服务器")

            # Step 2: 选择第 2 个服务器 (index=1).
            server = server_list[1]
            ip = server["ip"]
            port = int(server["port"])
            logger.info("连接到 LinkServer: %s:%d", ip, port)

            # TCP 连接.
            self._reader, self._writer = await asyncio.open_connection(ip, port)

            # Step 3: RSA 密钥交换.
            # 生成 R1_KEY (32 hex chars = 16 bytes).
            self._r1_key = self._get_random_key(32)
            logger.debug("R1 密钥: %s", self._r1_key)

            # RSA 加密 R1_KEY.
            r1_bytes = bytes.fromhex(self._r1_key)
            encrypted_r1 = rsa_helper.RSAEncrypt(self._server_public_key, r1_bytes)
            assert self._writer is not None
            self._writer.write(encrypted_r1)
            await self._writer.drain()

            # 发送客户端公钥 MD5 (16 bytes).
            client_md5_bytes = bytes.fromhex(self._client_public_key_md5)
            self._writer.write(client_md5_bytes)
            await self._writer.drain()

            # 接收 R2_KEY (256 bytes, RSA 加密).
            assert self._reader is not None
            encrypted_r2 = await self._reader.readexactly(_RSA_BLOCK_SIZE)
            r2_bytes = rsa_helper.RSADecrypt(self._client_private_key, encrypted_r2)
            self._r2_key = r2_bytes.hex()
            logger.debug("R2 密钥: %s", self._r2_key)

            # Step 4: 构建加密/解密密钥.
            # _encrypt_key = (R1 + R2).HexToBytes() = 32 bytes
            # _decrypt_key = (R2 + R1).HexToBytes() = 32 bytes
            self._encrypt_key = bytes.fromhex(self._r1_key + self._r2_key)
            self._decrypt_key = bytes.fromhex(self._r2_key + self._r1_key)

            # 创建 ChaCha8 加密器/解密器.
            self.encryption = chacha8.ChaCha8(self._encrypt_key)
            self.decryption = chacha8.ChaCha8(self._decrypt_key)

            logger.info("LinkServer 连接成功, 密钥交换完成")

        except Exception as e:
            logger.error("连接失败: %s", e)
            self.is_connected = False
            raise

    async def _get_link_host(self) -> List[dict]:
        """获取 LinkServer 服务器列表.

        从 ``LinkConnection.GetLinkHost`` 移植.

        GET ``https://g79.update.netease.com/linkserver_obt.list`` 获取
        JSON 数组格式的服务器列表.
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0, verify=False)

        resp = await self._http_client.get(LINKSERVER_LIST_URL)
        resp.raise_for_status()
        # C# 原版将返回文本包装在 {"id": [...]} 中再解析,
        # 此处直接解析 JSON 数组.
        return resp.json()

    @staticmethod
    def _get_random_key(length: int = 32) -> str:
        """生成随机十六进制密钥字符串.

        从 ``Function.GetRandomKey`` 移植.

        C# 原版使用 RNGCryptoServiceProvider 种子化的 Random 从
        "0123456789abcdef" 中选取字符. 此处使用 ``secrets`` 模块
        生成密码学安全的随机十六进制字符串.

        Parameters
        ----------
        length
            密钥字符串长度 (十六进制字符数). 默认 32 (对应 16 字节).

        Returns
        -------
        str
            十六进制密钥字符串.
        """
        # secrets.token_hex(n) 生成 2n 个十六进制字符.
        return secrets.token_hex(length // 2)

    # --- 数据收发 ------------------------------------------------------------

    async def send_data(self, data: bytes) -> None:
        """发送加密数据.

        从 ``LinkConnection.SendData`` 移植.

        流程:
        1. 记录原始数据长度
        2. ChaCha8 加密数据
        3. 前置 VarInt 长度
        4. 发送

        Parameters
        ----------
        data
            待发送的明文数据.
        """
        if not self.is_connected or self._writer is None or self.encryption is None:
            raise RuntimeError("连接未建立或已关闭")

        original_length = len(data)
        # ChaCha8 加密 (原地加密的 Python 等价: 返回新的加密数据).
        encrypted = self.encryption.Process(data)
        # 前置 VarInt 长度.
        varint = self._varint_encode(original_length)
        self._writer.write(varint + encrypted)
        await self._writer.drain()

    async def recv_data(self) -> Optional[bytes]:
        """接收并解密数据.

        反向操作 of :meth:`send_data`:

        1. 读取 VarInt 长度
        2. 读取对应长度的加密数据
        3. ChaCha8 解密

        Returns
        -------
        bytes or None
            解密后的明文数据, 如果连接已关闭则返回 None.
        """
        if not self.is_connected or self._reader is None or self.decryption is None:
            return None

        try:
            # 读取 VarInt 长度.
            length = await self._read_varint()
            if length <= 0:
                return b""
            # 读取加密数据.
            encrypted = await self._reader.readexactly(length)
            # ChaCha8 解密.
            return self.decryption.Process(encrypted)
        except asyncio.IncompleteReadError:
            logger.warning("连接已断开 (读取不完整)")
            self.is_connected = False
            return None
        except Exception as e:
            logger.error("接收数据失败: %s", e)
            self.is_connected = False
            return None

    # --- LoginV2 协议 -------------------------------------------------------

    async def do_login(self, uid: int, login_token: str) -> None:
        """LoginV2 登录协议.

        从 ``LinkConnection.do_login`` 移植.

        流程:
        1. AES-ECB 加密 login_token (用随机 16 字节 key)
        2. 构建 JSON: ``{"s2": encrypted_hex, "s1": random_key_hex, "is_zip": true, "uid": uid}``
        3. 前置 ``[0x00, 0x00, 0x07]`` + ``"LoginV2"`` + JSON
        4. 通过 :meth:`send_data` 加密发送

        Parameters
        ----------
        uid
            用户 UID (整数).
        login_token
            登录令牌 (LoginSRCToken).
        """
        if not self.is_connected:
            raise RuntimeError("连接未建立")

        # AES-ECB 加密 login_token.
        login_token_bytes = login_token.encode("utf-8")
        random_key = self._get_random_key(32)  # 32 hex chars = 16 bytes
        random_key_bytes = bytes.fromhex(random_key)  # 16 bytes → AES key
        encrypted_token = aes_helper.AES_ECB_Encrypt(login_token_bytes, random_key_bytes)
        s2 = encrypted_token.hex()

        # 构建 JSON (匹配 C# 字符串拼接格式).
        payload = f'{{"s2": "{s2}", "s1": "{random_key}", "is_zip": true, "uid": {uid}}}'
        logger.debug("LoginV2 payload: %s", payload[:200])

        # 构建数据包: [0x00, 0x00, len("LoginV2"), "LoginV2", payload].
        prefix = b"LoginV2"
        prefix_len = len(prefix)  # 7
        payload_bytes = payload.encode("utf-8")

        packet = bytearray(3 + prefix_len + len(payload_bytes))
        packet[2] = prefix_len  # array[2] = 7
        packet[3:3 + prefix_len + len(payload_bytes)] = prefix + payload_bytes

        await self.send_data(bytes(packet))
        logger.info("LoginV2 已发送")

    # --- VarInt 编码/解码 ----------------------------------------------------

    @staticmethod
    def _varint_encode(value: int) -> bytes:
        """将整数编码为 VarInt (Minecraft 变长整数格式).

        从 ``LinkConnection.VarInt`` 移植.

        每个字节使用低 7 位存储数据, 最高位 (0x80) 作为延续标志.

        Parameters
        ----------
        value
            待编码的非负整数.

        Returns
        -------
        bytes
            VarInt 编码后的字节序列.
        """
        result: List[int] = []
        while value >= 128:
            result.append((value & 0xFF) | 0x80)
            value >>= 7
        result.append(value)
        return bytes(result)

    async def _read_varint(self) -> int:
        """从流中读取 VarInt.

        Returns
        -------
        int
            解码后的整数.
        """
        assert self._reader is not None
        result = 0
        shift = 0
        while True:
            byte = (await self._reader.readexactly(1))[0]
            result |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                break
            shift += 7
        return result

    # --- 心跳 ----------------------------------------------------------------

    async def start_heartbeat(self) -> None:
        """启动心跳保活.

        从 ``LinkConnection.Tick`` 移植.

        每 30 秒发送 2 字节空数据 (``[0x00, 0x00]``), **不加密**,
        直接写入网络流.
        """
        heartbeat_data = b"\x00\x00"
        while self.is_connected:
            try:
                if self._writer is None or self._writer.is_closing():
                    break
                self._writer.write(heartbeat_data)
                await self._writer.drain()
                logger.debug("心跳已发送")
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.error("心跳发送失败: %s", e)
                self.is_connected = False
                break

    # --- 清理 ----------------------------------------------------------------

    async def close(self) -> None:
        """关闭连接并清理资源.

        从 ``LinkConnection.Close`` 移植.

        清理 ChaCha8 密钥, 关闭 TCP 连接和 HTTP 客户端.
        """
        self.is_connected = False

        # 取消心跳任务.
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # 清理 ChaCha8 密钥.
        if self.encryption is not None:
            self.encryption.Delete()
            self.encryption = None
        if self.decryption is not None:
            self.decryption.Delete()
            self.decryption = None

        # 清理密钥.
        self._encrypt_key = b"\x00" * 32
        self._decrypt_key = b"\x00" * 32
        self._r1_key = ""
        self._r2_key = ""

        # 关闭 TCP 连接.
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

        # 关闭 HTTP 客户端.
        if self._http_client is not None and not self._http_client.is_closed:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        logger.info("LinkServer 连接已关闭")

    async def __aenter__(self) -> "LinkConnection":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
