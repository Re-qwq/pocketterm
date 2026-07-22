@echo off
chcp 65001 >nul
title PocketTerm - Minecraft Bot Panel
echo ============================================================
echo    PocketTerm - Minecraft 网易版机器人控制面板
echo ============================================================
echo.

cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/4] 检查虚拟环境...
if not exist "backend\venv" (
    echo [*] 创建虚拟环境...
    python -m venv backend\venv
)

echo [2/4] 激活虚拟环境...
call backend\venv\Scripts\activate.bat

echo [3/4] 安装依赖...
pip install -r backend\requirements.txt -q

echo [4/4] 启动服务...
echo.
echo ============================================================
echo    控制面板地址: http://localhost:8000
echo    默认账号: admin / admin123
echo    按 Ctrl+C 停止服务
echo ============================================================
echo.

cd backend
start http://localhost:8000
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
