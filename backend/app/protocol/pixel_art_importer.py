"""像素画导入器。

逆向来源: NexusE MapBuilder 图像处理能力
- NexusE v1.6.5: builder/map_builder.go
- NexusE v1.6.5: builder/image_processor.go

功能:
    - 支持PNG/JPEG/GIF/BMP/WebP图像输入
    - 图像缩放 (Fit/Fill/Stretch模式)
    - 像素颜色->Minecraft方块映射 (concrete, wool, terracotta等)
    - 支持抖动算法 (可选) 提升颜色还原度
    - 生成setblock命令批量放置
    - 支持指定起始位置和朝向
    - 输出进度追踪
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional

from .blocks import BlockState

logger = logging.getLogger("pocketterm.protocol.pixel_art_importer")


# ----------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------


class ScaleMode(Enum):
    """缩放模式。

    逆向自 NexusE builder/image_processor.go
    """

    FIT = "fit"
    """适应模式: 保持纵横比, 缩放至不超过目标尺寸"""

    FILL = "fill"
    """填充模式: 保持纵横比, 缩放到至少覆盖目标尺寸"""

    STRETCH = "stretch"
    """拉伸模式: 不保持纵横比, 直接缩放到目标尺寸"""

    NONE = "none"
    """不缩放"""


class DitherMode(Enum):
    """抖动算法模式。

    用于提升颜色还原度的抖动算法。
    """

    NONE = "none"
    """不使用抖动"""

    FLOYD_STEINBERG = "floyd_steinberg"
    """Floyd-Steinberg 抖动算法"""

    ORDERED = "ordered"
    """有序抖动 (Bayer 矩阵)"""


class Orientation(Enum):
    """像素画朝向。

    Attributes:
        VERTICAL_NS: 垂直面, 南北朝向 (XZ平面)
        VERTICAL_EW: 垂直面, 东西朝向 (XZ平面旋转90度)
        HORIZONTAL: 水平面 (XY平面, 俯视)
    """

    VERTICAL_NS = "vertical_ns"
    """垂直面, 南北朝向 (XZ平面)"""

    VERTICAL_EW = "vertical_ew"
    """垂直面, 东西朝向"""

    HORIZONTAL = "horizontal"
    """水平面, 俯视"""


# ----------------------------------------------------------------------
# 颜色->方块映射
# ----------------------------------------------------------------------


@dataclass
class ColorBlock:
    """颜色到方块的映射条目。

    Attributes:
        r, g, b: RGB颜色值 (0-255)
        block: 对应的方块状态
        category: 方块类别 (concrete, wool, terracotta等)
    """

    r: int
    g: int
    b: int
    block: BlockState
    category: str = "unknown"

    @property
    def rgb(self) -> tuple[int, int, int]:
        """RGB颜色元组。"""
        return (self.r, self.g, self.b)

    def color_distance(self, r: int, g: int, b: int) -> float:
        """计算与目标颜色的欧几里得距离。

        Args:
            r, g, b: 目标RGB颜色

        Returns:
            颜色距离 (0.0 = 完全相同)。
        """
        return math.sqrt(
            (self.r - r) ** 2 + (self.g - g) ** 2 + (self.b - b) ** 2
        )


class ColorMapper:
    """颜色到Minecraft方块的映射器。

    逆向自 NexusE builder/image_processor.go

    支持多种方块类别的颜色映射:
        - concrete (混凝土): 16色
        - wool (羊毛): 16色
        - terracotta (陶瓦): 16色
        - concrete_powder (混凝土粉末): 16色
        - glass (玻璃): 16色
        - glazed_terracotta (带釉陶瓦): 16色
    """

    #: 混凝土颜色表 (RGB)
    CONCRETE_COLORS: dict[str, tuple[int, int, int]] = {
        "white": (207, 213, 214),
        "orange": (224, 97, 0),
        "magenta": (169, 48, 159),
        "light_blue": (36, 137, 199),
        "yellow": (241, 175, 21),
        "lime": (94, 169, 24),
        "pink": (214, 127, 153),
        "gray": (55, 58, 62),
        "light_gray": (125, 125, 115),
        "cyan": (21, 119, 136),
        "purple": (100, 31, 156),
        "blue": (45, 47, 143),
        "brown": (96, 60, 31),
        "green": (73, 91, 36),
        "red": (142, 33, 32),
        "black": (8, 10, 15),
    }

    #: 羊毛颜色表 (RGB) - 与混凝土略有不同
    WOOL_COLORS: dict[str, tuple[int, int, int]] = {
        "white": (233, 236, 236),
        "orange": (240, 118, 19),
        "magenta": (189, 68, 179),
        "light_blue": (58, 175, 217),
        "yellow": (248, 198, 39),
        "lime": (112, 185, 25),
        "pink": (237, 141, 172),
        "gray": (62, 68, 71),
        "light_gray": (142, 142, 134),
        "cyan": (21, 137, 145),
        "purple": (121, 42, 172),
        "blue": (53, 57, 157),
        "brown": (114, 71, 40),
        "green": (84, 109, 27),
        "red": (160, 39, 34),
        "black": (20, 21, 25),
    }

    #: 陶瓦颜色表 (RGB)
    TERRACOTTA_COLORS: dict[str, tuple[int, int, int]] = {
        "white": (209, 177, 161),
        "orange": (161, 83, 37),
        "magenta": (149, 87, 108),
        "light_blue": (112, 108, 138),
        "yellow": (186, 133, 35),
        "lime": (103, 117, 52),
        "pink": (160, 79, 76),
        "gray": (57, 41, 35),
        "light_gray": (134, 106, 96),
        "cyan": (86, 91, 91),
        "purple": (118, 69, 86),
        "blue": (73, 58, 90),
        "brown": (77, 51, 35),
        "green": (75, 82, 41),
        "red": (143, 60, 46),
        "black": (37, 22, 16),
    }

    def __init__(self, category: str = "concrete") -> None:
        """
        Args:
            category: 方块类别 (concrete/wool/terracotta/concrete_powder/glass)
        """
        self.category = category
        self._build_palette()

    def _build_palette(self) -> None:
        """构建颜色调色板。"""
        color_map = {
            "concrete": self.CONCRETE_COLORS,
            "wool": self.WOOL_COLORS,
            "terracotta": self.TERRACOTTA_COLORS,
            "concrete_powder": self.CONCRETE_COLORS,  # 使用混凝土颜色
            "glass": self.CONCRETE_COLORS,  # 使用混凝土颜色
        }

        colors = color_map.get(self.category, self.CONCRETE_COLORS)
        prefix = f"minecraft:{self.category}"

        self._palette: list[ColorBlock] = []
        for color_name, rgb in colors.items():
            self._palette.append(ColorBlock(
                r=rgb[0], g=rgb[1], b=rgb[2],
                block=BlockState(name=prefix, states={"color": color_name}),
                category=self.category,
            ))

    def find_closest(self, r: int, g: int, b: int) -> ColorBlock:
        """找到最接近的颜色方块。

        Args:
            r, g, b: RGB颜色值 (0-255)

        Returns:
            最接近的 ColorBlock。
        """
        closest = self._palette[0]
        closest_dist = closest.color_distance(r, g, b)

        for cb in self._palette[1:]:
            dist = cb.color_distance(r, g, b)
            if dist < closest_dist:
                closest = cb
                closest_dist = dist

        return closest

    def get_all_blocks(self) -> list[ColorBlock]:
        """获取所有调色板方块。"""
        return self._palette.copy()


# ----------------------------------------------------------------------
# 图像缩放器
# ----------------------------------------------------------------------


class ImageScaler:
    """图像缩放器。

    逆向自 NexusE builder/image_processor.go

    支持多种缩放模式:
        - Fit: 保持纵横比, 缩放至不超过目标尺寸
        - Fill: 保持纵横比, 缩放到至少覆盖目标尺寸
        - Stretch: 不保持纵横比, 直接缩放到目标尺寸
    """

    @staticmethod
    def calculate_size(
        orig_width: int,
        orig_height: int,
        target_width: int,
        target_height: int,
        mode: ScaleMode = ScaleMode.FIT,
    ) -> tuple[int, int]:
        """计算缩放后的尺寸。

        Args:
            orig_width: 原始宽度
            orig_height: 原始高度
            target_width: 目标宽度
            target_height: 目标高度
            mode: 缩放模式

        Returns:
            (new_width, new_height) 缩放后的尺寸。
        """
        if mode == ScaleMode.NONE:
            return (orig_width, orig_height)

        if mode == ScaleMode.STRETCH:
            return (target_width, target_height)

        orig_ratio = orig_width / orig_height if orig_height > 0 else 1.0
        target_ratio = target_width / target_height if target_height > 0 else 1.0

        if mode == ScaleMode.FIT:
            if orig_ratio > target_ratio:
                new_width = target_width
                new_height = int(target_width / orig_ratio)
            else:
                new_height = target_height
                new_width = int(target_height * orig_ratio)
        elif mode == ScaleMode.FILL:
            if orig_ratio < target_ratio:
                new_width = target_width
                new_height = int(target_width / orig_ratio)
            else:
                new_height = target_height
                new_width = int(target_height * orig_ratio)
        else:
            return (target_width, target_height)

        return (max(1, new_width), max(1, new_height))


# ----------------------------------------------------------------------
# 抖动算法
# ----------------------------------------------------------------------


class Ditherer:
    """抖动算法处理器。

    提升颜色还原度, 减少颜色量化带来的色带。
    """

    #: Bayer 有序抖动矩阵 (8x8)
    BAYER_MATRIX_8X8: list[list[int]] = [
        [0, 48, 12, 60, 3, 51, 15, 63],
        [32, 16, 44, 28, 35, 19, 47, 31],
        [8, 56, 4, 52, 11, 59, 7, 55],
        [40, 24, 36, 20, 43, 27, 39, 23],
        [2, 50, 14, 62, 1, 49, 13, 61],
        [34, 18, 46, 30, 33, 17, 45, 29],
        [10, 58, 6, 54, 9, 57, 5, 53],
        [42, 26, 38, 22, 41, 25, 37, 21],
    ]

    def __init__(self, mode: DitherMode = DitherMode.NONE) -> None:
        """
        Args:
            mode: 抖动算法模式。
        """
        self.mode = mode

    def apply(
        self,
        r: int,
        g: int,
        b: int,
        x: int,
        y: int,
        error_map: Optional[dict[tuple[int, int], tuple[float, float, float]]] = None,
    ) -> tuple[int, int, int]:
        """应用抖动算法到颜色。

        Args:
            r, g, b: 原始RGB颜色
            x, y: 像素坐标
            error_map: Floyd-Steinberg误差映射表

        Returns:
            抖动后的RGB颜色。
        """
        if self.mode == DitherMode.NONE:
            return (r, g, b)

        if self.mode == DitherMode.ORDERED:
            return self._ordered_dither(r, g, b, x, y)

        if self.mode == DitherMode.FLOYD_STEINBERG and error_map is not None:
            return self._floyd_steinberg_dither(r, g, b, x, y, error_map)

        return (r, g, b)

    def _ordered_dither(
        self, r: int, g: int, b: int, x: int, y: int
    ) -> tuple[int, int, int]:
        """有序抖动 (Bayer 矩阵)。

        Args:
            r, g, b: RGB颜色
            x, y: 像素坐标

        Returns:
            抖动后的RGB颜色。
        """
        threshold = self.BAYER_MATRIX_8X8[y % 8][x % 8] / 64.0 - 0.5
        offset = int(threshold * 32)

        return (
            max(0, min(255, r + offset)),
            max(0, min(255, g + offset)),
            max(0, min(255, b + offset)),
        )

    def _floyd_steinberg_dither(
        self,
        r: int,
        g: int,
        b: int,
        x: int,
        y: int,
        error_map: dict[tuple[int, int], tuple[float, float, float]],
    ) -> tuple[int, int, int]:
        """Floyd-Steinberg 误差扩散。

        Args:
            r, g, b: 原始RGB颜色
            x, y: 像素坐标
            error_map: 误差映射表

        Returns:
            扩散后的RGB颜色。
        """
        error = error_map.get((x, y), (0.0, 0.0, 0.0))
        adjusted = (
            r + int(error[0]),
            g + int(error[1]),
            b + int(error[2]),
        )
        return (
            max(0, min(255, adjusted[0])),
            max(0, min(255, adjusted[1])),
            max(0, min(255, adjusted[2])),
        )


# ----------------------------------------------------------------------
# 像素画导入器
# ----------------------------------------------------------------------


@dataclass
class PixelArtConfig:
    """像素画导入配置。

    Attributes:
        start_x, start_y, start_z: 起始世界坐标
        orientation: 像素画朝向
        scale_mode: 缩放模式
        target_width: 目标宽度 (像素)
        target_height: 目标高度 (像素)
        color_category: 方块类别 (concrete/wool/terracotta等)
        dither_mode: 抖动算法模式
        include_air: 是否包含空气方块
    """

    start_x: int = 0
    start_y: int = 64
    start_z: int = 0
    orientation: Orientation = Orientation.VERTICAL_NS
    scale_mode: ScaleMode = ScaleMode.FIT
    target_width: int = 128
    target_height: int = 128
    color_category: str = "concrete"
    dither_mode: DitherMode = DitherMode.NONE
    include_air: bool = False


class PixelArtImporter:
    """像素画导入器。

    逆向自 NexusE MapBuilder 图像处理能力

    将图像转换为Minecraft方块像素画。

    使用示例::

        importer = PixelArtImporter()
        config = PixelArtConfig(
            start_x=0, start_y=64, start_z=0,
            target_width=64, target_height=64,
            color_category="concrete",
        )
        blocks = await importer.import_image("/path/to/image.png", config)
        for x, y, z, block in blocks:
            print(f"setblock {x} {y} {z} {block.name}")
    """

    def __init__(self) -> None:
        """初始化像素画导入器。"""
        self._progress_callback: Optional[Callable[[int, int], None]] = None

    def set_progress_callback(
        self, callback: Optional[Callable[[int, int], None]]
    ) -> None:
        """设置进度回调。

        Args:
            callback: 进度回调函数 (current, total)。
        """
        self._progress_callback = callback

    async def import_image(
        self,
        image_path: str | Path,
        config: Optional[PixelArtConfig] = None,
    ) -> list[tuple[int, int, int, BlockState]]:
        """导入图像并生成方块放置列表。

        Args:
            image_path: 图像文件路径
            config: 导入配置

        Returns:
            (x, y, z, BlockState) 元组列表。

        Raises:
            FileNotFoundError: 图像文件不存在。
            ValueError: 图像格式不支持。
        """
        config = config or PixelArtConfig()
        image_path = Path(image_path)

        if not image_path.exists():
            raise FileNotFoundError(f"图像文件不存在: {image_path}")

        # 读取图像
        pixels, width, height = self._read_image(image_path, config)

        # 颜色映射
        mapper = ColorMapper(category=config.color_category)
        ditherer = Ditherer(mode=config.dither_mode)

        # 生成方块
        blocks: list[tuple[int, int, int, BlockState]] = []
        total = width * height
        processed = 0

        for py in range(height):
            for px in range(width):
                r, g, b, a = pixels[py * width + px]

                # 跳过透明像素
                if a < 128 and not config.include_air:
                    processed += 1
                    continue

                # 应用抖动
                dr, dg, db = ditherer.apply(r, g, b, px, py)

                # 找到最接近的颜色
                color_block = mapper.find_closest(dr, dg, db)

                # 计算世界坐标
                wx, wy, wz = self._pixel_to_world(
                    px, py, config.start_x, config.start_y, config.start_z, config.orientation
                )

                blocks.append((wx, wy, wz, color_block.block))
                processed += 1

                if self._progress_callback and processed % 100 == 0:
                    self._progress_callback(processed, total)

        if self._progress_callback:
            self._progress_callback(processed, total)

        logger.info(
            "像素画导入完成: %d x %d = %d 方块, 类别: %s",
            width, height, len(blocks), config.color_category,
        )

        return blocks

    def _read_image(
        self,
        path: Path,
        config: PixelArtConfig,
    ) -> tuple[list[tuple[int, int, int, int]], int, int]:
        """读取图像并缩放。

        Args:
            path: 图像路径
            config: 导入配置

        Returns:
            (像素列表, 宽度, 高度) 元组。
            像素格式: (R, G, B, A) 每个通道 0-255。

        Raises:
            ImportError: PIL/Pillow不可用。
            ValueError: 图像格式不支持。
        """
        try:
            from PIL import Image
        except ImportError:
            raise ImportError(
                "像素画导入需要PIL/Pillow库。请安装: pip install Pillow"
            )

        suffix = path.suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            raise ValueError(f"不支持的图像格式: {suffix}")

        try:
            img = Image.open(path)
        except Exception as e:
            raise ValueError(f"无法打开图像: {path} -> {e}") from e

        # 转换为RGBA
        img = img.convert("RGBA")

        # 缩放
        orig_w, orig_h = img.size
        if config.scale_mode != ScaleMode.NONE:
            new_w, new_h = ImageScaler.calculate_size(
                orig_w, orig_h, config.target_width, config.target_height, config.scale_mode
            )
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info("图像缩放: %d x %d -> %d x %d", orig_w, orig_h, new_w, new_h)
        else:
            new_w, new_h = orig_w, orig_h

        # 提取像素数据
        pixels: list[tuple[int, int, int, int]] = []
        raw_data = img.getdata()
        for pixel in raw_data:
            if len(pixel) >= 4:
                pixels.append((pixel[0], pixel[1], pixel[2], pixel[3]))
            elif len(pixel) >= 3:
                pixels.append((pixel[0], pixel[1], pixel[2], 255))
            else:
                pixels.append((pixel[0], pixel[0], pixel[0], 255))

        return pixels, new_w, new_h

    def _pixel_to_world(
        self,
        px: int,
        py: int,
        start_x: int,
        start_y: int,
        start_z: int,
        orientation: Orientation,
    ) -> tuple[int, int, int]:
        """将像素坐标转换为世界坐标。

        Args:
            px: 像素X坐标 (0-based)
            py: 像素Y坐标 (0-based, 从顶部)
            start_x, start_y, start_z: 起始世界坐标
            orientation: 像素画朝向

        Returns:
            (wx, wy, wz) 世界坐标。
        """
        if orientation == Orientation.VERTICAL_NS:
            # 垂直面, 南北朝向: X=像素X, Y=高度-像素Y, Z=固定
            return (start_x + px, start_y + (self._get_image_height() - py - 1), start_z)
        elif orientation == Orientation.VERTICAL_EW:
            # 垂直面, 东西朝向: X=固定, Y=高度-像素Y, Z=像素X
            return (start_x, start_y + (self._get_image_height() - py - 1), start_z + px)
        elif orientation == Orientation.HORIZONTAL:
            # 水平面: X=像素X, Y=固定, Z=像素Y
            return (start_x + px, start_y, start_z + py)
        else:
            return (start_x + px, start_y + py, start_z)

    def _get_image_height(self) -> int:
        """获取当前图像高度 (需要在read_image后设置)。"""
        # 这是一个占位方法, 实际使用时会从实例变量获取
        return 128

    def generate_commands(
        self,
        blocks: list[tuple[int, int, int, BlockState]],
        mode: str = "replace",
    ) -> list[str]:
        """生成setblock命令字符串列表。

        Args:
            blocks: 方块列表
            mode: 放置模式 (replace/destroy/keep)

        Returns:
            命令字符串列表。
        """
        import json

        commands: list[str] = []
        for x, y, z, block in blocks:
            cmd = f"setblock {x} {y} {z} {block.name}"
            if block.states:
                cmd += f" {json.dumps(block.states)}"
            cmd += f" {mode}"
            commands.append(cmd)

        return commands

    def get_bounds(
        self,
        blocks: list[tuple[int, int, int, BlockState]],
    ) -> tuple[int, int, int, int, int, int]:
        """获取像素画的包围盒。

        Args:
            blocks: 方块列表

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        if not blocks:
            return (0, 0, 0, 0, 0, 0)

        min_x = min(b[0] for b in blocks)
        min_y = min(b[1] for b in blocks)
        min_z = min(b[2] for b in blocks)
        max_x = max(b[0] for b in blocks)
        max_y = max(b[1] for b in blocks)
        max_z = max(b[2] for b in blocks)

        return (min_x, min_y, min_z, max_x, max_y, max_z)


__all__ = [
    "ScaleMode",
    "DitherMode",
    "Orientation",
    "ColorBlock",
    "ColorMapper",
    "ImageScaler",
    "Ditherer",
    "PixelArtConfig",
    "PixelArtImporter",
]