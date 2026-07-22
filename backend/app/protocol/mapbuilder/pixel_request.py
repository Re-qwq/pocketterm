"""pixel_request - 像素请求 (SendMapPixels)。

逆向自 NexusEgo v1.6.5 的 MapBuilder 像素请求系统。

逆向证据 (来自 strings_exclusive.txt 和 strings_api.txt):
    - mapbuilder.PixelRequest        -- 像素请求
    - mapbuilder.SubChunkPos         -- 子区块位置
    - mapbuilder.SubChunkEntry       -- 子区块条目
    - mapbuilder.SubChunkOffset      -- 子区块偏移
    - mapbuilder.SubChunkResponse    -- 子区块响应
    - mapbuilder.BlockPos            -- 方块位置
    - mapbuilder.subChunkKey         -- 子区块键
    - mapbuilder.MapAPI              -- 地图 API
    - mapbuilder.nexusAPI            -- Nexus API 封装
    - mapbuilder.StructureNBTAPI     -- 结构 NBT API
    - *func(int64, []mapbuilder.PixelRequest) error
    - *func(int32, mapbuilder.BlockPos, mapbuilder.BlockPos) (*mapbuilder.SubChunkResponse, error)
    - SendMapPixels: connection not ready
    - minecraft:filled_map

核心类型:
    - BlockPos:          方块位置 (x, y, z)
    - SubChunkPos:       子区块位置 (cx, cy, cz)
    - SubChunkKey:       子区块唯一键
    - SubChunkEntry:     子区块条目
    - SubChunkOffset:    子区块偏移
    - SubChunkResponse:  子区块响应
    - PixelRequest:      像素请求 (地图 ID + 像素数据)
    - MapAPI:            地图 API 接口
    - NexusAPI:          Nexus API 封装

工作流程:
    1. ImageInfo -> 转换为地图颜色
    2. 颜色数据 -> PixelRequest (map_id + pixels)
    3. SendMapPixels 发送到服务器
    4. 子区块映射 -> SubChunkPos/SubChunkEntry
    5. SubChunkResponse 确认结果
"""

from __future__ import annotations

import hashlib
import logging
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger("pocketterm.protocol.mapbuilder.pixel_request")


# ======================================================================
# 常量
# ======================================================================

#: Minecraft 地图分辨率 (128x128)
MAP_PIXEL_SIZE: int = 128

#: 子区块尺寸 (16x16x16)
SUBCHUNK_SIZE: int = 16

#: 区块尺寸 (16x16)
CHUNK_SIZE: int = 16

#: 世界高度 (基岩版, -64 ~ 320 = 384)
WORLD_MIN_Y: int = -64
WORLD_MAX_Y: int = 320

#: 地图最大 ID
MAX_MAP_ID: int = 65535

#: 像素请求重试次数
MAX_SEND_RETRIES: int = 3

#: 像素请求超时 (秒)
SEND_TIMEOUT_SECONDS: float = 10.0


# ======================================================================
# 异常
# ======================================================================


class PixelRequestError(Exception):
    """像素请求错误的基类。"""


class ConnectionNotReadyError(PixelRequestError):
    """连接未就绪 (逆向自 strings: "SendMapPixels: connection not ready")。"""


class SubChunkRequestError(PixelRequestError):
    """子区块请求失败。"""


class InvalidPixelDataError(PixelRequestError):
    """无效的像素数据。"""


# ======================================================================
# 数据类 - BlockPos
# ======================================================================


