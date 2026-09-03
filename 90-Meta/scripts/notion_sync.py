#!/usr/bin/env python3
"""my_jarvis 산출물(10-Daily / 15-Reports)을 Notion MyOrg로 자동 적재한다.

stdlib 전용. Notion 공개 REST API(pages.create)만 사용하므로 모든 요금제에서 무료다.

동작:
  - 10-Daily/<날짜>.md       → Daily Report DB (하루 1행). '## 1. 개요'를 '한 일'로,
                               본문에서 발견된 repo를 '관련 프로젝트' 관계로 연결.
  - 15-Reports/<repo>/<날짜>.md → '일별 개발 현황/이슈' DS (프로젝트 1:1).
                               frontmatter의 project(또는 폴더명)로 프로젝트 관계,
                               '## 요약'을 본문에, '## 다음 예정 작업'을 '다음 액션'에.

멱등성: 적재한 파일의 경로→page_id를 .cache/notion-sync.json에 기록해 재실행 시 건너뛴다.
        파일이 수정되면(mtime 변경) 다시 올리지 않는다 — 신규 파일만 적재한다.

사용법:
  NOTION_TOKEN=secret_xxx python3 notion_sync.py [--dry-run] [--once <파일경로>]
  (토큰은 90-Meta/.env의 NOTION_TOKEN으로도 읽는다)

launchd WatchPaths 모드에서는 인자 없이 실행 — 두 폴더를 스캔해 미적재분만 올린다.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

# ── 경로 ────────────────────────────────────────────────────────────────────
VAULT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # …/my_jarvis
DAILY_DIR = os.path.join(VAULT, "10-Daily")
REPORTS_DIR = os.path.join(VAULT, "15-Reports")
STATE_FILE = os.path.join(VAULT, ".cache", "notion-sync.json")
ENV_FILE = os.path.join(VAULT, "90-Meta", ".env")
LOG_FILE = os.path.join(VAULT, ".cache", "notion-sync.log")

# ── Notion 대상 ──────────────────────────────────────────────────────────────
NOTION_VERSION = "2025-09-03"  # data_source_id 부모 지원 버전
API_PAGES = "https://api.notion.com/v1/pages"

DAILY_REPORT_DS = "d70db37a-4b2e-4aa6-befe-b874f30a163a"   # Daily Report DB
DEV_STATUS_DS = "aea2cfc1-57eb-4134-8c3f-bf16da4b7d0c"     # 일별 개발 현황/이슈

# 로컬 폴더명 / repo 식별자 → Notion 프로젝트 page id
PROJECT_PAGE = {
    "chat_bot": "386bcf97-d36a-8062-bae6-da77ceba39f8",          # 교육운영 AI 챗봇 서비스
    "Lego-React": "386bcf97-d36a-80e0-b8d3-e126acd031d8",        # LEGO
    "predictive-finance": "386bcf97-d36a-80f6-a552-c83e8b9ae033", # 예측 회계 시스템
    "my_jarvis": "386bcf97-d36a-809d-94c9-c594ee61e0c7",          # Project J
}
# 데일리 본문에서 repo를 탐지하기 위한 별칭(소문자 비교)
REPO_ALIASES = {
    "chat_bot": "chat_bot",
    "lego-react": "Lego-React",
    "lego-spring": "Lego-React",   # 같은 LEGO 프로젝트로 귀속
    "predictive-finance": "predictive-finance",
    "my_jarvis": "my_jarvis",
}

TEXT_LIMIT = 1900  # Notion rich_text/제목 단일 문자열 상한(2000) 여유


def log(msg):
    line = msg.rstrip()
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_token():
    tok = os.environ.get("NOTION_TOKEN")
    if tok:
        return tok.strip()
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("NOTION_TOKEN") and "=" in ln:
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ── 파싱 ────────────────────────────────────────────────────────────────────
def parse_md(path):
    """frontmatter dict와 body(str)를 반환한다."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            for ln in block.splitlines():
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm, body, text


