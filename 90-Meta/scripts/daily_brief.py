#!/usr/bin/env python3
"""선제적 브리핑 — 스케줄러(launchd/cron)가 호출해 텔레그램으로 먼저 보낸다.

사용법:
  python3 90-Meta/scripts/daily_brief.py            # 아침 브리핑
  python3 90-Meta/scripts/daily_brief.py weekly     # 주간 업무 요약(주간보고 초안)
  python3 90-Meta/scripts/daily_brief.py --stdout   # 전송하지 않고 출력만 (테스트용)

브리핑 본문은 Claude Code 헤드리스 실행으로 생성한다 — vault의 CLAUDE.md 규칙과
그래프 자동 활용 훅이 그대로 적용되므로 근거(파일 경로·출처)가 붙는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_api as tg              # noqa: E402
from telegram_bot import BRIEF_PROMPT, run_claude   # noqa: E402

# 소유자 이름 — 90-Meta/.env의 OWNER_NAME (없으면 일반 표현으로 동작)
_OWNER = tg._cfg.get("OWNER_NAME", "").strip() or "나"
_OWNER_NOTE = (f"70-Activity/persons/{_OWNER}.md와 참여 프로젝트 노트"
               if _OWNER != "나" else "내 활동 노트(70-Activity/persons/)와 참여 프로젝트 노트")

WEEKLY_PROMPT = (
    f"이번 주 내({_OWNER}) 업무 요약을 주간보고 초안 형태로 작성한다.\n"
    f"1) {_OWNER_NOTE}에서 이번 주 업무를 모은다\n"
    "2) 프로젝트별로 묶어 '무엇을 했는가'를 한 줄씩 정리한다\n"
    "3) 진행중·일시정지로 남은 항목을 다음 주 계획 후보로 따로 모은다\n"
    "4) 업무 기록은 덤프 시점 스냅샷이므로 기준 날짜를 첫 줄에 밝힌다\n\n"
    "출력 형식(엄수): 텔레그램 메시지로 그대로 전송되므로 보고 본문만 출력한다. "
    "무엇을 조회했다는 서문, 구분선(---), 글자 수 관련 후기를 붙이지 않는다. "
    "마크다운 표·제목 없이 짧은 문단과 '- ' 목록만 쓰고 전체 2000자 이내로 쓴다."
)


def calendar_context():
    """캘린더는 스크립트가 직접 읽어 프롬프트에 실어준다.
    헤드리스 에이전트가 osascript를 호출하게 두면 TCC 권한 문제가 어디서 났는지 흐려진다."""
    import subprocess
    try:
        p = subprocess.run(["python3", str(Path(__file__).parent / "calendar_sync.py"),
                            "--back", "30", "--ahead", "2"],
                           cwd=Path(__file__).resolve().parents[2],
                           capture_output=True, text=True, timeout=300)
        out = (p.stdout or "").strip()
        return f"\n\n[캘린더 조회 결과 — 이 내용을 근거로 일정 항목을 쓴다]\n{out}" if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def main():
    args = [a for a in sys.argv[1:]]
    stdout_only = "--stdout" in args
    mode = "weekly" if "weekly" in args else "daily"
    prompt = WEEKLY_PROMPT if mode == "weekly" else BRIEF_PROMPT + calendar_context()
    header = "주간 업무 요약" if mode == "weekly" else "오늘의 브리핑"

    body = run_claude(prompt, timeout=900)
    text = f"[{header}]\n\n{body}"

    if stdout_only:
        print(text)
        return
    if not tg.ALLOWED:
        sys.exit("TELEGRAM_ALLOWED_CHAT_IDS 미설정 — 전송할 대상이 없습니다.")
    for chat in sorted(tg.ALLOWED):
        tg.send(chat, text)
    print(f"{header} 전송 완료 ({len(tg.ALLOWED)}명)")


if __name__ == "__main__":
    main()
