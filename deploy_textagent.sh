#!/bin/bash
# ============================================================
# TextAgent 一键部署脚本
# 域名: textagent.cn
# 服务器 IP: <REDACTED_SERVER_IP>
# ============================================================

set -e
echo "🚀 TextAgent 部署开始..."

# ============ 1. 系统更新 + 基础依赖 ============
echo "📦 [1/7] 安装系统依赖..."
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx ufw

# ============ 2. 配置 swap（2GB 虚拟内存） ============
echo "💾 [2/7] 配置 swap..."
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "  ✅ Swap 已创建"
else
    echo "  ⏭️ Swap 已存在，跳过"
fi

# ============ 3. 配置防火墙 ============
echo "🔒 [3/7] 配置防火墙..."
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
echo "  ✅ 防火墙已配置（22/80/443 端口开放）"

# ============ 4. 克隆项目 + 安装依赖 ============
echo "📥 [4/7] 部署项目代码..."
cd /root

# 如果目录已存在，先备份
if [ -d "TextagentForU" ]; then
    echo "  ⚠️ 检测到旧版本，备份中..."
    mv TextagentForU TextagentForU_backup_$(date +%Y%m%d_%H%M%S)
fi

git clone https://github.com/XinShu394/TextagentForU.git
cd TextagentForU/src

# 安装 Python 依赖（使用清华镜像加速）
pip3 install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo "  ✅ 项目代码已部署"

# ============ 5. 写入 .env 配置 ============
echo "⚙️ [5/7] 写入配置文件..."
cat > /root/TextagentForU/src/.env << 'ENVEOF'
# ============ TextAgent 环境变量配置 ============
# 全部通过阿里云百炼平台调用（一个 API Key 搞定）

# --- DeepSeek API [通过阿里云百炼调用] ---
DEEPSEEK_API_KEY=<REDACTED_API_KEY>
DEEPSEEK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DEEPSEEK_MODEL=deepseek-v3

# --- Qwen Flash API [同样走阿里云百炼] ---
QWEN_API_KEY=<REDACTED_API_KEY>
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen-plus-latest
QWEN_VL_MODEL=qwen-vl-max

# --- OneDrive [留空 = Lite 本地模式] ---
ONEDRIVE_CLIENT_ID=
ONEDRIVE_CLIENT_SECRET=
ONEDRIVE_REFRESH_TOKEN=

# --- 企业微信 [必填] ---
WEWORK_CORP_ID=<REDACTED_CORP_ID>
WEWORK_AGENT_ID=1000002
WEWORK_CORP_SECRET=<REDACTED_CORP_SECRET>
WEWORK_TOKEN=<REDACTED_WEWORK_TOKEN>
WEWORK_ENCODING_AES_KEY=<REDACTED_AES_KEY>

# --- 腾讯云 ASR [可选，语音识别] ---
TENCENT_APPID=
TENCENT_SECRET_ID=
TENCENT_SECRET_KEY=

# --- Obsidian Vault 路径 ---
OBSIDIAN_BASE=/应用/remotely-save/EmptyVault

# --- 心知天气 [可选] ---
SENIVERSE_KEY=
WEATHER_CITY=深圳

# --- 默认企微用户 ID [必填] ---
DEFAULT_USER_ID=ChenTian

# --- 异步处理端点 ---
PROCESS_ENDPOINT_URL=http://127.0.0.1:9000/process

# --- 多用户管理 ---
ADMIN_TOKEN=<REDACTED_ADMIN_TOKEN>
DAILY_MESSAGE_LIMIT=50
WEB_TOKEN_EXPIRE_HOURS=24
WEB_DOMAIN=textagent.cn

# --- 管理员企微 user_id（告警推送） ---
ADMIN_USER_ID=ChenTian
ENVEOF
echo "  ✅ .env 配置已写入"

# ============ 6. 配置 Nginx 反向代理 ============
echo "🌐 [6/7] 配置 Nginx..."

# 删除默认配置
rm -f /etc/nginx/sites-enabled/default

cat > /etc/nginx/sites-available/textagent << 'NGINXEOF'
server {
    listen 80;
    server_name textagent.cn www.textagent.cn;

    # 安全头
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 60;
        proxy_send_timeout 300;

        # 请求体大小（图片/视频上传）
        client_max_body_size 50m;
    }
}
NGINXEOF

# 启用配置
ln -sf /etc/nginx/sites-available/textagent /etc/nginx/sites-enabled/textagent

# 测试 + 重启
nginx -t && systemctl restart nginx && systemctl enable nginx
echo "  ✅ Nginx 已配置（textagent.cn → 127.0.0.1:9000）"

# ============ 7. 配置 systemd 服务（开机自启） ============
echo "🔧 [7/7] 配置 systemd 服务..."

cat > /etc/systemd/system/textagent.service << 'SVCEOF'
[Unit]
Description=TextAgent AI Assistant
After=network.target nginx.service
Wants=nginx.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/TextagentForU/src
ExecStart=/usr/bin/python3 /root/TextagentForU/src/app.py
Restart=always
RestartSec=10
StandardOutput=append:/root/textagent_stdout.txt
StandardError=append:/root/textagent_stderr.txt

# 环境变量
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable textagent
systemctl start textagent
echo "  ✅ TextAgent 服务已启动"

# ============ 完成 ============
echo ""
echo "============================================"
echo "🎉 TextAgent 部署完成！"
echo "============================================"
echo ""
echo "📋 部署信息："
echo "  域名:     textagent.cn"
echo "  服务器:   <REDACTED_SERVER_IP>"
echo "  服务端口: 9000（Nginx 80 转发）"
echo "  用户 ID:  ChenTian"
echo ""
echo "📋 接下来你需要做的："
echo ""
echo "  1️⃣  配置 DNS 解析（阿里云控制台）："
echo "     类型: A 记录"
echo "     主机记录: @"
echo "     记录值: <REDACTED_SERVER_IP>"
echo ""
echo "  2️⃣  企业微信配置接收消息 URL："
echo "     URL: http://textagent.cn/wework"
echo "     Token: <REDACTED_WEWORK_TOKEN>"
echo "     EncodingAESKey: <REDACTED_AES_KEY>"
echo ""
echo "  3️⃣  企业微信配置企业可信 IP："
echo "     IP: <REDACTED_SERVER_IP>"
echo ""
echo "📋 常用命令："
echo "  查看状态:   systemctl status textagent"
echo "  查看日志:   tail -f /root/textagent_stdout.txt"
echo "  重启服务:   systemctl restart textagent"
echo "  停止服务:   systemctl stop textagent"
echo "  Nginx日志:  tail -f /var/log/nginx/error.log"
echo ""