def section(body, header):
    """'## header'부터 다음 '## ' 직전까지의 본문을 반환한다(헤더 줄 제외)."""
    lines = body.splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.startswith("## "):
            if capturing:
                break
            # '## 1. 개요'처럼 번호가 붙어도 매칭되도록 헤더 문자열 포함 검사
            if header in ln:
                capturing = True
                continue
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def first_h1(body):
    for ln in body.splitlines():
        if ln.startswith("# "):
            return ln[2:].strip()
    return None


def clip(s, n=TEXT_LIMIT):
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def detect_repos(body):
    """그날 실제 변경한 repo를 폴더명 리스트로 반환한다.

    '## 1. 개요'의 'Repo' 표 첫 칸을 우선 사용한다(전일 액션 이월 등 본문 언급의
    과탐을 피하기 위함). 표가 없으면 개요 텍스트만 스캔하는 폴백을 쓴다.
    """
    found = []
    lines = body.splitlines()
    in_table = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("|") and "repo" in s.lower():
            in_table = True
            continue
        if in_table:
            if not s.startswith("|"):
                break
            if set(s) <= set("|-: "):  # 구분선 |---|
                continue
            cells = [c.strip().strip("`") for c in s.strip("|").split("|")]
            if cells:
                key = cells[0].lower()
                folder = REPO_ALIASES.get(key)
                if folder and folder not in found:
                    found.append(folder)
    if found:
        return found
    # 폴백: 개요 섹션 텍스트만 스캔(전체 본문 스캔은 과탐이므로 지양)
    scope = (section(body, "개요") or body[:600]).lower()
    for alias, folder in REPO_ALIASES.items():
        if alias in scope and folder not in found:
            found.append(folder)
    return found


def rich_text(s):
    s = (s or "").strip()
    return [{"type": "text", "text": {"content": clip(s)}}] if s else []


# ── 마크다운 → Notion 블록 변환 (원본 전체 적재) ─────────────────────────────
_LANG = {
    "": "plain text", "txt": "plain text", "text": "plain text",
    "js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
    "py": "python", "sh": "shell", "bash": "shell", "zsh": "shell",
    "yml": "yaml", "yaml": "yaml", "json": "json", "sql": "sql", "diff": "diff",
    "html": "html", "css": "css", "dockerfile": "docker", "docker": "docker",
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "java": "java", "go": "go", "rust": "rust", "c": "c", "cpp": "c++",
    "markdown": "markdown", "md": "markdown", "xml": "xml",
}


def _lang(tag):
    return _LANG.get(tag.strip().lower(), "plain text")


def _chunks(s, size=1900):
    """긴 문자열을 size 이하 rich_text 항목 리스트로 분할."""
    s = s or ""
    out = [{"type": "text", "text": {"content": s[i:i + size]}}
           for i in range(0, len(s), size)]
    return out or [{"type": "text", "text": {"content": ""}}]


def inline_rt(s):
    """한 줄의 인라인 마크다운(**굵게**, `코드`)을 rich_text 배열로 변환."""
    s = s.rstrip("\n")
    rt = []
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", s):
        if m.start() > pos:
            rt += _chunks(s[pos:m.start()])
        if m.group(1) is not None:
            for it in _chunks(m.group(1)):
                it["annotations"] = {"bold": True}
                rt.append(it)
        else:
            for it in _chunks(m.group(2)):
                it["annotations"] = {"code": True}
                rt.append(it)
        pos = m.end()
    if pos < len(s):
        rt += _chunks(s[pos:])
    return rt or [{"type": "text", "text": {"content": ""}}]


def _table_block(tbl_lines):
    rows = []
    for ln in tbl_lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and set("".join(cells)) <= set("-: "):
            continue  # |---| 구분선
        rows.append(cells)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    trows = []
    for r in rows:
        r = r + [""] * (width - len(r))
        trows.append({"object": "block", "type": "table_row",
                      "table_row": {"cells": [inline_rt(c) for c in r]}})
    return {"object": "block", "type": "table", "table": {
        "table_width": width, "has_column_header": True,
        "has_row_header": False, "children": trows}}


