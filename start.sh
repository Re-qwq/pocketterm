#!/bin/bash
set -e

echo "============================================================"
echo "  PocketTerm - Minecraft 网易版机器人控制面板"
echo "============================================================"
echo ""

cd "$(dirname "$0")"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

echo "[1/4] 检查虚拟环境..."
if [ ! -d "backend/venv" ]; then
    echo "[*] 创建虚拟环境..."
    python3 -m venv backend/venv
fi

echo "[2/4] 激活虚拟环境..."
source backend/venv/bin/activate

echo "[3/4] 安装依赖..."
pip install -r backend/requirements.txt -q

echo "[4/4] 启动服务..."
echo ""
echo "============================================================"
echo "  控制面板地址: http://localhost:8000"
echo "  默认账号: admin / admin123"
echo "  按 Ctrl+C 停止服务"
echo "============================================================"
echo ""

cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
