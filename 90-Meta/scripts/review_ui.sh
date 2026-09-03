#!/usr/bin/env bash
# review_ui.sh [start|stop|status] — ingest 관계 검토 웹 UI (기본: start)
set -euo pipefail

VAULT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT=57900
PIDFILE="$VAULT/.cache/review-ui.pid"
LOG="$VAULT/.cache/review-ui.log"
URL="http://localhost:$PORT"

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-start}" in
  start)
    mkdir -p "$VAULT/.cache"
    if is_running; then
      echo "이미 실행 중: $URL"
    else
      nohup python3 "$VAULT/90-Meta/review-ui/server.py" "$PORT" > "$LOG" 2>&1 &
      echo $! > "$PIDFILE"
      sleep 1
      is_running || { echo "기동 실패 — 로그: $LOG" >&2; exit 1; }
      echo "기동 완료: $URL"
    fi
    command -v open >/dev/null && open "$URL"
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "중지 완료"
    else
      echo "실행 중 아님"
    fi
    ;;
  status)
    is_running && echo "실행 중: $URL (pid $(cat "$PIDFILE"))" || echo "중지됨"
    ;;
  *)
    echo "사용법: review_ui.sh [start|stop|status]" >&2; exit 1
    ;;
esac
