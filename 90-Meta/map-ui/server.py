#!/usr/bin/env python3
"""에이전트 관제 맵 — 로컬 실시간 서버 (stdlib 전용, review-ui/server.py와 같은 방침).

하는 일:
  1. http://127.0.0.1:57910/ 에 맵(map.html)을 서빙한다
  2. 워처 스레드가 5초마다 변경을 스캔해 이벤트로 변환한다
       - repos.md의 각 repo 파일 변경  → 프로젝트 에이전트
       - vault 산출 폴더(위키/Daily/Inbox 등) → 기능 에이전트(개념 담당자/기록 담당자/비서/코드 리뷰어)
       - .cache/telegram-bot.log 새 줄   → 비서
  3. 기동 시 텔레그램 봇(telegram_bot.py)을 자식 프로세스로 함께 띄운다
       (이미 떠 있으면 건너뜀 — 중복 폴링 방지. 종료 시 함께 내린다)

이벤트는 메모리 + .cache/agent-events.jsonl 에 남는다. 이후 기능 에이전트들이
이 파일에 직접 이벤트를 쓰면(스캐폴딩 단계) 맵이 그대로 반영한다.

실행:  python3 90-Meta/map-ui/server.py
중지:  Ctrl-C (봇도 함께 종료)
"""
import atexit
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
EVENTS_FILE = CACHE / "agent-events.jsonl"
BOT = ROOT / "90-Meta" / "scripts" / "telegram_bot.py"
BOT_LOG = CACHE / "telegram-bot.log"
PORT = int(os.environ.get("MAP_PORT", "57910"))
SCAN_SEC = 5
UPLOAD_DIR = CACHE / "uploads"      # 채팅 첨부 파일 저장소
UPLOAD_MAX = 50 * 1024 * 1024        # 첨부 1건 최대 50MB
REVIEW_API = os.environ.get("REVIEW_API", "http://127.0.0.1:57900")   # review-ui(관계 검토·Neo4j) — /graph-api/* 프록시 대상

# 프로젝트 표시명 (없으면 폴더명 그대로)
NAMES = {
    # 예시 — 자신의 repo 폴더명을 표시명으로 매핑한다
    "my_jarvis": "자비스",
}
# vault 폴더 → 기능 에이전트 매핑 (my_jarvis repo는 이 규칙으로만 처리)
VAULT_MAP = [
    ("20-Wiki", "miner"), ("10-Daily", "scribe"), ("15-Reports", "scribe"),
    ("30-Feedback", "reviewer"), ("00-Inbox", "secretary"), ("60-Sources", "secretary"),
    ("50-Weekly", "retro"),
]
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist",
             "build", ".next", "target", ".obsidian", ".cache", ".omc",
             ".claudian", ".claude", ".idea", ".vscode"}

# ── 설정 저장소 (.agent-office/) ─────────────────────────
AOCFG_DIR = ROOT / ".agent-office"
AOCFG_FILE = AOCFG_DIR / "config.json"
AOPROJ_FILE = AOCFG_DIR / "projects.json"


