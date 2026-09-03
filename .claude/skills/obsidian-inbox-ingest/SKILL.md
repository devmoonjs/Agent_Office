---
name: obsidian-inbox-ingest
description: Obsidian vault의 Inbox에 투입된 문서를 사용자가 지정한 시각마다 자동으로 md화·ingest(위키 환류, 아카이브)한다. "매일 9시에 inbox 처리되게 해줘" 같은 스케줄 요청을 받으면 OS 스케줄러에 작업을 설치하고, 스케줄이 트리거한 실행에서는 ingest 규약을 수행한다. codex CLI와 Claude Code 어디서든 동작하는 이식용 스킬.
---

# obsidian-inbox-ingest — Inbox 자동 적재 스킬

이 스킬은 두 역할을 겸한다.

- **A. 스케줄 설치** — 사용자가 시각을 지정하면 OS 스케줄러(macOS launchd / Linux cron)에 ingest 작업을 등록한다
- **B. ingest 실행 규약** — 스케줄(또는 수동 호출)이 트리거한 실행에서 따르는 처리 절차

## 0. 볼트 설정 — 다른 환경에 이식할 때 이 표만 수정한다

| 키 | 기본값 | 설명 |
|---|---|---|
| `VAULT` | (에이전트 작업 루트) | Obsidian 볼트 루트. 절대 경로 |
| `INBOX` | `00-Inbox` | 문서 투입 폴더 (볼트 상대 경로) |
| `ARCHIVE` | `60-Sources` | 처리 완료 원본 보관 폴더 |
| `WIKI` | `20-Wiki` | 지식 노트 폴더 |
| `WORKDIR` | `.ingest` | 러너 스크립트·락·로그 위치 |

**로컬 규칙 우선 원칙**: 볼트에 `AGENTS.md`/`CLAUDE.md` 또는 자체 ingest 스킬(예: `.claude/skills/ingest`)이 있으면 문서 처리 절차는 **그 로컬 규칙을 우선**하고, 이 스킬의 B장은 로컬 규칙이 다루지 않는 부분만 보충한다. 스케줄 설치(A장)는 항상 이 스킬을 따른다.

## A. 스케줄 설치 절차

1. **시각 확인 (생략 금지)**: 사용자가 시각을 말하지 않았으면 반드시 묻는다. 임의로 정하지 않는다. 복수 시각(예: 09:00과 18:00)을 지원한다
2. **러너 생성**: `VAULT/WORKDIR/run.sh`를 아래 템플릿으로 만든다 (`__키__` 자리를 0장 표의 값으로 치환, `chmod +x`)
3. **스케줄러 등록**: 플랫폼별 템플릿(아래)으로 등록한다
4. **PATH 주의**: `which codex` 결과의 디렉토리를 스케줄러 환경 PATH에 반드시 포함한다. launchd/cron은 최소 PATH로 돌므로 빠지면 "codex: command not found"로 조용히 실패한다
5. **검증·증거 (생략 금지)**: 등록 후 `launchctl list | grep inbox-ingest`(macOS) 또는 `crontab -l`(Linux) 출력을 사용자에게 보여주고, 다음 실행 예정 시각을 한 줄로 확인한다

### 러너 템플릿 — `run.sh`

```bash
#!/bin/bash
# Inbox 자동 ingest 러너 — 락으로 중복 실행을 막고 codex를 헤드리스로 실행한다.
VAULT="__VAULT__"
LOCK="$VAULT/__WORKDIR__/lock"
LOG="$VAULT/__WORKDIR__/ingest.log"

# 이전 회차가 진행 중이면 조용히 종료한다 (ingest 1건이 다음 틱보다 길 수 있다)
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT

# 처리할 파일이 없으면 에이전트를 띄우지 않는다 (호출 비용 0).
# -mmin +1: 수정 1분 미만 파일 제외 — 복사·업로드가 끝나지 않은 파일 보호
find "$VAULT/__INBOX__" -maxdepth 1 -type f ! -name '.*' ! -name '*.json' -mmin +1 \
  | grep -q . || exit 0

echo "[$(date '+%F %T')] inbox ingest 시작" >> "$LOG"
codex exec --cd "$VAULT" --sandbox workspace-write \
  --output-last-message "$VAULT/__WORKDIR__/last.md" \
  "obsidian-inbox-ingest 스킬의 'B. ingest 실행 규약'에 따라 __INBOX__ 의 문서를 전부 처리하라." \
  >> "$LOG" 2>&1
echo "[$(date '+%F %T')] 종료 (exit $?)" >> "$LOG"
```

