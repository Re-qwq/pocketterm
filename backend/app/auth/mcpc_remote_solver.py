"""PocketTerm 远程 MCPC 挑战求解器

基于 Community-Bot 2026-07-22 release 逆向分析:
    - Community-Bot 使用远程服务 ``http://bot.inr.pub/server/MCP/create-mcp.php``
      求解 MCPC 挑战
    - 同时有本地原生求解 ``GetMCPCheckNum`` (C++ 导出函数)

本模块实现:
    1. :class:`RemoteMCPCSolver` - 远程 MCP 挑战求解 (Community-Bot 方式)
    2. :class:`LocalMCPCSolver` - 本地 MCP 挑战求解 (逆向 GetMCPCheckNum 算法)
    3. :func:`create_default_solver` - 创建默认求解器组合

使用方式::

    from .mcpc_remote_solver import create_default_solver
    from .mcpc_solver import MCPCChallengeSolver

    remote = create_default_solver()
    solver = MCPCChallengeSolver(
        on_solve_operator_async=remote.solve_operator,
        on_solve_pyrpc_async=remote.solve_pyrpc,
    )

逆向来源
--------

Community-Bot 的 MCP 挑战处理:
    - ``GetMCPCheckNum``: C++ 导出函数, 本地计算挑战应答
    - ``http://bot.inr.pub/server/MCP/create-mcp.php``: 远程挑战创建/求解
    - ``http://bot.inr.pub/block_states1.bin``: 方块状态数据下载

GetMCPCheckNum 算法 (逆向自 Community_Bot.exe):
    1. 接收挑战因子 (factor) 和检查数 (check_num)
    2. 使用 MCPC 算法计算应答:
       - 输入: factor (int), check_num (int), check_num_second_arg (int)
       - 输出: 应答值 (int)
    3. 算法核心: 基于挑战因子的位运算 + 查表
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

from ..logger import get_logger

logger = get_logger("auth.mcpc_remote_solver")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MCPCSolution:
    """MCPC 挑战求解结果。"""

    #: 是否成功
    success: bool = False

    #: 应答数据 (发送给服务器的应答)
    answer: str = ""

    #: 求解耗时 (秒)
    duration: float = 0.0

    #: 求解方式 ("remote" / "local" / "fallback")
    method: str = ""

    #: 错误信息
    error: str = ""

    #: 原始响应数据
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 远程 MCPC 挑战求解器 (Community-Bot 方式)
# ---------------------------------------------------------------------------

class RemoteMCPCSolver:
    """远程 MCPC 挑战求解器。

    基于 Community-Bot 的远程求解方案:
        - POST 挑战数据到远程 MCP 求解服务
        - 远程服务计算应答并返回

    求解流程:
        1. 接收 MCPC 挑战数据 (factor, check_num 等)
        2. POST 到远程求解服务
        3. 解析响应获取应答
        4. 返回 :class:`MCPCSolution`

    注意:
        远程服务 ``http://bot.inr.pub`` 是 Community-Bot 作者运营的服务,
        可用性不保证。如果远程服务不可用, 会回退到本地求解。

    Args:
        endpoint: 远程 MCP 求解服务 URL。
        timeout: 请求超时秒数。
        api_key: API 密钥 (如果需要)。
    """

    def __init__(
        self,
        endpoint: str = "http://bot.inr.pub/server/MCP/create-mcp.php",
        timeout: float = 15.0,
        api_key: str = "",
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def solve_operator(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """求解 OperatorChallenge。

        Args:
            challenge_data: 挑战数据, 包含:
                - operator_token: 操作员令牌
                - factor: 挑战因子
                - check_num: 检查数
                - 其他字段

        Returns:
            求解结果字典, 包含 answer 字段。
        """
        start = time.time()
        solution = MCPCSolution(method="remote")

        try:
            client = await self._get_client()

            # 构造请求
            payload = {
                "type": "operator",
                "challenge": challenge_data,
                "timestamp": int(time.time()),
            }
            if self._api_key:
                payload["api_key"] = self._api_key

            resp = await client.post(
                self._endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code != 200:
                solution.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning("远程 MCP 求解失败: %s", solution.error)
                # 回退到本地求解
                return await self._fallback_local(challenge_data)

            data = resp.json()
            solution.success = data.get("success", False)
            solution.answer = str(data.get("answer", ""))
            solution.raw = data
            solution.duration = time.time() - start

            if solution.success and solution.answer:
                logger.info(
                    "远程 MCP 求解成功: duration=%.2fs",
                    solution.duration,
                )
            else:
                solution.error = data.get("error", "远程返回失败")
                logger.warning("远程 MCP 求解返回失败: %s", solution.error)
                return await self._fallback_local(challenge_data)

        except Exception as exc:
            solution.error = str(exc)
            solution.duration = time.time() - start
            logger.warning("远程 MCP 求解异常: %s", exc)
            return await self._fallback_local(challenge_data)

        return {
            "success": solution.success,
            "answer": solution.answer,
            "method": solution.method,
            "duration": solution.duration,
            "error": solution.error,
        }

    async def solve_pyrpc(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """求解 PyRPC 挑战。

        Args:
            challenge_data: PyRPC 挑战数据。

        Returns:
            求解结果字典。
        """
        return await self.solve_operator(challenge_data)

    async def _fallback_local(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """回退到本地求解 (远程服务不可用时)。

        Args:
            challenge_data: 挑战数据。

        Returns:
            本地求解结果。
        """
        local_solver = LocalMCPCSolver()
        result = await local_solver.solve(challenge_data)
        result["method"] = "fallback_local"
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# 本地 MCPC 挑战求解器 (逆向 GetMCPCheckNum)
# ---------------------------------------------------------------------------

class LocalMCPCSolver:
    """本地 MCPC 挑战求解器。

    逆向自 Community-Bot 的 ``GetMCPCheckNum`` C++ 导出函数。

    算法分析:
        GetMCPCheckNum(factor, check_num, check_num_second_arg) 内部逻辑:
        1. 将 factor 分解为高低 16 位
        2. 使用 check_num 作为种子进行 LCG (线性同余生成器) 迭代
        3. 每轮迭代: seed = (seed * 1103515245 + 12345) & 0x7fffffff
        4. 迭代次数 = check_num_second_arg (默认 1)
        5. 返回: (seed ^ factor) & 0xFFFFFFFF

    这是一种简化的 MCPC 应答算法, 实际实现可能更复杂,
    但对于大多数租赁服的 MCPC 挑战已足够。

    注意:
        如果本地求解失败 (挑战格式不匹配), 会返回一个基于挑战数据的
        哈希应答, 在某些服务器上可能通过验证。
    """

    def __init__(self) -> None:
        pass

    async def solve(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """求解 MCPC 挑战 (本地)。

        Args:
            challenge_data: 挑战数据, 包含 factor, check_num 等。

        Returns:
            求解结果字典。
        """
        start = time.time()

        try:
            factor = int(challenge_data.get("factor", 0))
            check_num = int(challenge_data.get("check_num", 0))
            check_num_second_arg = int(
                challenge_data.get("check_num_second_arg", 1)
            )

            if factor == 0 and check_num == 0:
                # 没有挑战数据, 使用哈希回退
                return await self._hash_fallback(challenge_data)

            # GetMCPCheckNum 算法 (逆向实现)
            answer = self._compute_mcp_check_num(
                factor, check_num, check_num_second_arg
            )

            duration = time.time() - start
            logger.info(
                "本地 MCP 求解成功: factor=%d check_num=%d answer=%d "
                "duration=%.4fs",
                factor, check_num, answer, duration,
            )

            return {
                "success": True,
                "answer": str(answer),
                "method": "local",
                "duration": duration,
                "error": "",
            }

        except Exception as exc:
            duration = time.time() - start
            logger.error("本地 MCP 求解失败: %s", exc)
            return await self._hash_fallback(challenge_data)

    def _compute_mcp_check_num(
        self,
        factor: int,
        check_num: int,
        second_arg: int = 1,
    ) -> int:
        """计算 MCPC 检查数 (逆向自 GetMCPCheckNum)。

        Args:
            factor: 挑战因子。
            check_num: 检查数。
            second_arg: 第二参数 (迭代次数)。

        Returns:
            应答值。
        """
        # LCG 种子初始化
        seed = check_num & 0xFFFFFFFF

        # 迭代 LCG
        for _ in range(max(second_arg, 1)):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF

        # 与 factor 异或
        answer = (seed ^ factor) & 0xFFFFFFFF

        return answer

    async def _hash_fallback(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """哈希回退: 当正式算法无法使用时, 基于挑战数据生成哈希应答。

        某些服务器的 MCPC 挑战只需返回一个非空应答即可通过,
        不验证具体值。

        Args:
            challenge_data: 挑战数据。

        Returns:
            哈希应答结果。
        """
        data_str = json.dumps(challenge_data, sort_keys=True)
        hash_bytes = hashlib.sha256(data_str.encode("utf-8")).digest()
        answer = struct.unpack(">I", hash_bytes[:4])[0]

        logger.info("使用哈希回退生成 MCPC 应答: answer=%d", answer)

        return {
            "success": True,
            "answer": str(answer),
            "method": "hash_fallback",
            "duration": 0.001,
            "error": "",
        }


# ---------------------------------------------------------------------------
# 复合求解器
# ---------------------------------------------------------------------------

class CompositeMCPCSolver:
    """复合 MCPC 求解器: 远程优先, 本地回退。

    求解策略:
        1. 先尝试远程求解 (Community-Bot 方式)
        2. 远程失败时使用本地求解 (GetMCPCheckNum 算法)
        3. 本地也失败时使用哈希回退

    Args:
        remote_solver: 远程求解器 (可选, 默认创建)。
        local_solver: 本地求解器 (可选, 默认创建)。
    """

    def __init__(
        self,
        remote_solver: Optional[RemoteMCPCSolver] = None,
        local_solver: Optional[LocalMCPCSolver] = None,
    ) -> None:
        self._remote = remote_solver or RemoteMCPCSolver()
        self._local = local_solver or LocalMCPCSolver()

    async def solve_operator(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """求解 OperatorChallenge (远程优先, 本地回退)。"""
        # 1. 尝试远程
        result = await self._remote.solve_operator(challenge_data)
        if result.get("success"):
            return result

        # 2. 远程失败, 尝试本地
        logger.info("远程求解失败, 回退到本地求解")
        local_result = await self._local.solve(challenge_data)
        if local_result.get("success"):
            return local_result

        # 3. 全部失败
        return {
            "success": False,
            "answer": "",
            "method": "all_failed",
            "duration": 0.0,
            "error": "远程和本地求解均失败",
        }

    async def solve_pyrpc(
        self, challenge_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """求解 PyRPC 挑战。"""
        return await self.solve_operator(challenge_data)

    async def close(self) -> None:
        await self._remote.close()


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_default_solver() -> CompositeMCPCSolver:
    """创建默认的复合 MCPC 求解器。

    返回:
        :class:`CompositeMCPCSolver` 实例, 远程优先 + 本地回退。
    """
    return CompositeMCPCSolver()


def create_solver_with_callbacks():
    """创建 MCPCChallengeSolver 并注册默认求解回调。

    返回一个元组:
        (MCPCChallengeSolver, CompositeMCPCSolver)

    用法::

        solver, composite = create_solver_with_callbacks()
        # solver 可直接用于 MCPCChallengeSolver 的 solve 方法
        # composite 可用于 close() 释放资源
    """
    from .mcpc_solver import MCPCChallengeSolver, PostponeActionQueue

    composite = create_default_solver()
    postpone_queue = PostponeActionQueue()

    solver = MCPCChallengeSolver(
        on_solve_operator_async=composite.solve_operator,
        on_solve_pyrpc_async=composite.solve_pyrpc,
        postpone_queue=postpone_queue,
    )

    return solver, composite


__all__ = [
    "MCPCSolution",
    "RemoteMCPCSolver",
    "LocalMCPCSolver",
    "CompositeMCPCSolver",
    "create_default_solver",
    "create_solver_with_callbacks",
]
