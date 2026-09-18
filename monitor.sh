#!/usr/bin/env bash
# vLLM Monitor 控制脚本 (systemd 版): start / stop / restart / status / log / open
# 依赖: /etc/systemd/system/vllm-monitor.service 已安装, 且已配置免密 sudo
set -u
SVC="vllm-monitor"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/monitor.log"
PORT=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',8501))" 2>/dev/null || echo 8501)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PANEL="http://${IP:-<IP>}:$PORT"

run() { sudo "$@"; }

case "${1:-status}" in
  start)
    run systemctl start "$SVC"
    sleep 2
    run systemctl is-active "$SVC" | grep -q active && echo "已启动。面板: $PANEL" || { echo "启动失败, 查看日志:"; journalctl -u "$SVC" -n 20 --no-pager; exit 1; }
    ;;
  stop)
    run systemctl stop "$SVC"
    echo "已停止"
    ;;
  restart)
    run systemctl restart "$SVC"
    sleep 2
    run systemctl is-active "$SVC" | grep -q active && echo "已重启。面板: $PANEL" || { echo "重启失败"; exit 1; }
    ;;
  status)
    echo "=== 服务状态 ==="
    run systemctl status "$SVC" --no-pager | head -8
    echo "=== 自动识别 ==="
    curl -s --max-time 4 "http://127.0.0.1:$PORT/api/detected" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(服务未运行或无响应)"
    echo "=== 面板 ==="
    echo "$PANEL"
    ;;
  log)
    journalctl -u "$SVC" -f --no-pager
    ;;
  open)
    xdg-open "$PANEL" 2>/dev/null || echo "面板: $PANEL"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log|open}"
    exit 1
    ;;
esac
