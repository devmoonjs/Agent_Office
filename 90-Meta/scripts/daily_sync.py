#!/usr/bin/env python3
"""매일 17:00 하루 마감 동기화 — launchd가 호출한다.

흐름:
  1. 위키 → Neo4j 단방향 동기화 (Concept·Topic·RELATED_TO). 판단이 필요 없어 자동 적재
  2. 00-Inbox에 미처리 문서가 있으면 /ingest 헤드리스 실행 → 검토표 생성
  3. 검토 대기 건수 집계
  4. 결과를 텔레그램으로 발송 (미설정이면 표준출력만)

인물 관계는 여기서 적재하지 않는다 — SCHEMA 불변 규칙과 /ingest 규약에 따라
사용자 컨펌을 거쳐야 하며, 이 배치는 검토표를 만들어 알리는 데까지만 관여한다.

사용법:
  python3 90-Meta/scripts/daily_sync.py [--stdout] [--skip-ingest]
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_api as tg                      # noqa: E402
import wiki_sync                               # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
INBOX = ROOT / "00-Inbox"
REVIEW = ROOT / ".cache" / "review"
SKIP_SUFFIX = {".json"}                        # 녹음 meta 등 문서가 아닌 것


def inbox_docs():
    if not INBOX.is_dir():
        return []
    return [f for f in sorted(INBOX.iterdir())
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() not in SKIP_SUFFIX]


def run_ingest(fname, timeout=1800):
    """Inbox 문서 1건을 ingest한다 — codex 우선, 실패 시 claude 폴백.

    codex는 ~/.codex/skills/ingest(→ .claude/skills/ingest 심링크)로 같은 스킬 규약을
    읽는다. 응답은 --output-last-message 파일로 받는다 (stdout에는 이벤트 로그가 섞인다).
    """
    out_f = ROOT / ".cache" / "codex-ingest-last.md"
    prompt = (f'ingest 스킬을 사용해 "00-Inbox/{fname}" 를 처리하라. '
              f'스킬 규약(아카이브 경로, meta 권위, 검토 JSON)을 그대로 따른다.')
    try:
        out_f.unlink(missing_ok=True)
        p = subprocess.run(["codex", "exec", "--cd", str(ROOT),
                            "--sandbox", "workspace-write",
                            "--output-last-message", str(out_f), prompt],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        out = out_f.read_text().strip() if out_f.is_file() else ""
        out_f.unlink(missing_ok=True)
        if out:
            return ("[codex] " + out)[-400:]
    except FileNotFoundError:
        pass                                    # codex 미설치 — claude로
    except subprocess.TimeoutExpired:
        return f"{fname}: 시간 초과"
    try:
        p = subprocess.run(["claude", "-p", f'/ingest "00-Inbox/{fname}"',
                            "--dangerously-skip-permissions"],
                           cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return ("[claude 폴백] " + (p.stdout or p.stderr or "").strip())[-400:]
    except FileNotFoundError:
        return "codex·claude CLI 모두 없음"
    except subprocess.TimeoutExpired:
        return f"{fname}: 시간 초과"


def pending_reviews():
    if not REVIEW.is_dir():
        return []
    out = []
    for f in sorted(REVIEW.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("status", "pending") == "pending":
            out.append((d.get("doc") or f.stem, len(d.get("relations", []))))
    return out


def main():
    args = sys.argv[1:]
    lines = [f"[하루 마감 동기화] {date.today().isoformat()}", ""]

    try:
        lines.append(_wiki())
    except Exception as e:                                  # noqa: BLE001
        lines.append(f"위키 동기화 실패: {str(e)[:300]}")

    docs = inbox_docs()
    if docs and "--skip-ingest" not in args:
        lines += ["", f"Inbox 문서 {len(docs)}건 처리:"]
        for f in docs:
            lines.append(f"- {f.name}: {run_ingest(f.name)[:200]}")
    elif docs:
        lines += ["", f"Inbox 문서 {len(docs)}건 (ingest 생략)"]
    else:
        lines += ["", "Inbox 비어 있음"]

    try:                                        # 캘린더는 실패해도 나머지를 막지 않는다
        cal = subprocess.run(["python3", str(Path(__file__).parent / "calendar_sync.py"),
                              "--back", "45", "--ahead", "3"],
                             cwd=ROOT, capture_output=True, text=True, timeout=300)
        lines += ["", (cal.stdout or cal.stderr or "").strip()]
    except (OSError, subprocess.SubprocessError) as e:
        lines += ["", f"캘린더 조회 실패: {str(e)[:200]}"]

    pend = pending_reviews()
    if pend:
        lines += ["", f"검토 대기 {len(pend)}건 — 승인해야 그래프에 적재된다"]
        lines += [f"- {d} (관계 {n}건)" for d, n in pend[:5]]
        lines.append("review-ui: http://localhost:57900")
    else:
        lines += ["", "검토 대기 없음"]

    text = "\n".join(lines)
    if "--stdout" in args or not tg.ALLOWED:
        print(text)
        return
    for chat in sorted(tg.ALLOWED):
        tg.send(chat, text)
    print(text)


def _wiki():
    """wiki_sync를 인자 없이 호출해 요약 문자열을 얻는다."""
    argv = sys.argv
    sys.argv = ["wiki_sync", "--quiet"]
    try:
        return wiki_sync.main()
    finally:
        sys.argv = argv


if __name__ == "__main__":
    main()
