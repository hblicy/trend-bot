#!/bin/bash

# Trend Bot v1.2 重启脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🔄 重启 Trend Bot..."

# 停止
"${SCRIPT_DIR}/stop.sh"

# 等待 1 秒
sleep 1

# 启动
"${SCRIPT_DIR}/start.sh"
