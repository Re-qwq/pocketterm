"""MapBuilder - 视频播放器 / 地图构建器。

逆向自 NexusEgo 的 MapBuilder 模块, 适配到 PocketTerm 项目。
提供展示框放置、媒体加载 (视频帧提取) 和像素请求能力。

工作流程:
    1. 视频/图片输入 -> ImageInfo
    2. 像素分析 -> PixelRequest
    3. 子区块映射 -> SubChunkPos/SubChunkEntry
    4. 物品展示框放置 -> ItemFrameData
    5. 通过 SubChunkResponse 确认结果

主要组件:
    - MediaLoader:       媒体加载器 (ffmpeg 帧提取 + 图像缩放)
    - MapAPI:            地图 API (SendMapPixels)
    - NexusAPI:          Nexus API 封装
    - ItemFramePlacer:   物品展示框放置器
"""

from __future__ import annotations

from .media_loader import (
    MediaLoader, ImageInfo, MediaLoadError,
    ScaleMode, OverlayMode, MapConfig,
    load_image, extract_video_frames, resize_image,
)
from .pixel_request import (
    PixelRequest, SubChunkPos, SubChunkEntry, SubChunkOffset,
    SubChunkResponse, BlockPos, SubChunkKey,
    MapAPI, NexusAPI, PixelRequestError,
    send_map_pixels, build_pixel_requests,
)
from .item_frame_placer import (
    ItemFrameData, ItemFramePlacer, ItemFrameError,
    ItemFrameOrientation,
    place_item_frames, build_item_frame_data,
)

__all__ = [
    # media_loader
    "MediaLoader", "ImageInfo", "MediaLoadError",
    "ScaleMode", "OverlayMode", "MapConfig",
    "load_image", "extract_video_frames", "resize_image",
    # pixel_request
    "PixelRequest", "SubChunkPos", "SubChunkEntry", "SubChunkOffset",
    "SubChunkResponse", "BlockPos", "SubChunkKey",
    "MapAPI", "NexusAPI", "PixelRequestError",
    "send_map_pixels", "build_pixel_requests",
    # item_frame_placer
    "ItemFrameData", "ItemFramePlacer", "ItemFrameError",
    "ItemFrameOrientation",
    "place_item_frames", "build_item_frame_data",
]
