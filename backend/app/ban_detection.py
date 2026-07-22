"""封号检测系统。

功能:
    - 跟踪账号登录失败次数
    - 达到阈值时标记为疑似封号
    - 自动通知管理员 (通过日志)
    - 自动停止相关机器人
    - 支持手动解除封号标记

使用方式::

    from app.ban_detection import ban_detector

    # 报告登录失败
    count = ban_detector.report_login_failure(account_id, bot_id)

    # 检查是否疑似封号
    if ban_detector.is_suspected_ban(account_id):
        ...

    # 报告登录成功 (重置计数)
    ban_detector.report_login_success(account_id)

    # 手动解除封号标记
    ban_detector.clear_ban_flag(account_id)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("pocketterm.ban_detection")

#: 连续登录失败阈值 (达到此值视为疑似封号)
BAN_THRESHOLD = 3

#: 登录失败记录过期时间 (24 小时)
FAILURE_RECORD_TTL = 24 * 3600


@dataclass
class FailureRecord:
    """登录失败记录。"""
    account_id: str
    bot_id: str = ""
    failure_count: int = 0
    last_failure_at: float = 0.0
    first_failure_at: float = 0.0
    notified: bool = False  # 是否已通知管理员


class BanDetector:
    """封号检测器。"""

    def __init__(self):
        self._records: dict[str, FailureRecord] = {}  # account_id -> record
        self._banned_accounts: set[str] = set()  # 确认封号的账号

    def report_login_failure(
        self, account_id: str, bot_id: str = ""
    ) -> int:
        """报告一次登录失败, 返回当前累计失败次数。"""
        now = time.time()

        if account_id not in self._records:
            self._records[account_id] = FailureRecord(
                account_id=account_id,
                bot_id=bot_id,
                failure_count=0,
                first_failure_at=now,
            )

        record = self._records[account_id]
        record.failure_count += 1
        record.last_failure_at = now
        record.bot_id = bot_id or record.bot_id

        logger.warning(
            f"登录失败 [{account_id}]: 累计 {record.failure_count} 次 "
            f"(阈值 {BAN_THRESHOLD})"
        )

        # 达到阈值, 标记为疑似封号
        if record.failure_count >= BAN_THRESHOLD and not record.notified:
            record.notified = True
            self._banned_accounts.add(account_id)
            logger.error(
                f"疑似封号 [{account_id}]: 连续 {record.failure_count} 次登录失败"
            )
            return record.failure_count

        return record.failure_count

    def report_login_success(self, account_id: str) -> None:
        """报告登录成功, 重置失败计数。"""
        if account_id in self._records:
            logger.info(f"登录成功 [{account_id}], 重置失败计数")
            del self._records[account_id]
        # 登录成功也解除封号标记
        self._banned_accounts.discard(account_id)

    def is_suspected_ban(self, account_id: str) -> bool:
        """检查账号是否疑似被封号。"""
        return account_id in self._banned_accounts

    def get_failure_count(self, account_id: str) -> int:
        """获取当前累计失败次数。"""
        record = self._records.get(account_id)
        return record.failure_count if record else 0

    def get_record(self, account_id: str) -> Optional[dict]:
        """获取失败记录详情。"""
        record = self._records.get(account_id)
        if record is None:
            return None
        return {
            "account_id": record.account_id,
            "bot_id": record.bot_id,
            "failure_count": record.failure_count,
            "first_failure_at": record.first_failure_at,
            "last_failure_at": record.last_failure_at,
            "suspected_ban": account_id in self._banned_accounts,
            "notified": record.notified,
        }

    def get_all_banned_accounts(self) -> list[dict]:
        """获取所有疑似封号的账号记录。"""
        result = []
        for account_id in self._banned_accounts:
            record = self.get_record(account_id)
            if record:
                result.append(record)
        return result

    def clear_ban_flag(self, account_id: str) -> bool:
        """手动解除封号标记, 返回是否成功。"""
        if account_id in self._banned_accounts:
            self._banned_accounts.discard(account_id)
            if account_id in self._records:
                self._records[account_id].notified = False
                self._records[account_id].failure_count = 0
            logger.info(f"已解除封号标记 [{account_id}]")
            return True
        return False

    def cleanup_expired(self) -> int:
        """清理过期的失败记录, 返回清理数量。"""
        now = time.time()
        expired = [
            aid for aid, rec in self._records.items()
            if now - rec.last_failure_at > FAILURE_RECORD_TTL
        ]
        for aid in expired:
            del self._records[aid]
            # 如果有过期记录但不是确认封号, 也清理
            self._banned_accounts.discard(aid)
        return len(expired)

    def get_stats(self) -> dict:
        """获取统计信息。"""
        return {
            "total_tracked": len(self._records),
            "suspected_bans": len(self._banned_accounts),
            "threshold": BAN_THRESHOLD,
            "banned_accounts": list(self._banned_accounts),
        }


# 全局单例
ban_detector = BanDetector()
