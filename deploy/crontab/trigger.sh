#!/bin/bash
# ================================================================
# TextAgent cron 触发脚本
# 用法: trigger.sh <action>
# 功能: curl POST /system 端点，带超时、重试、4xx 不重试逻辑
# ================================================================

set -euo pipefail

ACTION="${1:?用法: trigger.sh <action>}"
URL="${TEXTAGENT_SYSTEM_URL:-http://127.0.0.1:9000/system}"
TIMEOUT="${TEXTAGENT_CRON_TIMEOUT:-120}"
MAX_RETRIES=1
TS=$(date '+%Y-%m-%d %H:%M:%S')

_fire() {
    # curl 返回 HTTP 状态码到 stdout，响应体写到临时文件
    local tmp
    tmp=$(mktemp)
    local http_code
    http_code=$(curl -s -o "$tmp" -w '%{http_code}' \
        -X POST "$URL" \
        -H 'Content-Type: application/json' \
        -d "{\"action\":\"${ACTION}\"}" \
        --connect-timeout 10 \
        --max-time "$TIMEOUT" \
    ) || {
        local exit_code=$?
        echo "[$TS] ❌ ${ACTION} curl 失败 (exit=$exit_code)"
        rm -f "$tmp"
        return 1
    }

    local body
    body=$(cat "$tmp")
    rm -f "$tmp"

    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
        echo "[$TS] ✅ ${ACTION} -> ${http_code}"
        return 0
    elif [ "$http_code" -ge 400 ] && [ "$http_code" -lt 500 ]; then
        # 4xx 客户端错误：不重试（请求本身有问题，重试也没用）
        echo "[$TS] ⚠️  ${ACTION} -> ${http_code} (4xx 不重试) body=${body:0:200}"
        return 2
    else
        # 5xx 或其他：可重试
        echo "[$TS] ❌ ${ACTION} -> ${http_code} body=${body:0:200}"
        return 1
    fi
}

# ─── 主逻辑：首次调用 + 重试 ───
if _fire; then
    exit 0
fi

exit_code=$?
if [ "$exit_code" -eq 2 ]; then
    # 4xx 错误，直接退出不重试
    exit 1
fi

# 5xx / 网络错误：等 5 秒后重试一次
for i in $(seq 1 $MAX_RETRIES); do
    echo "[$TS] 🔄 ${ACTION} 第 ${i} 次重试..."
    sleep 5
    if _fire; then
        exit 0
    fi
    retry_exit=$?
    if [ "$retry_exit" -eq 2 ]; then
        exit 1
    fi
done

echo "[$TS] 💀 ${ACTION} 重试 ${MAX_RETRIES} 次后仍失败"
exit 1
