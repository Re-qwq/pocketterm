#!/bin/bash
# =============================================================================
# PocketTerm 启动脚本
# =============================================================================
# 职责:
#   1. 使 Go 接入点二进制可执行
#   2. 初始化数据库 (创建所有表)
#   3. 启动 uvicorn 服务
#
# 用法:
#   本地运行:  ./start.sh
#   Docker 内: /app/start.sh  (WORKDIR=/app/backend)
# =============================================================================
set -e

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
# 自动识别当前所在目录, 兼容本地运行 (项目根目录) 和 Docker (backend 目录)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/app/main.py" ]; then
    # 当前在 backend/ 目录 (Docker 场景)
    BACKEND_DIR="$SCRIPT_DIR"
    PROJECT_ROOT="$(dirname "$BACKEND_DIR")"
elif [ -d "$SCRIPT_DIR/backend/app" ]; then
    # 当前在项目根目录 (本地运行)
    PROJECT_ROOT="$SCRIPT_DIR"
    BACKEND_DIR="$PROJECT_ROOT/backend"
else
    echo "[错误] 无法确定项目目录结构 (当前目录: $SCRIPT_DIR)"
    echo "       请从项目根目录或 backend 目录运行此脚本"
    exit 1
fi

echo "============================================================"
echo "  PocketTerm 启动脚本"
echo "  项目根目录: $PROJECT_ROOT"
echo "  后端目录:   $BACKEND_DIR"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. 使 Go 接入点二进制可执行
# ---------------------------------------------------------------------------
AP_BIN="$PROJECT_ROOT/access_point_go/pocketterm_ap"
if [ -f "$AP_BIN" ]; then
    chmod +x "$AP_BIN"
    echo "[1/3] Go 接入点二进制已设为可执行: $AP_BIN"
else
    echo "[1/3] [警告] 未找到 Go 接入点二进制: $AP_BIN (跳过)"
fi

# ---------------------------------------------------------------------------
# 2. 初始化数据库
# ---------------------------------------------------------------------------
echo "[2/3] 初始化数据库..."
cd "$BACKEND_DIR"

# 尝试通过应用层初始化数据库 (创建所有表)
# 如果失败 (例如缺少依赖或配置问题), 服务启动时的 lifespan 会再次初始化
python -c "
import asyncio
import sys

async def init_database():
    try:
        from app.database import get_db, close_db
        db = await get_db()
        print('  [OK] 数据库表已创建/验证')
        await close_db()
    except Exception as e:
        print(f'  [跳过] 应用层数据库初始化失败: {e}')
        print('  [信息] 数据库将在服务启动时自动初始化')

asyncio.run(init_database())
" 2>/dev/null || echo "  [信息] 数据库将在服务启动时自动初始化"

# ---------------------------------------------------------------------------
# 3. 启动 uvicorn 服务
# ---------------------------------------------------------------------------
echo "[3/3] 启动 uvicorn 服务..."
echo ""
echo "============================================================"
echo "  控制面板地址: http://localhost:8000"
echo "  默认账号: admin / admin123"
echo "  按 Ctrl+C 停止服务"
echo "============================================================"
echo ""

# 使用 exec 替换当前进程, 使 uvicorn 成为 PID 1,
# 正确接收 SIGTERM 等信号实现优雅关闭
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
