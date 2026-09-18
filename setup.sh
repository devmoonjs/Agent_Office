#!/usr/bin/env bash
# Agent Office 초기 설정 — 설치와 갱신 양쪽에서 호출된다 (멱등).
#
# 하는 일:
#   1. 사용자 설정 파일 생성 (.env, repos.md) — 이미 있으면 건드리지 않는다
#   2. 구버전 레이아웃 마이그레이션 — 사용자가 고치는 파일을 git 추적에서 뺀다
#   3. 의존성 점검 — 없는 것만 알려준다
#
# 볼트 골격은 git clone 시점에 이미 만들어져 있다.
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] 사용자 설정 파일"

if [ -f 90-Meta/.env ]; then
  echo "  90-Meta/.env 있음"
else
  cp 90-Meta/.env.example 90-Meta/.env
  echo "  90-Meta/.env 생성 — 편집기로 열어 토큰을 채운다 (선택 기능용)"
fi

if [ -f 90-Meta/repos.md ]; then
  echo "  90-Meta/repos.md 있음"
else
  cp 90-Meta/repos.example.md 90-Meta/repos.md
  echo "  90-Meta/repos.md 생성 — 분석 대상 폴더 경로를 적는다 (UI에서 추가해도 된다)"
fi

echo "[2/3] 마이그레이션"

# 사용자가 고치거나 UI가 자동으로 쓰는 파일이 git에 추적돼 있으면 git pull 이 충돌한다.
# --cached 라서 파일 내용은 그대로 두고 추적만 끊는다.
untrack() {
  if git ls-files --error-unmatch "$1" >/dev/null 2>&1; then
    git rm --cached -q "$1"
    echo "  추적 해제: $1 (내용은 보존)"
  fi
}
if [ -d .git ]; then
  untrack 90-Meta/repos.md
  untrack 90-Meta/map-ui/agent-config.json
fi

# 에이전트 프롬프트·스케줄을 gitignore된 .agent-office/ 로 옮긴다.
mkdir -p .agent-office
if [ -f 90-Meta/map-ui/agent-config.json ] && [ ! -f .agent-office/agent-config.json ]; then
  mv 90-Meta/map-ui/agent-config.json .agent-office/agent-config.json
  echo "  agent-config.json → .agent-office/ 로 이동"
fi

echo "[3/3] 의존성 점검"

need=0
check() {
  if command -v "$1" >/dev/null 2>&1; then
    printf "  %-8s 있음\n" "$1"
  else
    printf "  %-8s [없음] %s\n" "$1" "$2"
    need=1
  fi
}
check python3 "sudo apt install -y python3"
check git     "sudo apt install -y git"
check claude  "curl -fsSL https://claude.ai/install.sh | bash"
check tmux    "sudo apt install -y tmux          # 터미널 탭용"
check ttyd    "아래 ttyd 설치 명령 참고           # 터미널 탭용"
check pandoc  "sudo apt install -y pandoc        # 문서 변환용"
check ffmpeg  "sudo apt install -y ffmpeg        # 회의 녹음 전사용"

if ! command -v ttyd >/dev/null 2>&1; then
  echo
  echo "  ttyd 설치:"
  echo "    sudo curl -fsSL \"https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.\$(uname -m)\" -o /usr/local/bin/ttyd"
  echo "    sudo chmod +x /usr/local/bin/ttyd"
fi

echo
if [ "$need" = "1" ]; then
  echo "빠진 도구가 있다. 위 명령으로 설치하면 해당 기능이 켜진다."
else
  echo "모든 의존성 확인됨."
fi
echo "서버 실행:  python3 90-Meta/map-ui/server.py   →  http://127.0.0.1:57910"