@dataclass(frozen=True)
class BlockPos:
    """方块位置 (mapbuilder.BlockPos)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。

    Attributes:
        x: X 坐标。
        y: Y 坐标。
        z: Z 坐标。
    """

    x: int = 0
    y: int = 0
    z: int = 0

    def to_tuple(self) -> tuple[int, int, int]:
        """转换为元组。"""
        return (self.x, self.y, self.z)

    def offset(self, dx: int, dy: int, dz: int) -> "BlockPos":
        """偏移生成新位置。"""
        return BlockPos(self.x + dx, self.y + dy, self.z + dz)

    def to_chunk_pos(self) -> "ChunkPos":
        """转换为区块坐标。"""
        return ChunkPos(
            cx=self.x // CHUNK_SIZE,
            cz=self.z // CHUNK_SIZE,
        )

    def to_subchunk_pos(self) -> "SubChunkPos":
        """转换为子区块坐标。"""
        return SubChunkPos(
            cx=self.x // SUBCHUNK_SIZE,
            cy=self.y // SUBCHUNK_SIZE,
            cz=self.z // SUBCHUNK_SIZE,
        )


@dataclass(frozen=True)
class ChunkPos:
    """区块坐标。

    Attributes:
        cx: 区块 X 坐标。
        cz: 区块 Z 坐标。
    """

    cx: int = 0
    cz: int = 0

    def to_block_pos(self, ox: int = 0, oy: int = 0, oz: int = 0) -> BlockPos:
        """转换为方块坐标 (区块内偏移)。"""
        return BlockPos(
            x=self.cx * CHUNK_SIZE + ox,
            y=oy,
            z=self.cz * CHUNK_SIZE + oz,
        )


# ======================================================================
# 数据类 - SubChunk
# ======================================================================


@dataclass(frozen=True)
class SubChunkPos:
    """子区块位置 (mapbuilder.SubChunkPos)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。

    子区块尺寸: 16x16x16

    Attributes:
        cx: 子区块 X 坐标 (= block_x // 16)。
        cy: 子区块 Y 坐标 (= block_y // 16)。
        cz: 子区块 Z 坐标 (= block_z // 16)。
    """

    cx: int = 0
    cy: int = 0
    cz: int = 0

    def to_key(self) -> "SubChunkKey":
        """转换为子区块键。"""
        return SubChunkKey(self.cx, self.cy, self.cz)

    def to_block_origin(self) -> BlockPos:
        """获取子区块原点的方块坐标。"""
        return BlockPos(
            x=self.cx * SUBCHUNK_SIZE,
            y=self.cy * SUBCHUNK_SIZE,
            z=self.cz * SUBCHUNK_SIZE,
        )

    def contains(self, pos: BlockPos) -> bool:
        """检查方块位置是否在此子区块内。"""
        return (
            self.cx == pos.x // SUBCHUNK_SIZE
            and self.cy == pos.y // SUBCHUNK_SIZE
            and self.cz == pos.z // SUBCHUNK_SIZE
        )


@dataclass(frozen=True)
class SubChunkKey:
    """子区块唯一键 (mapbuilder.subChunkKey)。

    逆向自 strings_exclusive.txt: *mapbuilder.subChunkKey

    用于在 map[subChunkKey]*chunk.SubChunk 中作为键。

    Attributes:
        cx: 子区块 X 坐标。
        cy: 子区块 Y 坐标。
        cz: 子区块 Z 坐标。
    """

    cx: int = 0
    cy: int = 0
    cz: int = 0

    def to_pos(self) -> SubChunkPos:
        """转换为 SubChunkPos。"""
        return SubChunkPos(self.cx, self.cy, self.cz)

    def to_hash(self) -> int:
        """计算哈希值 (用于字典键)。"""
        return hash((self.cx, self.cy, self.cz))

    def __hash__(self) -> int:
        return self.to_hash()


@dataclass
class SubChunkOffset:
    """子区块偏移 (mapbuilder.SubChunkOffset)。

    表示子区块内的像素偏移。

    Attributes:
        x: X 偏移 (0-15)。
        y: Y 偏移 (0-15)。
        z: Z 偏移 (0-15)。
    """

    x: int = 0
    y: int = 0
    z: int = 0

    def to_index(self, width: int = SUBCHUNK_SIZE) -> int:
        """转换为一维索引。"""
        return self.y * width * width + self.z * width + self.x


