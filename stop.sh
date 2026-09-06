#!/bin/bash

# Trend Bot v1.2 停止脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/trend_bot.pid"

# 检查 PID 文件是否存在
if [ ! -f "$PID_FILE" ]; then
    echo "❌ Trend Bot 未在运行（未找到 PID 文件）"
    exit 1
fi

# 读取 PID
PID=$(cat "$PID_FILE")

# 检查进程是否存在
if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "⚠️  进程 $PID 不存在，清理 PID 文件..."
    rm -f "$PID_FILE"
    exit 1
fi

# 停止进程
echo "🛑 正在停止 Trend Bot (PID: $PID)..."
kill "$PID"

# 等待进程结束（最多 10 秒）
for i in {1..10}; do
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "✅ Trend Bot 已停止"
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

# 如果 10 秒后还未停止，强制终止
echo "⚠️  进程未响应，强制终止..."
kill -9 "$PID"
sleep 1

if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "✅ Trend Bot 已强制停止"
    rm -f "$PID_FILE"
    exit 0
else
    echo "❌ 无法停止进程 $PID"
    exit 1
fi