def md_to_blocks(body):
    """마크다운 본문 전체를 Notion 블록 리스트로 변환한다(요약 없이 원본 그대로)."""
    lines = body.split("\n")
    blocks, i, n = [], 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s == "":
            i += 1
            continue
        if s.startswith("```"):                       # 코드 펜스
            lang = _lang(s[3:])
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append({"object": "block", "type": "code",
                           "code": {"language": lang, "rich_text": _chunks("\n".join(buf))}})
            continue
        if s.startswith("|"):                          # 표
            buf = []
            while i < n and lines[i].strip().startswith("|"):
                buf.append(lines[i].strip())
                i += 1
            blk = _table_block(buf)
            if blk:
                blocks.append(blk)
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)            # 제목
        if m:
            lvl = min(len(m.group(1)), 3)
            blocks.append({"object": "block", "type": f"heading_{lvl}",
                           f"heading_{lvl}": {"rich_text": inline_rt(m.group(2))}})
            i += 1
            continue
        if s.startswith(">"):                           # 인용
            blocks.append({"object": "block", "type": "quote",
                           "quote": {"rich_text": inline_rt(s[1:].strip())}})
            i += 1
            continue
        if re.match(r"[-*]\s+", s):                     # 불릿
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": inline_rt(re.sub(r"^[-*]\s+", "", s))}})
            i += 1
            continue
        if re.match(r"\d+\.\s+", s):                    # 번호 목록
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": inline_rt(re.sub(r"^\d+\.\s+", "", s))}})
            i += 1
            continue
        if len(s) >= 3 and set(s) <= set("-"):          # 구분선
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        blocks.append({"object": "block", "type": "paragraph",   # 문단
                       "paragraph": {"rich_text": inline_rt(s)}})
        i += 1
    return blocks


# ── 페이로드 빌드 ────────────────────────────────────────────────────────────
def daily_payload(path):
    fm, body, text = parse_md(path)
    date = fm.get("created") or fm.get("date") or os.path.splitext(os.path.basename(path))[0]
    gae_yo = section(body, "개요") or first_h1(body) or "(개요 없음)"
    repos = detect_repos(body)
    relation = [{"id": PROJECT_PAGE[r]} for r in repos if r in PROJECT_PAGE]

    props = {
        "날짜": {"title": rich_text(date)},
        "일자": {"date": {"start": date}},
        "한 일": {"rich_text": rich_text(gae_yo)},
        "관련 링크": {"url": "file://" + path},
    }
    if relation:
        props["관련 프로젝트"] = {"relation": relation}

    children = [{
        "object": "block", "type": "quote",
        "quote": {"rich_text": [{"type": "text",
                  "text": {"content": clip("원본: " + os.path.relpath(path, VAULT))}}]},
    }] + md_to_blocks(body)
    return {"parent": {"type": "data_source_id", "data_source_id": DAILY_REPORT_DS},
            "properties": props, "children": children}


def report_payload(path):
    fm, body, text = parse_md(path)
    date = fm.get("created") or fm.get("date") or os.path.splitext(os.path.basename(path))[0]
    repo = fm.get("project") or os.path.basename(os.path.dirname(path))
    page_id = PROJECT_PAGE.get(repo)
    title = first_h1(body) or f"[{repo}] {date} 변경 보고"
    summary = section(body, "요약")
    next_actions = section(body, "다음 예정 작업")
    next_actions = re.sub(r"^[-*]\s*", "", next_actions, flags=re.M).replace("\n", "; ").strip()

    props = {
        "기록": {"title": rich_text(title)},
        "날짜": {"date": {"start": date}},
        "유형": {"select": {"name": "개발 현황"}},
        "상태": {"select": {"name": "진행중"}},
        "관련 링크": {"url": "file://" + path},
    }
    if page_id:
        props["프로젝트"] = {"relation": [{"id": page_id}]}
    if next_actions:
        props["다음 액션"] = {"rich_text": rich_text(next_actions)}

    children = [{
        "object": "block", "type": "quote",
        "quote": {"rich_text": [{"type": "text",
                  "text": {"content": clip("원본: " + os.path.relpath(path, VAULT))}}]},
    }] + md_to_blocks(body)
    return {"parent": {"type": "data_source_id", "data_source_id": DEV_STATUS_DS},
            "properties": props, "children": children}


