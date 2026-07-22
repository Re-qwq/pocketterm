"""内置验证码系统 - 生成图片验证码用于注册。"""
from __future__ import annotations

import base64
import io
import logging
import random
import string
import time
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("pocketterm.captcha")

# 验证码字符集 (去除易混淆字符)
_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# 内存存储: captcha_id -> {answer, expire_time}
_captcha_store: dict[str, dict] = {}

# 验证码有效期 (秒)
_CAPTCHA_TTL = 300  # 5 分钟


def _get_font(size: int = 36) -> ImageFont.FreeTypeFont:
    """获取字体, 尝试多个路径。"""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    # 回退到默认字体
    return ImageFont.load_default()


def generate_captcha() -> tuple[str, str]:
    """生成验证码, 返回 (captcha_id, base64_image)。

    生成的图片是 160x50 像素的 PNG, 包含 4 位随机字符。
    """
    # 生成随机文本
    text = "".join(random.choices(_CAPTCHA_CHARS, k=4))

    # 创建图片
    width, height = 160, 50
    image = Image.new("RGB", (width, height), color=(24, 26, 31))
    draw = ImageDraw.Draw(image)

    # 绘制干扰线
    for _ in range(6):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        line_color = (
            random.randint(60, 180),
            random.randint(60, 180),
            random.randint(60, 180),
        )
        draw.line([(x1, y1), (x2, y2)], fill=line_color, width=1)

    # 绘制验证码文本
    font = _get_font(36)
    char_width = width // (len(text) + 1)
    for i, char in enumerate(text):
        x = char_width * (i + 1) - 18 + random.randint(-5, 5)
        y = random.randint(2, 10)
        # 每个字符随机颜色
        char_color = (
            random.randint(200, 255),
            random.randint(200, 255),
            random.randint(200, 255),
        )
        draw.text((x, y), char, fill=char_color, font=font)

    # 绘制干扰点
    for _ in range(100):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        point_color = (
            random.randint(60, 180),
            random.randint(60, 180),
            random.randint(60, 180),
        )
        image.putpixel((x, y), point_color)

    # 转换为 base64
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

    # 存储
    captcha_id = f"cap_{random.randint(100000, 999999)}"
    _captcha_store[captcha_id] = {
        "answer": text,
        "expire_time": time.time() + _CAPTCHA_TTL,
    }

    # 清理过期验证码
    _cleanup_expired()

    logger.debug(f"生成验证码: id={captcha_id}, text={text}")
    return captcha_id, image_base64


def verify_captcha(captcha_id: str, answer: str) -> bool:
    """验证验证码。验证后自动删除 (一次性使用)。"""
    entry = _captcha_store.get(captcha_id)
    if entry is None:
        return False

    # 删除已使用的验证码
    del _captcha_store[captcha_id]

    # 检查是否过期
    if entry["expire_time"] < time.time():
        return False

    # 比较答案 (不区分大小写)
    return entry["answer"].upper() == answer.upper().strip()


def _cleanup_expired() -> None:
    """清理过期验证码。"""
    now = time.time()
    expired = [k for k, v in _captcha_store.items() if v["expire_time"] < now]
    for k in expired:
        del _captcha_store[k]
