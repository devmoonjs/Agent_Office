# Agent Office

Obsidian 볼트 위에서 도는 개인 AI 비서 워크스페이스. 회의 녹음·전사, 지식 위키 환류,
인물/프로젝트 그래프(Neo4j), 에이전트 실시간 관제 맵을 한 화면에서 다룬다.

**클론이 곧 볼트다.** 별도 생성 절차 없이 클론한 폴더가 그대로 볼트 루트가 된다.

## 빠른 시작

```bash
git clone https://github.com/<you>/Agent_Office.git
cd Agent_Office
bash setup.sh                 # 파이썬 의존성 + 90-Meta/.env 생성
python3 90-Meta/map-ui/server.py    # http://127.0.0.1:57800
```

## 구성

| 경로 | 역할 |
|---|---|
| `00-Inbox/` | 외부 문서 투입구 (`/ingest`가 처리) |
| `10-Daily/` `15-Reports/` | 일일 학습 노트 · 프로젝트 변경 보고서 |
| `20-Wiki/` | 원자 단위 지식 위키 |
| `60-Sources/` | 처리 완료 원본 아카이브 |
| `90-Meta/map-ui/` | Agent Office UI + API 서버 (`server.py`) |
| `90-Meta/scripts/` | 수집·전사·동기화 스크립트 |
| `90-Meta/neo4j/` | 그래프 DB 구성 (`docker compose up -d`) |
| `.claude/skills/` | `/daily-review` `/ingest` `/graph` 등 스킬 |

## 설정

- `90-Meta/repos.md` — 분석 대상 repo 절대경로를 한 줄에 하나씩 적는다.
- `90-Meta/.env` — `.env.example`를 복사해 토큰을 채운다. git에 올라가지 않는다.
- `CLAUDE.md` — 에이전트 운영 규칙. 자신의 문서 규칙에 맞게 고쳐 쓴다.

## 라이선스

MIT — `LICENSE` 참조.