# ── Notion 호출 ──────────────────────────────────────────────────────────────
_SCHEMA_CACHE = {}


def ds_property_names(token, ds_id):
    """데이터소스의 실제 속성명 집합을 반환(캐시). 조회 실패 시 None(→ 필터 생략)."""
    if ds_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[ds_id]
    url = "https://api.notion.com/v1/data_sources/" + ds_id
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Notion-Version": NOTION_VERSION,
    })
    names = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = set(data.get("properties", {}).keys())
    except Exception:
        names = None
    _SCHEMA_CACHE[ds_id] = names
    return names


def filter_props(token, payload, key):
    """페이로드 속성을 데이터소스 실제 스키마에 맞춰 정리(없는 속성 제외)."""
    ds_id = payload["parent"]["data_source_id"]
    known = ds_property_names(token, ds_id)
    if not known:
        return
    dropped = [k for k in list(payload["properties"]) if k not in known]
    for k in dropped:
        payload["properties"].pop(k)
    if dropped:
        log(f"[WARN] {key}: 데이터소스에 없는 속성 제외 {dropped}")


def _api(token, url, payload, method="POST"):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method=method, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_page(token, payload):
    """페이지를 생성한다. children은 100블록 상한이 있어 초과분은 append로 이어붙인다."""
    children = payload.get("children", [])
    payload["children"] = children[:100]
    res = _api(token, API_PAGES, payload)
    rest = children[100:]
    page_id = res.get("id")
    while rest and page_id:
        batch, rest = rest[:100], rest[100:]
        _api(token, f"https://api.notion.com/v1/blocks/{page_id}/children",
             {"children": batch}, method="PATCH")
    return res


# ── 메인 ────────────────────────────────────────────────────────────────────
def iter_targets(once=None):
    """(상대키, 절대경로, 종류) 튜플을 생성한다. 종류: 'daily' | 'report'."""
    if once:
        p = os.path.abspath(once)
        if p.startswith(DAILY_DIR):
            yield os.path.relpath(p, VAULT), p, "daily"
        elif p.startswith(REPORTS_DIR):
            yield os.path.relpath(p, VAULT), p, "report"
        return
    for name in sorted(os.listdir(DAILY_DIR)) if os.path.isdir(DAILY_DIR) else []:
        if name.endswith(".md"):
            p = os.path.join(DAILY_DIR, name)
            yield os.path.relpath(p, VAULT), p, "daily"
    if os.path.isdir(REPORTS_DIR):
        for repo in sorted(os.listdir(REPORTS_DIR)):
            d = os.path.join(REPORTS_DIR, repo)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".md"):
                    p = os.path.join(d, name)
                    yield os.path.relpath(p, VAULT), p, "report"


def main(argv):
    dry = "--dry-run" in argv
    once = None
    if "--once" in argv:
        i = argv.index("--once")
        once = argv[i + 1] if i + 1 < len(argv) else None

    token = load_token()
    if not token and not dry:
        log("[ERROR] NOTION_TOKEN 미설정 — 90-Meta/.env에 NOTION_TOKEN=secret_xxx 추가 필요")
        return 1

    state = load_state()
    created = 0
    for key, path, kind in iter_targets(once):
        if key in state:
            continue
        try:
            payload = daily_payload(path) if kind == "daily" else report_payload(path)
        except Exception as e:  # 파싱 실패는 건너뛰되 로그
            log(f"[SKIP] 파싱 실패 {key}: {e}")
            continue
        if dry:
            log(f"[DRY] {kind:6s} {key}  →  관계 {payload['properties'].get('관련 프로젝트') or payload['properties'].get('프로젝트')}")
            continue
        try:
            filter_props(token, payload, key)
            res = create_page(token, payload)
            state[key] = res.get("id", "created")
            save_state(state)
            created += 1
            log(f"[OK] {kind:6s} {key}  →  {res.get('url', '')}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            log(f"[FAIL] {key}: HTTP {e.code} {body[:300]}")
        except urllib.error.URLError as e:
            log(f"[FAIL] {key}: {e}")
    if not dry:
        log(f"[DONE] 신규 적재 {created}건")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
