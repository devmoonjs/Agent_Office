#!/usr/bin/env bash
# Agent Office 업데이트 — 설정 화면의 [업데이트] 버튼과 터미널 양쪽에서 쓴다.
#
#   bash update.sh --check    상태만 조회 (JSON 출력, 아무것도 바꾸지 않는다)
#   bash update.sh            업데이트 실행. 로컬 수정이 있으면 중단한다
#   bash update.sh --force    로컬 수정을 .agent-office/backup/ 에 백업한 뒤 덮어쓴다
#
# 사용자 파일(.env, repos.md, .agent-office/, 볼트 노트)은 git 추적 대상이 아니므로
# 어느 경우에도 손대지 않는다.
set -uo pipefail
cd "$(dirname "$0")"

MODE="${1:-run}"
# 추적 중인 원격 브랜치. 설정돼 있지 않으면 origin/main 으로 본다.
UPSTREAM="$(git rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo origin/main)"
REMOTE="${UPSTREAM%%/*}"
BRANCH="${UPSTREAM#*/}"

if [ ! -d .git ]; then
  echo '{"ok":false,"error":"git 저장소가 아니다 — 설치 방식을 확인한다"}'
  exit 1
fi

git fetch --quiet "$REMOTE" "$BRANCH" 2>/dev/null || {
  echo '{"ok":false,"error":"원격 조회 실패 — 네트워크를 확인한다"}'
  exit 1
}

CUR="$(git rev-parse --short HEAD)"
CUR_TS="$(git log -1 --date=format:'%Y-%m-%d' --pretty=%ad)"
LATEST="$(git rev-parse --short "$UPSTREAM" 2>/dev/null || echo "$CUR")"
BEHIND="$(git rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo 0)"
# 추적 파일 중 수정된 것만 (untracked·ignored 는 무시)
DIRTY="$(git diff --name-only HEAD 2>/dev/null | tr '\n' ',' | sed 's/,$//')"

json() {
  printf '{"ok":%s,"current":"%s","current_date":"%s","latest":"%s","behind":%s,"dirty":"%s","branch":"%s"%s}\n' \
    "$1" "$CUR" "$CUR_TS" "$LATEST" "$BEHIND" "$DIRTY" "$UPSTREAM" "${2:-}"
}

if [ "$MODE" = "--check" ]; then
  json true
  exit 0
fi

if [ "$BEHIND" = "0" ]; then
  json true ',"message":"이미 최신입니다"'
  exit 0
fi

if [ -n "$DIRTY" ] && [ "$MODE" != "--force" ]; then
  json false ',"error":"로컬 수정이 있어 업데이트를 중단했다. 강제 업데이트를 쓰면 백업 후 덮어쓴다"'
  exit 2
fi

if [ -n "$DIRTY" ]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  BK=".agent-office/backup/$TS"
  mkdir -p "$BK"
  IFS=',' read -ra FILES <<< "$DIRTY"
  for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    mkdir -p "$BK/$(dirname "$f")"
    cp "$f" "$BK/$f"
  done
  git checkout -- $(echo "$DIRTY" | tr ',' ' ') 2>/dev/null
  echo "  로컬 수정을 $BK 에 백업했다." >&2
fi

if ! git pull --ff-only --quiet "$REMOTE" "$BRANCH" 2>&1; then
  json false ',"error":"git pull 실패 — 터미널에서 직접 확인이 필요하다"'
  exit 1
fi

bash setup.sh >&2 || true

CUR="$(git rev-parse --short HEAD)"
CUR_TS="$(git log -1 --date=format:'%Y-%m-%d' --pretty=%ad)"
BEHIND=0
DIRTY=""
json true ',"message":"업데이트 완료 — 서버를 재시작한다"'
