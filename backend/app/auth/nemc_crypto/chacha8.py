"""ChaCha8 流密码实现.

从 ``MCStudio.Utils/ChaCha8.cs`` + ``ChaChaX.cs`` 移植.

C# 原版通过 P/Invoke 调用 ``mcl.common.dll`` 原生库实现 ChaCha.
此处使用纯 Python 实现, 遵循 RFC 8439 的 IETF 变体
(counter=word12, nonce=words13-15), 但只执行 **8 轮** (而非 20 轮).

ChaCha8 是 ChaCha20 的缩减轮数变体, 广泛用于 NetEase Minecraft 中国版
的联机服务器通信加密.
"""

from __future__ import annotations

import struct
from typing import List, Optional

__all__ = ["ChaCha8", "ChaCha20"]

#: ChaCha 常量 "expand 32-byte k".
_CONSTANTS: List[int] = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]

#: 32 位掩码.
_MASK32: int = 0xFFFFFFFF

#: 默认 IV (对应 ASCII "163 NetEase\\n").
_DEFAULT_IV: bytes = bytes([49, 54, 51, 32, 78, 101, 116, 69, 97, 115, 101, 10])


def _rotl32(value: int, count: int) -> int:
    """32 位循环左移."""
    value &= _MASK32
    return ((value << count) | (value >> (32 - count))) & _MASK32


def _quarter_round(state: List[int], a: int, b: int, c: int, d: int) -> None:
    """ChaCha quarter round (原地修改 state)."""
    state[a] = (state[a] + state[b]) & _MASK32
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 16)

    state[c] = (state[c] + state[d]) & _MASK32
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 12)

    state[a] = (state[a] + state[b]) & _MASK32
    state[d] ^= state[a]
    state[d] = _rotl32(state[d], 8)

    state[c] = (state[c] + state[d]) & _MASK32
    state[b] ^= state[c]
    state[b] = _rotl32(state[b], 7)


class ChaCha20:
    """ChaCha20 流密码 (可指定轮数).

    子类 :class:`ChaCha8` 固定使用 8 轮.

    Parameters
    ----------
    key
        32 字节 (256 位) 密钥.
    iv
        12 字节 nonce/IV. 如不提供, 使用默认值 ``b"163 NetEase\\n"``.
    rounds
        轮数 (ChaCha20=20, ChaCha8=8).
    """

    def __init__(
        self,
        key: bytes,
        iv: Optional[bytes] = None,
        rounds: int = 20,
    ) -> None:
        if len(key) != 32:
            raise ValueError(f"密钥长度必须为 32 字节, 实际为 {len(key)}")
        if iv is None:
            iv = _DEFAULT_IV
        if len(iv) != 12:
            raise ValueError(f"IV 长度必须为 12 字节, 实际为 {len(iv)}")

        self._rounds: int = rounds
        self._key: bytes = key
        self._iv: bytes = iv
        self._counter: int = 0
        self._deleted: bool = False

    def _block(self, counter: int) -> bytes:
        """生成一个 64 字节的密钥流块."""
        # 初始状态: 4 常量 + 8 密钥字 + 1 计数器 + 3 nonce 字.
        state = list(_CONSTANTS)
        # 将 32 字节密钥拆分为 8 个 32 位小端整数.
        state.extend(struct.unpack("<8I", self._key))
        # 计数器.
        state.append(counter & _MASK32)
        # Nonce (3 个字).
        state.extend(struct.unpack("<3I", self._iv))

        working = list(state)

        # 执行 rounds/2 次双轮 (每轮 = 1 列 + 1 对角).
        for _ in range(self._rounds // 2):
            # 列轮.
            _quarter_round(working, 0, 4, 8, 12)
            _quarter_round(working, 1, 5, 9, 13)
            _quarter_round(working, 2, 6, 10, 14)
            _quarter_round(working, 3, 7, 11, 15)
            # 对角轮.
            _quarter_round(working, 0, 5, 10, 15)
            _quarter_round(working, 1, 6, 11, 12)
            _quarter_round(working, 2, 7, 8, 13)
            _quarter_round(working, 3, 4, 9, 14)

        # 累加初始状态.
        result = [(working[i] + state[i]) & _MASK32 for i in range(16)]
        return struct.pack("<16I", *result)

    def Process(self, data: bytes) -> bytes:
        """加密/解密数据 (XOR 密钥流).

        ChaCha 是流密码, 加密与解密是同一操作.

        Parameters
        ----------
        data
            待处理的数据.

        Returns
        -------
        bytes
            处理后的数据 (与输入等长).
        """
        if self._deleted:
            raise RuntimeError("ChaCha 实例已 Delete, 不可再用")
        if not data:
            return b""

        result = bytearray(len(data))
        offset = 0
        while offset < len(data):
            keystream = self._block(self._counter)
            self._counter = (self._counter + 1) & _MASK32

            chunk_end = min(offset + 64, len(data))
            chunk_len = chunk_end - offset
            for i in range(chunk_len):
                result[offset + i] = data[offset + i] ^ keystream[i]
            offset = chunk_end

        return bytes(result)

    def Delete(self) -> None:
        """清理密钥 (对应 C# ``ChaChaX.Delete``).

        调用后 :meth:`Process` 将抛出异常.
        """
        self._key = b"\x00" * 32
        self._iv = b"\x00" * 12
        self._deleted = True


class ChaCha8(ChaCha20):
    """ChaCha8 流密码 (8 轮).

    继承自 :class:`ChaCha20`, 固定使用 8 轮.

    Parameters
    ----------
    key
        32 字节 (256 位) 密钥.
    iv
        12 字节 nonce/IV. 如不提供, 使用默认值 ``b"163 NetEase\\n"``.
    """

    def __init__(self, key: bytes, iv: Optional[bytes] = None) -> None:
        super().__init__(key, iv, rounds=8)
