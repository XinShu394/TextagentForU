#!/bin/bash
# ================================================================
# TextAgent Docker 容器入口
# 同时启动 cron 服务和 gunicorn 应用服务器
# ================================================================

set -e

echo "[Entrypoint] 启动 cron 服务..."
cron

echo "[Entrypoint] 验证 crontab 已加载..."
crontab -l

# ── gunicorn 参数 ──
# GUNICORN_WORKERS: worker 进程数（默认 1，保持 SQLite 单写兼容）
#   - 注意：当前架构使用模块级全局变量（_msg_cache, _token_cache 等），
#     多 worker 下这些缓存不共享。Phase 1 引入 SQLite 后可安全增加 worker。
# GUNICORN_THREADS: 每个 worker 的线程数（默认 4，替代 Flask threaded=True）
WORKERS=${GUNICORN_WORKERS:-1}
THREADS=${GUNICORN_THREADS:-4}
PORT=${SERVER_PORT:-9000}

echo "[Entrypoint] 启动 gunicorn (workers=${WORKERS}, threads=${THREADS}, port=${PORT})..."
exec gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers "${WORKERS}" \
    --threads "${THREADS}" \
    --timeout 600 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    "app:create_app()"
