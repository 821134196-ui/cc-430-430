#!/usr/bin/env bash
# 单条本地启动命令：建库 + 演示数据 + 预约/保护/放行/签收样例 + 启动 Web
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
exec python3 -m app.cli up --host 127.0.0.1 --port "${PORT:-8000}"