Claude Code로 돌리는 환경이면 `codex exec ...` 줄을
`claude -p "/ingest" --dangerously-skip-permissions`로 바꾼다.

### macOS — launchd 템플릿

`~/Library/LaunchAgents/com.obsidian.inbox-ingest.plist` 로 저장 후
`launchctl load ~/Library/LaunchAgents/com.obsidian.inbox-ingest.plist`.
시각 변경은 unload → 수정 → load 순서다. 맥이 자고 있었으면 깨어난 뒤 실행된다.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.obsidian.inbox-ingest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>__VAULT__/__WORKDIR__/run.sh</string>
  </array>
  <!-- 사용자가 지정한 시각. 복수 시각이면 dict를 여러 개 담은 array로 쓴다 -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>__HOUR__</integer>
    <key>Minute</key><integer>__MINUTE__</integer>
  </dict>
  <key>StandardOutPath</key><string>__VAULT__/__WORKDIR__/launchd.log</string>
  <key>StandardErrorPath</key><string>__VAULT__/__WORKDIR__/launchd.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- `which codex`의 디렉토리를 반드시 포함할 것 -->
    <key>PATH</key><string>__CODEX_BIN_DIR__:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

### Linux — cron 템플릿

```
# crontab -e  (분 시 일 월 요일)
0 9 * * * PATH=__CODEX_BIN_DIR__:/usr/local/bin:/usr/bin:/bin /bin/bash __VAULT__/__WORKDIR__/run.sh
```

## B. ingest 실행 규약

1. **대상**: `INBOX` 루트의 파일. 숨김 파일·`.json`(메타데이터)은 제외. 수정된 지 60초 미만인 파일은 다음 회차로 미룬다
2. **md화**:
   - `.md`/`.txt`: 직접 읽는다
   - `.docx`/`.rtf`: macOS는 `textutil -convert txt`, Linux는 `pandoc -t markdown`으로 변환 후 읽는다
   - `.pdf`/이미지: 직접 읽는다 (읽기 도구가 지원하는 경우), 불가하면 처리하지 않고 보고한다
   - 원문 요지를 구조화한 md 노트로 작성한다. frontmatter에 `type`, `created`(YYYY-MM-DD), `source`(원본 파일명)를 필수로 넣는다
3. **위키 환류**: 문서에서 재사용 가치가 있는 개념·패턴을 추출해 `WIKI`에 노트를 만든다. **작성 전 같은 주제의 기존 노트를 파일명과 aliases 양쪽으로 검색**하고, 있으면 새로 만들지 말고 보강한다
4. **아카이브**: 처리한 원본은 `ARCHIVE`로 **이동한다 (삭제 금지)**. 내용이 이미 처리된 문서의 중복 투입으로 판명되면 파일명에 `-inbox-duplicate` 접미사를 붙여 아카이브하고 기존 노트는 건드리지 않는다
5. **시크릿 마스킹**: API key·토큰·비밀번호 패턴이 보이면 기록하지 않고 `[REDACTED]` 처리한 뒤 최종 보고에 경고를 남긴다
6. **실패 격리**: 처리에 실패한 파일은 `INBOX/_failed/`로 이동하고 사유를 로그에 남긴다. 그대로 두면 매 회차 같은 파일로 무한 재시도가 발생한다
7. **보고**: 처리 건수, 생성·보강한 노트 경로, 아카이브 경로를 최종 메시지로 요약한다. 처리할 것이 없었으면 그렇다고 한 줄만 남긴다

## 이식 — 새 "Obsidian + codex" 환경 설치 스텝

1. 이 스킬 폴더를 새 머신의 `~/.codex/skills/obsidian-inbox-ingest/`로 복사한다. Claude Code와 겸용하려면 볼트의 `.claude/skills/`에 두고 `ln -s <볼트>/.claude/skills/obsidian-inbox-ingest ~/.codex/skills/obsidian-inbox-ingest`로 심링크한다
2. 볼트 폴더 구성이 다르면 0장 표의 값만 고친다
3. codex에게 말한다: "inbox 자동 적재를 매일 09:00로 설정해줘" → A장 절차가 수행된다
4. 요구사항: codex CLI 설치·로그인(`codex login status`로 확인), macOS는 `textutil` 내장, Linux는 `pandoc` 설치
