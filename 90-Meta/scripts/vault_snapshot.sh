#!/usr/bin/env bash
# vault_snapshot.sh [YYYY-MM-DD] — vault의 미커밋 변경을 해당 날짜 커밋으로 스냅샷
#
# 용도: my_jarvis vault 자체를 daily-review 추적 대상에 합류시킨다.
# vault는 커밋 습관이 없는 작업 공간이므로, 매일 리뷰 직전에 이 스크립트가
# 전날 작업분을 자동 커밋한다.
#
# 날짜 처리: daily-review는 보통 다음날 아침에 전날(D)을 분석한다. 이때 커밋
# 시각이 실행 시점(D+1)이면 git log --since/--until 윈도우(D)에서 누락되므로,
# author/committer date를 D 23:59:00으로 고정해 수집 윈도우 안에 넣는다.
set -euo pipefail

VAULT="$(cd "$(dirname "$0")/../.." && pwd)"
DATE="${1:-$(date -v-1d +%F)}"

cd "$VAULT"
[ -d .git ] || { echo "오류: vault가 git repo가 아님" >&2; exit 1; }

git add -A
if git diff --cached --quiet; then
  echo "스냅샷 생략: 변경 없음"
  exit 0
fi

CHANGED=$(git diff --cached --stat | tail -1)
GIT_AUTHOR_DATE="$DATE 23:59:00" GIT_COMMITTER_DATE="$DATE 23:59:00" \
  git commit --quiet -m "vault: $DATE 작업 스냅샷 — $CHANGED"
echo "스냅샷 커밋 완료: $(git log --oneline -1)"
