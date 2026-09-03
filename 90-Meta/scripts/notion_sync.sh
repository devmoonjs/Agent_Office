#!/bin/bash
# my_jarvis → Notion 동기화 실행 래퍼 (launchd / 수동 공용).
# 스크립트가 .cache/notion-sync.log에 자체 로깅하므로 별도 리다이렉트는 하지 않는다.
cd "$(dirname "$0")/../.." || exit 1   # → my_jarvis 루트
exec /usr/bin/python3 90-Meta/scripts/notion_sync.py "$@"
