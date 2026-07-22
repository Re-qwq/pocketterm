#!/usr/bin/env python3
"""
PocketTerm 数据导出脚本
=======================

将所有业务数据导出为单个 JSON 文件，用于服务器迁移。

导出内容:
    1. 用户         (pocketterm.db -> users 表)
    2. 面板         (pocketterm.db -> panels 表)
    3. 卡密         (pocketterm.db -> card_keys 表)
    4. 公告         (pocketterm.db -> announcements / comments / reactions 表)
    5. 机器人配置    (pocketterm.db -> bot_instances 表, 去除 sauth_json)
    6. 4399 账号    (backend/data/accounts.json)
    7. 系统设置      (pocketterm.db -> system_settings 表 + settings.json)
    8. 设备指纹      (backend/data/device_fingerprints.json)

用法:
    python export_data.py [输出文件路径]

    不指定输出路径时, 默认输出到 pocketterm_export.json

示例:
    python export_data.py
    python export_data.py /tmp/migration_backup.json

注意:
    - 机器人配置中的 sauth_json (登录凭证) 会被移除, 需在新服务器重新配置
    - 4399 账号包含 sauth_json, 会完整导出
    - 用户密码哈希会完整导出, 迁移后无需重置密码
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 路径定义
# ---------------------------------------------------------------------------
SCRIPT_DIR: Path = Path(__file__).resolve().parent
BACKEND_DIR: Path = SCRIPT_DIR / "backend"
DATA_DIR: Path = BACKEND_DIR / "data"

POCKETTERM_DB: Path = DATA_DIR / "pocketterm.db"
ACCOUNTS_DB: Path = DATA_DIR / "accounts.db"
ACCOUNTS_JSON: Path = DATA_DIR / "accounts.json"
DEVICE_FINGERPRINTS_JSON: Path = DATA_DIR / "device_fingerprints.json"
SETTINGS_JSON: Path = DATA_DIR / "settings.json"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Any:
    """安全读取 JSON 文件, 不存在时返回 None。"""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [警告] 读取 {path} 失败: {e}")
        return None


def _query_all(db_path: Path, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """执行 SQL 查询, 返回字典列表。"""
    if not db_path.exists():
        print(f"  [警告] 数据库文件不存在: {db_path}")
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows
    except sqlite3.Error as e:
        print(f"  [警告] 查询失败 ({db_path}): {e}")
        return []


def _table_exists(db_path: Path, table_name: str) -> bool:
    """检查 SQLite 数据库中某张表是否存在。"""
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        result = cursor.fetchone() is not None
        conn.close()
        return result
    except sqlite3.Error:
        return False


def _strip_sauth_json(config_str: str) -> Any:
    """从机器人配置 JSON 中移除 sauth_json 字段。

    Args:
        config_str: bot_instances.config 列的 JSON 字符串。

    Returns:
        去除 sauth_json 后的配置字典; 解析失败时返回原始字符串。
    """
    if not config_str:
        return {}
    try:
        config = json.loads(config_str)
        if isinstance(config, dict):
            config.pop("sauth_json", None)
            return config
        return config
    except (json.JSONDecodeError, TypeError):
        return config_str


# ---------------------------------------------------------------------------
# 导出函数
# ---------------------------------------------------------------------------
def export_users() -> List[Dict[str, Any]]:
    """导出所有用户。"""
    print("  [1/8] 导出用户...")
    return _query_all(POCKETTERM_DB, "SELECT * FROM users ORDER BY created_at DESC")


def export_panels() -> List[Dict[str, Any]]:
    """导出所有面板。"""
    print("  [2/8] 导出面板...")
    return _query_all(POCKETTERM_DB, "SELECT * FROM panels ORDER BY created_at DESC")


def export_card_keys() -> List[Dict[str, Any]]:
    """导出所有卡密。"""
    print("  [3/8] 导出卡密...")
    return _query_all(POCKETTERM_DB, "SELECT * FROM card_keys ORDER BY created_at DESC")


def export_announcements() -> Dict[str, Any]:
    """导出所有公告 (含评论和反应)。"""
    print("  [4/8] 导出公告...")
    result: Dict[str, Any] = {
        "announcements": [],
        "comments": [],
        "reactions": [],
    }

    result["announcements"] = _query_all(
        POCKETTERM_DB, "SELECT * FROM announcements ORDER BY created_at DESC"
    )

    if _table_exists(POCKETTERM_DB, "announcement_comments"):
        result["comments"] = _query_all(
            POCKETTERM_DB,
            "SELECT * FROM announcement_comments ORDER BY created_at ASC",
        )

    if _table_exists(POCKETTERM_DB, "announcement_reactions"):
        result["reactions"] = _query_all(
            POCKETTERM_DB,
            "SELECT * FROM announcement_reactions ORDER BY created_at ASC",
        )

    return result


def export_bot_configs() -> List[Dict[str, Any]]:
    """导出所有机器人配置 (去除 sauth_json)。"""
    print("  [5/8] 导出机器人配置 (去除 sauth_json)...")
    bots = _query_all(
        POCKETTERM_DB, "SELECT * FROM bot_instances ORDER BY created_at DESC"
    )
    for bot in bots:
        # 从 config JSON 中移除 sauth_json
        if "config" in bot:
            bot["config"] = _strip_sauth_json(bot["config"])
    return bots


def export_accounts_4399() -> Any:
    """导出 4399 账号 (accounts.json)。"""
    print("  [6/8] 导出 4399 账号 (accounts.json)...")
    data = _read_json(ACCOUNTS_JSON)
    if data is None:
        return {}
    return data


def export_system_settings() -> Dict[str, Any]:
    """导出系统设置 (system_settings 表 + settings.json)。"""
    print("  [7/8] 导出系统设置...")
    result: Dict[str, Any] = {
        "database_settings": [],
        "settings_file": None,
    }

    # 从数据库导出 system_settings 表
    if _table_exists(POCKETTERM_DB, "system_settings"):
        result["database_settings"] = _query_all(
            POCKETTERM_DB, "SELECT * FROM system_settings ORDER BY key ASC"
        )

    # 从 settings.json 导出系统设置文件
    settings_data = _read_json(SETTINGS_JSON)
    if settings_data is not None:
        result["settings_file"] = settings_data

    return result


def export_device_fingerprints() -> Any:
    """导出设备指纹 (device_fingerprints.json)。"""
    print("  [8/8] 导出设备指纹...")
    data = _read_json(DEVICE_FINGERPRINTS_JSON)
    if data is None:
        return {}
    return data


# ---------------------------------------------------------------------------
# 主导出函数
# ---------------------------------------------------------------------------
def export_all(output_path: str) -> None:
    """执行完整数据导出, 写入单个 JSON 文件。"""
    print()
    print("=" * 60)
    print("  PocketTerm 数据导出工具")
    print("=" * 60)
    print(f"  数据目录: {DATA_DIR}")
    print(f"  输出文件: {output_path}")
    print()

    # 检查数据库是否存在
    if not POCKETTERM_DB.exists():
        print(f"  [错误] 主数据库不存在: {POCKETTERM_DB}")
        print("         请确认在项目根目录运行此脚本, 且数据库已初始化。")
        sys.exit(1)

    # 组装导出数据
    export: Dict[str, Any] = {
        "export_version": "1.0",
        "exported_at": _now_iso(),
        "source": "PocketTerm",
        "data_dir": str(DATA_DIR),
    }

    # 1. 用户
    export["users"] = export_users()

    # 2. 面板
    export["panels"] = export_panels()

    # 3. 卡密
    export["card_keys"] = export_card_keys()

    # 4. 公告 (含评论和反应)
    export["announcements"] = export_announcements()

    # 5. 机器人配置 (去除 sauth_json)
    export["bot_configs"] = export_bot_configs()

    # 6. 4399 账号
    export["accounts_4399"] = export_accounts_4399()

    # 7. 系统设置
    export["system_settings"] = export_system_settings()

    # 8. 设备指纹
    export["device_fingerprints"] = export_device_fingerprints()

    # 写入输出文件
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

    # 打印汇总
    _print_summary(export, output_path)


def _print_summary(export: Dict[str, Any], output_path: str) -> None:
    """打印导出结果汇总。"""
    accounts_4399 = export.get("accounts_4399", {})
    if isinstance(accounts_4399, dict):
        accounts_count = len(accounts_4399)
    elif isinstance(accounts_4399, list):
        accounts_count = len(accounts_4399)
    else:
        accounts_count = 0

    device_fps = export.get("device_fingerprints", {})
    if isinstance(device_fps, dict):
        fps_list = device_fps.get("fingerprints", [])
        if isinstance(fps_list, list):
            fps_count = len(fps_list)
        elif isinstance(device_fps, dict):
            # 旧格式: { "account_id": {...}, ... }
            fps_count = len(device_fps)
        else:
            fps_count = 0
    else:
        fps_count = 0

    announcements_data = export.get("announcements", {})

    file_size = os.path.getsize(output_path)
    if file_size >= 1024 * 1024:
        size_str = f"{file_size / (1024 * 1024):.2f} MB"
    elif file_size >= 1024:
        size_str = f"{file_size / 1024:.2f} KB"
    else:
        size_str = f"{file_size} B"

    print()
    print("=" * 60)
    print("  导出完成!")
    print("=" * 60)
    print(f"  输出文件:     {output_path} ({size_str})")
    print(f"  导出时间:     {export.get('exported_at', 'N/A')}")
    print("-" * 60)
    print(f"  用户:         {len(export.get('users', []))} 条")
    print(f"  面板:         {len(export.get('panels', []))} 条")
    print(f"  卡密:         {len(export.get('card_keys', []))} 条")
    print(f"  公告:         {len(announcements_data.get('announcements', []))} 条")
    print(f"  公告评论:     {len(announcements_data.get('comments', []))} 条")
    print(f"  公告反应:     {len(announcements_data.get('reactions', []))} 条")
    print(f"  机器人配置:   {len(export.get('bot_configs', []))} 条 (已去除 sauth_json)")
    print(f"  4399 账号:    {accounts_count} 条")
    sys_settings = export.get("system_settings", {})
    print(f"  系统设置(DB): {len(sys_settings.get('database_settings', []))} 条")
    print(f"  系统设置文件: {'有' if sys_settings.get('settings_file') else '无'}")
    print(f"  设备指纹:     {fps_count} 条")
    print("=" * 60)
    print()
    print("  注意事项:")
    print("  - 机器人配置中的 sauth_json 已移除, 需在新服务器重新配置")
    print("  - 4399 账号包含 sauth_json, 迁移后可直接使用")
    print("  - 用户密码哈希已完整导出, 迁移后无需重置密码")
    print("  - 请妥善保管导出文件, 其中包含敏感信息")
    print()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "pocketterm_export.json"
    export_all(output)