def ao_config():
    """볼트 루트 .agent-office/config.json을 읽는다. 없으면 빈 dict."""
    try:
        return json.loads(AOCFG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ao_config(cfg):
    AOCFG_DIR.mkdir(parents=True, exist_ok=True)
    AOCFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def ao_projects():
    """사용자가 UI에서 추가한 프로젝트 목록."""
    try:
        return json.loads(AOPROJ_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_ao_projects(prs):
    AOCFG_DIR.mkdir(parents=True, exist_ok=True)
    AOPROJ_FILE.write_text(json.dumps(prs, ensure_ascii=False, indent=2), encoding="utf-8")


def use_graph():
    """Neo4j 그래프 기능 활성 여부. 기본 false."""
    return bool(ao_config().get("useGraph", False))


_lock = threading.Lock()
_events = []          # [{id,ts,agent,text}]
_next_id = 1


def emit(agent, text):
    global _next_id
    ev = {"id": 0, "ts": time.strftime("%H:%M:%S"), "agent": agent, "text": text[:120]}
    with _lock:
        ev["id"] = _next_id
        _next_id += 1
        _events.append(ev)
        del _events[:-500]
    try:
        CACHE.mkdir(exist_ok=True)
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(f"[{ev['ts']}] {agent}: {text}", flush=True)


def parse_repos():
    """repos.md + .agent-office/projects.json의 절대경로를 병합한다. 존재하는 디렉토리만 채택. 경로 기준 dedupe."""
    out = []
    seen = set()
    # 1) repos.md (사용자 전용 파일 — 읽기만)
    try:
        txt = (ROOT / "90-Meta" / "repos.md").read_text(encoding="utf-8")
    except OSError:
        txt = ""
    for line in txt.splitlines():
        line = line.strip()
        if not line.startswith("/"):
            continue
        path = line.split("|")[0].split("#")[0].strip()
        p = Path(path)
        if p.is_dir() and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    # 2) .agent-office/projects.json (UI에서 추가한 프로젝트)
    for pr in ao_projects():
        path = pr.get("path", "")
        if not path:
            continue
        p = Path(path)
        if p.is_dir() and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
            # 표시명이 있으면 NAMES에도 등록
            if pr.get("name"):
                NAMES.setdefault(p.name, pr["name"])
    return out


def scan_tree(base: Path, since: float, limit=20000):
    """since 이후 수정된 파일 목록 (숨김/빌드 폴더 제외). (files, max_mtime)"""
    changed, newest, seen = [], since, 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            seen += 1
            if seen > limit:
                return changed, newest
            try:
                mt = os.stat(os.path.join(dirpath, fn)).st_mtime
            except OSError:
                continue
            if mt > newest:
                newest = mt
            if mt > since:
                changed.append(os.path.relpath(os.path.join(dirpath, fn), base))
    return changed, newest


def watcher():
    repos = [r for r in parse_repos() if r.resolve() != ROOT]
    state = {str(r): time.time() for r in repos}      # repo → 마지막 스캔 기준 mtime
    vault_state = {k: time.time() for k, _ in VAULT_MAP}
    log_pos = BOT_LOG.stat().st_size if BOT_LOG.is_file() else 0
    emit("system", f"워처 시작 — repo {len(repos)}개 + vault + 텔레그램 로그 감시")
    while True:
        time.sleep(SCAN_SEC)
        # 1) 프로젝트 repo
        for r in repos:
            key = str(r)
            try:
                changed, newest = scan_tree(r, state[key])
            except OSError:
                continue
            if changed:
                state[key] = newest
                names = ", ".join(Path(c).name for c in changed[:3])
                more = f" 외 {len(changed)-3}건" if len(changed) > 3 else ""
                emit("proj:" + r.name, f"파일 {len(changed)}개 변경 — {names}{more}")
        # 2) vault 산출 폴더 → 기능 에이전트
        for folder, agent in VAULT_MAP:
            base = ROOT / folder
            if not base.is_dir():
                continue
            changed, newest = scan_tree(base, vault_state[folder])
            if changed:
                vault_state[folder] = newest
                emit(agent, f"{folder} 갱신 — {Path(changed[0]).name}"
                            + (f" 외 {len(changed)-1}건" if len(changed) > 1 else ""))
        # 2.5) 캐스팅 프로젝트 폴더 → biz 에이전트
        for pr in cprojects():
            d = CAST_OUT / pr["name"]
            if not d.is_dir():
                continue
            key = "cpd:" + pr["id"]
            if key not in vault_state:
                vault_state[key] = time.time()
            changed, newest = scan_tree(d, vault_state[key])
            if changed:
                vault_state[key] = newest
                emit("biz:" + pr["id"], f"산출물 갱신 — {Path(changed[0]).name}"
                     + (f" 외 {len(changed)-1}건" if len(changed) > 1 else ""))
        # 3) 텔레그램 로그 tail → 비서
        try:
            if BOT_LOG.is_file():
                size = BOT_LOG.stat().st_size
                if size > log_pos:
                    with BOT_LOG.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(log_pos)
                        tail = f.read()
                    log_pos = size
                    lines = [l.strip() for l in tail.splitlines() if l.strip()]
                    if lines:
                        emit("secretary", "텔레그램 — " + lines[-1][:80])
                elif size < log_pos:
                    log_pos = size
        except OSError:
            pass


# ── 비서 채팅 (헤드리스 에이전트 실행) ─────────────────
CHAT = []            # [{role:'user'|'assistant', ts, text}]
_chat_lock = threading.Lock()
CHAT_FILE = CACHE / "secretary-chat.json"
try:
    CHAT = json.loads(CHAT_FILE.read_text(encoding="utf-8"))[-200:]
except (OSError, ValueError):
    CHAT = []

SECRETARY_PERSONA = """너는 이 vault 주인의 개인 비서다. 지금은 채팅 대화이므로 CLAUDE.md의
문서 스타일 규칙(평서형 ~다 종결, 기술문서 톤)은 이 답변에 적용하지 않는다.

- 실제 비서처럼 정중하고 자연스러운 존댓말로 말한다 ("~입니다", "~해 드릴까요?")
- 보고서식 나열 대신 요점을 한두 문장으로 먼저 말하고, 세부는 그 뒤에 짧게 붙인다
- 정보만 던지지 말고 비서답게 다음 행동을 제안하거나 되묻는다 ("캘린더에 잡아드릴까요?", "더 찾아볼까요?")
- 근거 파일은 문장 끝에 괄호로 조용히 (`경로`) 표기한다
- 모르는 것은 아는 척하지 않고 "확인해서 말씀드리겠습니다"라고 한다
- 자료 조회(그래프, vault 검색)는 평소 규칙대로 수행하되, 답변 문체만 위를 따른다"""


def _save_chat():
    try:
        CHAT_FILE.write_text(json.dumps(CHAT[-200:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _history_block():
    """최근 대화 8턴을 프롬프트용 텍스트로 (현재 질문 제외)."""
    turns = CHAT[-9:-1] if len(CHAT) > 1 else []
    if not turns:
        return ""
    lines = []
    for m in turns:
        who = "사용자" if m["role"] == "user" else "비서(너)"
        lines.append(f"{who}: {m['text'][:500]}")
    return "\n\n[이전 대화 — 맥락 참고용]\n" + "\n".join(lines)
WEB_STYLE = ("\n\n(출력 규칙: 웹 채팅에 마크다운으로 렌더링된다. 제목(##/###), **굵게**, "
             "목록, 표, 코드블록을 자유롭게 쓰되, 파일 경로·문서명·코드 식별자·명령어는 "
             "반드시 `백틱`으로 감싼다. 근거 파일 경로를 함께 적는다. 3000자 이내.)")


def find_claude():
    # 표준 설치 경로 우선 — PATH의 claude가 앱 내장 바이너리(미로그인)일 수 있다
    cand = Path.home() / ".local/bin/claude"
    if cand.is_file():
        return str(cand)
    return shutil.which("claude") or "claude"


# ── 모델 선택 ─────────────────────────────────────────
# UI에서 고른 값이 claude CLI의 --model 인자로 들어간다. 값은 argv에 직접 붙으므로
# 반드시 아래 허용 목록으로만 통과시킨다 (임의 문자열이면 '--...' 형태로 옵션이 주입된다).
UI_SETTINGS = CACHE / "ui-settings.json"
MODELS = [
    ("", "기본값 (CLI 설정)"),
    ("fable", "Fable 5"),
    ("opus", "Opus 5"),
    ("opus[1m]", "Opus 5 · 1M"),
    ("sonnet", "Sonnet 5"),
    ("sonnet[1m]", "Sonnet 5 · 1M"),
    ("haiku", "Haiku 4.5"),
]
MODEL_VALUES = {v for v, _ in MODELS}
MODEL_LABELS = dict(MODELS)


def current_model():
    """UI에서 고른 전역 모델. 허용 목록 밖의 값은 기본값으로 취급한다."""
    m = _load_json(UI_SETTINGS, {}).get("model", "")
    return m if m in MODEL_VALUES else ""


def save_model(m):
    if m not in MODEL_VALUES:
        return False
    d = _load_json(UI_SETTINGS, {})
    d["model"] = m
    try:
        CACHE.mkdir(exist_ok=True)
        UI_SETTINGS.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


def model_args(override=None):
    """--model 인자. 데스크별 지정(override)이 전역 설정보다 우선하고,
    둘 다 비어 있으면 인자를 붙이지 않아 CLI 기본값을 그대로 쓴다."""
    m = override if (override and override in MODEL_VALUES) else current_model()
    return ["--model", m] if m else []


# 메뉴바 앱·맵 UI·텔레그램이 동시에 질문하면 claude 프로세스가 그 수만큼 뜬다.
# 상한을 넘는 요청은 여기서 대기시킨다(요청은 버리지 않는다).
_claude_slots = threading.BoundedSemaphore(int(os.environ.get("MAP_MAX_CLAUDE", "3")))


def run_agent(prompt, timeout=600, style=True, model=None):
    """vault를 작업 디렉토리로 claude 헤드리스 실행 — CLAUDE.md 규칙·그래프 훅 적용."""
    # 부모가 Claude 세션 안일 때 물려받는 마커를 제거 (자식 세션 오인 → 로그인 오류 방지)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
    with _claude_slots:
        try:
            r = subprocess.run([find_claude(), "-p", prompt + (WEB_STYLE if style else ""),
                                "--dangerously-skip-permissions"] + model_args(model),
                               cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, env=env)
            out = (r.stdout or "").strip()
            return out or f"(응답 없음 — exit {r.returncode}: {(r.stderr or '')[:200]})"
        except subprocess.TimeoutExpired:
            return "(시간 초과 — 질문을 좁혀 다시 시도)"
        except OSError as e:
            return f"(claude CLI 실행 실패: {e})"


def _fmt_tool(name, inp):
    """stream-json의 tool_use 이벤트 → 사람이 읽는 진행 문구."""
    def rel(p):
        p = str(p or "")
        pre = str(ROOT) + os.sep
        return p[len(pre):] if p.startswith(pre) else p
    if name == "Read":
        return "파일 읽는 중 — " + rel(inp.get("file_path"))
    if name == "Write":
        return "파일 작성 중 — " + rel(inp.get("file_path"))
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "파일 수정 중 — " + rel(inp.get("file_path"))
    if name == "Bash":
        return "명령 실행 — " + (inp.get("description") or inp.get("command") or "")[:80]
    if name == "Grep":
        return "본문 검색 — " + str(inp.get("pattern", ""))[:60]
    if name == "Glob":
        return "파일 탐색 — " + str(inp.get("pattern", ""))[:60]
    if name in ("Task", "Agent"):
        return "보조 에이전트 — " + str(inp.get("description", ""))[:60]
    if name in ("WebSearch", "WebFetch"):
        return "웹 조회 — " + str(inp.get("query") or inp.get("url") or "")[:60]
    if name == "Skill":
        return "스킬 실행 — " + str(inp.get("skill", ""))
    if name == "TodoWrite":
        return "작업 목록 갱신"
    return "도구 실행 — " + name


def run_agent_stream(prompt, on_line, timeout=600, style=True, model=None, on_delta=None, ctl=None):
    """run_agent의 스트리밍판 — 도구 사용/사고 이벤트를 on_line(문구)으로 중계하고 최종 답변을 반환.
    on_delta가 있으면 답변 텍스트를 생성되는 즉시 조각(delta)으로 흘려보낸다. 인자가 None이면
    '새 텍스트 블록 시작 — 지금까지의 초안을 버려라'는 신호다(도구 사용 전 중간 발언 → 최종 답변)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
    with _claude_slots:
        return _run_agent_stream_locked(prompt, on_line, timeout, style, model, env, on_delta, ctl)


def _run_agent_stream_locked(prompt, on_line, timeout, style, model, env, on_delta=None, ctl=None):
    # ctl = {"cancel":bool, "proc":Popen|None} — 정지 버튼이 여기에 신호를 넣는다.
    # 슬롯 대기 중에 눌렸으면 아예 띄우지 않는다(대기열에 걸린 질문도 즉시 취소된다).
    if ctl and ctl.get("cancel"):
        return CANCEL_MSG
    try:
        proc = subprocess.Popen(
            [find_claude(), "-p", prompt + (WEB_STYLE if style else ""),
             "--dangerously-skip-permissions", "--output-format", "stream-json",
             "--verbose"] + (["--include-partial-messages"] if on_delta else [])
            + model_args(model),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, stdin=subprocess.DEVNULL, env=env)
    except OSError as e:
        return f"(claude CLI 실행 실패: {e})"
    if ctl is not None:
        ctl["proc"] = proc
        if ctl.get("cancel"):        # Popen 직후 정지가 들어온 경우
            proc.kill()
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    reply = None
    try:
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "stream_event" and on_delta:
                # 부분 메시지 — 답변이 생성되는 즉시 한 조각씩 흘린다(웹 채팅의 타자 효과).
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "content_block_start" and ((e.get("content_block") or {}).get("type")) == "text":
                    on_delta(None)          # 새 텍스트 블록 — 이전 초안(중간 발언)은 버린다
                elif et == "content_block_delta" and ((e.get("delta") or {}).get("type")) == "text_delta":
                    on_delta((e.get("delta") or {}).get("text") or "")
            elif t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    ct = c.get("type")
                    if ct == "tool_use":
                        on_line(_fmt_tool(c.get("name", ""), c.get("input") or {}))
                    elif ct == "thinking":
                        s = (c.get("thinking") or "").strip()
                        if s:
                            on_line("[판단] " + s.splitlines()[0][:90])
                    elif ct == "text":
                        s = (c.get("text") or "").strip()
                        if s:
                            on_line("[정리] " + s.splitlines()[0][:90])
            elif t == "result":
                reply = (ev.get("result") or "").strip() or reply
        proc.wait()
    finally:
        killer.cancel()
    if ctl and ctl.get("cancel"):
        return CANCEL_MSG
    if reply:
        return reply
    if proc.returncode and proc.returncode < 0:
        return "(시간 초과 — 질문을 좁혀 다시 시도)"
    err = ""
    try:
        err = (proc.stderr.read() or "")[:200]
    except (OSError, ValueError):
        pass
    return f"(응답 없음 — exit {proc.returncode}: {err})"


CANCEL_MSG = "(중단됨 — 사용자가 정지했습니다)"
CHAT_JOBS = {}        # job_id → {"lines":[...], "done":bool, "reply":str|None, "ts":epoch, "ctl":{...}}
_job_seq = [0]


def new_job():
    """대화 job 생성 — ctl은 정지 버튼이 쓰는 제어 채널이다."""
    with _chat_lock:
        _job_seq[0] += 1
        job = str(_job_seq[0])
        now = time.time()
        for k in [k for k, v in CHAT_JOBS.items() if now - v["ts"] > 3600]:
            CHAT_JOBS.pop(k, None)
        CHAT_JOBS[job] = {"lines": [], "done": False, "reply": None, "draft": "",
                          "ts": now, "ctl": {"cancel": False, "proc": None}}
    return job


def job_stop(job):
    """실행 중인 대화를 중단한다 — claude 프로세스를 죽이고, 그때까지 생성된 초안을 답변으로 남긴다.
    프로세스가 아직 안 떴으면(슬롯 대기 중) cancel 플래그만 세워 기동 자체를 막는다."""
    st = CHAT_JOBS.get(job)
    if not st or st.get("done"):
        return False
    with _chat_lock:
        ctl = st.setdefault("ctl", {"cancel": False, "proc": None})
        ctl["cancel"] = True
        st["cancel"] = True
        draft = (st.get("draft") or "").strip()
        st["reply"] = (draft + "\n\n*(사용자 요청으로 중단)*") if draft else CANCEL_MSG
        st["draft"] = ""
        st["done"] = True          # UI는 여기서 즉시 멈춘다. 프로세스 정리는 아래에서
        proc = ctl.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    return True


def chat_start(text):
    """질문 접수 → 백그라운드 작업 시작, 진행 조회용 job id 반환."""
    with _chat_lock:
        CHAT.append({"role": "user", "ts": time.strftime("%H:%M:%S"), "text": text})
        _save_chat()
    job = new_job()
    emit("secretary", "질문 접수 — " + text[:50])
    threading.Thread(target=_chat_job, args=(job, text), daemon=True).start()
    return job


def _draft_sink(st):
    """생성 중인 답변을 job에 누적한다 — /chat/progress가 이 초안을 그대로 내려보내
    웹 채팅이 글자 단위로 그린다. None은 '새 텍스트 블록 시작 → 초안 초기화' 신호."""
    def on_delta(piece):
        with _chat_lock:
            st["draft"] = "" if piece is None else (st.get("draft") or "") + piece
    return on_delta


def _chat_job(job, text):
    st = CHAT_JOBS[job]

    def on_line(s):
        with _chat_lock:
            st["lines"].append(time.strftime("%H:%M:%S") + "  " + s)

    prompt = SECRETARY_PERSONA + _history_block() + "\n\n[사용자의 현재 메시지]\n" + text
    reply = run_agent_stream(prompt, on_line, on_delta=_draft_sink(st), ctl=st["ctl"])
    with _chat_lock:
        if st.get("cancel"):        # 정지 버튼이 이미 답변(초안)을 확정해 뒀다
            reply = st.get("reply") or CANCEL_MSG
        else:
            st["reply"] = reply
            st["done"] = True
        CHAT.append({"role": "assistant", "ts": time.strftime("%H:%M:%S"), "text": reply})
        _save_chat()
    emit("secretary", ("중단됨 — " if st.get("cancel") else "답변 완료 — ") + text[:40])


# ── 기능·프로젝트 에이전트 채팅 (메뉴바 앱·맵 공용) ───────
# 색·이모지는 map.html의 FUN/PCOLS 팔레트와 같은 값이다(스프라이트를 메뉴바에서도 같게 그리기 위해).
# map.html을 바꾸면 여기도 맞춘다.
AGENT_LOOK = {
    "secretary": ("#e07a3e", "#3b3f4a", "🧑‍💼"), "scribe":   ("#8ab4f8", "#3b3f4a", "📝"),
    "miner":     ("#c58af9", "#4a3b4a", "🧠"), "auditor":  ("#f87171", "#4a3b3b", "🔍"),
    "tutor":     ("#f5c343", "#4a453b", "🏋️"), "reviewer": ("#4ade80", "#3b4a3f", "🧐"),
    "retro":     ("#9aa0ab", "#3b3f4a", "📊"), "gardener": ("#7fb069", "#3f4a3b", "🌱"),
}
PROJ_COLORS = ["#e8a15c", "#7cc0e8", "#b58af9", "#8fd18a", "#e88a9a", "#c9b45c", "#8ab4c8", "#d0906c"]
CAST_COLORS = ["#f0a8c0", "#a8d8f0", "#c8f0a8", "#f0d8a8", "#d8a8f0", "#a8f0d8"]
CHAT_STYLE = """\n\n지금은 채팅 대화이므로 CLAUDE.md의 문서 스타일 규칙(평서형 ~다 종결, 기술문서 톤)은
이 답변에 적용하지 않는다. 자연스러운 존댓말로, 요점을 먼저 한두 문장으로 말하고 세부는 짧게 붙인다.
근거 파일은 (`경로`)로 조용히 표기한다. 모르는 것은 아는 척하지 않는다."""


CONFIRM_BOT_PERSONA_GRAPH = (
    "너는 '관계 컨펌 봇'이다. 이 vault의 인물·조직·프로젝트·회의·액션아이템 지식그래프(Neo4j)를 담당한다. "
    "사용자의 질문은 90-Meta/neo4j/SCHEMA.md의 스키마에 맞춰 Cypher로 바꾸고 "
    "`bash 90-Meta/scripts/graph.sh query \"<cypher>\"`로 실행해 답한다. 이름 매칭이 안 되면 aliases 포함 검색으로 폴백한다. "
    "결과는 70-Activity/persons·projects 노트와 60-Sources/meetings 문서로 교차 보강하고, 근거(엣지 source·파일 경로)를 병기한다. "
    "Concept은 한글 별칭으로 부른다. 그래프 쓰기(적재·병합·삭제)는 절대 직접 하지 않는다 — 그것은 UI의 검토·적재 흐름이 담당한다.")
CONFIRM_BOT_PERSONA_NOGRAPH = (
    "너는 '관계 컨펌 봇'이다. 이 vault의 인물·조직·프로젝트·회의·문서를 관리한다. "
    "현재 지식그래프(Neo4j)가 비활성 상태이므로 Cypher 질의는 사용하지 않는다. "
    "70-Activity/persons·projects 노트와 60-Sources/meetings 문서를 검색해 답한다. "
    "근거(파일 경로)를 병기한다. 그래프를 활성화하려면 설정에서 '지식 그래프 사용'을 켜야 한다.")


def confirm_bot_persona():
    return CONFIRM_BOT_PERSONA_GRAPH if use_graph() else CONFIRM_BOT_PERSONA_NOGRAPH


def agent_chat_key_ok(key):
    if key in ("confirm-bot", "meeting-recorder"):
        return True
    if key in AGENT_JOBS and key != "secretary":
        return True
    return key.startswith("proj:") and repo_of(key) is not None


def agent_chat_file(key):
    return CACHE / ("agent-chat-" + re.sub(r"[^\w.-]", "_", key) + ".json")


def agent_persona(key):
    if key == "confirm-bot":
        return confirm_bot_persona() + CHAT_STYLE
    if key == "meeting-recorder":
        return MEETING_PERSONA + CHAT_STYLE
    if key.startswith("proj:"):
        repo = repo_of(key)
        name = NAMES.get(repo.name, repo.name)
        return (f"너는 '{name}' 프로젝트({repo.name}) 담당 에이전트다. 저장소 경로: {repo}\n"
                f"이 저장소의 코드와 git 이력(git -C '{repo}' log/diff/show), vault의 "
                f"15-Reports/{repo.name}/ 보고서, 10-Daily 노트를 근거로 이 프로젝트에 관한 질문에 답한다. "
                "다른 프로젝트 얘기는 하지 않는다. 코드 근거는 파일경로:라인으로 인용한다."
                + CHAT_STYLE)
    job = AGENT_JOBS[key]
    return (f"[네 역할] {job['label']}. 평소 임무: {job.get('prompt', '')}\n\n"
            "지금은 사용자가 너에게 직접 말을 건 것이다. 임무를 바로 실행하지 말고 질문에 답하거나, "
            "사용자가 명시적으로 요청한 작업만 수행하고 결과를 보고한다."
            + CHAT_STYLE)


def agent_chat_start(key, text, task=None):
    """기능/프로젝트 에이전트에게 질문 — 비서 채팅과 같은 job/progress 방식.
    task가 있으면 사용자 메시지 대신 그 지시문을 실행한다(호출 점검 등). 대화 이력에는 text가 남는다."""
    hfile = agent_chat_file(key)
    hist = _load_json(hfile, [])
    hist.append({"role": "user", "ts": time.strftime("%H:%M:%S"), "text": text})
    job = new_job()
    emit(key, "질문 접수 — " + text[:50])

    def work():
        st = CHAT_JOBS[job]

        def on_line(s):
            with _chat_lock:
                st["lines"].append(time.strftime("%H:%M:%S") + "  " + s)
        hblock = "\n".join(("사용자" if m["role"] == "user" else "너") + ": " + m["text"][:500]
                           for m in hist[-9:-1])
        prompt = (agent_persona(key)
                  + ("\n\n[이전 대화 — 맥락 참고용]\n" + hblock if hblock else "")
                  + "\n\n[사용자의 현재 메시지]\n" + (task or text))
        reply = run_agent_stream(prompt, on_line, timeout=900, on_delta=_draft_sink(st), ctl=st["ctl"])
        if st.get("cancel"):
            reply = st.get("reply") or CANCEL_MSG
        hist.append({"role": "assistant", "ts": time.strftime("%H:%M:%S"), "text": reply})
        try:
            CACHE.mkdir(exist_ok=True)
            hfile.write_text(json.dumps(hist[-200:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        with _chat_lock:
            if not st.get("cancel"):
                st["reply"] = reply
                st["done"] = True
        emit(key, ("중단됨 — " if st.get("cancel") else "답변 완료 — ") + text[:40])
    threading.Thread(target=work, daemon=True).start()
    return job


# ── 기능 에이전트 실행 (v1 스캐폴딩) ────────────────────
AGENT_JOBS = {
    "scribe": {
        "label": "기록 담당자", "timeout": 900,
        "prompt": "너는 기록 담당자다. 90-Meta/repos.md의 각 repo에서 오늘 커밋과 diff를 수집해 "
                  "(필요하면 bash 90-Meta/scripts/collect_diffs.sh 활용) 변경이 있는 repo만 "
                  "15-Reports/<repo>/오늘날짜.md 보고서와 10-Daily 오늘 노트의 '변경 요약' 절을 작성·갱신하라. "
                  "사실만 기록하고 해석하지 않는다. 시크릿 패턴은 [REDACTED]. 변경이 없으면 '변경 없음'만 보고하라."},
    "miner": {
        "label": "개념 담당자", "timeout": 900,
        "prompt": "너는 개념 담당자다. 가장 최근의 10-Daily 노트와 15-Reports 보고서에서 기술 개념 후보를 뽑아 "
                  "20-Wiki 전체와 파일명·aliases 양쪽으로 대조하라. 이미 있는 개념은 버리고, 새 개념만 "
                  "위키 규칙(frontmatter, 백링크 2개, MOC 등록)대로 스텁 생성하라. 생성·보강 목록을 보고하라."},
    "auditor": {
        "label": "누락 감시자", "timeout": 900,
        "prompt": "너는 누락 감시자다. 가장 최근 Daily 노트의 변경 요약과 20-Wiki를 대조해 ① 개념으로 매핑되지 않은 "
                  "변경 덩어리 ② 신규 개념의 선수 개념 부재(위키에 없거나 confidence: low)를 찾아라. "
                  "발견한 공백을 .cache/learn-queue.json에 {concept, reason, priority} 배열로 기록(기존 내용에 병합)하고 결과를 보고하라."},
    "tutor": {
        "label": "트레이너", "timeout": 900,
        "prompt": "너는 트레이너다. 문답을 두 계열로 생성해 오늘 10-Daily 노트의 '## 오늘의 문답' 절에 기록하라(없으면 노트 생성). "
                  "(a) 개념형 3개 — .cache/learn-queue.json 큐와 20-Wiki의 confidence:low 노트를 재료로, "
                  "왜/대안/failure mode 중 하나를 겨냥한다. 유형 태그 [왜]/[대안]/[failure mode]. "
                  "(b) 업무형 2개 — 오늘(없으면 최근)의 15-Reports 보고서와 10-Daily 변경 요약을 재료로, "
                  "사람이 AI에게 맡긴 업무를 실제로 이해하고 판단했는지 확인하는 질문. 유형 태그 [업무]. "
                  "예: '오늘 X 프로젝트에서 무엇이 왜 바뀌었는지 본인의 언어로 설명하라', "
                  "'AI가 내린 결정 중 본인이 직접 검증한 것은 무엇이고 근거는?', "
                  "'이 변경이 실무·고객에게 미치는 영향은?'. 개발 지식이 아니라 업무 사실·판단을 묻는다. "
                  "난이도 규칙(엄수): 1문항 = 1질문. 하위 질문을 '또한/그리고/~하며'로 이어붙이지 말고, 물을 것이 여럿이면 문항을 나눈다. "
                  "question은 200자 이내의 한 문장 질문이며 답을 위한 맥락(파일·커밋·상황)은 한 줄로만 붙인다. "
                  "개념형은 2단계 구조로 낸다: ① 4지선다(choices 4개, 오답은 그럴듯한 흔한 오해로), ② 맞힌 뒤 한 줄로 서술하는 꼬리질문(followup). "
                  "업무형은 서술형(choices 없음)이되 '무엇을·왜' 하나만 묻는다. "
                  "모든 문항에 hint(정답을 말하지 않는 생각의 방향 한 줄)와 answer_outline(채점 기준이 되는 모범답안 2~3문장)을 함께 넣는다. "
                  "추가로 모든 문항을 .cache/tutor-questions.json에 "
                  "[{id:'YYYY-MM-DD-N', type, concept(개념형만), project(관련 repo 폴더명 — 예: Cyber-Security, 해당 없으면 '공통'), question, "
                  "choices(개념형만, 문자열 4개), answer(개념형만, 정답 인덱스 0~3), followup(개념형만), hint, answer_outline, status:'pending'}] 배열로 저장하라"
                  "(기존 파일이 있으면 pending 항목은 유지하며 병합). 미응답 기존 문항은 carried_over를 true로, carry_count를 +1 하라. "
                  "carry_count가 3 이상인 문항은 그대로 두지 말고 같은 개념을 위 난이도 규칙(1질문·4지선다·힌트)으로 쉽게 다시 써서 교체하라(id 유지)."},
    "reviewer": {
        "label": "코드 리뷰어", "timeout": 900,
        "prompt": "너는 코드 리뷰어다. 90-Meta/repos.md의 repo 중 최근 7일 변경이 가장 큰 것을 골라 "
                  "보안 체크리스트(SQL injection·XSS·JWT·시크릿·에러 노출)와 품질 관점으로 리뷰하고 "
                  "30-Feedback/code-review/에 저장하라. 지적마다 파일:라인 인용과 심각도, failure mode를 명시하라."},
    "retro": {
        "label": "분석가", "timeout": 900,
        "prompt": "너는 AI 활용 분석가다. 오늘 하루의 작업 기록(10-Daily, 15-Reports, git 이력)에서 "
                  "AI에게 시킨 지시·위임 패턴을 분석하라 — 재작업이 있었던 지시와 없었던 지시를 대조해 원인을 밝히고, "
                  "좋은 지시 패턴 후보는 20-Wiki/patterns 승격 후보로 표시해 30-Feedback/ai-usage/오늘날짜.md에 기록하라."},
    "gardener": {
        "label": "위키 관리자", "timeout": 900,
        "prompt": "너는 위키 관리자다. 20-Wiki에서 ① 백링크 2개 미만 노트 연결 ② 가장 오래된 스텁 1~2개 채움 "
                  "③ 표기만 다른 중복 병합을 수행하고 처리 내역을 보고하라. 확신 없는 병합은 하지 말고 보고만 하라."},
    "secretary": {
        "label": "비서", "timeout": 900,
        "cmd": [sys.executable, str(ROOT / "90-Meta/scripts/daily_brief.py")]},
}
CHAIN = ["scribe", "miner", "auditor", "tutor"]          # 야간 학습 루프 순서
SCHEDULE = [                                              # (시각 창 10분 — 서버가 꺼져 있으면 그 회차는 건너뜀)
    {"key": "brief",      "job": "secretary",  "at": "08:30"},
    {"key": "retro",      "job": "retro",      "at": "18:00"},              # 매일 (사용자 지정)
    {"key": "learn-loop", "job": "learn-loop", "at": "23:30"},
    {"key": "gardener",   "job": "gardener",   "at": "10:00", "dow": 5},    # 토요일
    {"key": "proj-check", "job": "proj-check", "at": "09:50"},              # 매일 — 프로젝트별 일정·이슈·누락 점검
]
# ── 에이전트 설정 오버라이드 (역할 프롬프트·루틴) — UI(⚙ 드로어)에서 편집, 파일로 영속 ──
# 파일: 90-Meta/map-ui/agent-config.json  {"prompts": {agent: text}, "schedules": {key: {at,dow,enabled}}}
# 기본값은 위 AGENT_JOBS / SECRETARY_PERSONA / SCHEDULE 이며, 오버라이드는 기동 시와 저장 시 즉시 반영된다.
AGENT_CONFIG = HERE / "agent-config.json"
DEFAULT_PROMPTS = {k: v.get("prompt", "") for k, v in AGENT_JOBS.items()}
DEFAULT_PROMPTS["secretary"] = SECRETARY_PERSONA
DEFAULT_SCHEDULE = [dict(x) for x in SCHEDULE]
SCHED_LABELS = {"brief": "아침 브리핑", "retro": "AI 활용 분석", "learn-loop": "학습 루프 (기록→개념→누락→문답 공통)", "proj-check": "프로젝트 점검 (일정·이슈·누락)",
                "gardener": "위키 유지보수"}
_cfg_lock = threading.Lock()


def load_agent_config():
    try:                                              # _load_json은 아래에서 정의되므로 직접 읽는다
        cfg = json.loads(AGENT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def save_agent_config(cfg):
    AGENT_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_agent_config():
    """오버라이드를 런타임 구조(AGENT_JOBS·SECRETARY_PERSONA·SCHEDULE)에 반영한다. 스케줄러는 SCHEDULE
    리스트 객체를 그대로 순회하므로 in-place로 교체한다."""
    global SECRETARY_PERSONA
    cfg = load_agent_config()
    for k, base in DEFAULT_PROMPTS.items():
        txt = (cfg.get("prompts") or {}).get(k) or base
        if k == "secretary":
            SECRETARY_PERSONA = txt
        elif k in AGENT_JOBS and "prompt" in AGENT_JOBS[k]:
            AGENT_JOBS[k]["prompt"] = txt
    new = []
    seen = set()
    for base in DEFAULT_SCHEDULE:
        o = (cfg.get("schedules") or {}).get(base["key"]) or {}
        seen.add(base["key"])
        if o.get("enabled") is False:
            continue
        e = {"key": base["key"], "job": base["job"], "at": o.get("at") or base["at"]}
        dow = o["dow"] if "dow" in o else base.get("dow")
        if dow is not None and dow != "":
            e["dow"] = int(dow)
        new.append(e)
    for key, o in (cfg.get("schedules") or {}).items():          # 기본에 없던 에이전트 단독 루틴
        if key in seen or key not in AGENT_JOBS or o.get("enabled") is False or not o.get("at"):
            continue
        e = {"key": key, "job": key, "at": o["at"]}
        if o.get("dow") not in (None, ""):
            e["dow"] = int(o["dow"])
        new.append(e)
    SCHEDULE[:] = new


def agent_config_view(key):
    """드로어용: 역할 프롬프트(현재/기본)와 이 에이전트에 걸린 루틴 목록."""
    cfg = load_agent_config()
    cur = (cfg.get("prompts") or {}).get(key) or DEFAULT_PROMPTS.get(key, "")
    scheds = []
    for base in DEFAULT_SCHEDULE:
        related = base["job"] == key or (base["job"] == "learn-loop" and key in CHAIN)
        if not related:
            continue
        o = (cfg.get("schedules") or {}).get(base["key"]) or {}
        scheds.append({"key": base["key"], "label": SCHED_LABELS.get(base["key"], base["key"]),
                       "at": o.get("at") or base["at"],
                       "dow": (o["dow"] if "dow" in o else base.get("dow")),
                       "enabled": o.get("enabled", True), "shared": base["job"] == "learn-loop"})
    if not scheds and key in AGENT_JOBS:                           # 루틴 없는 에이전트 — 단독 루틴 슬롯
        o = (cfg.get("schedules") or {}).get(key) or {}
        scheds.append({"key": key, "label": AGENT_JOBS[key]["label"] + " 단독 루틴",
                       "at": o.get("at") or "", "dow": o.get("dow"), "enabled": o.get("enabled", bool(o.get("at"))),
                       "shared": False})
    return {"agent": key, "prompt": cur, "default_prompt": DEFAULT_PROMPTS.get(key, ""),
            "customized": bool((cfg.get("prompts") or {}).get(key)), "schedules": scheds}


def update_agent_config(key, body):
    with _cfg_lock:
        cfg = load_agent_config()
        cfg.setdefault("prompts", {})
        cfg.setdefault("schedules", {})
        if "prompt" in body:
            txt = str(body.get("prompt") or "").strip()
            if not txt or txt == DEFAULT_PROMPTS.get(key, ""):
                cfg["prompts"].pop(key, None)                    # 빈 값·기본값과 같으면 기본으로 복원
            else:
                cfg["prompts"][key] = txt[:8000]
        for sc in body.get("schedules") or []:
            k = str(sc.get("key") or "")
            if not re.fullmatch(r"[\w-]+", k):
                continue
            at = str(sc.get("at") or "")
            if at and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                continue
            dow = sc.get("dow")
            dow = None if dow in (None, "", "null") else max(0, min(6, int(dow)))
            cfg["schedules"][k] = {"at": at, "dow": dow, "enabled": bool(sc.get("enabled", True))}
        save_agent_config(cfg)
    apply_agent_config()
    emit(key, "설정 변경 — 역할·루틴 갱신")
    return agent_config_view(key)


apply_agent_config()

# ── 산출물 파일 열람 ──────────────────────────────────
ALLOWED_DIRS = ["10-Daily", "15-Reports", "20-Wiki", "30-Feedback", "50-Weekly", "25-Casting", "60-Sources"]


def safe_path(rel):
    rel = (rel or "").strip().strip("/")
    if not any(rel == a or rel.startswith(a + "/") for a in ALLOWED_DIRS):
        return None
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT) + os.sep):
        return None
    return p


def list_dir(rel):
    base = safe_path(rel)
    if not base or not base.is_dir():
        return None
    dirs, files = [], []
    for e in sorted(base.iterdir()):
        if e.name.startswith("."):
            continue
        if e.is_dir():
            dirs.append(e.name)
        elif e.suffix == ".md":
            files.append({"name": e.name, "path": (rel + "/" + e.name).strip("/"),
                          "mtime": e.stat().st_mtime})
    files.sort(key=lambda f: -f["mtime"])
    for f in files:
        f["mtime"] = time.strftime("%m-%d %H:%M", time.localtime(f["mtime"]))
    return {"dirs": dirs, "files": files[:40]}


# ── 코치 문답 채팅 ────────────────────────────────────
TQ_FILE = CACHE / "tutor-questions.json"
TCHAT_FILE = CACHE / "tutor-chat.json"
try:
    TCHAT = json.loads(TCHAT_FILE.read_text(encoding="utf-8"))[-200:]
except (OSError, ValueError):
    TCHAT = []

TUTOR_PERSONA = """너는 트레이너(채점자)다. 사용자가 문항에 답을 제출했다. 아래 규칙으로 채점하라.

- 답변의 첫 줄은 반드시 다음 중 하나로 시작하라: `판정: O` (정답) / `판정: X` (오답) / `판정: △` (부분 — 재도전)
- 판정 줄 다음에 짧은 코멘트를 잇는다
- 맞으면 한 줄 해설 + 심화 꼬리 질문 1개. 틀리거나 부분이면 정답을 바로 주지 말고 힌트로 재도전을 유도
- 개념형 문항([왜]/[대안]/[failure mode])에서 판정이 명확하면, 관련 20-Wiki 노트 frontmatter의
  confidence를 실제 파일에서 갱신하고 답변 끝에 `confidence: low→medium` 형식으로 명시
- [업무] 문항은 위키 갱신 없이 이해 확인만 — 답이 보고서 내용과 어긋나면 해당 보고서 경로를 짚어라
- 채점이 끝난 문항은 .cache/tutor-questions.json에서 status를 'answered'로 바꿔라
- 말투: 간결한 존댓말. 정답 낭독이 아니라 사고 유도가 목적이다"""


def tutor_questions():
    try:
        return json.loads(TQ_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        notes = sorted((ROOT / "10-Daily").glob("2*.md"), reverse=True)
        for n in notes[:7]:
            txt = n.read_text(encoding="utf-8")
            if "## 오늘의 문답" not in txt:
                continue
            sec = txt.split("## 오늘의 문답", 1)[1]
            qs = []
            for m in re.finditer(r"### (Q\d+)\.\s*(.+?)\n(.*?)(?=\n### |\Z)", sec, re.S):
                body = m.group(3).split("**답변**")[0].strip()
                qs.append({"id": n.stem + "-" + m.group(1), "type": "",
                           "project": "공통",
                           "question": m.group(2).strip() + "\n\n" + body[:600],
                           "status": "pending"})
            if qs:
                return qs
    except OSError:
        pass
    return []


QA_RESULTS = CACHE / "qa-results.json"


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def record_qa(qid, project, verdict):
    """O/X 확정 시 결과 축적 + 문항 상태 갱신 (△는 재도전 대기로 pending 유지)."""
    if not qid or verdict not in ("O", "X"):
        return
    res = _load_json(QA_RESULTS, [])
    res.append({"qid": qid, "project": project or "공통", "verdict": verdict,
                "date": time.strftime("%Y-%m-%d"), "ts": time.strftime("%H:%M")})
    try:
        QA_RESULTS.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    qs = _load_json(TQ_FILE, [])
    for q in qs:
        if q.get("id") == qid:
            q["status"] = "answered"
            q["verdict"] = verdict
    try:
        TQ_FILE.write_text(json.dumps(qs, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def tutor_turn(text, question, qid="", project="", outline=""):
    with _chat_lock:
        TCHAT.append({"role": "user", "ts": time.strftime("%H:%M:%S"), "text": text})
        try:
            TCHAT_FILE.write_text(json.dumps(TCHAT[-200:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    emit("tutor", "답안 제출 — 채점 중")
    hist = "\n".join(("사용자" if m["role"] == "user" else "코치(너)") + ": " + m["text"][:400]
                     for m in TCHAT[-9:-1])
    prompt = (TUTOR_PERSONA
              + ("\n\n[이전 문답 대화]\n" + hist if hist else "")
              + "\n\n[현재 문항]\n" + (question or "(문항 미지정 — 자유 질문으로 간주)")
              + ("\n\n[출제자 해설 — 채점 기준. 사용자에게 그대로 낭독하지 말 것]\n" + outline if outline else "")
              + "\n\n[사용자 답변]\n" + text)
    reply = run_agent(prompt, timeout=600)
    with _chat_lock:
        TCHAT.append({"role": "assistant", "ts": time.strftime("%H:%M:%S"), "text": reply})
        try:
            TCHAT_FILE.write_text(json.dumps(TCHAT[-200:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    m = re.search(r"판정\s*[:：]\s*(O|X|△)", reply[:200])
    verdict = m.group(1) if m else ""
    record_qa(qid, project, verdict)
    emit("tutor", "채점 완료" + (f" — 판정 {verdict}" if verdict else ""))
    return reply, verdict


# ── 트레이너 도식(eli5) — 문항을 그림으로 풀어주는 SVG, 필요할 때만(lazy) 생성 ──────
FIG_DIR = CACHE / "tutor-figures"
# 도식 생성 모델. 기본 Sonnet(비용 1/4). 정답 노출이 잦은 유형은 여기서 상위 모델로 올린다 — 예: "failure mode": ""(CLI 기본값)
FIGURE_MODEL_BY_TYPE = {}
FIGURE_MODEL_DEFAULT = "sonnet"
FIGURE_MODEL_ESCALATE = ""          # 정답 노출 재시도 시 쓰는 모델 ("" = CLI 기본값 = 상위 모델)
FIGURE_PROMPT = """학습 문항을 초보자도 풀 수 있게 돕는 도식 한 장을 SVG로 그려라.

출력 규칙(위반 시 폐기된다):
- 답은 텍스트로만 한다. 파일을 만들거나(Write/Edit 금지) 저장하지 말고, 설명문 없이 <svg …>…</svg> 한 덩어리만 출력한다.
- 폭 720px, viewBox 지정, 한글 라벨, 글자는 최소·그림 위주. 단계별 박스와 화살표로 메커니즘(데이터 흐름·실행 시점·실패 조건)을 그린다.
- 정답을 그리지 않는다. 문항이 직접 묻는 항목(어디로·무엇을·어떤 경로·왜)은 이름을 쓰지 말고 '?' 박스나 빈칸으로 남기고,
  그 옆에 생각을 여는 질문형 단서 한 줄만 단다. 결과·영향을 목록으로 열거하는 것도 정답 노출이다.
- 문항에 등장하는 고유명(코드 식별자·파일명·프로젝트명)은 그대로 써도 된다. 문항 밖의 사실은 지어내지 않는다.
- 파일을 읽지 말고 문항 텍스트만으로 그린다. <script>·외부 이미지·폰트 링크 금지.

문항 유형: [{type}]
문항: {question}
"""
LEAK_PROMPT = """아래는 학습 문항과, 그 문항을 돕기 위해 그린 도식(SVG)의 텍스트다. 도식이 문항이 묻는 답을 직접 써 버렸는지 판정하라.
'답을 썼다'의 기준: 문항이 묻는 대상(어디/무엇/왜/어떤 경로)의 구체 이름·위치·목록이 도식 텍스트에 그대로 등장한다.
배경 맥락(문항에 이미 나온 식별자)이나 '?'로 비운 자리는 노출이 아니다.
첫 줄에 LEAK 또는 OK 한 단어만 쓰고, 둘째 줄에 근거를 20자 이내로 쓴다.

[문항]
{question}

[도식 텍스트]
{svg_text}
"""


def _svg_extract(text):
    i, j = text.find("<svg"), text.rfind("</svg>")
    if i < 0 or j < 0:
        return ""
    svg = text[i:j + 6]
    svg = re.sub(r"<script.*?</script>", "", svg, flags=re.S | re.I)     # 방어 — 화면에 그대로 삽입된다
    svg = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*')", "", svg, flags=re.I)
    return svg


def _svg_text(svg):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", svg)).strip()[:6000]


def figure_leaks(question, svg):
    """도식이 정답을 써 버렸는지 Haiku로 판정. 판정 불가면 False(통과)."""
    out = run_agent(LEAK_PROMPT.format(question=question, svg_text=_svg_text(svg)),
                    timeout=120, style=False, model="haiku")
    return out.strip().upper().startswith("LEAK")


def tutor_figure(q, force=False):
    """문항 q의 도식을 반환 {svg, cached, model, leak_warning}. 캐시(.cache/tutor-figures/<id>.svg) 우선."""
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    qid = re.sub(r"[^\w\-.]", "_", q.get("id") or "q")
    svg_f, meta_f = FIG_DIR / (qid + ".svg"), FIG_DIR / (qid + ".json")
    if not force and svg_f.is_file():
        return {"svg": svg_f.read_text(encoding="utf-8"), "cached": True, **_load_json(meta_f, {})}
    qtype = q.get("type") or "문답"
    model = FIGURE_MODEL_BY_TYPE.get(qtype, FIGURE_MODEL_DEFAULT)
    prompt = FIGURE_PROMPT.format(type=qtype, question=q.get("question", ""))
    attempts, svg, leak = [], "", False
    for step in range(3):                       # ① 생성 → SVG 없으면 같은 모델 재시도 → 정답 노출이면 상위 모델
        emit("tutor", f"도식 생성 중 — {q.get('id')} ({model or '기본 모델'}, {step + 1}차)")
        svg = _svg_extract(run_agent(prompt, timeout=300, style=False, model=model))
        if not svg:
            attempts.append({"model": model, "result": "no-svg"})
            continue
        leak = figure_leaks(q.get("question", ""), svg)
        attempts.append({"model": model, "result": "leak" if leak else "ok"})
        if not leak:
            break
        if model == FIGURE_MODEL_ESCALATE:      # 상위 모델도 노출이면 경고 붙여 그대로 낸다
            break
        model = FIGURE_MODEL_ESCALATE
    if not svg:
        return {"svg": "", "cached": False, "error": "도식 생성 실패 — SVG가 반환되지 않았습니다", "attempts": attempts}
    meta = {"model": model or "default", "leak_warning": leak, "attempts": attempts,
            "created": time.strftime("%Y-%m-%d %H:%M")}
    try:
        svg_f.write_text(svg, encoding="utf-8")
        meta_f.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    emit("tutor", "도식 완료 — " + q.get("id", "") + (" (정답 노출 경고)" if leak else ""))
    return {"svg": svg, "cached": False, **meta}


def _q_by_id(qid):
    qs = _load_json(TQ_FILE, [])
    for q in qs:
        if q.get("id") == qid:
            return qs, q
    return qs, None


def _save_questions(qs):
    try:
        TQ_FILE.write_text(json.dumps(qs, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def tutor_answer(qid, action, choice=None):
    """LLM 없이 처리되는 문항 동작. choice: 4지선다(1차 오답→힌트, 2차 오답→X), skip: 이월, giveup: X + 해설."""
    qs, q = _q_by_id(qid)
    if not q:
        return {"ok": False, "error": "문항 없음"}
    if action == "skip":
        q["carried_over"] = True
        q["carry_count"] = int(q.get("carry_count") or 0) + 1
        _save_questions(qs)
        emit("tutor", "문항 이월 — " + qid)
        return {"ok": True, "status": "skipped"}
    if action == "giveup":
        _save_questions(qs)
        record_qa(qid, q.get("project"), "X")
        emit("tutor", "정답 공개(X) — " + qid)
        return {"ok": True, "status": "X", "verdict": "X", "outline": q.get("answer_outline") or "",
                "reply": "이번엔 해설을 보고 넘어갑니다. 같은 개념이 며칠 뒤 다른 형태로 다시 나옵니다."}
    if action == "choice":
        if not isinstance(q.get("choices"), list) or q.get("answer") is None:
            return {"ok": False, "error": "선택형 문항이 아닙니다"}
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            return {"ok": False, "error": "choice 인덱스 필요"}
        q["attempts"] = int(q.get("attempts") or 0) + 1
        correct = choice == int(q["answer"])
        if correct:
            _save_questions(qs)
            record_qa(qid, q.get("project"), "O")
            emit("tutor", "선택형 정답(O) — " + qid)
            return {"ok": True, "status": "O", "verdict": "O", "outline": q.get("answer_outline") or "",
                    "followup": q.get("followup") or "", "reply": "정답입니다."}
        if q["attempts"] >= 2:
            _save_questions(qs)
            record_qa(qid, q.get("project"), "X")
            emit("tutor", "선택형 오답(X) — " + qid)
            return {"ok": True, "status": "X", "verdict": "X", "outline": q.get("answer_outline") or "",
                    "answer": int(q["answer"]), "reply": "두 번 틀렸습니다. 해설을 읽고 넘어갑니다."}
        _save_questions(qs)
        return {"ok": True, "status": "retry", "verdict": "△", "hint": q.get("hint") or "",
                "reply": "아닙니다. 힌트를 보고 한 번 더 골라 보세요."}
    return {"ok": False, "error": "알 수 없는 action"}


# ── 프로젝트 변경 설명 ────────────────────────────────
EXPLAIN_FILE = CACHE / "explain-cache.json"


def repo_of(agent_key):
    name = agent_key.split(":", 1)[1] if ":" in agent_key else agent_key
    for r in parse_repos():
        if r.name == name:
            return r
    return None


def git_out(repo, args, timeout=15):
    try:
        r = subprocess.run(["git", "-C", str(repo)] + args,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def commits_of(agent_key):
    repo = repo_of(agent_key)
    if not repo:
        return None
    log = git_out(repo, ["log", "--pretty=%h|%ad|%s", "--date=format:%m-%d %H:%M", "-6"])
    dirty = git_out(repo, ["status", "--porcelain"])
    return {"commits": [dict(zip(("sha", "date", "msg"), l.split("|", 2)))
                        for l in log.splitlines() if "|" in l],
            "uncommitted": len([l for l in dirty.splitlines() if l.strip()])}


def last_work_of(repo):
    """프로젝트의 '마지막 작업' — 커밋·보고서·회의록·문서 중 가장 최근 항목.
    반환: {"date","msg","kind","ts"} 또는 None. 회의록/문서는 frontmatter project: 에 repo 폴더명
    또는 표시명(NAMES)이 들어 있으면 해당 프로젝트로 본다."""
    cands = []

    def doc_ts(f, datestr):
        """문서 시각 — 파일명/frontmatter의 YYYY-MM-DD가 mtime과 같은 날이면 mtime(시각 정보 보존),
        다르면 그 날짜 자체(동기화·재저장으로 바뀐 mtime을 작업 시각으로 오판하지 않기 위해)."""
        try:
            mt = int(f.stat().st_mtime)
        except OSError:
            mt = 0
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", datestr or "")
        if not m:
            return mt
        try:
            day = time.mktime(time.strptime(m.group(0), "%Y-%m-%d"))
        except (ValueError, OverflowError):
            return mt
        return mt if time.strftime("%Y-%m-%d", time.localtime(mt)) == m.group(0) else int(day)
    ct = git_out(repo, ["log", "-1", "--pretty=%ct|%s"])
    if "|" in ct:
        t, msg = ct.split("|", 1)
        try:
            cands.append((int(t), "커밋", msg))
        except ValueError:
            pass
    rep_dir = ROOT / "15-Reports" / repo.name
    if rep_dir.is_dir():
        for f in rep_dir.glob("*.md"):
            cands.append((doc_ts(f, f.stem), "보고서", f.stem))
    names = {repo.name.lower(), NAMES.get(repo.name, repo.name).lower().replace(" ", "")}
    for sub, kind in (("meetings", "회의록"), ("docs", "문서")):
        d = ROOT / "60-Sources" / sub
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:3000]
            except OSError:
                continue
            m = re.search(r"^project:\s*(.+)$", head, re.M)
            if not m:
                continue
            pv = m.group(1).lower().replace(" ", "")
            if not any(n in pv for n in names):
                continue
            title = re.search(r"^meeting:\s*(.+)$", head, re.M) or re.search(r"^title:\s*(.+)$", head, re.M)
            dm = re.search(r"^date:\s*(.+)$", head, re.M)
            cands.append((doc_ts(f, dm.group(1) if dm else f.stem), kind,
                          (title.group(1) if title else f.stem).strip()))
    if not cands:
        return None
    t, kind, msg = max(cands)
    return {"ts": t, "kind": kind, "msg": msg,
            "date": time.strftime("%m-%d %H:%M", time.localtime(t))}


def explain_repo(agent_key):
    repo = repo_of(agent_key)
    if not repo:
        return "(repo를 찾을 수 없습니다)"
    head = git_out(repo, ["rev-parse", "--short", "HEAD"]) or "none"
    try:
        cache = json.loads(EXPLAIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(agent_key)
    if hit and hit.get("sha") == head:
        return hit["text"]
    emit(agent_key, "[실행] 변경 내역 쉬운 설명 생성 중")
    diff = git_out(repo, ["log", "-1", "--stat", "--patch"], timeout=20)[:8000]
    prompt = ("다음은 '" + repo.name + "' 저장소의 가장 최근 커밋 내용이다.\n\n" + diff + "\n\n"
              "비개발자도 이해할 수 있게 설명하라 — ① 무엇이 바뀌었나 ② 왜 바꿨을 것 같나 "
              "③ 사용자·업무에 뭐가 달라지나. 각 1~2문장, 전문용어는 풀어서. 파일명은 `백틱`으로.")
    text = run_agent(prompt, timeout=300)
    cache[agent_key] = {"sha": head, "text": text, "ts": time.strftime("%m-%d %H:%M")}
    try:
        EXPLAIN_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    emit(agent_key, "[완료] 변경 설명 생성")
    return text


# ── 프로젝트 점검(호출) — 일정·이슈·누락을 프로젝트 에이전트가 먼저 알린다 ──
CHECK_AT = "09:50"
CHECK_LABEL = "프로젝트 점검 (일정·이슈·누락)"


def _calendar_text():
    try:
        p = subprocess.run([sys.executable, str(ROOT / "90-Meta/scripts/calendar_sync.py"), "--back", "7", "--ahead", "3"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        return (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _project_meetings(repo, limit=4):
    """이 프로젝트의 최근 회의록 경로 — frontmatter project: 에 repo명/표시명이 들어간 것."""
    names = {repo.name.lower(), NAMES.get(repo.name, repo.name).lower().replace(" ", "")}
    out = []
    d = ROOT / "60-Sources" / "meetings"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md"), reverse=True):
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:3000]
        except OSError:
            continue
        m = re.search(r"^project:\s*(.+)$", head, re.M)
        if m and any(n in m.group(1).lower().replace(" ", "") for n in names):
            out.append(str(f.relative_to(ROOT)))
        if len(out) >= limit:
            break
    return out


def project_check_task(key, calendar=None):
    repo = repo_of(key)
    today = time.strftime("%Y-%m-%d")
    c = commits_of(key) or {}
    last = last_work_of(repo)
    rep_dir = ROOT / "15-Reports" / repo.name
    reports = sorted(f.stem for f in rep_dir.glob("*.md")) if rep_dir.is_dir() else []
    facts = [f"- 오늘: {today}",
             f"- 마지막 작업: {last['date']} {last['kind']} — {last['msg']}" if last else "- 마지막 작업: 기록 없음",
             f"- 미커밋 변경: {c.get('uncommitted', 0)}건",
             "- 최근 커밋: " + ("; ".join(x['date'] + ' ' + x['msg'][:60] for x in c.get('commits', [])[:4]) or "없음"),
             f"- 최근 보고서(15-Reports/{repo.name}): " + (", ".join(reports[-3:]) or "없음"),
             "- 이 프로젝트 최근 회의록: " + (", ".join(_project_meetings(repo)) or "없음")]
    cal = _calendar_text() if calendar is None else calendar
    return ("[호출 — 프로젝트 점검] 사용자가 묻기 전에 먼저 알려야 할 것을 보고한다. 아래 순서로, 해당 항목이 없으면 그 절은 "
            "'없음' 한 줄로 끝낸다. 각 항목은 근거(파일 경로 또는 커밋)를 괄호로 붙인다.\n"
            "1) 일정 — 오늘·3일 이내 이 프로젝트 관련 일정(캘린더), 회의록의 액션 아이템 중 기한이 지났거나 임박한 것 "
            "(회의록 파일의 액션 아이템·due를 읽고, 가능하면 `bash 90-Meta/scripts/graph.sh query`로 "
            "이 프로젝트 ActionItem의 due/status도 확인)\n"
            "2) 이슈 — 미커밋 변경 방치, 오래 멈춘 작업, 최근 커밋에서 보이는 위험(보안·미완성 TODO 등)\n"
            "3) 빼먹었을 수 있는 것 — 회의에서 내가(사용자) 맡기로 한 일 중 이후 커밋·보고서·노트에 흔적이 없는 것, "
            "회의록/보고서 누락\n"
            "전부 특이사항이 없으면 '오늘은 특이사항 없음' 한 줄과 마지막 작업만 말한다. 전체 15줄 이내.\n\n"
            "[서버가 미리 수집한 사실]\n" + "\n".join(facts)
            + ("\n\n[캘린더 조회 결과 — 이 프로젝트와 관련된 항목만 고른다]\n" + cal if cal else "\n\n[캘린더: 조회 결과 없음]"))


def project_check_start(key):
    """UI 호출 버튼 — 대화 job으로 실행해 진행 화면·답변이 채팅에 남는다."""
    return agent_chat_start(key, "호출 — 일정·이슈·빼먹은 것 점검", task=project_check_task(key))


def project_check_all():
    """09:50 루틴 — 등록된 모든 프로젝트를 순서대로 점검하고 결과를 각 에이전트 대화에 남긴다."""
    cal = _calendar_text()
    emit("system", "프로젝트 점검 시작 — 일정·이슈·누락")
    for r in parse_repos():
        key = "proj:" + r.name
        try:
            job = agent_chat_start(key, "호출(정기 09:50) — 일정·이슈·빼먹은 것 점검", task=project_check_task(key, calendar=cal))
            while not CHAT_JOBS[job]["done"]:          # 순차 실행 — claude 슬롯 독점 방지
                time.sleep(3)
        except Exception as e:                          # noqa: BLE001 — 한 프로젝트 실패가 나머지를 막지 않게
            emit(key, "점검 실패 — " + str(e)[:80])
    emit("system", "프로젝트 점검 완료")


# ── 오늘 할 일 / 완료 — 홈 화면(에이전트 미선택) 집계 ─────────────────
_today_mem = {"ts": 0, "data": None}


def _owner_name():
    try:
        for line in (ROOT / "90-Meta" / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("OWNER_NAME="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _graph_rows(cypher, timeout=15):
    """graph.sh query 출력(plain CSV, 첫 줄 헤더)을 행 리스트로 — 컨테이너가 없으면 []."""
    try:
        r = subprocess.run(["bash", str(ROOT / "90-Meta/scripts/graph.sh"), "query", cypher],
                           cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    import csv
    import io
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return []
    rows = list(csv.reader(io.StringIO("\n".join(lines)), skipinitialspace=True))
    return [[None if c == "NULL" else c for c in row] for row in rows[1:]]


def _review_api(path, timeout=5):
    import urllib.request
    try:
        with urllib.request.urlopen(REVIEW_API + "/api/" + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:                                   # noqa: BLE001 — review-ui 미기동 시 빈 값
        return None


sys.path.insert(0, str(ROOT / "90-Meta" / "scripts"))
try:
    import action_items as AI                       # 그래프 ActionItem('할 일') 읽기·완료 처리
except Exception:                                   # noqa: BLE001
    AI = None


def _pnorm(s):
    return re.sub(r"[\s\-_]+", "", (s or "")).lower()


def project_repo_key(project):
    """ActionItem.project(표시명) → 등록 repo 에이전트 키. 없으면 ''."""
    n = _pnorm(project)
    if not n:
        return ""
    for r in parse_repos():
        if n in (_pnorm(r.name), _pnorm(NAMES.get(r.name, ""))):
            return "proj:" + r.name
    return ""


def repo_project_names(repo_name):
    return {_pnorm(repo_name), _pnorm(NAMES.get(repo_name, repo_name))}


def action_items(project=None, only_open=False):
    if not AI:
        return []
    try:
        items = AI.fetch(None, only_open)
    except Exception:                               # noqa: BLE001 — Neo4j 미기동
        return []
    if project:
        names = repo_project_names(project.split(":", 1)[1]) if project.startswith("proj:") else {_pnorm(project)}
        items = [x for x in items if _pnorm(x.get("project")) in names]
    return items


def action_view(x, today):
    due = x.get("due") or ""
    who = ", ".join(x.get("who") or []) or "담당 미정"
    return {"kind": "할 일", "id": x["id"], "title": x.get("desc") or x["id"], "project": x.get("project") or "",
            "sub": who + " · " + ("기한 " + due if due else "기한 없음") + (" · " + x["meeting"] if x.get("meeting") else ""),
            "source": x.get("source") or "", "meeting": x.get("meeting") or "", "date": x.get("date") or "",
            "agent": project_repo_key(x.get("project")) or "confirm-bot", "due": due,
            "urgent": bool(due and due <= today), "status": x.get("status"), "done_at": x.get("done_at") or ""}


ORDER_FILE = CACHE / "action_order.json"       # {프로젝트명: [ActionItem id, ...]} — 홈 카드 드래그 정렬 결과


def action_order():
    try:
        d = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_action_order(project, ids):
    d = action_order()
    d[project] = ids
    ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def today_items():
    """{"todo":[...], "done":[...], "ts"} — 항목: {kind, title, sub, agent, due, urgent}.
    출처: 캘린더·그래프 ActionItem·검토/Inbox 대기·미커밋·보고서 누락·문답·루틴·오늘 생성 문서·오늘 커밋."""
    now = time.time()
    if _today_mem["data"] and now - _today_mem["ts"] < 60:
        return _today_mem["data"]
    today = time.strftime("%Y-%m-%d")
    todo, done = [], []
    owner = _owner_name()

    # 1) 캘린더 — 오늘 일정
    try:
        r = subprocess.run([sys.executable, str(ROOT / "90-Meta/scripts/calendar_sync.py"), "--back", "0", "--ahead", "1", "--json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        cal = json.loads(r.stdout or "{}")
        for e in cal.get("upcoming", []) + cal.get("markers", []) + cal.get("missing", []):
            if e.get("date") != today:
                continue
            t = "종일" if e.get("all_day") else e.get("time", "")
            (done if e.get("time", "99:99") < time.strftime("%H:%M") and not e.get("all_day") else todo).append(
                {"kind": "일정", "title": e.get("summary", ""), "sub": t + (" · " + e["project"] if e.get("project") else ""),
                 "agent": "meeting-recorder", "due": today})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    # 2) 그래프 — 할 일(ActionItem). 열린 것: 내 담당 + 담당 미정(등록 프로젝트) / 오늘 완료: 완료 칸
    for x in action_items():
        if not (owner and owner in (x.get("who") or [])):   # 내 담당분만 — 타인 담당·담당 미정은 프로젝트 드로어에서 본다
            continue
        v = action_view(x, today)
        if x.get("status") == "done":
            if x.get("done_at") == today:
                done.append(v)
        else:
            todo.append(v)

    # 3) 관계 검토 대기 · Inbox 대기
    for r in _review_api("reviews") or []:
        if r.get("status") == "pending":
            todo.append({"kind": "검토", "title": "관계 검토 — " + (r.get("doc") or r.get("file", "")),
                         "sub": "관계 %s건 대기" % r.get("count", 0), "agent": "confirm-bot"})
    for it in _review_api("inbox") or []:
        if it.get("gone") or it.get("job") in ("running", "done"):
            continue
        todo.append({"kind": "Inbox", "title": "ingest — " + it.get("file", ""), "sub": "00-Inbox 대기", "agent": "confirm-bot"})

    # 4) 프로젝트 — 미커밋 · 오늘 커밋 · 보고서
    qs = tutor_questions()
    for r in parse_repos():
        if r.resolve() == ROOT:
            continue
        key = "proj:" + r.name
        name = NAMES.get(r.name, r.name)
        c = commits_of(key) or {"commits": [], "uncommitted": 0}
        if c["uncommitted"]:
            todo.append({"kind": "커밋", "title": name + " — 작업 중인 변경 %d건 저장" % c["uncommitted"], "sub": r.name, "agent": key})
        n_today = len(git_out(r, ["log", "--since=midnight", "--pretty=%h"]).splitlines())
        if n_today:
            done.append({"kind": "커밋", "title": name + " — 오늘 커밋 %d건" % n_today, "sub": c["commits"][0]["msg"][:60] if c["commits"] else r.name, "agent": key})
            if not (ROOT / "15-Reports" / r.name / (today + ".md")).is_file():
                todo.append({"kind": "보고서", "title": name + " — 오늘 변경 보고서 작성", "sub": "15-Reports/%s/%s.md 없음" % (r.name, today), "agent": "scribe"})
        pend = sum(1 for q in qs if (q.get("project") or "") == r.name and q.get("status") == "pending")
        if pend:
            todo.append({"kind": "문답", "title": name + " — 오늘 문답 %d건" % pend, "sub": "트레이너", "agent": "tutor"})
    pend_common = sum(1 for q in qs if (q.get("project") or "공통") == "공통" and q.get("status") == "pending")
    if pend_common:
        todo.append({"kind": "문답", "title": "공통 개념 문답 %d건" % pend_common, "sub": "트레이너", "agent": "tutor"})
    qa_done = [x for x in _load_json(QA_RESULTS, []) if x.get("date") == today]
    if qa_done:
        done.append({"kind": "문답", "title": "문답 %d건 답변" % len(qa_done), "sub": "O %d · X %d" % (
            sum(1 for x in qa_done if x.get("verdict") == "O"), sum(1 for x in qa_done if x.get("verdict") == "X")), "agent": "tutor"})

    # 5) 루틴 — 오늘 예정/완료
    sched_done = _load_json(SCHED_STATE, {})
    wd = time.localtime().tm_wday
    for sc in SCHEDULE:
        if "dow" in sc and sc["dow"] != wd:
            continue
        label = SCHED_LABELS.get(sc["key"], AGENT_JOBS.get(sc["job"], {}).get("label", sc["key"]))
        agent = sc["job"] if sc["job"] in AGENT_JOBS else ("secretary" if sc["job"] != "proj-check" else "")
        item = {"kind": "루틴", "title": label, "sub": "매일 " + sc["at"], "agent": agent, "due": sc["at"]}
        (done if sched_done.get(sc["key"]) == today else todo).append(item)

    # 6) 오늘 생성된 문서
    docs = [("10-Daily", "학습 노트", "scribe"), ("15-Reports", "보고서", "scribe"), ("30-Feedback", "피드백", "retro"),
            ("60-Sources/meetings", "회의록", "meeting-recorder"), ("20-Wiki", "위키", "gardener")]
    for sub, label, agent in docs:
        d = ROOT / sub
        if not d.is_dir():
            continue
        n = 0
        last = ""
        for f in d.rglob("*.md"):
            try:
                if time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime)) == today:
                    n += 1
                    last = f.stem
            except OSError:
                pass
        if n:
            done.append({"kind": "문서", "title": label + " %d건 작성·갱신" % n, "sub": last[:50], "agent": agent})

    saved = action_order()

    def order(x):
        pj = x.get("project") or "~"
        ids = saved.get(pj, [])
        rank = ids.index(x["id"]) if x.get("id") in ids else len(ids)      # 사용자가 드래그로 고정한 순서 우선
        return (pj, rank, 0 if x.get("urgent") else 1, x.get("due") or "9", x["kind"])
    todo.sort(key=order)
    data = {"todo": todo, "done": done, "ts": time.strftime("%H:%M"), "date": today, "owner": owner}
    _today_mem.update(ts=now, data=data)
    return data


# ── 완료 캘린더 — 날짜별 완료 집계 ────────────────────
# 할 일 완료는 그래프 done_at(권위 출처), 나머지는 git·파일 mtime·캐시에서 그때그때 집계한다.
# 월 단위로 캐시한다 — 이번 달은 60초, 지난달은 1시간 (커밋 amend 등 뒤늦은 변경 반영).
CAL_DIR = CACHE / "calendar"
DOC_DIRS = [("10-Daily", "학습 노트"), ("15-Reports", "보고서"), ("30-Feedback", "피드백"),
            ("60-Sources/meetings", "회의록"), ("20-Wiki", "위키")]


def _month_range(ym):
    y, m = (int(x) for x in ym.split("-"))
    start = "%04d-%02d-01" % (y, m)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return start, "%04d-%02d-01" % (ny, nm)


def _cal_commits(start, end):
    """날짜 → {repo표시명: {n, msgs, reports}}. 커밋 목록과 그날의 변경 보고서 경로를 함께 담는다."""
    out = {}
    for r in parse_repos():                     # vault(my_jarvis)도 포함한다 — 여기서 한 작업도 그날의 일이다
        name = NAMES.get(r.name, r.name)
        log = git_out(r, ["log", "--since=" + start, "--until=" + end,
                          "--pretty=%ad\x1f%h\x1f%s", "--date=format:%Y-%m-%d"], timeout=25)
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3 or not (start <= parts[0] < end):
                continue
            d, sha, msg = parts[0], parts[1], parts[2]
            e = out.setdefault(d, {}).setdefault(name, {"n": 0, "msgs": [], "reports": []})
            e["n"] += 1
            if len(e["msgs"]) < 25:
                e["msgs"].append({"sha": sha, "msg": msg[:120]})
        for d, byrepo in out.items():            # 그날의 변경 보고서 (있을 때만)
            e = byrepo.get(name)
            if not e or e["reports"]:
                continue
            rep = ROOT / "15-Reports" / r.name / (d + ".md")
            if rep.is_file():
                e["reports"].append(str(rep.relative_to(ROOT)))
    return out


_DOC_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_DOC_DATE8 = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")


def _doc_date(f):
    """문서가 다루는 날짜 — 파일명의 YYYY-MM-DD 또는 YYYYMMDD, 없으면 frontmatter date:.
    created:는 쓰지 않는다 — 생성일은 갱신되지 않아 문서를 영영 과거에 묶어 버린다.
    이 값은 버킷을 바꾸지 않고 표시용 꼬리표로만 쓴다 (버킷 기준은 mtime = 작업한 날)."""
    for rx in (_DOC_DATE, _DOC_DATE8):
        m = rx.search(f.stem)
        if m:
            y, mo, d = m.groups()
            if "01" <= mo <= "12" and "01" <= d <= "31":
                return "%s-%s-%s" % (y, mo, d)
    try:
        head = f.read_text(encoding="utf-8", errors="ignore")[:1200]
    except OSError:
        return None
    m = re.search(r"^date:\s*(.+)$", head, re.M)
    if m:
        d = _DOC_DATE.search(m.group(1))
        if d:
            return d.group(0)
    return None


def _cal_docs(start, end):
    """날짜 → {종류: [{path, name}]}. mtime 기준이라 일괄 재저장(LiveSync 등)에 부풀 수 있다 — suspect로 표시한다."""
    out = {}
    for sub, label in DOC_DIRS:
        d = ROOT / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*.md"):
            try:
                st = f.stat()
            except OSError:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
            if start <= day < end:
                sub_parent = str(f.relative_to(ROOT / sub).parent)     # 15-Reports/<repo>/, meetings/transcripts/
                disp = f.stem if sub_parent == "." else (NAMES.get(sub_parent, sub_parent) + " · " + f.stem)
                e = {"path": str(f.relative_to(ROOT)), "name": disp,
                     "at": time.strftime("%H:%M", time.localtime(st.st_mtime))}
                fordate = _doc_date(f)
                if fordate and fordate != day:      # 다룬 날짜가 손댄 날과 다를 때만 꼬리표를 단다
                    e["for"] = fordate
                out.setdefault(day, {}).setdefault(label, []).append(e)
    for day in out.values():                     # 최근 수정순 (절삭하지 않는다 — 건수와 목록이 어긋나면 안 된다)
        for label in day:
            day[label].sort(key=lambda x: x["at"], reverse=True)
    return out


def calendar_month(ym, force=False):
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    cache = CAL_DIR / (ym + ".json")
    cur = ym == time.strftime("%Y-%m")
    ttl = 60 if cur else 3600
    if not force and cache.is_file():
        try:
            hit = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - hit.get("ts", 0) < ttl:
                return hit
        except (OSError, ValueError):
            pass
    start, end = _month_range(ym)
    days = {}

    def day(d):
        return days.setdefault(d, {"actions": [], "commits": {}, "docs": {}, "qa": 0, "routines": []})

    today = time.strftime("%Y-%m-%d")
    for x in action_items():                                   # 완료된 할 일 — 체크·회의 소급 모두
        d = x.get("done_at")
        if x.get("status") != "done" or not d or not (start <= d < end):
            continue
        v = action_view(x, today)
        v["src_kind"] = x.get("done_src") or "check"
        day(d)["actions"].append(v)
    for d, v in _cal_commits(start, end).items():
        day(d)["commits"] = v
    for d, v in _cal_docs(start, end).items():
        day(d)["docs"] = v
    for x in _load_json(QA_RESULTS, []):                        # 문답
        d = x.get("date")
        if d and start <= d < end:
            day(d)["qa"] = day(d)["qa"] + 1
    try:                                                        # 루틴 실행 이력
        for line in ROUTINE_LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            d = r.get("date")
            if d and start <= d < end:
                lb = r.get("label") or r.get("job") or ""
                if lb and lb not in day(d)["routines"]:
                    day(d)["routines"].append(lb)
    except OSError:
        pass
    # mtime 왜곡 의심일 — 문서 건수가 그 달 중앙값의 6배를 넘고 20건 이상
    def _ndocs(v):
        return sum(len(x) for x in v["docs"].values())
    counts = sorted(_ndocs(v) for v in days.values() if v["docs"])
    med = counts[len(counts) // 2] if counts else 0
    for d, v in days.items():
        n = _ndocs(v)
        if n >= 20 and med and n > med * 6:
            v["suspect"] = "문서 %d건 — 일괄 재저장으로 보인다 (mtime 기준)" % n
    out = {"month": ym, "days": days, "ts": time.time(), "generated": time.strftime("%H:%M"),
           "total": {"actions": sum(len(v["actions"]) for v in days.values()),
                     "commits": sum(sum(e["n"] for e in v["commits"].values()) for v in days.values()),
                     "docs": sum(_ndocs(v) for v in days.values())}}
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return out


RUNS = {}             # agent → {"running":bool, "last":"HH:MM:SS", "note":str}
_runs_lock = threading.Lock()


def _run_one(name):
    job = AGENT_JOBS[name]
    with _runs_lock:
        if RUNS.get(name, {}).get("running"):
            return False
        RUNS[name] = {"running": True, "last": time.strftime("%H:%M:%S"), "note": "실행 중"}
    emit(name, f"[실행] {job['label']} 작업 시작")
    try:
        if "cmd" in job:
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
            r = subprocess.run(job["cmd"], cwd=ROOT, capture_output=True, text=True,
                               timeout=job["timeout"], env=env)
            out = (r.stdout or r.stderr or "").strip()
        else:
            out = run_agent(job["prompt"] + BATON_RULE, timeout=job["timeout"], style=False,
                            model=job.get("model"))
    except subprocess.TimeoutExpired:
        out = "(시간 초과)"
    first = out.splitlines()[0][:90] if out else "(출력 없음)"
    with _runs_lock:
        RUNS[name] = {"running": False, "last": time.strftime("%H:%M:%S"), "note": first}
    emit(name, f"[완료] {first}")
    log_routine(name, name, job["label"], first)
    return True


FULL = ["scribe", "reviewer", "miner", "auditor", "retro", "tutor"]   # 일괄 실행 순서
BATON = CACHE / "baton.md"
BATON_RULE = ("\n\n[배턴 프로토콜] 작업 시작 전 .cache/baton.md가 있으면 읽고, 다른 에이전트가 "
              "너에게 남긴 확인 요청을 이번 작업에 반영하라. 작업을 마치면 같은 파일에 "
              "'## <너의 역할명> → 다음에게' 절을 추가해 ① 다음 에이전트가 확인해야 할 것 "
              "② 의심점·미해결 ③ 후속 작업 제안을 3줄 이내로 남겨라. 파일 전체를 지우지 말고 "
              "자신의 기존 절만 갱신하라.")


def run_job(name):
    if name in ("learn-loop", "full-loop"):
        seq = CHAIN if name == "learn-loop" else FULL
        title = "야간 학습 루프" if name == "learn-loop" else "전체 일괄 실행"
        with _runs_lock:
            if RUNS.get(name, {}).get("running"):
                return
            RUNS[name] = {"running": True, "last": time.strftime("%H:%M:%S"), "note": "체인 실행 중"}
        try:
            BATON.write_text("# 배턴 — " + time.strftime("%Y-%m-%d %H:%M") + " " + title
                             + " 사이클\n(각 에이전트가 다음 에이전트에게 남기는 인수인계)\n",
                             encoding="utf-8")
        except OSError:
            pass
        emit("system", title + " 시작 — " + "→".join(AGENT_JOBS[a]["label"] for a in seq))
        for a in seq:
            _run_one(a)
        with _runs_lock:
            RUNS[name] = {"running": False, "last": time.strftime("%H:%M:%S"), "note": "체인 완료"}
        emit("system", title + " 종료 — 코치 문답이 갱신되었습니다")
    elif name == "proj-check":
        with _runs_lock:
            if RUNS.get(name, {}).get("running"):
                return
            RUNS[name] = {"running": True, "last": time.strftime("%H:%M:%S"), "note": "점검 중"}
        project_check_all()
        with _runs_lock:
            RUNS[name] = {"running": False, "last": time.strftime("%H:%M:%S"), "note": "점검 완료"}
    elif name in AGENT_JOBS:
        _run_one(name)


SCHED_STATE = CACHE / "agent-schedule.json"
ROUTINE_LOG = CACHE / "routine-log.jsonl"      # 루틴 실행 이력 (완료 캘린더용) — agent-schedule.json은 마지막 1회만 남긴다


def log_routine(key, job, label, note=""):
    """루틴 1회 실행을 한 줄 append. 지우지 않으므로 날짜별 조회가 가능하다."""
    try:
        CACHE.mkdir(exist_ok=True)
        with ROUTINE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": time.strftime("%Y-%m-%d"), "ts": time.strftime("%H:%M:%S"),
                                "key": key, "job": job, "label": label, "note": note[:120]},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def scheduler():
    try:
        done = json.loads(SCHED_STATE.read_text())
    except (OSError, ValueError):
        done = {}
    while True:
        time.sleep(20)
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        cur = now.tm_hour * 60 + now.tm_min
        for s in SCHEDULE:
            if "dow" in s and now.tm_wday != s["dow"]:
                continue
            hh, mm = map(int, s["at"].split(":"))
            at = hh * 60 + mm
            if at <= cur < at + 10 and done.get(s["key"]) != today:
                done[s["key"]] = today
                try:
                    SCHED_STATE.write_text(json.dumps(done))
                except OSError:
                    pass
                log_routine(s["key"], s["job"], SCHED_LABELS.get(s["key"], s["job"]))
                threading.Thread(target=run_job, args=(s["job"],), daemon=True).start()


# ── 토큰 사용량 (5시간 창) ────────────────────────────
OMC_USAGE_CACHE = Path.home() / ".claude/plugins/oh-my-claudecode/.usage-cache.json"
_usage_mem = {"ts": 0, "data": None}


def fetch_usage_direct():
    """키체인의 OAuth 토큰으로 공식 사용량 API 직접 조회 (폴백)."""
    import urllib.request
    cred = subprocess.run(["security", "find-generic-password",
                           "-s", "Claude Code-credentials", "-w"],
                          capture_output=True, text=True, timeout=5)
    tok = json.loads(cred.stdout).get("claudeAiOauth", {}).get("accessToken", "")
    if not tok:
        return None
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers={"Authorization": "Bearer " + tok,
                                          "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode())
    fh = d.get("five_hour") or {}
    wk = d.get("seven_day") or {}
    return {"fiveHourPercent": round(fh.get("utilization") or 0),
            "weeklyPercent": round(wk.get("utilization") or 0),
            "fiveHourResetsAt": fh.get("resets_at") or ""}


USAGE_LAST = CACHE / "usage-last.json"      # 마지막 성공값 보존 — 일시 오류에도 표시 유지


def get_usage():
    now = time.time()
    if _usage_mem["data"] and now - _usage_mem["ts"] < 55:
        return _usage_mem["data"]
    data, updated = None, None
    try:  # 1차: OMC HUD 캐시 — 성공 항목만 채택 (error:true인 실패 캐시는 무시)
        c = json.loads(OMC_USAGE_CACHE.read_text(encoding="utf-8"))
        if not c.get("error") and c.get("data") and c["data"].get("fiveHourPercent") is not None:
            data = c["data"]
            updated = time.strftime("%H:%M", time.localtime(c["timestamp"] / 1000))
    except (OSError, ValueError, KeyError):
        pass
    if data is None:
        try:  # 2차: 공식 API 직접 조회
            data = fetch_usage_direct()
            if data:
                updated = time.strftime("%H:%M")
        except Exception:                                   # noqa: BLE001
            data = None
    if data is not None:                                    # 성공 → 보존
        try:
            USAGE_LAST.write_text(json.dumps({"data": data, "updated": updated},
                                             ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    else:                                                   # 3차: 마지막 성공값
        try:
            last = json.loads(USAGE_LAST.read_text(encoding="utf-8"))
            data, updated = last["data"], last["updated"]
        except (OSError, ValueError, KeyError):
            pass
    out = {"pct": (data or {}).get("fiveHourPercent"),
           "weekly": (data or {}).get("weeklyPercent"),
           "updated": updated,
           "resets": str((data or {}).get("fiveHourResetsAt", ""))}
    _usage_mem.update(ts=now, data=out)
    return out


# ── AI 직원 캐스팅 (casting 레포 연동) ─────────────────
CASTING_DIR = Path(os.environ.get("CASTING_DIR", str(Path.home() / "Documents/casting")))
ROSTER_FILE = CACHE / "casting-roster.json"
CAST_OUT = ROOT / "25-Casting"          # 직원 산출물 대기함 — 사람 컨펌 후 00-Inbox로
_cast_cache = {"catalog": None, "prompts": None}


def casting_catalog():
    if _cast_cache["catalog"] is not None:
        return _cast_cache["catalog"]
    depts, cur = [], None
    try:
        txt = (CASTING_DIR / "references/catalog.md").read_text(encoding="utf-8")
    except OSError:
        return []
    for line in txt.splitlines():
        m = re.match(r"^##\s*\d+\.\s*(.+)", line)
        if m:
            cur = {"name": m.group(1).strip(), "employees": []}
            depts.append(cur)
            continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if cur and len(c) >= 4 and c[0].isdigit():
            cur["employees"].append({"id": int(c[0]), "title": c[1],
                                     "en": c[2], "desc": c[3]})
    _cast_cache["catalog"] = depts
    return depts


def casting_prompt(eid):
    if _cast_cache["prompts"] is None:
        try:
            txt = (CASTING_DIR / "references/agent-prompts.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        secs = {}
        for m in re.finditer(r"^## \[(\w+)\][^\n]*\n(.*?)(?=\n---|\n## \[|\Z)", txt, re.S | re.M):
            secs[m.group(1)] = m.group(2).strip()
        _cast_cache["prompts"] = secs
    return _cast_cache["prompts"].get(str(eid), "")


def emp_of(eid):
    for d in casting_catalog():
        for e in d["employees"]:
            if e["id"] == eid:
                return e
    return None


def roster():
    return _load_json(ROSTER_FILE, [])


def save_roster(r):
    try:
        ROSTER_FILE.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


CPROJ_FILE = CACHE / "casting-projects.json"


def cprojects():
    return _load_json(CPROJ_FILE, [])


def save_cprojects(x):
    try:
        CPROJ_FILE.write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def cproj_of(pid):
    for pr in cprojects():
        if pr["id"] == pid:
            return pr
    return None


def cproj_dir(pr):
    d = CAST_OUT / pr["name"]
    d.mkdir(parents=True, exist_ok=True)
    return d


CAST_STYLE = ("\n\n[산출물 규칙] 답변이 문서 산출물(보고서·제안서·기획안·카피 등)로 완성되는 경우, "
              "vault의 25-Casting/ 폴더에 '직책-주제-YYYYMMDD.md' 파일로 저장하라. 파일 맨 위에 "
              "frontmatter(status: draft, author: <직책>, created: 날짜)를 넣고, 저장했다면 답변 끝에 "
              "`저장: 25-Casting/<파일명>`을 명시하라. 이 폴더는 사람이 컨펌해야 다음 단계로 넘어가는 대기함이다. "
              "간단한 문답이면 파일을 만들지 마라."
              "\n\n(웹 채팅에 마크다운으로 렌더링된다. 파일 경로·용어는 `백틱`으로.)")


def cast_chat_file(eid):
    return CACHE / f"casting-chat-{eid}.json"


def cast_turn(eid, text, pid=""):
    emp = emp_of(eid)
    persona = casting_prompt(eid)
    if not emp or not persona:
        return "(직원 정보를 찾을 수 없습니다)", ""
    pr = cproj_of(pid) if pid else None
    # 이력: 프로젝트가 있으면 팀 공유 이력, 없으면(TF) 직원별 이력
    hfile = (CACHE / f"casting-pchat-{pid}.json") if pr else cast_chat_file(eid)
    hist = _load_json(hfile, [])
    hist.append({"role": "user", "who": "나", "ts": time.strftime("%H:%M:%S"), "text": text})
    agent_key = ("biz:" + pid) if pr else ("cast:" + str(eid))
    emit(agent_key, f"업무 접수({emp['title']}) — {text[:36]}")
    hblock = "\n".join((m.get("who", "사용자") if m["role"] == "user"
                        else m.get("who", "팀원")) + ": " + m["text"][:400]
                       for m in hist[-11:-1])
    ctx = ""
    style = CAST_STYLE
    if pr:
        outs = sorted(cproj_dir(pr).glob("*.md"), key=lambda f: -f.stat().st_mtime)
        ctx = (f"\n\n[소속 프로젝트] {pr['name']} — 목표: {pr.get('goal','')}\n"
               f"[프로젝트 기존 산출물] " + (", ".join(f.name for f in outs[:8]) or "없음"))
        style = CAST_STYLE.replace("25-Casting/", f"25-Casting/{pr['name']}/")
    prompt = (persona + style + ctx
              + ("\n\n[팀 대화 이력]\n" + hblock if hblock else "")
              + "\n\n[사용자의 현재 요청]\n" + text)
    reply = run_agent(prompt, timeout=900, style=False)
    hist.append({"role": "assistant", "who": emp["title"],
                 "ts": time.strftime("%H:%M:%S"), "text": reply})
    try:
        hfile.write_text(json.dumps(hist[-300:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    emit(agent_key, f"업무 완료({emp['title']}) — {text[:28]}")
    return reply, emp["title"]


def _pchat_append(pid, entry):
    f = CACHE / f"casting-pchat-{pid}.json"
    h = _load_json(f, [])
    h.append(entry)
    try:
        f.write_text(json.dumps(h[-300:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def team_run(pid, text):
    """팀 자유 토론 — 전원 첫 발언 후, 서로 지목·반박하는 라운드를 돌고 팀장이 결론."""
    pr = cproj_of(pid)
    key = "team:" + pid
    members = [m for m in (pr.get("members") or []) if emp_of(m)]
    titles = {m: emp_of(m)["title"] for m in members}
    with _runs_lock:
        RUNS[key] = {"running": True, "last": time.strftime("%H:%M:%S"), "note": "팀 토론 중"}
    _pchat_append(pid, {"role": "user", "who": "나", "ts": time.strftime("%H:%M:%S"), "text": text})
    emit("biz:" + pid, f"팀 토론 시작 — {len(members)}명")
    discussion = []                      # [(직책, 발언)]

    def dlog(n=16):
        return "\n\n".join(f"{w}: {t}" for w, t in discussion[-n:]) or "(아직 발언 없음)"

    def utter(eid, first):
        emp, persona = emp_of(eid), casting_prompt(eid)
        rule = ("[토론 규칙] 지금은 팀 자유 토론이다. 파일을 만들지 마라. 발언은 3~6문장으로 짧게. "
                "이미 나온 말을 반복하지 말고, 동의·반박·보완·질문 중심으로 말하라. "
                "특정 팀원에게 물으려면 @직책 으로 지목하라. "
                + ("너의 첫 발언이다 — 네 역할 관점의 핵심 의견을 내라."
                   if first else "덧붙일 것이 없으면 정확히 PASS 라고만 출력하라."))
        prompt = (persona + "\n\n" + rule
                  + f"\n\n[프로젝트] {pr['name']} — 목표: {pr.get('goal','')}"
                  + "\n[토론 주제] " + text
                  + "\n\n[지금까지의 토론]\n" + dlog())
        reply = run_agent(prompt, timeout=420, style=False).strip()
        if not first and reply.upper().startswith("PASS") and len(reply) < 20:
            return False
        discussion.append((emp["title"], reply))
        _pchat_append(pid, {"role": "assistant", "who": emp["title"],
                            "ts": time.strftime("%H:%M:%S"), "text": reply})
        emit("biz:" + pid, f"발언 — {emp['title']}")
        return True

    # 라운드 1: 전원 첫 의견 (랜덤 순서)
    order = members[:]
    random.shuffle(order)
    for eid in order:
        utter(eid, True)
    # 라운드 2~3: 티키타카 — 지목당한 사람 우선, 나머지 랜덤. 전원 PASS면 종료
    for rnd in (2, 3):
        emit("biz:" + pid, f"토론 라운드 {rnd}")
        recent = " ".join(t for _, t in discussion[-len(members):])
        called = [m for m in members if ("@" + titles[m]) in recent]
        rest = [m for m in members if m not in called]
        random.shuffle(rest)
        spoke = 0
        for eid in called + rest:
            if utter(eid, False):
                spoke += 1
        if spoke == 0:
            break
    # 팀장 결론
    lead = casting_prompt("lead")
    emit("biz:" + pid, "팀장 결론 정리 중")
    prompt = (lead + f"\n\n[프로젝트] {pr['name']} — 목표: {pr.get('goal','')}"
              + "\n[토론 주제] " + text
              + "\n\n[토론 전문]\n" + "\n\n".join(f"{w}: {t}" for w, t in discussion)
              + "\n\n토론이 끝났다. 팀장으로서 ① 결론 ② 합의된 것과 이견으로 남은 것 "
              + "③ 다음 단계(담당 직책 지정)를 정리하라. 파일은 만들지 마라.")
    reply = run_agent(prompt, timeout=600, style=False)
    _pchat_append(pid, {"role": "assistant", "who": "팀장 결론",
                        "ts": time.strftime("%H:%M:%S"), "text": reply})
    with _runs_lock:
        RUNS[key] = {"running": False, "last": time.strftime("%H:%M:%S"),
                     "note": f"토론 완료 — 발언 {len(discussion)}건"}
    emit("biz:" + pid, "팀 토론 종료 — 팀장 결론 게시")


def cast_confirm(rel):
    """25-Casting(하위 폴더 포함)의 파일을 사람 컨펌 후 00-Inbox로 이동."""
    rel = rel.strip().lstrip("/")
    if not rel.startswith("25-Casting/"):
        return False
    src = (ROOT / rel).resolve()
    if not src.is_file() or src.suffix != ".md" or CAST_OUT.resolve() not in src.parents:
        return False
    name = src.name
    dst = ROOT / "00-Inbox" / name
    i = 1
    while dst.exists():
        dst = ROOT / "00-Inbox" / f"{dst.stem}-{i}{dst.suffix}"
        i += 1
    src.rename(dst)
    emit("secretary", f"산출물 컨펌 — {name} → 00-Inbox 이동 (/ingest 대상)")
    return True


# ── 텔레그램 봇 동시 기동 ──────────────────────────────
_bot_proc = None


def bot_already_running():
    try:
        r = subprocess.run(["pgrep", "-f", "telegram_bot.py"], capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p.strip() and int(p) != os.getpid()]
        return bool(pids)
    except OSError:
        return False


def start_bot():
    global _bot_proc
    if not BOT.is_file():
        emit("system", "telegram_bot.py 없음 — 봇 기동 생략")
        return
    if bot_already_running():
        emit("secretary", "텔레그램 봇 이미 실행 중 — 재기동 생략")
        return
    CACHE.mkdir(exist_ok=True)
    logf = BOT_LOG.open("a", encoding="utf-8")
    _bot_proc = subprocess.Popen([sys.executable, str(BOT)], cwd=ROOT,
                                 stdout=logf, stderr=logf)
    emit("secretary", f"텔레그램 봇 기동 (pid {_bot_proc.pid})")


def stop_bot():
    if _bot_proc and _bot_proc.poll() is None:
        _bot_proc.terminate()
        try:
            _bot_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _bot_proc.kill()
        print("텔레그램 봇 종료", flush=True)


atexit.register(stop_bot)


# ── 미팅 기록자 (meeting-recorder) — 녹음 저장 후 전사 → 회의록·ingest 자동 실행 ──────
# 흐름: UI가 review-ui(/graph-api/recordings)로 meta+오디오를 저장 → /meeting/save 로 여기 알림
#   ① meta.json에 장소·회의명·일시 등 UI 폼 값을 보강(review-ui는 topic/attendees/project/context만 저장)
#   ② 90-Meta/scripts/record_worker.sh <오디오> 로 전사(mlx-whisper, 수 분)
#   ③ headless claude로 /ingest 실행 → 회의록(60-Sources/meetings) + 관계 검토(.cache/review) 생성
# 진행 상황은 emit("meeting-recorder", …) 이벤트로 흘러 기록자 대화에 시스템 라인으로 보인다.
RECORD_DIR = ROOT / "00-Inbox" / "recordings"
RECORD_WORKER = ROOT / "90-Meta" / "scripts" / "record_worker.sh"
MEETING_KEY = "meeting-recorder"


def meeting_audio(rid):
    for f in RECORD_DIR.glob(rid + ".*"):
        if f.suffix.lower() in (".webm", ".mp4", ".m4a", ".wav", ".mp3", ".ogg"):
            return f
    return None


_SEG_RE = re.compile(r"\[(\d+):(\d{2}):(\d{2})\.\d+\s*-->")


def _meeting_phase(phase, note, pct=None):
    """RUNS[meeting-recorder]의 단계를 갈아끼운다. started는 단계 시작 시각(경과 시간의 기준)."""
    with _runs_lock:
        RUNS[MEETING_KEY] = {"running": True, "last": time.strftime("%H:%M:%S"), "note": note,
                             "phase": phase, "started": time.time(), "pct": pct}


def _transcribe(audio, total_sec):
    """record_worker.sh를 스트리밍으로 돌리며 mlx-whisper의 세그먼트 타임스탬프로 진행률(%)을 갱신한다.
    total_sec(녹음 길이)이 0이면 %는 표시하지 않고 경과 시간만 남는다. 반환: (성공여부, 마지막 줄)."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(["bash", str(RECORD_WORKER), str(audio)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, stdin=subprocess.DEVNULL)
    killer = threading.Timer(3600, proc.kill)
    killer.start()
    last = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            m = _SEG_RE.search(line)
            if m and total_sec > 0:
                done = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                with _runs_lock:
                    st = RUNS.get(MEETING_KEY)
                    if st:
                        st["pct"] = max(0, min(99, int(done * 100 / total_sec)))
            elif not m:
                last = line
        proc.wait()
    finally:
        killer.cancel()
    return proc.returncode == 0, last


def meeting_pipeline(rid):
    _meeting_phase("transcribe", "전사 중", 0)
    audio = meeting_audio(rid)
    ok = False
    try:
        if not audio:
            emit(MEETING_KEY, f"[오류] 오디오 파일 없음 — {rid}")
            return
        total = 0
        try:
            total = int(json.loads((RECORD_DIR / f"{rid}.meta.json").read_text(encoding="utf-8")).get("duration_sec") or 0)
        except (OSError, ValueError):
            pass
        emit(MEETING_KEY, f"전사 시작 — {audio.name} (mlx-whisper" + (f", 녹음 {total // 60}분" if total else "") + ")")
        good, last = _transcribe(audio, total)
        emit(MEETING_KEY, ("전사 완료 — " if good else "[오류] 전사 실패 — ") + last[:90])
        if not good:
            return
        _meeting_phase("ingest", "회의록 작성·ingest 중")
        emit(MEETING_KEY, "회의록 작성 시작 — /ingest (meta의 회의명·참석자·프로젝트·장소를 frontmatter로)")
        out = run_agent(f'/ingest "00-Inbox/recordings/{rid}.transcript.md" — 같은 id의 meta.json(회의명·참석자·프로젝트·장소·일시)을 '
                        "frontmatter의 권위 출처로 사용하고, 회의록을 60-Sources/meetings에 아카이브한 뒤 관계 검토 JSON을 생성하라.",
                        timeout=1800, style=False)
        first = out.splitlines()[0][:90] if out else "(출력 없음)"
        emit(MEETING_KEY, "회의록·관계 추출 완료 — " + first)
        emit("confirm-bot", f"새 검토 도착 — {rid} (미팅 기록자)")
        ok = True
    finally:
        with _runs_lock:
            RUNS[MEETING_KEY] = {"running": False, "last": time.strftime("%H:%M:%S"),
                                 "note": "완료 — 관계 컨펌 봇에서 검토" if ok else "실패 — 로그 확인"}


def meeting_save(body):
    rid = Path(str(body.get("id") or "")).name
    meta_f = RECORD_DIR / f"{rid}.meta.json"
    if not rid or not meta_f.is_file():
        return None
    try:
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {"id": rid}
    for k in ("title", "location", "project", "context"):
        if body.get(k) is not None:
            meta[k] = str(body.get(k)).strip()[:300]
    if body.get("attendees") is not None:
        meta["attendees"] = [str(a).strip() for a in body["attendees"] if str(a).strip()]
    if meta.get("title"):
        meta["topic"] = meta["title"]
    meta.setdefault("location", "회사")
    meta["recorded_at"] = str(body.get("recorded_at") or meta.get("recorded_at") or time.strftime("%Y-%m-%d %H:%M"))
    meta["duration_sec"] = int(body.get("duration_sec") or 0)
    meta["source"] = "agent-office/meeting-recorder"
    meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(MEETING_KEY, f"녹음 저장 — {meta.get('topic', rid)} · 참석 {len(meta.get('attendees', []))}명 · {meta.get('location')}")
    with _runs_lock:
        busy = RUNS.get(MEETING_KEY, {}).get("running", False)
    if not busy:
        threading.Thread(target=meeting_pipeline, args=(rid,), daemon=True).start()
    return {"ok": True, "id": rid, "started": not busy}


def meeting_regulars(project):
    """프로젝트의 과거 회의록(60-Sources/meetings frontmatter project:/attendees:)에서 참석자 빈도를 집계한다.
    사람 이름으로 보이는 항목(한글 2~4자, 조직명 접미 제외, '참석자 N' 제외)만 센다."""
    norm = lambda x: re.sub(r"[^0-9a-z가-힣]", "", (x or "").lower())   # 공백·하이픈·대소문자 무시 (project-j ≡ Project J)
    key = norm(project)
    if not key:
        return []
    names = {key}
    for repo, disp in NAMES.items():                       # 표시명 ↔ 폴더명 상호 인식
        if key in (norm(repo), norm(disp)):
            names.update({norm(repo), norm(disp)})
    counts = {}
    d = ROOT / "60-Sources" / "meetings"
    if not d.is_dir():
        return []
    for f in d.glob("*.md"):
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        m = re.search(r"^project:\s*(.+)$", head, re.M)
        if not m:
            continue
        pv = norm(m.group(1))
        if not any(n and (n in pv or pv in n) for n in names):
            continue
        am = re.search(r"^attendees:\s*\n((?:\s+-\s.*\n?)+)", head, re.M)
        if not am:
            continue
        for line in am.group(1).splitlines():
            nm = re.sub(r"^\s*-\s*", "", line).strip().strip('"').split("(")[0].strip()
            if not re.fullmatch(r"[가-힣]{2,4}", nm) or nm.startswith("참석자"):
                continue
            if re.search(r"(팀|본부|연구소|실|랩|협회|센터|부|국)$", nm) and len(nm) > 2:
                continue
            counts[nm] = counts.get(nm, 0) + 1
    return sorted(({"name": k, "count": v} for k, v in counts.items()),
                  key=lambda x: (-x["count"], x["name"]))


# ── 실시간 회의 보조 — 답변도우미 · 참견모드 ────────────────────
# 기존 파이프라인(녹음 → 종료 → 전사 → 회의록)은 전부 사후 처리다. 여기는 그 옆에 붙는 별도 경로로,
# 회의가 진행되는 동안 브라우저가 20초 조각을 올려 주면 상주 전사기로 즉시 받아 적고
# 토글이 켜진 모드에 한해 claude에게 한 줄짜리 도움말을 만들게 한다.
#   답변도우미(assist)   — 누가 사용자에게 물었을 때 그대로 말할 수 있는 답변 초안
#   참견모드(interject)  — 발언에서 모순·근거 부족이 보이면 사용자가 던질 확인 질문
# 원본 녹음/회의록 파이프라인에는 손대지 않는다. 조각은 전사 후 즉시 삭제한다.
LIVE_DIR = CACHE / "live"
LIVE_SCRIPT = ROOT / "90-Meta" / "scripts" / "live_transcribe.py"
LIVE_CHUNK_MAX = 8 * 1024 * 1024      # 조각 1건 최대 8MB (20초 webm은 보통 100~300KB)
LIVE_MIN_NEW = 60                     # 이만큼 새 전사가 쌓여야 claude를 부른다
LIVE_MIN_GAP = 20                     # 직전 호출과의 최소 간격(초) — 회의 내내 연속 호출을 막는다
LIVE_BACKLOG = 3                      # 대기 조각이 이보다 많으면 오래된 것을 버린다(실시간성 우선)
LIVE_MODEL = os.environ.get("MAP_LIVE_MODEL", "sonnet")  # 참견 품질이 얕으면 쓸모가 없다 — 속도보다 판단력
LIVE_KB_MODEL = os.environ.get("MAP_LIVE_KB_MODEL", "sonnet")  # 회의당 1회 도는 사전조사
LIVE_TTL = 6 * 3600

LIVE = {}                             # sid → 세션 상태
_live_lock = threading.Lock()
_live_tx_lock = threading.Lock()      # 상주 전사기는 한 번에 한 조각만 처리한다
_live_proc = [None]


def _live_transcriber():
    """상주 전사기(live_transcribe.py)를 얻는다 — 없거나 죽었으면 새로 띄운다. 실패 시 None."""
    p = _live_proc[0]
    if p and p.poll() is None:
        return p
    uv = shutil.which("uv") or str(Path.home() / "anaconda3" / "bin" / "uv")
    # Apple Silicon이면 mlx-whisper, 아니면 faster-whisper
    dep = "mlx-whisper" if sys.platform == "darwin" else "faster-whisper"
    try:
        p = subprocess.Popen([uv, "run", "--with", dep, str(LIVE_SCRIPT)],
                             cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError:
        return None
    _live_proc[0] = p
    return p


def _live_transcribe_file(path):
    """조각 하나를 전사해 텍스트를 돌려준다. 첫 호출은 모델 로딩 때문에 수십 초 걸릴 수 있다."""
    with _live_tx_lock:
        p = _live_transcriber()
        if not p:
            return ""
        try:
            p.stdin.write(str(path) + "\n")
            p.stdin.flush()
        except (OSError, ValueError):
            _live_proc[0] = None
            return ""
        while True:
            line = p.stdout.readline()
            if not line:                       # 프로세스가 죽었다 — 다음 조각에서 재기동한다
                _live_proc[0] = None
                return ""
            line = line.strip()
            if not line.startswith("{"):       # 모델 다운로드 진행 등 잡음 줄
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("ready"):
                continue
            return (j.get("text") or "").strip()


def _live_sid(raw):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(raw or ""))[:64]
    return sid or None


def live_session(sid, create=False):
    with _live_lock:
        s = LIVE.get(sid)
        if s is None and create:
            now = time.time()
            for k in [k for k, v in LIVE.items() if now - v["ts"] > LIVE_TTL]:
                LIVE.pop(k, None)
            s = LIVE[sid] = {"segs": [], "cards": [], "assist": False, "interject": False,
                             "queue": [], "pending": "", "busy": False, "last_ai": 0.0,
                             "meta": {}, "ts": now, "worker": False, "dropped": 0, "err": "",
                             "brief": "", "kb": "", "kb_key": "", "kb_state": "none"}
        if s:
            s["ts"] = time.time()
        return s


LIVE_PROMPT = (
    "너는 지금 진행 중인 회의에 함께 앉아 듣고 있는 비서다. 아래 발언은 실시간 음성인식 결과라 "
    "오탈자와 끊긴 문장이 섞여 있다. 문맥으로 보정해서 읽어라.\n\n"
    "[회의] {title} · 프로젝트 {project} · 참석 {attendees}\n"
    "[네가 도울 사람] 사용자\n\n"
    "[프로젝트 사전지식 — 회의 전에 vault에서 조사해 둔 것]\n{kb}\n\n"
    "[회의 브리프 — 네가 직전 호출에서 정리해 둔 누적 상태]\n{brief}\n\n"
    "[최근 발언 원문]\n{ctx}\n\n"
    "[방금 새로 들어온 발언]\n{new}\n\n"
    "[네가 이미 띄운 카드 — 같은 말을 반복하지 마라]\n{recent}\n\n"
    "아래 JSON 객체 하나만 출력하라. 설명도 코드펜스도 붙이지 마라.\n"
    '{{"brief": "", "answer": "", "question": ""}}\n'
    "- brief: 회의 브리프를 새 발언까지 반영해 갱신한 전문. '쟁점 / 결정 / 미해결 / 숫자·기한' 네 줄로 "
    "누적 관리하고 900자를 넘기지 마라. 새로 배운 것이 없으면 기존 브리프를 그대로 다시 써라. 항상 채운다.\n"
    "- answer: {answer_rule}\n"
    "- question: {question_rule}\n"
    "answer·question은 각각 3문장 이내의 한국어다. 해당 상황이 아니면 빈 문자열(\"\")로 두고 억지로 채우지 마라 — "
    "침묵이 얕은 참견보다 낫다. 회의는 계속 흐르고 있으니 vault 파일을 읽지 말고 위에 주어진 것만으로 즉시 답하라."
)
LIVE_RULES = {
    "answer_on": ("누군가 사용자에게 질문했거나 그가 답해야 할 대목이 있으면, 그가 그대로 말할 수 있는 답변 초안. "
                  "사전지식·브리프에 있는 사실(날짜·수치·결정·담당자)을 근거로 삼아 구체적으로 쓰고, "
                  "자료에 없는 것은 지어내지 말고 '확인해서 회신하겠다'는 식으로 처리하라."),
    "answer_off": "이번에는 쓰지 않는다. 항상 빈 문자열.",
    "question_on": ("지금 던져야 값이 있는 확인 질문 하나. 다음 순서로 근거를 찾아라 — "
                    "(1) 사전지식·브리프의 기존 결정·수치와 방금 발언이 어긋나는가, "
                    "(2) 미해결로 남아 있던 항목을 지금 짚고 넘어갈 수 있는가, "
                    "(3) 담당자·기한·범위·검수 기준이 빠진 채 합의로 넘어가려 하는가, "
                    "(4) 전제나 근거 수치가 확인되지 않았는가. "
                    "질문 앞에 근거를 한 구절로 붙여라(예: '8월 회의에서 X로 정했는데 — …?'). "
                    "누구나 할 수 있는 되묻기('구체적으로 어떤 건가요?', '일정은 어떻게 되나요?')는 금지다. "
                    "위 네 가지 중 어디에도 걸리지 않으면 빈 문자열로 두고 넘어가라."),
    "question_off": "이번에는 쓰지 않는다. 항상 빈 문자열.",
}

# 회의당 1회 — 프로젝트/회의명이 정해지면 백그라운드로 vault를 뒤져 브리핑을 만든다.
# 참견의 깊이는 결국 이 텍스트의 질에서 나온다(실시간 호출은 파일을 읽을 시간이 없다).
LIVE_KB_PROMPT = (
    "회의가 곧 시작된다. 회의를 실시간으로 듣는 비서가 참고할 브리핑을 vault에서 조사해 만들어라.\n"
    "[회의명] {title}\n[프로젝트] {project}\n[참석자] {attendees}\n\n"
    "조사 대상 — 15-Reports/<repo>/, 70-Activity/projects/·persons/, 60-Sources/meetings/의 최근 회의록, "
    "20-Wiki/의 관련 개념, 필요하면 그래프(bash 90-Meta/scripts/graph.sh query \"...\").\n\n"
    "아래 형식의 순수 텍스트만 1200자 이내로 출력한다(머리말·코드펜스 금지).\n"
    "개요: 이 프로젝트가 무엇을 하는 일인지 2문장\n"
    "최근 경과: 최근 회의·보고서에서 결정된 것 3~5개, 날짜와 함께\n"
    "미해결: 아직 닫히지 않은 액션아이템·쟁점 3개 이내, 담당자와 함께\n"
    "인물: 참석자별 역할과 이 프로젝트에서의 관심사 한 줄씩\n"
    "숫자·기한: 회의에서 어긋나면 바로 알아챌 수치·일정\n"
    "용어: 이 회의에서 나올 고유 용어 5개 이내와 짧은 뜻\n\n"
    "찾지 못한 항목은 '자료 없음'이라고 쓴다. 추측으로 채우지 마라."
)


def _live_kb_run(sid, key, meta):
    out = ""
    try:
        out = run_agent(LIVE_KB_PROMPT.format(
            title=meta.get("title") or "(미입력)",
            project=meta.get("project") or "(미지정)",
            attendees=", ".join(meta.get("attendees") or []) or "(미입력)"),
            timeout=300, style=False, model=LIVE_KB_MODEL)
    except OSError as e:
        out = ""
    with _live_lock:
        s = LIVE.get(sid)
        if not s or s.get("kb_key") != key:      # 그새 프로젝트가 바뀌었다 — 늦게 온 결과는 버린다
            return
        ok = bool(out) and not out.startswith("(")
        s["kb"] = out.strip()[:4000] if ok else ""
        s["kb_state"] = "ready" if ok else "failed"


def _live_kb_sync(s, sid):
    """프로젝트/회의명이 정해지거나 바뀌면 사전조사를 (재)실행한다. _live_lock을 쥔 채 호출한다."""
    meta = s["meta"]
    if not (meta.get("project") or "").strip():
        return
    key = (meta.get("project") or "").strip()   # 회의명 타이핑마다 재조사하지 않도록 프로젝트만 키로 쓴다
    if key == s.get("kb_key"):
        return
    s["kb_key"], s["kb"], s["kb_state"] = key, "", "loading"
    threading.Thread(target=_live_kb_run, args=(sid, key, dict(meta)), daemon=True).start()


def _live_add_card(sid, kind, text):
    with _live_lock:
        s = LIVE.get(sid)
        if not s:
            return
        s["cards"].append({"id": len(s["cards"]) + 1, "kind": kind,
                           "text": text[:600], "ts": time.strftime("%H:%M")})


def _live_assist_run(sid, ctx, new, meta, assist, interject, kb, kb_state, brief, recent):
    try:
        prompt = LIVE_PROMPT.format(
            title=meta.get("title") or "(회의명 미입력)",
            project=meta.get("project") or "(미지정)",
            attendees=", ".join(meta.get("attendees") or []) or "(미입력)",
            kb=kb or ("조사 중 — 아직 도착하지 않았다. 사전지식 없이 판단하되 확실하지 않으면 침묵하라."
                      if kb_state == "loading" else "자료 없음"),
            brief=brief or "(아직 없음 — 이번 호출에서 처음 만든다)",
            ctx=ctx or "(없음)", new=new,
            recent="\n".join("- " + r for r in recent) or "(없음)",
            answer_rule=LIVE_RULES["answer_on" if assist else "answer_off"],
            question_rule=LIVE_RULES["question_on" if interject else "question_off"])
        out = run_agent(prompt, timeout=180, style=False, model=LIVE_MODEL)
        m = re.search(r"\{.*\}", out or "", re.S)
        j = json.loads(m.group(0)) if m else {}
        ans = str(j.get("answer") or "").strip()
        q = str(j.get("question") or "").strip()
        nb = str(j.get("brief") or "").strip()
        if nb:
            with _live_lock:
                s = LIVE.get(sid)
                if s:
                    s["brief"] = nb[:1200]
        if assist and ans:
            _live_add_card(sid, "assist", ans)
        if interject and q:
            _live_add_card(sid, "interject", q)
    except (ValueError, TypeError, KeyError, IndexError) as e:
        with _live_lock:
            s = LIVE.get(sid)
            if s:
                s["err"] = f"보조 응답 해석 실패 — {str(e)[:80]}"
    finally:
        with _live_lock:
            s = LIVE.get(sid)
            if s:
                s["busy"] = False


def _live_maybe_assist(sid):
    """새로 쌓인 전사가 충분하고 직전 호출과 간격이 벌어졌을 때만 claude를 부른다."""
    with _live_lock:
        s = LIVE.get(sid)
        if not s or s["busy"]:
            return
        if not (s["assist"] or s["interject"]):
            s["pending"] = ""                       # 꺼져 있으면 쌓아둘 이유가 없다
            return
        new = s["pending"].strip()
        if len(new) < LIVE_MIN_NEW and "?" not in new:
            return
        if time.time() - s["last_ai"] < LIVE_MIN_GAP:
            return
        s["busy"], s["pending"], s["last_ai"] = True, "", time.time()
        ctx = " ".join(x["text"] for x in s["segs"])[-2500:]
        recent = [c["kind"] + ": " + c["text"][:120] for c in s["cards"][-5:]]
        args = (sid, ctx, new, dict(s["meta"]), s["assist"], s["interject"],
                s["kb"], s["kb_state"], s["brief"], recent)
    threading.Thread(target=_live_assist_run, args=args, daemon=True).start()


def _live_worker(sid):
    while True:
        with _live_lock:
            s = LIVE.get(sid)
            item = s["queue"].pop(0) if (s and s["queue"]) else None
            if item is None:
                if s:
                    s["worker"] = False
                return
        text = _live_transcribe_file(item)
        try:
            Path(item).unlink()
        except OSError:
            pass
        if text:
            with _live_lock:
                s = LIVE.get(sid)
                if s:
                    s["segs"].append({"t": time.strftime("%H:%M:%S"), "text": text})
                    s["pending"] = (s["pending"] + " " + text).strip()
        _live_maybe_assist(sid)


def live_chunk(sid, seq, data):
    """조각 저장 + 큐 적재. 밀리면 오래된 조각을 버려 실시간성을 지킨다(원본 녹음은 영향 없음)."""
    s = live_session(sid, create=True)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    f = LIVE_DIR / f"{sid}-{int(seq):04d}.webm"
    try:
        f.write_bytes(data)
    except OSError as e:
        return {"ok": False, "error": str(e)[:120]}
    start = False
    with _live_lock:
        while len(s["queue"]) >= LIVE_BACKLOG:
            old = s["queue"].pop(0)
            s["dropped"] += 1
            try:
                Path(old).unlink()
            except OSError:
                pass
        s["queue"].append(str(f))
        if not s["worker"]:
            s["worker"] = start = True
    if start:
        threading.Thread(target=_live_worker, args=(sid,), daemon=True).start()
    return {"ok": True, "queued": len(s["queue"]), "dropped": s["dropped"]}


def live_mode(sid, body):
    """토글 상태와 회의 메타를 갱신한다. 둘 다 꺼지면 대기 중인 조각도 비운다."""
    s = live_session(sid, create=True)
    with _live_lock:
        for k in ("assist", "interject"):
            if body.get(k) is not None:
                s[k] = bool(body.get(k))
        for k in ("title", "project"):
            if body.get(k) is not None:
                s["meta"][k] = str(body.get(k))[:200]
        if body.get("attendees") is not None:
            s["meta"]["attendees"] = [str(a)[:40] for a in body["attendees"]][:30]
        if s["assist"] or s["interject"]:
            _live_kb_sync(s, sid)
        on = s["assist"] or s["interject"]
        if not on:
            for q in s["queue"]:
                try:
                    Path(q).unlink()
                except OSError:
                    pass
            s["queue"], s["pending"] = [], ""
        return {"ok": True, "assist": s["assist"], "interject": s["interject"],
                "kb_state": s["kb_state"]}


def live_state(sid, since):
    s = live_session(sid)
    if not s:
        return {"ok": True, "n": 0, "cards": [], "segs": 0, "tail": "", "queued": 0,
                "kb_state": "none"}
    with _live_lock:
        return {"ok": True, "n": len(s["cards"]), "cards": s["cards"][max(0, since):],
                "segs": len(s["segs"]), "queued": len(s["queue"]), "busy": s["busy"],
                "dropped": s["dropped"], "err": s["err"], "kb_state": s["kb_state"],
                "tail": " ".join(x["text"] for x in s["segs"][-2:])[-220:]}


def live_stop(sid):
    """세션 종료 — 남은 조각 파일을 지우고 상태를 버린다. 전사기는 다음 회의를 위해 남겨둔다."""
    with _live_lock:
        s = LIVE.pop(sid, None)
    if s:
        for q in s["queue"]:
            try:
                Path(q).unlink()
            except OSError:
                pass
    return {"ok": True, "segs": len(s["segs"]) if s else 0}


MEETING_PERSONA = (
    "너는 '미팅 기록자'다. 회의 녹음을 받아 전사하고, 사용자가 녹음 중에 입력한 회의명·참석자·프로젝트·장소·일시를 "
    "frontmatter의 권위 출처로 삼아 회의록을 작성해 60-Sources/meetings에 아카이브하며, 관계 추출 검토를 생성한다. "
    "사용자의 질문에는 00-Inbox/recordings(진행 중)와 60-Sources/meetings(완료) 문서를 근거로 답한다. "
    "회의록 본문은 결정·액션아이템·논의를 구분해 정리하고 발언자 추정은 '추정'으로 표기한다.")


# ── review-ui 프록시 ─────────────────────────────────
# /graph-api/<path> → REVIEW_API/api/<path>. 관계 검토·그래프·동의어·녹음 API를 새 셸(office.html)이
# 같은 origin에서 쓰기 위한 통로. review-ui 코드베이스는 그대로 두고(Docker) 여기서 중계만 한다.
def proxy_review(handler, method):
    import urllib.request
    import urllib.error
    u = urlparse(handler.path)
    target = REVIEW_API + "/api/" + u.path[len("/graph-api/"):] + (("?" + u.query) if u.query else "")
    body = None
    if method == "POST":
        try:
            n = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        body = handler.rfile.read(n) if n > 0 else b""
    req = urllib.request.Request(target, data=body, method=method)
    ct = handler.headers.get("Content-Type")
    if ct:
        req.add_header("Content-Type", ct)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data, status, rct = r.read(), r.status, r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        data, status, rct = e.read(), e.code, e.headers.get("Content-Type", "application/json")
    except (urllib.error.URLError, OSError) as e:
        data = json.dumps({"error": "review-ui(57900)에 연결할 수 없습니다 — 컨테이너 jarvis-review-ui 상태 확인",
                           "detail": str(e)[-200:]}, ensure_ascii=False).encode()
        status, rct = 502, "application/json; charset=utf-8"
    handler.send_response(status)
    handler.send_header("Content-Type", rct)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# ── 로컬 터미널(ttyd + tmux) ───────────────────────────
# 에이전트 탭 옆에 실제 셸을 띄운다. 에이전트별로 ttyd 프로세스 1개 + tmux 세션 1개를
# 두고, cwd를 그 에이전트의 저장소 경로로 잡는다(프로젝트가 아니면 vault 루트).
# ttyd는 127.0.0.1(lo0)에만 바인딩한다 — 인증이 없으므로 외부 노출 금지.
# 브라우저를 닫아도 tmux 세션은 남고, 다시 열면 `new-session -A`가 같은 세션에 붙는다.
TERM_PORT0 = int(os.environ.get("MAP_TERM_PORT", "57920"))
_terms = {}          # agent key → {"port","proc","session","cwd"}
_terms_lock = threading.Lock()


def _term_free_port():
    used = {t["port"] for t in _terms.values()}
    for p in range(TERM_PORT0, TERM_PORT0 + 40):
        if p in used:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return None


def _term_slug(key):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", key).strip("-").lower() or "shell"


def term_target(agent_key):
    """에이전트 → (tmux 세션명, 작업 디렉토리). 프로젝트 에이전트만 저장소로 내려간다."""
    cwd = ROOT
    if agent_key.startswith("proj:"):
        r = repo_of(agent_key)
        if r and Path(r).is_dir():
            cwd = Path(r)
    return "jarvis-" + _term_slug(agent_key), cwd


def term_start(agent_key):
    """ttyd를 (필요하면) 띄우고 접속 정보를 돌려준다. 이미 살아 있으면 그대로 재사용한다."""
    ttyd = shutil.which("ttyd")
    if not ttyd:
        return {"error": "ttyd가 설치돼 있지 않습니다 — `brew install ttyd` 후 다시 시도하세요."}
    if not shutil.which("tmux"):
        return {"error": "tmux가 설치돼 있지 않습니다 — `brew install tmux` 후 다시 시도하세요."}
    session, cwd = term_target(agent_key)
    with _terms_lock:
        t = _terms.get(agent_key)
        if t and t["proc"].poll() is None:
            return {"url": f"http://127.0.0.1:{t['port']}", "session": session, "cwd": str(cwd)}
        port = _term_free_port()
        if port is None:
            return {"error": "빈 포트를 찾지 못했습니다."}
        # -O(origin 검사)는 쓰지 않는다 — iframe 부모가 57910, ttyd가 다른 포트라 웹소켓이 막힌다.
        # 대신 루프백 바인딩으로 외부 접근 자체를 차단한다.
        loopback = "lo0" if sys.platform == "darwin" else "lo"
        args = [ttyd, "-p", str(port), "-i", loopback, "-W",
                "-t", "fontSize=13", "-t", "fontFamily=ui-monospace,SFMono-Regular,Menlo,monospace",
                "-t", "disableLeaveAlert=true", "-t", "titleFixed=" + session,
                "-t", 'theme={"background":"#0a0a0a","foreground":"#d6d6d6","cursor":"#d6d6d6","selectionBackground":"#33415a"}',
                "tmux", "new-session", "-A", "-s", session, "-c", str(cwd)]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL)
        except OSError as e:
            return {"error": f"ttyd 실행 실패: {e}"}
        _terms[agent_key] = {"port": port, "proc": proc, "session": session, "cwd": str(cwd)}
    time.sleep(0.35)     # ttyd가 리슨을 열 때까지 — iframe이 먼저 붙으면 빈 화면이 된다
    # 로그인 전용 세션 — 열자마자 로그인 명령을 보낸다
    if agent_key == "login-claude":
        subprocess.Popen(["tmux", "send-keys", "-t", session, "claude", "Enter"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif agent_key == "login-codex":
        subprocess.Popen(["tmux", "send-keys", "-t", session, "codex login", "Enter"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"url": f"http://127.0.0.1:{port}", "session": session, "cwd": str(cwd)}


def term_stop_all():
    with _terms_lock:
        for t in _terms.values():
            try:
                t["proc"].terminate()
            except OSError:
                pass
        _terms.clear()


atexit.register(term_stop_all)


# ── 프로젝트 에이전트 정의 생성 ─────────────────────────
def _create_agent_def(name, path_str, slug):
    """UI에서 프로젝트를 추가할 때 .claude/agents/<slug>.md를 생성한다."""
    agents_dir = ROOT / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    af = agents_dir / f"{slug}.md"
    if af.is_file():
        return
    af.write_text(f"""---
name: {slug}
description: "{name}" 프로젝트 전담 에이전트. 저장소 경로 {path_str} 의 코드와 git 이력을 분석하고 질문에 답한다.
---

# {name} 프로젝트 에이전트

이 에이전트는 "{name}" 프로젝트를 담당한다.

- 저장소 경로: `{path_str}`
- `git -C '{path_str}' log/diff/show`로 이력을 조회한다
- `15-Reports/{Path(path_str).name}/` 보고서와 `10-Daily` 노트를 근거로 답한다
- 다른 프로젝트의 코드는 다루지 않는다
- 코드 근거는 파일경로:라인으로 인용한다
""", encoding="utf-8")


# ── HTTP ──────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html", "/legacy"):
            # "/"는 새 셸(office.html — Grok Bot 스킨), "/legacy"는 이전 탭형 맵 UI
            try:
                body = (HERE / ("map.html" if u.path == "/legacy" else "office.html")).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/meeting/attendees":
            self._json({"attendees": meeting_regulars(parse_qs(u.query).get("project", [""])[0])})
        elif u.path == "/meeting/live/state":
            q = parse_qs(u.query)
            sid = _live_sid(q.get("id", [""])[0])
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            if not sid:
                self.send_error(400)
                return
            self._json(live_state(sid, since))
        elif u.path.startswith("/graph-api/"):
            proxy_review(self, "GET")
        elif u.path in ("/office-graph.js", "/office-meeting.js"):
            try:
                body = (HERE / u.path.lstrip("/")).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/popchat":
            # 메뉴바 앱(90-Meta/menubar)의 팝오버가 여는 경량 채팅. 채팅 API는 맵과 공유한다.
            try:
                body = (HERE / "popchat.html").read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path in ("/human_jarvis.gif", "/human_jarvis_still.png"):
            try:
                body = (HERE / u.path.lstrip("/")).read_bytes()
            except OSError:
                self.send_error(404)
                return
            ctype = "image/gif" if u.path.endswith(".gif") else "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/agents":
            repos = [r for r in parse_repos() if r.resolve() != ROOT]
            cast = [{"key": "cast:" + str(i), "name": (emp_of(i) or {}).get("title", str(i)),
                     "desc": (emp_of(i) or {}).get("desc", "")} for i in roster()]
            biz = [{"key": "biz:" + pr["id"], "name": pr["name"], "goal": pr.get("goal", ""),
                    "members": [(emp_of(m) or {}).get("title", m) for m in pr.get("members", [])]}
                   for pr in cprojects()]
            for i, c in enumerate(cast):
                c["color"], c["hair"] = CAST_COLORS[i % len(CAST_COLORS)], "#5a4a3a"
            funcs = [{"key": k, "name": AGENT_JOBS[k]["label"], "color": v[0], "hair": v[1],
                      "emoji": v[2]} for k, v in AGENT_LOOK.items() if k in AGENT_JOBS]
            self._json({"projects": [{"key": "proj:" + r.name,
                                      "name": NAMES.get(r.name, r.name),
                                      "path": str(r),
                                      "color": PROJ_COLORS[i % len(PROJ_COLORS)],
                                      "hair": "#3b3f4a", "emoji": "🛠️"}
                                     for i, r in enumerate(repos)],
                        "functions": funcs, "casting": cast, "biz": biz})
        elif u.path == "/agent/chat":
            key = parse_qs(u.query).get("agent", [""])[0]
            if not agent_chat_key_ok(key):
                self.send_error(400)
                return
            self._json({"messages": _load_json(agent_chat_file(key), [])[-100:]})
        elif u.path == "/agent/config":
            key = parse_qs(u.query).get("agent", [""])[0]
            if key not in DEFAULT_PROMPTS:
                self.send_error(400)
                return
            self._json(agent_config_view(key))
        elif u.path == "/usage":
            self._json(get_usage())
        elif u.path == "/api/config":
            cfg = ao_config()
            self._json({"transcribeModel": cfg.get("transcribeModel", "small"),
                        "useGraph": cfg.get("useGraph", False)})
        elif u.path == "/api/projects":
            self._json({"projects": ao_projects()})
        elif u.path == "/api/login-status":
            # AI 계정 로그인 상태 — 자격 파일 존재 여부로 판정
            claude_ok = (Path.home() / ".claude").is_dir() and any((Path.home() / ".claude").iterdir())
            codex_ok = (Path.home() / ".codex").is_dir() or (Path.home() / ".config" / "codex").is_dir()
            self._json({"claude": claude_ok, "codex": codex_ok})
        elif u.path == "/models":
            self._json({"models": [{"value": v, "label": l} for v, l in MODELS],
                        "current": current_model()})
        elif u.path == "/files":
            d = list_dir(parse_qs(u.query).get("dir", [""])[0])
            if d is None:
                self.send_error(404)
            else:
                self._json(d)
        elif u.path == "/file":
            fp = safe_path(parse_qs(u.query).get("path", [""])[0])
            if not fp or not fp.is_file() or fp.suffix != ".md":
                self.send_error(404)
            else:
                self._json({"text": fp.read_text(encoding="utf-8", errors="replace")[:200000]})
        elif u.path == "/tutor/questions":
            self._json({"questions": tutor_questions()})
        elif u.path == "/tutor/figure":                 # 캐시 조회만 — 생성은 POST
            qid = re.sub(r"[^\w\-.]", "_", parse_qs(u.query).get("qid", [""])[0])
            svg_f = FIG_DIR / (qid + ".svg")
            if qid and svg_f.is_file():
                self._json({"svg": svg_f.read_text(encoding="utf-8"), "cached": True, **_load_json(FIG_DIR / (qid + ".json"), {})})
            else:
                self._json({"cached": False})
        elif u.path == "/tutor/chat":
            with _chat_lock:
                self._json({"messages": TCHAT[-100:]})
        elif u.path == "/explain":
            agent = parse_qs(u.query).get("agent", [""])[0]
            try:
                cache = json.loads(EXPLAIN_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
            hit = cache.get(agent) or {}
            self._json({"text": hit.get("text"), "ts": hit.get("ts"), "sha": hit.get("sha")})
        elif u.path == "/casting/catalog":
            prs = []
            for pr in cprojects():
                d = CAST_OUT / pr["name"]
                pend = len(list(d.glob("*.md"))) if d.is_dir() else 0
                prs.append(dict(pr, pending=pend))
            self._json({"departments": casting_catalog(), "roster": roster(),
                        "projects": prs,
                        "available": (CASTING_DIR / "references/catalog.md").is_file()})
        elif u.path == "/casting/prompt":
            eid = parse_qs(u.query).get("id", ["0"])[0]
            try:
                eid_i = int(eid)
            except ValueError:
                self.send_error(400)
                return
            self._json({"text": casting_prompt(eid_i),
                        "emp": emp_of(eid_i) or {}})
        elif u.path == "/casting/chat":
            q = parse_qs(u.query)
            pid = q.get("project", [""])[0]
            if pid:
                self._json({"messages": _load_json(CACHE / f"casting-pchat-{pid}.json", [])[-100:]})
            else:
                eid = q.get("id", ["0"])[0]
                self._json({"messages": _load_json(cast_chat_file(eid), [])[-100:]})
        elif u.path == "/casting/outputs":
            pid = parse_qs(u.query).get("project", [""])[0]
            pr = cproj_of(pid) if pid else None
            base = cproj_dir(pr) if pr else CAST_OUT
            sub = ("25-Casting/" + pr["name"] + "/") if pr else "25-Casting/"
            CAST_OUT.mkdir(exist_ok=True)
            fs = [{"name": f.name, "path": sub + f.name,
                   "mtime": time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
                  for f in sorted(base.glob("*.md"),
                                  key=lambda x: -x.stat().st_mtime)]
            self._json({"files": fs})
        elif u.path == "/qa/summary":
            qs = tutor_questions()
            res = _load_json(QA_RESULTS, [])
            today = time.strftime("%Y-%m-%d")
            names = {("proj:" + r.name): NAMES.get(r.name, r.name) for r in parse_repos()}
            projs = {}
            for q in qs:
                pr = q.get("project") or "공통"
                d = projs.setdefault(pr, {"pending": 0, "today": 0})
                if q.get("status") == "pending":
                    d["pending"] += 1
                if str(q.get("id", "")).startswith(today):
                    d["today"] += 1
            grass, score = {}, {"O": 0, "X": 0}
            for r2 in res:
                grass.setdefault(r2["project"], {}).setdefault(r2["date"], 0)
                if r2["verdict"] == "O":
                    grass[r2["project"]][r2["date"]] += 1
                if r2["date"] == today:
                    score[r2["verdict"]] = score.get(r2["verdict"], 0) + 1
            self._json({"questions": qs, "projects": projs, "grass": grass,
                        "score": score, "names": names, "today": today})
        elif u.path == "/actions":
            q = parse_qs(u.query)
            today = time.strftime("%Y-%m-%d")
            items = [action_view(x, today) for x in action_items(q.get("project", [""])[0] or None)]
            self._json({"items": items, "open": sum(1 for x in items if x["status"] != "done"),
                        "done": sum(1 for x in items if x["status"] == "done")})
        elif u.path == "/calendar":
            q = parse_qs(u.query)
            ym = q.get("month", [time.strftime("%Y-%m")])[0]
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ym):
                self.send_error(400)
                return
            self._json(calendar_month(ym, force=bool(q.get("refresh"))))
        elif u.path == "/today":
            if parse_qs(u.query).get("refresh"):
                _today_mem["ts"] = 0
            self._json(today_items())
        elif u.path == "/dashboard":
            qs = tutor_questions()
            out = []
            for r in parse_repos():
                if r.resolve() == ROOT:
                    continue
                key = "proj:" + r.name
                c = commits_of(key) or {"commits": [], "uncommitted": 0}
                rep_dir = ROOT / "15-Reports" / r.name
                latest = ""
                if rep_dir.is_dir():
                    mds = sorted(rep_dir.glob("*.md"), reverse=True)
                    latest = mds[0].name if mds else ""
                pend = sum(1 for q in qs if (q.get("project") or "") == r.name
                           and q.get("status") == "pending")
                exp = (_load_json(EXPLAIN_FILE, {}).get(key) or {})
                out.append({"key": key, "name": NAMES.get(r.name, r.name), "repo": r.name,
                            "commit": (c["commits"][0] if c["commits"] else None),
                            "last": last_work_of(r),
                            "uncommitted": c["uncommitted"], "report": latest,
                            "qa_pending": pend, "explain_ts": exp.get("ts", ""),
                            "explain": (exp.get("text") or "")[:180]})
            self._json({"projects": out, "ts": time.strftime("%H:%M")})
        elif u.path == "/commits":
            c = commits_of(parse_qs(u.query).get("agent", [""])[0])
            if c is None:
                self.send_error(404)
            else:
                self._json(c)
        elif u.path == "/chat":
            with _chat_lock:
                self._json({"messages": CHAT[-100:]})
        elif u.path == "/chat/progress":
            q = parse_qs(u.query)
            job = q.get("job", [""])[0]
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            st = CHAT_JOBS.get(job)
            if not st:
                self._json({"done": True, "lines": [],
                            "reply": "(작업을 찾을 수 없습니다 — 서버가 재시작된 것 같습니다. 다시 질문해 주세요.)"})
            else:
                with _chat_lock:
                    self._json({"done": st["done"], "lines": st["lines"][since:],
                                "reply": st["reply"], "draft": st.get("draft") or ""})
        elif u.path == "/events":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with _lock:
                evs = [e for e in _events if e["id"] > since][-200:]
                last = _next_id - 1
            with _runs_lock:
                # started(단계 시작 epoch)가 있으면 응답 시점 기준 경과 초를 실어 보낸다 —
                # 클라이언트는 이 값에 수신 후 흐른 시간만 더해 1초 티커를 돌린다(시계 차이 무관).
                now = time.time()
                runs = {k: dict(v, elapsed=int(now - v["started"])) if v.get("started") else dict(v)
                        for k, v in RUNS.items()}
            # uiver: 셸(office.html + 보조 js)의 최종 수정 시각. 브라우저가 옛 JS를 들고 돌면
            # 값이 바뀌므로 클라이언트가 "새로고침" 배너를 띄운다.
            try:
                uiver = int(max((HERE / f).stat().st_mtime
                                for f in ("office.html", "office-graph.js", "office-meeting.js")
                                if (HERE / f).exists()))
            except Exception:
                uiver = 0
            self._json({"last": last, "events": evs, "runs": runs, "uiver": uiver,
                        "bot": bool(_bot_proc and _bot_proc.poll() is None) or bot_already_running()})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/graph-api/"):
            proxy_review(self, "POST")
            return
        if path == "/term/start":
            # 로컬 터미널 열기 — 에이전트별 ttyd+tmux를 띄우고 iframe이 붙을 URL을 돌려준다.
            try:
                n = int(self.headers.get("Content-Length", "0"))
                agent = str((json.loads(self.rfile.read(n).decode()) or {}).get("agent", ""))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self._json(term_start(agent or "vault"))
            return
        if path == "/chat/stop":
            # 정지 버튼 — 실행 중인 대화 job을 끊는다. 초안이 있으면 그 지점까지가 답변으로 남는다.
            try:
                n = int(self.headers.get("Content-Length", "0"))
                job = str((json.loads(self.rfile.read(n).decode()) or {}).get("job", ""))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self._json({"ok": job_stop(job)})
            return
        if path == "/meeting/live/chunk":
            # 회의 중 20초 오디오 조각 — 본문은 바이트 그대로, 세션 id·순번은 쿼리로 온다
            q = parse_qs(urlparse(self.path).query)
            sid = _live_sid(q.get("id", [""])[0])
            try:
                seq = int(q.get("seq", ["0"])[0])
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not sid or n <= 0:
                self.send_error(400)
                return
            if n > LIVE_CHUNK_MAX:
                self.send_error(413)
                return
            self._json(live_chunk(sid, seq, self.rfile.read(n)))
            return
        if path in ("/meeting/live/mode", "/meeting/live/stop"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode()) if n else {}
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            sid = _live_sid(b.get("id"))
            if not sid:
                self.send_error(400)
                return
            self._json(live_mode(sid, b) if path.endswith("/mode") else live_stop(sid))
            return
        if path == "/meeting/save":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            res = meeting_save(b)
            if res is None:
                self.send_error(404)
                return
            self._json(res)
            return
        if path == "/upload":
            # 채팅 창 드래그앤드롭 첨부 — 본문은 파일 바이트 그대로, 파일명은 X-File-Name(URL 인코딩) 헤더.
            # .cache/uploads/ 에 저장하고 절대 경로를 돌려준다. 클라이언트가 메시지에 경로를 붙여 보내면
            # 에이전트가 그 파일을 직접 읽는다.
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if n <= 0 or n > UPLOAD_MAX:
                self.send_error(413 if n > UPLOAD_MAX else 400)
                return
            from urllib.parse import unquote
            raw = unquote(self.headers.get("X-File-Name", "") or "file")
            name = re.sub(r"[^\w.\-가-힣 ()\[\]]", "_", Path(raw).name).strip() or "file"
            data = self.rfile.read(n)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / (time.strftime("%Y%m%d-%H%M%S-") + name)
            i = 1
            while dest.exists():
                dest = UPLOAD_DIR / (time.strftime("%Y%m%d-%H%M%S-") + f"{i}-" + name)
                i += 1
            try:
                dest.write_bytes(data)
            except OSError:
                self.send_error(500)
                return
            self._json({"ok": True, "path": str(dest), "name": name, "size": len(data)})
            return
        if path == "/model":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                m = str(json.loads(self.rfile.read(n).decode()).get("model", ""))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not save_model(m):                      # 허용 목록 밖 → 거절
                self.send_error(400)
                return
            emit("system", "모델 변경 — " + MODEL_LABELS[m] + " (다음 실행부터 적용)")
            self._json({"ok": True, "model": m})
            return
        if path == "/api/config":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            cfg = ao_config()
            if "transcribeModel" in b and b["transcribeModel"] in ("small", "medium"):
                cfg["transcribeModel"] = b["transcribeModel"]
            if "useGraph" in b:
                cfg["useGraph"] = bool(b["useGraph"])
            save_ao_config(cfg)
            emit("system", "설정 변경 — " + ", ".join(f"{k}={v}" for k, v in b.items() if k in ("transcribeModel", "useGraph")))
            self._json({"ok": True, **{k: cfg.get(k) for k in ("transcribeModel", "useGraph")}})
            return
        if path == "/api/projects":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                action = b.get("action", "")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if action == "add":
                name = re.sub(r"[^\w가-힣 .\-]", "", (b.get("name") or "")).strip()[:60]
                path_str = (b.get("path") or "").strip()
                if not name or not path_str:
                    self.send_error(400)
                    return
                prs = ao_projects()
                # 경로 기준 dedupe
                if not any(p.get("path") == path_str for p in prs):
                    slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40] or "project"
                    prs.append({"name": name, "path": path_str, "slug": slug})
                    save_ao_projects(prs)
                    # 에이전트 정의 파일 생성
                    _create_agent_def(name, path_str, slug)
                    emit("system", f"프로젝트 추가 — {name} ({path_str})")
                self._json({"ok": True, "projects": ao_projects()})
            elif action == "delete":
                path_str = (b.get("path") or "").strip()
                prs = ao_projects()
                removed = [p for p in prs if p.get("path") == path_str]
                prs = [p for p in prs if p.get("path") != path_str]
                save_ao_projects(prs)
                # 생성했던 에이전트 파일 제거
                for p in removed:
                    slug = p.get("slug", "")
                    af = ROOT / ".claude" / "agents" / f"{slug}.md"
                    if af.is_file():
                        af.unlink()
                emit("system", f"프로젝트 제거 — {path_str}")
                self._json({"ok": True, "projects": ao_projects()})
            else:
                self.send_error(400)
            return
        if path == "/run":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                agent = (json.loads(self.rfile.read(n).decode()).get("agent") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if agent not in AGENT_JOBS and agent not in ("learn-loop", "full-loop", "proj-check"):
                self.send_error(400)
                return
            with _runs_lock:
                busy = RUNS.get(agent, {}).get("running", False)
            if not busy:
                threading.Thread(target=run_job, args=(agent,), daemon=True).start()
            self._json({"ok": True, "started": not busy})
            return
        if path == "/prompt":
            # review-ui(Docker, claude CLI 없음)가 검토 채팅용으로 빌려 쓰는 원시 프롬프트 실행. 127.0.0.1 바인딩 전제.
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                prompt = str(b.get("prompt") or "")
                timeout = min(int(b.get("timeout") or 420), 900)
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not prompt.strip():
                self.send_error(400)
                return
            emit("system", "검토 UI 채팅 — 비서 응답 생성 중")
            self._json({"text": run_agent(prompt, timeout=timeout, style=False)})
            return
        if path == "/tutor/chat":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            reply, verdict = tutor_turn((b.get("text") or "").strip(),
                                        (b.get("question") or "").strip(),
                                        (b.get("qid") or "").strip(),
                                        (b.get("project") or "").strip(),
                                        (b.get("outline") or "").strip())
            self._json({"reply": reply, "verdict": verdict})
            return
        if path in ("/tutor/figure", "/tutor/answer"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                qid = str(b.get("qid") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if path == "/tutor/answer":
                self._json(tutor_answer(qid, str(b.get("action") or ""), b.get("choice")))
                return
            _qs, q = _q_by_id(qid)
            if not q:
                self._json({"error": "문항 없음"})
                return
            self._json(tutor_figure(q, force=bool(b.get("force"))))
            return
        if path in ("/casting/hire", "/casting/fire"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                eid = int(json.loads(self.rfile.read(n).decode()).get("id", 0))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            r = roster()
            if path.endswith("hire") and eid not in r and emp_of(eid):
                r.append(eid)
                emit("system", f"캐스팅 — {emp_of(eid)['title']} 합류")
            if path.endswith("fire") and eid in r:
                r.remove(eid)
                emit("system", f"캐스팅 해제 — {(emp_of(eid) or {}).get('title', eid)}")
            save_roster(r)
            self._json({"roster": r})
            return
        if path == "/casting/chat":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                eid = int(b.get("id", 0))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            text, pid = (b.get("text") or "").strip(), (b.get("project") or "").strip()
            if b.get("async"):
                # 메뉴바 팝오버용 — 창을 닫아도 답을 받을 수 있게 job으로 돌리고 /chat/progress로 조회한다
                job = new_job()

                def cast_work():
                    reply, title = cast_turn(eid, text, pid)
                    with _chat_lock:
                        if not CHAT_JOBS.get(job, {}).get("cancel"):   # 정지된 job은 덮지 않는다
                            CHAT_JOBS[job].update(reply=reply, done=True, title=title)
                threading.Thread(target=cast_work, daemon=True).start()
                self._json({"job": job})
                return
            reply, title = cast_turn(eid, text, pid)
            self._json({"reply": reply, "title": title})
            return
        if path == "/casting/team":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                pid = (b.get("project") or "").strip()
                text = (b.get("text") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            pr = cproj_of(pid)
            if not pr or not text:
                self.send_error(400)
                return
            if not pr.get("members"):
                self._json({"ok": False, "error": "배정된 직원이 없습니다 — 직원 칩의 [배정]으로 팀을 꾸리세요"})
                return
            key = "team:" + pid
            with _runs_lock:
                busy = RUNS.get(key, {}).get("running", False)
            if not busy:
                threading.Thread(target=team_run, args=(pid, text), daemon=True).start()
            self._json({"ok": True, "started": not busy})
            return
        if path == "/casting/project":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                name = re.sub(r"[^\w가-힣 .-]", "", (b.get("name") or "")).strip()[:40]
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not name:
                self.send_error(400)
                return
            prs = cprojects()
            pid = re.sub(r"\s+", "-", name)
            if not cproj_of(pid):
                pr = {"id": pid, "name": name, "goal": (b.get("goal") or "")[:300],
                      "members": [], "created": time.strftime("%Y-%m-%d")}
                prs.append(pr)
                save_cprojects(prs)
                cproj_dir(pr)
                emit("system", f"캐스팅 프로젝트 생성 — {name}")
            self._json({"projects": cprojects()})
            return
        if path == "/casting/project/members":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            prs = cprojects()
            for pr in prs:
                if pr["id"] == b.get("id"):
                    pr["members"] = [int(x) for x in (b.get("members") or []) if emp_of(int(x))]
            save_cprojects(prs)
            self._json({"projects": prs})
            return
        if path == "/casting/project/delete":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                pid = json.loads(self.rfile.read(n).decode()).get("id", "")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            prs = [x for x in cprojects() if x["id"] != pid]
            save_cprojects(prs)
            emit("system", f"캐스팅 프로젝트 해산 — {pid} (폴더·산출물은 보존)")
            self._json({"projects": prs})
            return
        if path == "/casting/confirm":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                rel = json.loads(self.rfile.read(n).decode()).get("path", "")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self._json({"ok": cast_confirm(rel)})
            return
        if path == "/agent/config":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                key = str(b.get("agent") or "")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if key not in DEFAULT_PROMPTS:
                self.send_error(400)
                return
            try:
                self._json(update_agent_config(key, b))
            except (OSError, ValueError):
                self.send_error(500)
            return
        if path == "/agent/chat":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                key, text = (b.get("agent") or "").strip(), (b.get("text") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not text or not agent_chat_key_ok(key):
                self.send_error(400)
                return
            self._json({"job": agent_chat_start(key, text)})
            return
        if path == "/action/order":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                pj, ids = str(b.get("project") or "").strip(), b.get("ids") or []
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not pj or not isinstance(ids, list) or len(ids) > 500 or not all(
                    isinstance(i, str) and re.fullmatch(r"[\w\-가-힣.]+#a\d+", i) for i in ids):
                self.send_error(400)
                return
            save_action_order(pj, ids)
            _today_mem["ts"] = 0
            self._json({"ok": True})
            return
        if path == "/action/done":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                b = json.loads(self.rfile.read(n).decode())
                aid = str(b.get("id") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not re.fullmatch(r"[\w\-가-힣.]+#a\d+", aid) or not AI:
                self.send_error(400)
                return
            try:
                AI.mark(aid, not b.get("reopen"))
            except Exception as e:                  # noqa: BLE001
                self._json({"ok": False, "error": str(e)[-200:]})
                return
            _today_mem["ts"] = 0
            try:                                     # 캘린더 캐시 무효화 — 체크 즉시 반영되게
                (CAL_DIR / (time.strftime("%Y-%m") + ".json")).unlink(missing_ok=True)
            except OSError:
                pass
            emit("system", ("할 일 완료 — " if not b.get("reopen") else "할 일 다시 열기 — ") + aid.split("#")[0][:40])
            self._json({"ok": True})
            return
        if path == "/agent/call":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                key = (json.loads(self.rfile.read(n).decode()).get("agent") or "").strip()
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not key.startswith("proj:") or repo_of(key) is None:
                self.send_error(400)
                return
            self._json({"job": project_check_start(key)})
            return
        if path == "/explain":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                agent = json.loads(self.rfile.read(n).decode()).get("agent", "")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self._json({"text": explain_repo(agent)})
            return
        if path != "/chat":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            text = (body.get("text") or "").strip()
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not text:
            self.send_error(400)
            return
        job = chat_start(text)           # 비동기 — 진행은 /chat/progress?job=…로 폴링
        self._json({"job": job})


def main():
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    start_bot()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

    def _usage_loop():
        while True:
            time.sleep(60)
            try:
                get_usage()
            except Exception:                               # noqa: BLE001
                pass
    threading.Thread(target=_usage_loop, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        sys.exit(f"포트 {PORT}가 이미 사용 중 — 기존 서버를 종료(Ctrl-C)한 뒤 다시 실행하세요.")
    print(f"에이전트 맵: http://127.0.0.1:{PORT}/  (Ctrl-C로 종료 — 봇도 함께 내려간다)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
