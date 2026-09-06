#!/bin/bash

# Trend Bot v1.2 状态查询脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/trend_bot.pid"
LOG_FILE="${SCRIPT_DIR}/trend_detector.log"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Trend Bot v1.2 运行状态"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 检查 PID 文件
if [ ! -f "$PID_FILE" ]; then
    echo "状态: ❌ 未运行"
    exit 0
fi

# 读取 PID
PID=$(cat "$PID_FILE")

# 检查进程是否存在
if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "状态: ⚠️  异常（PID 文件存在但进程不存在）"
    echo "PID 文件: $PID_FILE"
    echo "建议: 运行 './stop.sh' 清理，然后运行 './start.sh' 重新启动"
    exit 1
fi

# 获取进程信息
PROCESS_INFO=$(ps -p "$PID" -o pid,etime,rss,cmd --no-headers)
UPTIME=$(echo "$PROCESS_INFO" | awk '{print $2}')
MEMORY=$(echo "$PROCESS_INFO" | awk '{print $3}')
MEMORY_MB=$(awk "BEGIN {printf \"%.1f\", $MEMORY/1024}")

echo "状态: ✅ 运行中"
echo "PID: $PID"
echo "运行时长: $UPTIME"
echo "内存占用: ${MEMORY_MB} MB"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 显示最近的日志
if [ -f "$LOG_FILE" ]; then
    echo ""
    echo "📋 最近 10 条日志:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    tail -n 10 "$LOG_FILE"
fi