@dataclass
class SubChunkEntry:
    """子区块条目 (mapbuilder.SubChunkEntry)。

    逆向自 strings_exclusive.txt: *[]mapbuilder.SubChunkEntry
    和 protocol.(*SubChunkEntry).Marshal

    表示一个子区块的请求/响应条目。

    Attributes:
        pos: 子区块位置。
        offset: 子区块内偏移。
        data: 子区块数据 (方块状态)。
        success: 是否成功获取。
        error_msg: 错误信息 (如果失败)。
    """

    pos: SubChunkPos = field(default_factory=SubChunkPos)
    offset: SubChunkOffset = field(default_factory=SubChunkOffset)
    data: bytes = b""
    success: bool = False
    error_msg: str = ""

    def marshal(self) -> bytes:
        """序列化为字节流 (对应 protocol.(*SubChunkEntry).Marshal)。"""
        parts: list[bytes] = []
        # 位置 (3x int32 小端)
        parts.append(struct.pack("<iii", self.pos.cx, self.pos.cy, self.pos.cz))
        # 偏移 (3x uint8)
        parts.append(struct.pack("<BBB", self.offset.x, self.offset.y, self.offset.z))
        # 数据长度 + 数据
        parts.append(struct.pack("<I", len(self.data)))
        parts.append(self.data)
        # 成功标志 (1 byte)
        parts.append(struct.pack("<B", 1 if self.success else 0))
        # 错误消息长度 + 消息
        err_bytes = self.error_msg.encode("utf-8")
        parts.append(struct.pack("<I", len(err_bytes)))
        parts.append(err_bytes)
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes, offset: int = 0) -> tuple["SubChunkEntry", int]:
        """从字节流反序列化。

        Returns:
            (SubChunkEntry, 消费的字节数)。
        """
        cx, cy, cz, ox, oy, oz = struct.unpack_from("<iiiBBB", data, offset)
        pos = SubChunkPos(cx, cy, cz)
        entry_offset = SubChunkOffset(ox, oy, oz)
        offset += 15

        (data_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        chunk_data = data[offset:offset + data_len]
        offset += data_len

        (success,) = struct.unpack_from("<B", data, offset)
        offset += 1

        (err_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        err_msg = data[offset:offset + err_len].decode("utf-8")
        offset += err_len

        return cls(
            pos=pos, offset=entry_offset, data=chunk_data,
            success=bool(success), error_msg=err_msg,
        ), offset


@dataclass
class SubChunkResponse:
    """子区块响应 (mapbuilder.SubChunkResponse)。

    逆向自 strings_exclusive.txt: *mapbuilder.SubChunkResponse

    逆向函数签名:
        func(int32, mapbuilder.BlockPos, mapbuilder.BlockPos) (*mapbuilder.SubChunkResponse, error)

    表示子区块请求的响应, 包含中心子区块和邻接子区块数据。

    Attributes:
        dimension: 维度 ID (0=主世界, 1=下界, 2=末地)。
        center: 中心子区块位置。
        entries: 子区块条目列表。
        success: 整体是否成功。
        timestamp: 响应时间戳。
    """

    dimension: int = 0
    center: SubChunkPos = field(default_factory=SubChunkPos)
    entries: list[SubChunkEntry] = field(default_factory=list)
    success: bool = False
    timestamp: float = field(default_factory=time.time)

    def get_entry(self, pos: SubChunkPos) -> SubChunkEntry | None:
        """获取指定位置的子区块条目。"""
        for entry in self.entries:
            if entry.pos == pos:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "dimension": self.dimension,
            "center": asdict(self.center),
            "entry_count": len(self.entries),
            "success": self.success,
            "timestamp": self.timestamp,
        }


# ======================================================================
# 数据类 - PixelRequest
# ======================================================================


@dataclass
class PixelRequest:
    """像素请求 (mapbuilder.PixelRequest)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。
    逆向自 strings_exclusive.txt: *[]mapbuilder.PixelRequest

    逆向函数签名:
        func(int64, []mapbuilder.PixelRequest) error
        (对应 SendMapPixels(mapId int64, requests []PixelRequest) error)

    表示向服务器发送的地图像素更新请求。

    Attributes:
        map_id: 地图 ID (0-65535)。
        pixels: 像素颜色索引数组 (128x128 = 16384 个字节)。
        offset_x: X 方向偏移 (像素, 通常 0)。
        offset_y: Y 方向偏移 (像素, 通常 0)。
        scale: 地图缩放级别 (0-4)。
    """

    map_id: int = 0
    pixels: bytes = b""
    offset_x: int = 0
    offset_y: int = 0
    scale: int = 0

    def __post_init__(self) -> None:
        """校验像素数据。"""
        if self.pixels:
            if len(self.pixels) != MAP_PIXEL_SIZE * MAP_PIXEL_SIZE:
                raise InvalidPixelDataError(
                    f"pixel data must be {MAP_PIXEL_SIZE * MAP_PIXEL_SIZE} bytes "
                    f"(128x128), got {len(self.pixels)}"
                )
        if not (0 <= self.map_id <= MAX_MAP_ID):
            raise InvalidPixelDataError(
                f"map_id must be 0-{MAX_MAP_ID}, got {self.map_id}"
            )
        if not (0 <= self.scale <= 4):
            raise InvalidPixelDataError(
                f"scale must be 0-4, got {self.scale}"
            )

    @property
    def pixel_count(self) -> int:
        """像素总数。"""
        return len(self.pixels)

    def get_pixel(self, x: int, y: int) -> int:
        """获取指定位置的像素颜色索引。

        Args:
            x: X 坐标 (0-127)。
            y: Y 坐标 (0-127)。

        Returns:
            颜色索引 (0-63)。
        """
        if not (0 <= x < MAP_PIXEL_SIZE and 0 <= y < MAP_PIXEL_SIZE):
            raise IndexError(f"pixel ({x}, {y}) out of bounds")
        return self.pixels[y * MAP_PIXEL_SIZE + x]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "map_id": self.map_id,
            "pixel_count": self.pixel_count,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "scale": self.scale,
        }


