#!/usr/bin/env bash
# 单条本地启动命令：重置演示数据 → 创建预约/放行/签收样例 → 启动 Web 服务。
# 用法：
#   ./run.sh            等同 demo：生成样例并启动
#   ./run.sh demo       同上
#   ./run.sh serve      保留现有数据，仅启动
#   ./run.sh reset      重置数据库与保护照片
#   ./run.sh test       运行测试（容量竞争/占位过期/重量边界/字段脱敏/签收幂等）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${APP_PORT:-8000}"
export APP_PORT="$PORT"

case "${1:-demo}" in
  demo)
    echo "▶ 生成预约 / 门岗放行 / 签收样例并启动服务（端口 ${PORT}）…"
    exec python3 -m app demo
    ;;
  serve)
    exec python3 -m app
    ;;
  reset)
    exec python3 -m app reset
    ;;
  test)
    exec python3 -m unittest discover -s tests -v
    ;;
  *)
    echo "未知参数：$1（可用：demo | serve | reset | test）" >&2
    exit 2
    ;;
esac
