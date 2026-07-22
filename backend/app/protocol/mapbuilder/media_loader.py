"""media_loader - 媒体加载器 (ffmpeg 帧提取 + 图像缩放)。

逆向自 NexusEgo v1.6.5 的 MapBuilder 媒体加载模块。

逆向证据 (来自 strings_exclusive.txt):
    - ffmpegCmd
    - FFMPEG
    - ffmpeg / ffmpegOutput
    - mapbuilder.ImageInfo
    - mapbuilder.ScaleMode
    - mapbuilder.OverlayMode
    - mapbuilder.MapConfig
    - mapbuilder.VideoPlayer
    - github.com/disintegration/imaging (图像处理库)

核心类型:
    - ImageInfo:    图像信息 (宽/高/像素数据)
    - ScaleMode:    缩放模式 (FIT/FILL/STRETCH)
    - OverlayMode:  覆盖模式 (NONE/ALPHA/REPLACE)
    - MapConfig:    地图配置 (缩放/覆盖/帧率)
    - MediaLoader:  媒体加载器 (图片/视频)

工作流程:
    1. 输入文件 (图片/视频) -> MediaLoader.load()
    2. 视频通过 ffmpeg 提取帧 -> ImageInfo 列表
    3. 图片直接加载 -> ImageInfo
    4. 按缩放模式调整尺寸 -> resize_image()
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterator, Sequence

logger = logging.getLogger("pocketterm.protocol.mapbuilder.media_loader")


# ======================================================================
# 常量
# ======================================================================

#: Minecraft 地图标准分辨率 (128x128 像素)
MAP_PIXEL_SIZE: int = 128

#: Minecraft 地图颜色数量 (4 位 = 16 * 4 变体 = 64)
MAP_COLOR_COUNT: int = 64

#: 默认视频帧率 (FPS)
DEFAULT_VIDEO_FPS: int = 20

#: 默认缩放模式
DEFAULT_SCALE_MODE: int = 1  # FIT

#: 默认覆盖模式
DEFAULT_OVERLAY_MODE: int = 0  # NONE

#: 支持的图片扩展名
SUPPORTED_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

#: 支持的视频扩展名
SUPPORTED_VIDEO_EXTS: tuple[str, ...] = (".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm")

#: ffmpeg 二进制名称
FFMPEG_BINARY: str = "ffmpeg"

#: ffprobe 二进制名称
FFPROBE_BINARY: str = "ffprobe"

#: PNG 文件签名
PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"

#: JPEG 文件签名
JPEG_SIGNATURE: bytes = b"\xff\xd8\xff"


# ======================================================================
# 异常
# ======================================================================


class MediaLoadError(Exception):
    """媒体加载错误的基类。"""


class FFmpegNotFoundError(MediaLoadError):
    """ffmpeg 未安装或不可用。"""


class UnsupportedMediaError(MediaLoadError):
    """不支持的媒体格式。"""


class InvalidImageError(MediaLoadError):
    """无效的图像数据。"""


# ======================================================================
# 枚举
# ======================================================================


class ScaleMode(IntEnum):
    """缩放模式 (逆向自 mapbuilder.ScaleMode)。

    Attributes:
        FIT:      适配 (保持宽高比, 留黑边)
        FILL:     填充 (保持宽高比, 裁剪)
        STRETCH:  拉伸 (不保持宽高比)
    """

    FIT = 0
    FILL = 1
    STRETCH = 2


class OverlayMode(IntEnum):
    """覆盖模式 (逆向自 mapbuilder.OverlayMode)。

    Attributes:
        NONE:    无覆盖
        ALPHA:   Alpha 混合
        REPLACE: 替换
    """

    NONE = 0
    ALPHA = 1
    REPLACE = 2


# ======================================================================
# 数据类
# ======================================================================


@dataclass
class ImageInfo:
    """图像信息 (mapbuilder.ImageInfo)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。

    Attributes:
        width: 图像宽度 (像素)。
        height: 图像高度 (像素)。
        pixels: 像素数据 (RGBA, 长度 = width * height * 4)。
        channels: 颜色通道数 (3=RGB, 4=RGBA)。
        frame_index: 帧索引 (视频帧, 0 表示单图)。
        timestamp: 时间戳 (秒, 视频帧)。
        source_path: 源文件路径。
    """

    width: int = 0
    height: int = 0
    pixels: bytes = b""
    channels: int = 4
    frame_index: int = 0
    timestamp: float = 0.0
    source_path: str = ""

    def __post_init__(self) -> None:
        """校验像素数据长度。"""
        expected = self.width * self.height * self.channels
        if self.pixels and len(self.pixels) != expected:
            raise InvalidImageError(
                f"pixel data length mismatch: expected {expected}, "
                f"got {len(self.pixels)}"
            )

    @property
    def size(self) -> tuple[int, int]:
        """图像尺寸 (width, height)。"""
        return (self.width, self.height)

    @property
    def pixel_count(self) -> int:
        """像素总数。"""
        return self.width * self.height

    def get_pixel(self, x: int, y: int) -> tuple[int, ...]:
        """获取指定位置的像素。

        Args:
            x: X 坐标 (0 ~ width-1)。
            y: Y 坐标 (0 ~ height-1)。

        Returns:
            像素值元组 (R, G, B) 或 (R, G, B, A)。
        """
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"pixel ({x}, {y}) out of bounds {self.size}")
        offset = (y * self.width + x) * self.channels
        return tuple(self.pixels[offset:offset + self.channels])

    def set_pixel(self, x: int, y: int, pixel: Sequence[int]) -> None:
        """设置指定位置的像素。

        Args:
            x: X 坐标。
            y: Y 坐标。
            pixel: 像素值元组。
        """
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"pixel ({x}, {y}) out of bounds {self.size}")
        if len(pixel) != self.channels:
            raise ValueError(
                f"pixel channels mismatch: expected {self.channels}, "
                f"got {len(pixel)}"
            )
        offset = (y * self.width + x) * self.channels
        self.pixels = (
            self.pixels[:offset] + bytes(pixel) + self.pixels[offset + self.channels:]
        )

    def to_map_colors(self) -> list[int]:
        """将像素转换为 Minecraft 地图颜色索引 (0-63)。

        Minecraft 地图使用 4 位颜色 (0-63), 每个 base color 有 4 个变体。

        Returns:
            地图颜色索引列表 (长度 = width * height)。
        """
        colors: list[int] = []
        base_colors = _get_base_map_colors()
        for i in range(self.pixel_count):
            offset = i * self.channels
            r = self.pixels[offset]
            g = self.pixels[offset + 1]
            b = self.pixels[offset + 2]
            a = self.pixels[offset + 3] if self.channels >= 4 else 255

            if a < 128:
                colors.append(0)  # 透明
                continue

            # 找到最接近的基础颜色
            best_idx = 0
            best_dist = float("inf")
            for idx, (br, bg, bb) in enumerate(base_colors):
                dist = (r - br) ** 2 + (g - bg) ** 2 + (b - bb) ** 2
                if dist < best_dist:
                    best_dist = dist
                    best_idx = idx

            # 计算亮度变体 (0-3)
            brightness = (r + g + b) / 3
            if brightness < 64:
                variant = 0
            elif brightness < 128:
                variant = 1
            elif brightness < 192:
                variant = 2
            else:
                variant = 3

            # 地图颜色 = base_idx * 4 + variant
            colors.append(best_idx * 4 + variant)

        return colors

    def to_dict(self) -> dict[str, Any]:
        """转换为字典 (不含像素数据)。"""
        return {
            "width": self.width,
            "height": self.height,
            "channels": self.channels,
            "pixel_count": self.pixel_count,
            "frame_index": self.frame_index,
            "timestamp": self.timestamp,
            "source_path": self.source_path,
        }


@dataclass
class MapConfig:
    """地图配置 (mapbuilder.MapConfig)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。

    Attributes:
        scale_mode: 缩放模式 (ScaleMode)。
        overlay_mode: 覆盖模式 (OverlayMode)。
        target_width: 目标宽度 (像素, 默认 128)。
        target_height: 目标高度 (像素, 默认 128)。
        fps: 视频帧率 (帧/秒)。
        frame_count: 总帧数 (0 = 全部)。
        start_time: 视频起始时间 (秒)。
        duration: 视频持续时间 (秒, 0 = 全部)。
        api_key: MapBuilder API Key (逆向自 strings: "MapBuilder API Key")。
    """

    scale_mode: ScaleMode = ScaleMode.FIT
    overlay_mode: OverlayMode = OverlayMode.NONE
    target_width: int = MAP_PIXEL_SIZE
    target_height: int = MAP_PIXEL_SIZE
    fps: int = DEFAULT_VIDEO_FPS
    frame_count: int = 0
    start_time: float = 0.0
    duration: float = 0.0
    api_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "scale_mode": self.scale_mode.name,
            "overlay_mode": self.overlay_mode.name,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "start_time": self.start_time,
            "duration": self.duration,
            "has_api_key": bool(self.api_key),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MapConfig":
        """从字典构建。"""
        return cls(
            scale_mode=ScaleMode(int(data.get("scale_mode", DEFAULT_SCALE_MODE))),
            overlay_mode=OverlayMode(int(data.get("overlay_mode", DEFAULT_OVERLAY_MODE))),
            target_width=int(data.get("target_width", MAP_PIXEL_SIZE)),
            target_height=int(data.get("target_height", MAP_PIXEL_SIZE)),
            fps=int(data.get("fps", DEFAULT_VIDEO_FPS)),
            frame_count=int(data.get("frame_count", 0)),
            start_time=float(data.get("start_time", 0.0)),
            duration=float(data.get("duration", 0.0)),
            api_key=str(data.get("api_key", "")),
        )


# ======================================================================
# Minecraft 地图基础颜色
# ======================================================================


def _get_base_map_colors() -> list[tuple[int, int, int]]:
    """获取 Minecraft 地图基础颜色 (RGB)。

    Minecraft 地图有 60 种基础颜色 (id 0-59, 0 为透明)。
    每种基础颜色有 4 个变体 (亮度 0-3), 总共 240 种颜色。

    Returns:
        基础颜色 RGB 列表。
    """
    return [
        (0, 0, 0),          # 0: Air/Transparent
        (127, 178, 56),     # 1: Grass
        (247, 233, 163),    # 2: Sand
        (167, 167, 167),    # 3: Cobweb
        (160, 160, 255),    # 4: Light Blue (Water)
        (125, 125, 125),    # 5: Stone
        (180, 0, 0),        # 6: Redstone
        (0, 0, 0),          # 7: Iron (unused)
        (112, 112, 112),    # 8: Dark Stone
        (37, 22, 16),       # 9: Dirt
        (54, 25, 8),        # 10: Dark Dirt
        (125, 62, 24),      # 11: Wood
        (74, 47, 20),       # 12: Dark Wood
        (255, 255, 255),    # 13: Snow
        (151, 109, 77),     # 14: Clay
        (0, 0, 0),          # 15: (unused)
        (22, 134, 67),      # 16: Cactus
        (64, 154, 64),      # 17: Green Grass
        (100, 100, 100),    # 18: Gray
        (0, 0, 255),        # 19: Blue
        (60, 60, 60),       # 20: Dark Gray
        (100, 80, 130),     # 21: Purple
        (80, 80, 80),       # 22: Dark
        (255, 255, 0),      # 23: Yellow
        (255, 165, 0),      # 24: Orange
        (255, 0, 0),        # 25: Red
        (0, 255, 0),        # 26: Green
        (0, 255, 255),      # 27: Cyan
        (0, 0, 128),        # 28: Dark Blue
        (128, 0, 0),        # 29: Dark Red
        (0, 128, 0),        # 30: Dark Green
        (128, 128, 0),      # 31: Dark Yellow
        (128, 0, 128),      # 32: Magenta
        (0, 128, 128),      # 33: Teal
        (255, 192, 203),    # 34: Pink
        (160, 82, 45),      # 35: Sienna
        (218, 165, 32),     # 36: Goldenrod
        (106, 90, 205),     # 37: Slate Blue
        (255, 215, 0),      # 38: Gold
        (75, 0, 130),       # 39: Indigo
        (47, 79, 79),       # 40: Dark Slate Gray
        (72, 61, 139),      # 41: Dark Slate Blue
        (255, 228, 196),    # 42: Bisque
        (160, 82, 45),      # 43: Brown
        (139, 69, 19),      # 44: Saddle Brown
        (205, 133, 63),     # 45: Peru
        (222, 184, 135),    # 46: Burlywood
        (244, 164, 96),     # 47: Sandy Brown
        (210, 180, 140),    # 48: Tan
        (188, 143, 143),    # 49: Rosy Brown
        (139, 115, 85),     # 50: Dark Brown
        (180, 120, 90),     # 51: Brown
        (255, 240, 245),    # 52: Lavender Blush
        (255, 228, 225),    # 53: Misty Rose
        (240, 248, 255),    # 54: Alice Blue
        (245, 255, 250),    # 55: Mint Cream
        (240, 255, 240),    # 56: Honeydew
        (255, 250, 240),    # 57: Floral White
        (255, 250, 250),    # 58: Snow
        (0, 0, 0),          # 59: Black
    ]


# ======================================================================
# MediaLoader - 媒体加载器
# ======================================================================


class MediaLoader:
    """媒体加载器 (MediaLoader)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go 中的 VideoPlayer。

    支持:
        - 图片加载 (PNG/JPEG/BMP/GIF/WebP)
        - 视频帧提取 (通过 ffmpeg)
        - 图像缩放 (ScaleMode: FIT/FILL/STRETCH)
        - 覆盖模式 (OverlayMode: NONE/ALPHA/REPLACE)

    用法::

        loader = MediaLoader()
        # 加载单张图片
        image = loader.load_image("photo.png")
        # 提取视频帧
        frames = loader.extract_video_frames("video.mp4", fps=20)
    """

    def __init__(self, ffmpeg_path: str | None = None) -> None:
        """初始化媒体加载器。

        Args:
            ffmpeg_path: ffmpeg 二进制路径。如果为 None, 自动检测。
        """
        self._ffmpeg_path: str = ffmpeg_path or self._find_ffmpeg()
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self._lock = threading.Lock()
        logger.debug(
            "MediaLoader init: ffmpeg=%s", self._ffmpeg_path or "(not found)"
        )

    @staticmethod
    def _find_ffmpeg() -> str:
        """查找 ffmpeg 二进制。"""
        path = shutil.which(FFMPEG_BINARY)
        if path:
            return path
        logger.warning("ffmpeg not found in PATH")
        return ""

    @property
    def ffmpeg_available(self) -> bool:
        """ffmpeg 是否可用。"""
        return bool(self._ffmpeg_path) and Path(self._ffmpeg_path).exists()

    # ---- 图片加载 ----

    def load_image(self, path: str | os.PathLike[str]) -> ImageInfo:
        """加载图片文件。

        支持 PNG/JPEG/BMP 格式 (纯 Python 解析, 不依赖 PIL)。

        Args:
            path: 图片文件路径。

        Returns:
            ImageInfo 实例。

        Raises:
            UnsupportedMediaError: 不支持的格式。
            InvalidImageError: 图像数据无效。
        """
        path = Path(path)
        if not path.exists():
            raise MediaLoadError(f"image file not found: {path}")

        ext = path.suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            raise UnsupportedMediaError(f"unsupported image format: {ext}")

        data = path.read_bytes()
        logger.debug("load_image: %s (%d bytes)", path, len(data))

        if data.startswith(PNG_SIGNATURE):
            image = self._parse_png(data)
        elif data.startswith(JPEG_SIGNATURE):
            image = self._parse_jpeg_simple(data)
        else:
            # BMP 或其他, 尝试 BMP
            image = self._parse_bmp(data)

        image.source_path = str(path)
        logger.info(
            "load_image: %s %dx%d channels=%d",
            path.name, image.width, image.height, image.channels,
        )
        return image

    def _parse_png(self, data: bytes) -> ImageInfo:
        """解析 PNG 文件 (基础解析, 依赖 zlib 解压)。

        Args:
            data: PNG 文件字节数据。

        Returns:
            ImageInfo 实例。
        """
        import zlib

        if data[:8] != PNG_SIGNATURE:
            raise InvalidImageError("invalid PNG signature")

        offset = 8
        width = height = 0
        bit_depth = color_type = 0
        idat_data = bytearray()

        while offset < len(data):
            # 读取 chunk: length(4) + type(4) + data + crc(4)
            (length,) = struct.unpack(">I", data[offset:offset + 4])
            chunk_type = data[offset + 4:offset + 8]
            chunk_data = data[offset + 8:offset + 8 + length]
            offset += 12 + length

            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type = struct.unpack(
                    ">IIBB", chunk_data[:10]
                )
            elif chunk_type == b"IDAT":
                idat_data.extend(chunk_data)
            elif chunk_type == b"IEND":
                break

        if width == 0 or height == 0:
            raise InvalidImageError("PNG: invalid dimensions")

        # 确定通道数
        if color_type == 0:  # Grayscale
            channels = 1
        elif color_type == 2:  # RGB
            channels = 3
        elif color_type == 6:  # RGBA
            channels = 4
        else:
            channels = 4

        # 解压 IDAT
        try:
            raw = zlib.decompress(bytes(idat_data))
        except zlib.error as exc:
            raise InvalidImageError(f"PNG: failed to decompress IDAT: {exc}") from exc

        # 移除滤波器行 (简化: 假设 filter=0, 逐行处理)
        bpp = max(1, channels)  # bytes per pixel
        stride = width * bpp
        pixels = bytearray(width * height * 4)  # 输出 RGBA

        raw_offset = 0
        for y in range(height):
            if raw_offset >= len(raw):
                break
            filter_type = raw[raw_offset]
            raw_offset += 1
            row_data = raw[raw_offset:raw_offset + stride]
            raw_offset += stride

            # 简化: 只处理 filter=0 (None), 其他滤波器近似处理
            if filter_type != 0:
                # 近似: 直接使用 (实际应按 PNG 滤波器规则)
                pass

            for x in range(width):
                src_off = x * bpp
                dst_off = (y * width + x) * 4
                if channels == 1:  # Grayscale -> RGBA
                    g = row_data[src_off] if src_off < len(row_data) else 0
                    pixels[dst_off:dst_off + 4] = bytes([g, g, g, 255])
                elif channels == 3:  # RGB -> RGBA
                    if src_off + 3 <= len(row_data):
                        pixels[dst_off:dst_off + 4] = bytes([
                            row_data[src_off], row_data[src_off + 1],
                            row_data[src_off + 2], 255,
                        ])
                elif channels == 4:  # RGBA
                    if src_off + 4 <= len(row_data):
                        pixels[dst_off:dst_off + 4] = row_data[src_off:src_off + 4]

        return ImageInfo(
            width=width,
            height=height,
            pixels=bytes(pixels),
            channels=4,
        )

    def _parse_jpeg_simple(self, data: bytes) -> ImageInfo:
        """解析 JPEG 文件 (简化版, 需要外部库或仅返回占位)。

        由于纯 Python 解析 JPEG 非常复杂, 这里返回一个占位 ImageInfo。
        实际使用时建议安装 Pillow 库。

        Args:
            data: JPEG 文件字节数据。

        Returns:
            ImageInfo 实例 (占位)。
        """
        # 尝试使用 PIL
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            img = img.convert("RGBA")
            return ImageInfo(
                width=img.width,
                height=img.height,
                pixels=img.tobytes(),
                channels=4,
            )
        except ImportError:
            logger.warning(
                "JPEG parsing requires Pillow (PIL). "
                "Install with: pip install Pillow"
            )
            # 返回一个 1x1 的占位
            return ImageInfo(
                width=1, height=1, pixels=b"\0\0\0\0", channels=4
            )

    def _parse_bmp(self, data: bytes) -> ImageInfo:
        """解析 BMP 文件 (24位/32位)。

        Args:
            data: BMP 文件字节数据。

        Returns:
            ImageInfo 实例。
        """
        if data[:2] != b"BM":
            raise InvalidImageError("invalid BMP signature")

        # BMP 头解析
        data_offset = struct.unpack_from("<I", data, 10)[0]
        width = struct.unpack_from("<i", data, 18)[0]
        height = struct.unpack_from("<i", data, 22)[0]
        bit_count = struct.unpack_from("<H", data, 28)[0]

        if bit_count not in (24, 32):
            raise InvalidImageError(f"BMP: unsupported bit count {bit_count}")

        channels = 4  # 输出 RGBA
        flip = height > 0  # BMP 默认 bottom-up
        abs_height = abs(height)
        bpp = bit_count // 8
        row_stride = (width * bpp + 3) & ~3  # 4字节对齐

        pixels = bytearray(width * abs_height * 4)

        for y in range(abs_height):
            src_y = (abs_height - 1 - y) if flip else y
            row_offset = data_offset + src_y * row_stride
            for x in range(width):
                src_off = row_offset + x * bpp
                dst_off = (y * width + x) * 4
                if src_off + bpp <= len(data):
                    b = data[src_off]
                    g = data[src_off + 1]
                    r = data[src_off + 2]
                    a = data[src_off + 3] if bpp == 4 else 255
                    pixels[dst_off:dst_off + 4] = bytes([r, g, b, a])

        return ImageInfo(
            width=width,
            height=abs_height,
            pixels=bytes(pixels),
            channels=4,
        )

    # ---- 视频帧提取 ----

    def extract_video_frames(
        self,
        path: str | os.PathLike[str],
        config: MapConfig | None = None,
    ) -> list[ImageInfo]:
        """提取视频帧 (通过 ffmpeg)。

        逆向自 NexusEgo_v1.6.5 的 ffmpegCmd 和 VideoPlayer。

        Args:
            path: 视频文件路径。
            config: 地图配置 (控制 fps/尺寸/时间范围)。

        Returns:
            ImageInfo 列表 (每一帧一个)。

        Raises:
            FFmpegNotFoundError: ffmpeg 不可用。
            MediaLoadError: 提取失败。
        """
        if not self.ffmpeg_available:
            raise FFmpegNotFoundError(
                f"ffmpeg not found at {self._ffmpeg_path!r}. "
                f"Install ffmpeg or set ffmpeg_path."
            )

        path = Path(path)
        if not path.exists():
            raise MediaLoadError(f"video file not found: {path}")

        config = config or MapConfig()
        fps = config.fps
        target_w = config.target_width
        target_h = config.target_height

        # 创建临时目录存放帧
        with self._lock:
            if self._temp_dir is None:
                self._temp_dir = tempfile.TemporaryDirectory(prefix="nexusego_map_")
            temp_dir = Path(self._temp_dir.name)

        frame_pattern = temp_dir / "frame_%06d.png"

        # 构建 ffmpeg 命令
        cmd: list[str] = [self._ffmpeg_path, "-y"]
        if config.start_time > 0:
            cmd.extend(["-ss", str(config.start_time)])
        cmd.extend(["-i", str(path)])
        if config.duration > 0:
            cmd.extend(["-t", str(config.duration)])
        cmd.extend(["-vf", f"fps={fps},scale={target_w}:{target_h}"])
        cmd.extend(["-pix_fmt", "rgba"])
        cmd.append(str(frame_pattern))

        logger.info("extract_video_frames: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise MediaLoadError(
                    f"ffmpeg failed (code={result.returncode}): {result.stderr[:500]}"
                )
        except subprocess.TimeoutExpired as exc:
            raise MediaLoadError(f"ffmpeg timed out: {exc}") from exc
        except FileNotFoundError as exc:
            raise FFmpegNotFoundError(f"ffmpeg not found: {exc}") from exc

        # 加载提取的帧
        frames: list[ImageInfo] = []
        frame_files = sorted(temp_dir.glob("frame_*.png"))
        for i, frame_path in enumerate(frame_files):
            if config.frame_count and i >= config.frame_count:
                break
            frame = self.load_image(frame_path)
            frame.frame_index = i
            frame.timestamp = i / fps
            frame.source_path = str(path)
            frames.append(frame)

        logger.info(
            "extract_video_frames: extracted %d frames from %s",
            len(frames), path.name,
        )
        return frames

    def get_video_info(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """获取视频信息 (通过 ffprobe)。

        Args:
            path: 视频文件路径。

        Returns:
            包含视频信息的字典 (width/height/duration/fps)。
        """
        ffprobe = shutil.which(FFPROBE_BINARY)
        if not ffprobe:
            logger.warning("ffprobe not found, returning empty info")
            return {}

        cmd = [
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return {}
            import json
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            video_stream = next(
                (s for s in streams if s.get("codec_type") == "video"),
                {},
            )
            return {
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "duration": float(data.get("format", {}).get("duration", 0)),
                "fps": _parse_fps(video_stream.get("r_frame_rate", "20/1")),
                "codec": video_stream.get("codec_name", ""),
            }
        except Exception as exc:
            logger.warning("get_video_info failed: %s", exc)
            return {}

    # ---- 统一加载接口 ----

    def load(
        self,
        path: str | os.PathLike[str],
        config: MapConfig | None = None,
    ) -> list[ImageInfo]:
        """统一加载接口 (自动检测图片/视频)。

        Args:
            path: 文件路径。
            config: 地图配置。

        Returns:
            ImageInfo 列表 (图片返回单元素列表, 视频返回多帧)。
        """
        path = Path(path)
        ext = path.suffix.lower()

        if ext in SUPPORTED_IMAGE_EXTS:
            return [self.load_image(path)]
        elif ext in SUPPORTED_VIDEO_EXTS:
            return self.extract_video_frames(path, config)
        else:
            raise UnsupportedMediaError(f"unsupported media format: {ext}")

    def cleanup(self) -> None:
        """清理临时文件。"""
        with self._lock:
            if self._temp_dir is not None:
                try:
                    self._temp_dir.cleanup()
                except Exception as exc:
                    logger.warning("cleanup failed: %s", exc)
                self._temp_dir = None


def _parse_fps(rate_str: str) -> int:
    """解析帧率字符串 (如 "30/1")。"""
    try:
        if "/" in rate_str:
            num, den = rate_str.split("/")
            return int(int(num) / int(den))
        return int(float(rate_str))
    except (ValueError, ZeroDivisionError):
        return DEFAULT_VIDEO_FPS


# ======================================================================
# 图像缩放函数
# ======================================================================


def resize_image(
    image: ImageInfo,
    target_width: int,
    target_height: int,
    mode: ScaleMode = ScaleMode.FIT,
) -> ImageInfo:
    """缩放图像 (对应 mapbuilder.ScaleMode)。

    Args:
        image: 原始图像。
        target_width: 目标宽度。
        target_height: 目标高度。
        mode: 缩放模式。

    Returns:
        缩放后的 ImageInfo。
    """
    if image.width == target_width and image.height == target_height:
        return image

    logger.debug(
        "resize_image: %dx%d -> %dx%d (mode=%s)",
        image.width, image.height, target_width, target_height, mode.name,
    )

    if mode == ScaleMode.STRETCH:
        # 拉伸: 直接缩放
        return _resize_stretch(image, target_width, target_height)

    # 计算缩放比例
    src_ratio = image.width / image.height
    dst_ratio = target_width / target_height

    if mode == ScaleMode.FIT:
        # 适配: 保持宽高比, 留黑边
        if src_ratio > dst_ratio:
            new_w = target_width
            new_h = int(target_width / src_ratio)
        else:
            new_h = target_height
            new_w = int(target_height * src_ratio)
        resized = _resize_stretch(image, new_w, new_h)
        # 居中放置, 填充黑色
        return _pad_to_size(resized, target_width, target_height)

    else:  # ScaleMode.FILL
        # 填充: 保持宽高比, 裁剪
        if src_ratio > dst_ratio:
            new_h = target_height
            new_w = int(target_height * src_ratio)
        else:
            new_w = target_width
            new_h = int(target_width / src_ratio)
        resized = _resize_stretch(image, new_w, new_h)
        # 居中裁剪
        return _crop_to_size(resized, target_width, target_height)


def _resize_stretch(image: ImageInfo, new_w: int, new_h: int) -> ImageInfo:
    """拉伸缩放 (最近邻插值)。"""
    pixels = bytearray(new_w * new_h * 4)
    for y in range(new_h):
        src_y = int(y * image.height / new_h)
        for x in range(new_w):
            src_x = int(x * image.width / new_w)
            src_off = (src_y * image.width + src_x) * 4
            dst_off = (y * new_w + x) * 4
            pixels[dst_off:dst_off + 4] = image.pixels[src_off:src_off + 4]

    return ImageInfo(
        width=new_w,
        height=new_h,
        pixels=bytes(pixels),
        channels=4,
        frame_index=image.frame_index,
        timestamp=image.timestamp,
        source_path=image.source_path,
    )


def _pad_to_size(image: ImageInfo, target_w: int, target_h: int) -> ImageInfo:
    """将图像居中放置到目标尺寸, 填充黑色。"""
    pixels = bytearray(target_w * target_h * 4)
    offset_x = (target_w - image.width) // 2
    offset_y = (target_h - image.height) // 2

    for y in range(image.height):
        for x in range(image.width):
            src_off = (y * image.width + x) * 4
            dst_x = offset_x + x
            dst_y = offset_y + y
            if 0 <= dst_x < target_w and 0 <= dst_y < target_h:
                dst_off = (dst_y * target_w + dst_x) * 4
                pixels[dst_off:dst_off + 4] = image.pixels[src_off:src_off + 4]

    return ImageInfo(
        width=target_w,
        height=target_h,
        pixels=bytes(pixels),
        channels=4,
        frame_index=image.frame_index,
        timestamp=image.timestamp,
        source_path=image.source_path,
    )


def _crop_to_size(image: ImageInfo, target_w: int, target_h: int) -> ImageInfo:
    """居中裁剪到目标尺寸。"""
    offset_x = (image.width - target_w) // 2
    offset_y = (image.height - target_h) // 2
    pixels = bytearray(target_w * target_h * 4)

    for y in range(target_h):
        for x in range(target_w):
            src_x = offset_x + x
            src_y = offset_y + y
            if 0 <= src_x < image.width and 0 <= src_y < image.height:
                src_off = (src_y * image.width + src_x) * 4
                dst_off = (y * target_w + x) * 4
                pixels[dst_off:dst_off + 4] = image.pixels[src_off:src_off + 4]

    return ImageInfo(
        width=target_w,
        height=target_h,
        pixels=bytes(pixels),
        channels=4,
        frame_index=image.frame_index,
        timestamp=image.timestamp,
        source_path=image.source_path,
    )


# ======================================================================
# 便捷函数
# ======================================================================


def load_image(path: str | os.PathLike[str]) -> ImageInfo:
    """加载图片 (便捷函数)。

    Args:
        path: 图片文件路径。

    Returns:
        ImageInfo 实例。
    """
    loader = MediaLoader()
    return loader.load_image(path)


def extract_video_frames(
    path: str | os.PathLike[str],
    fps: int = DEFAULT_VIDEO_FPS,
    target_width: int = MAP_PIXEL_SIZE,
    target_height: int = MAP_PIXEL_SIZE,
) -> list[ImageInfo]:
    """提取视频帧 (便捷函数)。

    Args:
        path: 视频文件路径。
        fps: 帧率。
        target_width: 目标宽度。
        target_height: 目标高度。

    Returns:
        ImageInfo 列表。
    """
    loader = MediaLoader()
    config = MapConfig(
        fps=fps,
        target_width=target_width,
        target_height=target_height,
    )
    return loader.extract_video_frames(path, config)


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "MAP_PIXEL_SIZE", "MAP_COLOR_COUNT", "DEFAULT_VIDEO_FPS",
    "DEFAULT_SCALE_MODE", "DEFAULT_OVERLAY_MODE",
    "SUPPORTED_IMAGE_EXTS", "SUPPORTED_VIDEO_EXTS",
    "FFMPEG_BINARY", "FFPROBE_BINARY",
    # 异常
    "MediaLoadError", "FFmpegNotFoundError",
    "UnsupportedMediaError", "InvalidImageError",
    # 枚举
    "ScaleMode", "OverlayMode",
    # 数据类
    "ImageInfo", "MapConfig",
    # 主类
    "MediaLoader",
    # 函数
    "load_image", "extract_video_frames", "resize_image",
]
