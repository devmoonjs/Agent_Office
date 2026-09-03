# Notion 자동 동기화

`10-Daily/`·`15-Reports/`에 새 `.md`가 생기면 Notion **MyOrg**로 자동 적재한다.
launchd가 두 폴더를 실시간 감시하고, 신규 파일만 멱등하게 올린다.

## 구성 요소

| 파일 | 역할 |
|---|---|
| `90-Meta/scripts/notion_sync.py` | 동기화 본체 (stdlib 전용, Notion REST API) |
| `90-Meta/scripts/notion_sync.sh` | 실행 래퍼 (launchd/수동 공용) |
| `~/Library/LaunchAgents/com.myjarvis.notion-sync.plist` | 폴더 감시 + 30분 백스톱 |
| `90-Meta/.env` | `NOTION_TOKEN` (gitignore, 직접 생성) |
| `.cache/notion-sync.json` | 적재 완료 파일 기록 (중복 방지, 기존 21건 등록됨) |
| `.cache/notion-sync.log` | 실행 로그 |

## 적재 규칙

본문은 **원본 .md 전체를 Notion 블록으로 변환**해 그대로 넣는다(요약 아님). 제목·표·코드블록·목록·인용을 변환하고, 100블록 초과 시 분할 append, 2000자 초과 텍스트는 분할한다. 데이터소스에 없는 속성은 자동 제외한다(스키마 조회 후 필터링).

- `10-Daily/<날짜>.md` → **Daily Report** DB. 속성: `날짜`/`일자`=날짜, `한 일`=개요 미리보기, `관련 프로젝트`=개요 `Repo` 표 첫 칸. 본문=원본 전체.
- `15-Reports/<repo>/<날짜>.md` → **일별 개발 현황/이슈**. 속성: `기록`=H1, `유형`=개발 현황, `프로젝트`=frontmatter `project`(또는 폴더명), `다음 액션`=`## 다음 예정 작업`. 본문=원본 전체.

폴더명 → Notion 프로젝트: `chat_bot`=교육운영 AI 챗봇 서비스 · `Lego-React`=LEGO · `predictive-finance`=예측 회계 시스템 · `my_jarvis`=Project J. (스크립트 `PROJECT_PAGE`/`REPO_ALIASES`에서 수정)

## 활성화 (1회, 토큰 필요)

1. **integration 생성** — https://www.notion.so/my-integrations → New integration → Internal → **Secret** 복사. (무료)
2. **DB 연결** — Notion에서 **MyOrg**, **Daily Report**, **Project**(상위 페이지) 각각 `···` → Connections → 위 integration 추가.
3. **토큰 저장** — `cp 90-Meta/.env.example 90-Meta/.env` 후 `NOTION_TOKEN=` 값을 실제 secret으로 교체.
4. **연결 테스트** (실제 적재 없음):
   ```bash
   cd ~/Documents/my_jarvis && /usr/bin/python3 90-Meta/scripts/notion_sync.py --dry-run
   ```
5. **launchd 등록**:
   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myjarvis.notion-sync.plist
   # 또는 구버전 macOS: launchctl load ~/Library/LaunchAgents/com.myjarvis.notion-sync.plist
   ```

이후 새 데일리/리포트가 생기면 자동 적재된다.

## 운영

- **수동 1회 실행**: `90-Meta/scripts/notion_sync.sh`
- **특정 파일만**: `... notion_sync.py --once 10-Daily/2026-06-22.md`
- **로그 확인**: `tail -f .cache/notion-sync.log`
- **재적재**(실수 시): `.cache/notion-sync.json`에서 해당 키 줄을 지우고 다시 실행.
- **중지**: `launchctl bootout gui/$(id -u)/com.myjarvis.notion-sync`
- **새 프로젝트 추가 시**: `notion_sync.py`의 `PROJECT_PAGE`·`REPO_ALIASES`에 폴더명→page id 추가, plist `WatchPaths`에 하위폴더 추가 후 재등록.

## 주의

- 토큰(`90-Meta/.env`)은 절대 커밋 금지 (`.gitignore` 등록됨).
- 이미 올린 21건은 `.cache/notion-sync.json`에 등록되어 재적재되지 않는다.
- Notion 쿼리/SQL(MCP)은 Enterprise 전용이나, 이 스크립트는 페이지 **생성**만 하므로 무료 범위다.
