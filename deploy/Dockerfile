FROM python:3.11-slim

WORKDIR /app

# 安装 cron + 必要工具
RUN apt-get update && apt-get install -y --no-install-recommends cron curl && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码（含 web_static/ 前端页面）
COPY src/ ./src/

# 复制 crontab 配置和触发脚本
COPY deploy/crontab/textagent.cron /etc/cron.d/textagent
COPY deploy/crontab/trigger.sh /opt/textagent/trigger.sh
RUN chmod 0644 /etc/cron.d/textagent && \
    chmod +x /opt/textagent/trigger.sh && \
    crontab /etc/cron.d/textagent

# 创建数据目录（多用户数据 + 系统数据）和日志目录
RUN mkdir -p /app/data/_textagent_system /var/log

# 设置数据目录环境变量
ENV DATA_DIR=/app/data

WORKDIR /app/src

# 端口
EXPOSE 9000

# 健康检查（使用 curl 更轻量）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${SERVER_PORT:-9000}/health || exit 1

# 启动入口脚本：同时启动 cron 和 gunicorn
COPY deploy/crontab/entrypoint.sh /opt/textagent/entrypoint.sh
RUN chmod +x /opt/textagent/entrypoint.sh

CMD ["/opt/textagent/entrypoint.sh"]