# ======================================================================
# Protocol - Connection
# ======================================================================


class Connection(Protocol):
    """连接接口 (协议类型)。

    逆向自 NexusEgo_v1.6.5 的网络连接抽象。
    """

    def is_ready(self) -> bool:
        """连接是否就绪。"""
        ...

    def send_pixels(self, map_id: int, pixels: list[PixelRequest]) -> bool:
        """发送像素数据。"""
        ...

    def request_subchunk(
        self,
        dimension: int,
        origin: BlockPos,
        target: BlockPos,
    ) -> SubChunkResponse:
        """请求子区块数据。"""
        ...


# ======================================================================
# MapAPI - 地图 API 接口
# ======================================================================


class MapAPI:
    """地图 API (mapbuilder.MapAPI)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。

    提供地图相关的 API 操作:
        - send_map_pixels:    发送地图像素
        - request_subchunk:   请求子区块
        - allocate_map_id:    分配地图 ID
        - get_map_data:       获取地图数据

    Attributes:
        connection: 网络连接。
        next_map_id: 下一个可用的地图 ID。
    """

    def __init__(
        self,
        connection: Connection | None = None,
        next_map_id: int = 0,
    ) -> None:
        self._connection: Connection | None = connection
        self._next_map_id: int = next_map_id
        self._lock = threading.Lock()
        self._allocated_ids: set[int] = set()
        logger.debug("MapAPI init: next_map_id=%d", next_map_id)

    @property
    def connection(self) -> Connection | None:
        """获取当前连接。"""
        return self._connection

    def set_connection(self, conn: Connection) -> None:
        """设置网络连接。"""
        self._connection = conn
        logger.debug("MapAPI.set_connection: %s", type(conn).__name__)

    def is_ready(self) -> bool:
        """检查连接是否就绪。"""
        if self._connection is None:
            return False
        return self._connection.is_ready()

    def allocate_map_id(self) -> int:
        """分配一个新的地图 ID。

        Returns:
            地图 ID。
        """
        with self._lock:
            map_id = self._next_map_id
            self._next_map_id += 1
            self._allocated_ids.add(map_id)
            logger.debug("MapAPI.allocate_map_id: %d", map_id)
            return map_id

    def release_map_id(self, map_id: int) -> None:
        """释放地图 ID。"""
        with self._lock:
            self._allocated_ids.discard(map_id)

    def send_map_pixels(self, requests: list[PixelRequest]) -> bool:
        """发送地图像素数据 (SendMapPixels)。

        逆向自 strings: "SendMapPixels: connection not ready"

        Args:
            requests: 像素请求列表。

        Returns:
            True 如果发送成功。

        Raises:
            ConnectionNotReadyError: 连接未就绪。
        """
        if not self.is_ready():
            logger.error("SendMapPixels: connection not ready")
            raise ConnectionNotReadyError("SendMapPixels: connection not ready")

        if not requests:
            logger.warning("SendMapPixels: no requests to send")
            return True

        # 按 map_id 分组
        grouped: dict[int, list[PixelRequest]] = {}
        for req in requests:
            grouped.setdefault(req.map_id, []).append(req)

        total_sent = 0
        for map_id, reqs in grouped.items():
            logger.debug(
                "SendMapPixels: map_id=%d, requests=%d", map_id, len(reqs)
            )
            for attempt in range(MAX_SEND_RETRIES):
                try:
                    if self._connection and self._connection.send_pixels(map_id, reqs):
                        total_sent += len(reqs)
                        break
                    else:
                        logger.warning(
                            "SendMapPixels: attempt %d failed for map_id=%d",
                            attempt + 1, map_id,
                        )
                except Exception as exc:
                    logger.warning(
                        "SendMapPixels: attempt %d error for map_id=%d: %s",
                        attempt + 1, map_id, exc,
                    )
                if attempt < MAX_SEND_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
            else:
                logger.error(
                    "SendMapPixels: all retries failed for map_id=%d", map_id
                )
                return False

        logger.info("SendMapPixels: sent %d/%d requests", total_sent, len(requests))
        return total_sent == len(requests)

    def request_subchunk(
        self,
        dimension: int,
        origin: BlockPos,
        target: BlockPos,
    ) -> SubChunkResponse:
        """请求子区块数据。

        逆向自函数签名:
            func(int32, mapbuilder.BlockPos, mapbuilder.BlockPos) (*mapbuilder.SubChunkResponse, error)

        Args:
            dimension: 维度 ID。
            origin: 起始方块位置。
            target: 目标方块位置。

        Returns:
            SubChunkResponse。

        Raises:
            ConnectionNotReadyError: 连接未就绪。
            SubChunkRequestError: 请求失败。
        """
        if not self.is_ready():
            raise ConnectionNotReadyError(
                "request_subchunk: connection not ready"
            )

        logger.debug(
            "request_subchunk: dim=%d origin=%s target=%s",
            dimension, origin, target,
        )

        try:
            assert self._connection is not None
            response = self._connection.request_subchunk(dimension, origin, target)
            response.dimension = dimension
            return response
        except PixelRequestError:
            raise
        except Exception as exc:
            raise SubChunkRequestError(
                f"request_subchunk failed: {exc}"
            ) from exc


