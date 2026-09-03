#!/usr/bin/env bash
# Agent Office 초기 설정 — 의존성 설치와 .env 생성만 담당한다.
# 볼트 골격은 git clone 시점에 이미 만들어져 있다.
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/2] 파이썬 의존성 확인"
python3 -m pip install --quiet --upgrade requests pyyaml 2>/dev/null || \
  echo "  경고: pip 설치 실패. 수동으로 requests, pyyaml을 설치한다."

echo "[2/2] 90-Meta/.env 생성"
if [ -f 90-Meta/.env ]; then
  echo "  이미 존재한다. 건너뛴다."
else
  cp 90-Meta/.env.example 90-Meta/.env
  echo "  90-Meta/.env 를 만들었다. 편집기로 열어 토큰을 채운다."
fi

echo
echo "다음: 90-Meta/repos.md 에 분석 대상 repo 경로를 적고"
echo "      python3 90-Meta/map-ui/server.py  →  http://127.0.0.1:57800"
