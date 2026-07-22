#!/bin/sh
# =============================================================================
# PocketTerm Docker 入口脚本
# =============================================================================
# 职责：
#   1. 初始化数据目录 —— 首次启动时将镜像内预置的静态数据文件
#      (block_mapping.json, version_config.json 等) 复制到挂载的卷中，
#      避免卷挂载覆盖镜像内容导致文件丢失。
#   2. 初始化配置文件 —— 将 config.production.yaml 作为模板复制到
#      持久化目录，使密码、JWT 密钥等配置在容器重建后仍然保留。
#   3. 启动 uvicorn 服务。
# =============================================================================
set -e

# -- 路径定义 ---------------------------------------------------------------
DATA_DIR="/app/backend/data"
STAGING_DIR="/app/backend/data_staging"
CONFIG_TEMPLATE="/app/backend/config.production.yaml"
CONFIG_FILE="/app/backend/config.yaml"
PERSISTENT_CONFIG="${DATA_DIR}/config.yaml"

echo "============================================================"
echo "  PocketTerm Docker Entrypoint"
echo "  POCKETTERM_ENV = ${POCKETTERM_ENV:-production}"
echo "============================================================"

# -- 1. 初始化数据目录 -----------------------------------------------------
# Docker 卷挂载会覆盖镜像内 /app/backend/data 的原有内容。
# 因此在构建阶段将静态数据文件暂存到 data_staging，
# 首次启动时复制到挂载的卷中。
if [ -d "$STAGING_DIR" ]; then
    for file in "$STAGING_DIR"/*; do
        [ -f "$file" ] || continue
        filename=$(basename "$file")
        target="${DATA_DIR}/${filename}"
        if [ ! -f "$target" ]; then
            cp "$file" "$target"
            echo "[entrypoint] 初始化数据文件: ${filename}"
        fi
    done
fi

# -- 2. 初始化配置文件 -----------------------------------------------------
# 将配置文件存放在挂载的 data 目录中，使容器重建后
# 密码、JWT 密钥等配置不会丢失。
if [ ! -f "$PERSISTENT_CONFIG" ]; then
    echo "[entrypoint] 首次启动，从生产模板初始化配置文件..."
    cp "$CONFIG_TEMPLATE" "$PERSISTENT_CONFIG"
else
    echo "[entrypoint] 使用已有配置文件: ${PERSISTENT_CONFIG}"
fi

# 创建符号链接，使应用从 backend/config.yaml 读取持久化的配置
ln -sf "$PERSISTENT_CONFIG" "$CONFIG_FILE"

# -- 3. 启动 uvicorn --------------------------------------------------------
echo "[entrypoint] 启动 PocketTerm 服务..."
echo "[entrypoint] 监听地址: http://0.0.0.0:8000"
echo "============================================================"

# 使用 exec 替换当前进程，使 uvicorn 成为 PID 1，
# 正确接收 SIGTERM 等信号实现优雅关闭
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