# ======================================================================
# NexusAPI - Nexus API 封装
# ======================================================================


class NexusAPI:
    """Nexus API 封装 (mapbuilder.nexusAPI)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/nexus_api.go。
    逆向自 strings: "nexus/utils/mapbuilder.(*nexusAPI).omega"

    封装了 MicroOmega 接口, 提供高级地图操作:
        - omega: 获取 MicroOmega 实例
        - decodeStructureNBTs: 解码结构 NBT

    Attributes:
        map_api: 地图 API 实例。
        api_key: MapBuilder API Key。
    """

    def __init__(
        self,
        map_api: MapAPI | None = None,
        api_key: str = "",
    ) -> None:
        self._map_api: MapAPI = map_api or MapAPI()
        self._api_key: str = api_key
        self._structure_cache: dict[str, dict[str, Any]] = {}
        logger.debug("NexusAPI init: api_key=%s", "***" if api_key else "(none)")

    @property
    def map_api(self) -> MapAPI:
        """获取地图 API。"""
        return self._map_api

    def set_api_key(self, key: str) -> None:
        """设置 API Key。"""
        self._api_key = key

    def omega(self) -> MapAPI:
        """获取 MicroOmega 接口 (对应 nexusAPI.omega())。

        逆向自 strings: "nexus/utils/mapbuilder.(*nexusAPI).omega"

        Returns:
            MapAPI 实例 (代表 MicroOmega 的地图操作接口)。
        """
        return self._map_api

    def decode_structure_nbt(self, nbt_data: bytes) -> dict[str, Any]:
        """解码结构 NBT (对应 nexusAPI.decodeStructureNBTs)。

        逆向自 strings: "nexus/utils/mapbuilder.decodeStructureNBTs"

        Args:
            nbt_data: NBT 字节数据。

        Returns:
            解码后的结构字典。
        """
        # 缓存键
        cache_key = hashlib.sha256(nbt_data).hexdigest()[:16]

        if cache_key in self._structure_cache:
            logger.debug("decode_structure_nbt: cache hit %s", cache_key)
            return self._structure_cache[cache_key]

        # 简化: 返回基本结构信息
        # 实际实现应使用 nbt_parser 解码
        structure: dict[str, Any] = {
            "size": [0, 0, 0],
            "palette": [],
            "blocks": [],
            "entities": [],
            "data_hash": cache_key,
            "data_size": len(nbt_data),
        }

        self._structure_cache[cache_key] = structure
        logger.debug(
            "decode_structure_nbt: decoded %d bytes, hash=%s",
            len(nbt_data), cache_key,
        )
        return structure

    def build_pixel_requests(
        self,
        image_data: bytes,
        width: int,
        height: int,
        map_id: int = 0,
    ) -> list[PixelRequest]:
        """从图像数据构建像素请求列表。

        Args:
            image_data: 图像像素数据 (RGBA)。
            width: 图像宽度。
            height: 图像高度。
            map_id: 起始地图 ID。

        Returns:
            像素请求列表 (每 128x128 一组)。
        """
        requests: list[PixelRequest] = []

        tiles_x = (width + MAP_PIXEL_SIZE - 1) // MAP_PIXEL_SIZE
        tiles_y = (height + MAP_PIXEL_SIZE - 1) // MAP_PIXEL_SIZE

        current_map_id = map_id
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                pixels = bytearray(MAP_PIXEL_SIZE * MAP_PIXEL_SIZE)
                for py in range(MAP_PIXEL_SIZE):
                    for px in range(MAP_PIXEL_SIZE):
                        src_x = tx * MAP_PIXEL_SIZE + px
                        src_y = ty * MAP_PIXEL_SIZE + py
                        if src_x < width and src_y < height:
                            src_off = (src_y * width + src_x) * 4
                            r = image_data[src_off]
                            g = image_data[src_off + 1]
                            b = image_data[src_off + 2]
                            # 简化: 用亮度映射到颜色索引
                            brightness = (r + g + b) // 3
                            color_idx = min(brightness * 63 // 255, 63)
                            pixels[py * MAP_PIXEL_SIZE + px] = color_idx

                requests.append(PixelRequest(
                    map_id=current_map_id,
                    pixels=bytes(pixels),
                    offset_x=tx * MAP_PIXEL_SIZE,
                    offset_y=ty * MAP_PIXEL_SIZE,
                ))
                current_map_id += 1

        logger.debug(
            "build_pixel_requests: %dx%d -> %d tiles, %d requests",
            width, height, tiles_x * tiles_y, len(requests),
        )
        return requests

    def place_map_in_world(
        self,
        requests: list[PixelRequest],
        origin: BlockPos,
        frames_per_row: int = 8,
    ) -> list[BlockPos]:
        """计算地图在世界中放置的位置。

        Args:
            requests: 像素请求列表。
            origin: 起始方块位置。
            frames_per_row: 每行展示框数量。

        Returns:
            每个地图对应的方块位置列表。
        """
        positions: list[BlockPos] = []
        for i, req in enumerate(requests):
            row = i // frames_per_row
            col = i % frames_per_row
            pos = BlockPos(
                x=origin.x + col,
                y=origin.y,
                z=origin.z + row,
            )
            positions.append(pos)

        logger.debug(
            "place_map_in_world: %d maps at origin %s",
            len(positions), origin,
        )
        return positions


# ======================================================================
# 便捷函数
# ======================================================================

_global_map_api: MapAPI | None = None
_global_map_api_lock = threading.Lock()


def _get_global_map_api() -> MapAPI:
    """获取全局 MapAPI 单例。"""
    global _global_map_api
    with _global_map_api_lock:
        if _global_map_api is None:
            _global_map_api = MapAPI()
        return _global_map_api


def send_map_pixels(requests: list[PixelRequest]) -> bool:
    """发送地图像素数据 (便捷函数)。

    逆向自 strings: "SendMapPixels: connection not ready"

    Args:
        requests: 像素请求列表。

    Returns:
        True 如果发送成功。

    Raises:
        ConnectionNotReadyError: 连接未就绪。
    """
    return _get_global_map_api().send_map_pixels(requests)


def build_pixel_requests(
    pixels: bytes,
    width: int,
    height: int,
    map_id: int = 0,
) -> list[PixelRequest]:
    """从像素数据构建请求列表 (便捷函数)。

    Args:
        pixels: 像素数据 (RGBA)。
        width: 图像宽度。
        height: 图像高度。
        map_id: 起始地图 ID。

    Returns:
        PixelRequest 列表。
    """
    api = NexusAPI()
    return api.build_pixel_requests(pixels, width, height, map_id)


def request_subchunk(
    dimension: int,
    origin: BlockPos,
    target: BlockPos,
) -> SubChunkResponse:
    """请求子区块数据 (便捷函数)。

    Args:
        dimension: 维度 ID。
        origin: 起始位置。
        target: 目标位置。

    Returns:
        SubChunkResponse。
    """
    return _get_global_map_api().request_subchunk(dimension, origin, target)


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "MAP_PIXEL_SIZE", "SUBCHUNK_SIZE", "CHUNK_SIZE",
    "WORLD_MIN_Y", "WORLD_MAX_Y", "MAX_MAP_ID",
    "MAX_SEND_RETRIES", "SEND_TIMEOUT_SECONDS",
    # 异常
    "PixelRequestError", "ConnectionNotReadyError",
    "SubChunkRequestError", "InvalidPixelDataError",
    # 数据类
    "BlockPos", "ChunkPos",
    "SubChunkPos", "SubChunkKey", "SubChunkOffset",
    "SubChunkEntry", "SubChunkResponse",
    "PixelRequest",
    # 接口
    "Connection",
    # 主类
    "MapAPI", "NexusAPI",
    # 便捷函数
    "send_map_pixels", "build_pixel_requests", "request_subchunk",
]
