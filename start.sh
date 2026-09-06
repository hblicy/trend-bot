#!/bin/bash

# Trend Bot v1.2 启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/trend_bot.pid"
LOG_FILE="${SCRIPT_DIR}/trend_detector.log"

# 检查是否已在运行
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "❌ Trend Bot 已经在运行 (PID: $PID)"
        exit 1
    else
        echo "⚠️  发现过期的 PID 文件，正在清理..."
        rm -f "$PID_FILE"
    fi
fi

# 检查 Python 虚拟环境（如果存在）
if [ -d "${SCRIPT_DIR}/venv" ]; then
    echo "🔧 激活虚拟环境..."
    source "${SCRIPT_DIR}/venv/bin/activate"
fi

# 启动程序
echo "🚀 启动 Trend Bot v1.2..."
cd "$SCRIPT_DIR"
nohup python3 trend_detector.py >> "$LOG_FILE" 2>&1 &

# 保存 PID
PID=$!
echo $PID > "$PID_FILE"

# 等待 2 秒检查是否成功启动
sleep 2
if ps -p "$PID" > /dev/null 2>&1; then
    echo "✅ Trend Bot 启动成功 (PID: $PID)"
    echo "📋 日志文件: $LOG_FILE"
    echo "💡 使用 './stop.sh' 停止程序"
    echo "💡 使用 './status.sh' 查看运行状态"
else
    echo "❌ 启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
