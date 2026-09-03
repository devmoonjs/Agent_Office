#!/usr/bin/env python3
"""애플 캘린더(Calendar.app) → Obsidian 단방향 연동.

읽기 전용이다. 캘린더에 쓰지 않는다 — 캘린더가 Source of Truth이고,
폰·회사 시스템과 얽혀 있어 실수로 쓰면 복구가 어렵다.

하는 일:
  1. 지난 회의 중 `60-Sources/meetings/`에 기록이 없는 것을 찾아낸다
  2. 다가오는 일정을 브리핑에 쓸 수 있는 형태로 낸다
  3. 제목의 `[사업]` 접두를 그래프의 Project 이름과 대조한다

사용법:
  python3 90-Meta/scripts/calendar_sync.py                 # 사람이 읽는 요약
  python3 90-Meta/scripts/calendar_sync.py --json          # 다른 스크립트용
  python3 90-Meta/scripts/calendar_sync.py --back 60 --ahead 14

접근 방식은 osascript다. Google Calendar MCP와 달리 대화형 인증이 없어
launchd/헤드리스에서도 동작한다. 다만 TCC 권한 컨텍스트가 다를 수 있어
launchd 최초 실행 시 거부되면 권한을 한 번 승인해야 한다.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEETINGS = ROOT / "60-Sources" / "meetings"
# 업무 일정이 들어 있는 캘린더. 미리 알림·생일·공휴일·Siri 제안은 제외한다
CALENDARS = ["집", "직장"]

SCRIPT = """
set d1 to (current date) - {back} * days
set time of d1 to 0
set d2 to (current date) + {ahead} * days
set time of d2 to 0
set out to ""
tell application "Calendar"
  repeat with c in calendars
    set cn to name of c
    if cn is in {{{cals}}} then
      tell c
        set evs to (every event whose start date is greater than or equal to d1 ¬
                    and start date is less than or equal to d2)
        repeat with e in evs
          set sd to start date of e
          set out to out & cn & tab & (summary of e) & tab & (year of sd) & tab & ¬
            ((month of sd) as integer) & tab & (day of sd) & tab & ¬
            (hours of sd) & tab & (minutes of sd) & linefeed
        end repeat
      end tell
    end if
  end repeat
end tell
return out
"""


def read_events(back, ahead):
    cals = ", ".join(f'"{c}"' for c in CALENDARS)
    src = SCRIPT.format(back=back, ahead=ahead, cals=cals)
    try:
        p = subprocess.run(["osascript", "-e", src], capture_output=True, text=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"캘린더 조회 실패: {e}") from e
    if p.returncode != 0:
        raise RuntimeError(f"캘린더 접근 거부 또는 오류: {(p.stderr or '').strip()[:300]}")
    events = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        cal, summary, y, mo, d, h, mi = f[0], f[1].strip(), *f[2:7]
        try:
            when = datetime(int(y), int(mo), int(d), int(h), int(mi))
        except ValueError:
            continue
        events.append({
            "calendar": cal, "summary": summary, "when": when,
            "date": when.date().isoformat(),
            "time": f"{int(h):02d}:{int(mi):02d}",
            "all_day": int(h) == 0 and int(mi) == 0,     # 종일/표식성 일정 구분
            "project": (re.match(r"^\[([^\]]+)\]", summary) or [None, None])[1],
        })
    return sorted(events, key=lambda e: e["when"])


def note_dates():
    """회의록이 존재하는 날짜 집합 (파일명 앞 YYYY-MM-DD 기준)."""
    if not MEETINGS.is_dir():
        return set()
    out = set()
    for f in MEETINGS.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m:
            out.add(m.group(1))
    return out


def analyze(events):
    today = date.today()
    have = note_dates()
    missing, upcoming, markers = [], [], []
    for e in events:
        d = date.fromisoformat(e["date"])
        if d > today:
            upcoming.append(e)
        elif e["all_day"]:
            markers.append(e)                     # 종일 일정은 회의록 대상에서 뺀다
        elif e["date"] not in have:
            missing.append(e)
    return missing, upcoming, markers


def render(missing, upcoming, markers, back, ahead):
    L = []
    if upcoming:
        L.append(f"다가오는 일정 ({ahead}일 이내) {len(upcoming)}건")
        for e in upcoming:
            proj = f" · {e['project']}" if e["project"] else ""
            t = "종일" if e["all_day"] else e["time"]
            L.append(f"- {e['date']} {t} {e['summary']}{proj}")
    else:
        L.append(f"다가오는 일정 없음 ({ahead}일 이내)")
    L.append("")
    if missing:
        L.append(f"회의록 없는 지난 일정 {len(missing)}건 (최근 {back}일)")
        for e in missing[-10:]:
            proj = f" · {e['project']}" if e["project"] else ""
            L.append(f"- {e['date']} {e['summary']}{proj}")
        L.append("기록하려면 회의록을 60-Sources/meetings/ 에 만들거나 녹음을 /ingest 한다")
    else:
        L.append(f"회의록 누락 없음 (최근 {back}일)")
    if markers:
        L.append("")
        L.append(f"종일 일정 {len(markers)}건은 회의록 대상에서 제외: "
                 + ", ".join(f"{e['date']} {e['summary']}" for e in markers[-5:]))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--back", type=int, default=45)
    ap.add_argument("--ahead", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        events = read_events(args.back, args.ahead)
    except RuntimeError as e:
        msg = str(e)
        if args.json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"캘린더 연동 실패 — {msg}\n"
                  "launchd에서 처음 실행하는 경우 캘린더 접근 권한 승인이 필요할 수 있다.")
        sys.exit(1)

    missing, upcoming, markers = analyze(events)
    if args.json:
        strip = lambda es: [{k: v for k, v in e.items() if k != "when"} for e in es]
        print(json.dumps({"missing": strip(missing), "upcoming": strip(upcoming),
                          "markers": strip(markers), "total": len(events)},
                         ensure_ascii=False, indent=1))
    else:
        print(render(missing, upcoming, markers, args.back, args.ahead))


if __name__ == "__main__":
    main()
