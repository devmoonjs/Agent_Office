#!/usr/bin/env bash
# collect_diffs.sh — repos.md에 등록된 repo들의 특정 날짜 커밋/diff 수집
#
# 사용법:
#   collect_diffs.sh [YYYY-MM-DD]     # 기본: 어제
#
# 동작:
#   - 90-Meta/repos.md에서 절대경로 라인을 읽는다 (사용자 관리 파일, 읽기 전용)
#   - 경로가 git repo면 그대로, 상위 폴더면 하위(depth 2)의 git repo들로 자동 확장
#   - 해당 날짜의 커밋별 메시지 + stat + diff를 수집 (커밋당 diff 줄 수 상한 적용)
#   - 시크릿 의심 라인은 [REDACTED] 처리
#   - 결과: .cache/diffs/YYYY-MM-DD.md (Obsidian에는 숨김)
#
# repo 라인 옵션 (경로 뒤 '|'):  branch=<브랜치>, max-diff=<줄수, 기본 500>
set -uo pipefail

VAULT="$(cd "$(dirname "$0")/../.." && pwd)"
REPOS_FILE="$VAULT/90-Meta/repos.md"
yesterday() {
  if date -v-1d +%F >/dev/null 2>&1; then date -v-1d +%F   # BSD/macOS
  else date -d yesterday +%F; fi                             # GNU/Linux
}
DATE="${1:-$(yesterday)}"
OUT_DIR="$VAULT/.cache/diffs"
OUT="$OUT_DIR/$DATE.md"
DEFAULT_MAX_DIFF=500

mkdir -p "$OUT_DIR"
{
  echo "# Diff 수집 결과 — $DATE"
  echo ""
} > "$OUT"

redact() {
  # 시크릿 패턴이 들어간 라인은 값 부분을 [REDACTED]로 치환
  sed -E \
    -e 's/((api[_-]?key|secret|token|passw(or)?d|credential|private[_-]?key)[[:alnum:]_-]*[[:space:]]*[=:][[:space:]]*).+/\1[REDACTED]/Ig' \
    -e 's/AKIA[0-9A-Z]{16}/[REDACTED-AWS-KEY]/g' \
    -e 's/(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})/[REDACTED-JWT]/g' \
    -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED-API-KEY]/g'
}

# repos.md에서 절대경로 라인 추출 (코드블록 안 포함, '#' 이후는 주석)
REPO_LINES=$(grep -E '^[[:space:]]*/' "$REPOS_FILE" | sed 's/#.*$//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' || true)

if [ -z "$REPO_LINES" ]; then
  echo "repos.md에 등록된 repo가 없습니다." >> "$OUT"
  echo "$OUT"
  exit 0
fi

TOTAL_COMMITS=0

collect_repo() {
  local repo="$1" branch="$2" max_diff="$3"
  local name range_opt=""
  name=$(basename "$repo")

  [ -n "$branch" ] && range_opt="$branch"

  local hashes
  hashes=$(git -C "$repo" log $range_opt --since="$DATE 00:00:00" --until="$DATE 23:59:59" --pretty=%H 2>/dev/null || true)
  [ -z "$hashes" ] && return 0

  local count
  count=$(echo "$hashes" | wc -l | tr -d ' ')
  TOTAL_COMMITS=$((TOTAL_COMMITS + count))

  {
    echo "## repo: $name"
    echo "- 경로: $repo"
    echo "- 커밋 수: $count"
    echo ""
  } >> "$OUT"

  local h
  for h in $hashes; do
    {
      echo "### commit ${h:0:8} — $(git -C "$repo" log -1 --pretty='%s (%an, %ad)' --date=format:'%H:%M' "$h")"
      echo ""
      echo '```'
      git -C "$repo" show --stat --pretty=format: "$h" | head -50
      echo '```'
      echo ""
      echo '```diff'
      git -C "$repo" show --pretty=format: "$h" | head -n "$max_diff" | redact
      local total_lines
      total_lines=$(git -C "$repo" show --pretty=format: "$h" | wc -l | tr -d ' ')
      if [ "$total_lines" -gt "$max_diff" ]; then
        echo ""
        echo "... (diff ${total_lines}줄 중 ${max_diff}줄까지만 수집 — 상한 초과 절단)"
      fi
      echo '```'
      echo ""
    } >> "$OUT"
  done
}

while IFS= read -r line; do
  path="${line%%|*}"
  path="$(echo "$path" | sed 's/[[:space:]]*$//')"
  opts="${line#*|}"
  [ "$opts" = "$line" ] && opts=""

  branch=""
  max_diff=$DEFAULT_MAX_DIFF
  if [ -n "$opts" ]; then
    branch=$(echo "$opts" | grep -oE 'branch=[^,[:space:]]+' | cut -d= -f2 || true)
    md=$(echo "$opts" | grep -oE 'max-diff=[0-9]+' | cut -d= -f2 || true)
    [ -n "$md" ] && max_diff=$md
  fi

  if [ ! -d "$path" ]; then
    echo "> ⚠️ 경로 없음(건너뜀): $path" >> "$OUT"
    continue
  fi

  if [ -d "$path/.git" ]; then
    collect_repo "$path" "$branch" "$max_diff"
  else
    # 상위 폴더 → 하위 git repo로 확장
    while IFS= read -r child; do
      collect_repo "${child%/.git}" "$branch" "$max_diff"
    done < <(find "$path" -maxdepth 2 -name .git -type d 2>/dev/null)
  fi
done <<< "$REPO_LINES"

{
  echo "---"
  echo "총 커밋: $TOTAL_COMMITS"
} >> "$OUT"

echo "$OUT"
