"""动态 IP 获取工具。

获取客户端真实公网 IP 地址, 用于 sauth_json 的 ip 和 aim_info.aim 字段。

Community-Bot 真实样本中使用的是客户端真实 IP (如 100.100.100.100),
而非 127.0.0.1。使用真实 IP 更接近正常客户端行为, 降低被反作弊识别的风险。

IP 来源:
  1. 优先使用外部 API 获取公网 IP
  2. 回退到本地检测
  3. 最终回退到 127.0.0.1

支持的外部 API:
  - https://httpbin.org/ip (JSON: {"origin": "x.x.x.x"})
  - https://api.ipify.org?format=json (JSON: {"ip": "x.x.x.x"})
  - https://ifconfig.me/ip (纯文本 IP)

缓存策略:
  - IP 缓存 5 分钟, 避免频繁请求
  - 并发请求使用锁保护
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Optional

import httpx

logger = logging.getLogger("pocketterm.dynamic_ip")

# IP 缓存时间 (秒)
_CACHE_TTL = 300  # 5 分钟

# 外部 IP 检测 API (按优先级排序)
_IP_APIS = [
    ("https://httpbin.org/ip", "origin"),
    ("https://api.ipify.org?format=json", "ip"),
    ("https://ifconfig.me/ip", None),  # None = 纯文本响应
]

# 缓存
_cached_ip: str = ""
_cached_at: float = 0.0
_cache_lock = asyncio.Lock()


async def get_public_ip(force_refresh: bool = False) -> str:
    """获取客户端公网 IP 地址。

    优先使用外部 API, 失败则回退到本地检测。

    Args:
        force_refresh: 是否强制刷新缓存。

    Returns:
        公网 IP 地址字符串 (如 "1.2.3.4")。
    """
    global _cached_ip, _cached_at

    # 检查缓存
    if not force_refresh and _cached_ip:
        if time.time() - _cached_at < _CACHE_TTL:
            return _cached_ip

    async with _cache_lock:
        # 双重检查 (其他协程可能已刷新)
        if not force_refresh and _cached_ip:
            if time.time() - _cached_at < _CACHE_TTL:
                return _cached_ip

        # 尝试外部 API
        ip = await _try_external_apis()
        if not ip:
            # 回退到本地检测
            ip = _get_local_ip()

        if ip:
            _cached_ip = ip
            _cached_at = time.time()
            logger.debug("获取到公网 IP: %s", ip)
        else:
            ip = "127.0.0.1"
            logger.warning("无法获取公网 IP, 回退到 127.0.0.1")

        return ip


async def _try_external_apis() -> Optional[str]:
    """尝试通过外部 API 获取公网 IP。"""
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url, json_key in _IP_APIS:
            try:
                resp = await client.get(url, headers={"User-Agent": "curl/7.68.0"})
                if resp.status_code != 200:
                    continue

                if json_key:
                    data = resp.json()
                    ip = data.get(json_key, "")
                else:
                    ip = resp.text.strip()

                # 验证 IP 格式
                if _is_valid_ip(ip):
                    return ip
            except Exception as e:
                logger.debug("API %s 失败: %s", url, e)
                continue

    return None


def _get_local_ip() -> Optional[str]:
    """获取本地 IP 地址 (通过 socket 连接检测)。

    创建一个到外部地址的 UDP socket (不实际发送数据),
    操作系统会选择正确的本地接口 IP。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 连接到公共 DNS (不实际发送数据)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if _is_valid_ip(ip) and ip != "127.0.0.1":
                return ip
        finally:
            sock.close()
    except Exception as e:
        logger.debug("本地 IP 检测失败: %s", e)

    return None


def _is_valid_ip(ip: str) -> bool:
    """验证 IP 地址格式。"""
    if not ip or not isinstance(ip, str):
        return False
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        try:
            val = int(part)
            if val < 0 or val > 255:
                return False
        except ValueError:
            return False
    return True


def clear_cache() -> None:
    """清除 IP 缓存。"""
    global _cached_ip, _cached_at
    _cached_ip = ""
    _cached_at = 0.0


__all__ = [
    "get_public_ip",
    "clear_cache",
]
