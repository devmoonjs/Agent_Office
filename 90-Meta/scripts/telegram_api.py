#!/usr/bin/env python3
"""Telegram Bot API 얇은 래퍼 — stdlib 전용 (review-ui/server.py와 같은 방침).

설정은 `90-Meta/.env`에서 읽는다 (이 파일은 .gitignore 대상):
  TELEGRAM_BOT_TOKEN=<BotFather 발급 토큰>
  TELEGRAM_ALLOWED_CHAT_IDS=<쉼표 구분 chat_id — 본인만>

보안: 허용 chat_id가 비어 있으면 봇은 기동하지 않는다(fail closed).
본인 chat_id를 모르면 봇을 띄운 뒤 아무 메시지나 보내면 콘솔에 출력된다.
"""
import html
import json
import re
import shutil
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "90-Meta" / ".env"
MSG_LIMIT = 3900          # 텔레그램 4096자 제한 — 여유를 두고 분할


def load_env():
    cfg = {}
    if ENV.is_file():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


_cfg = load_env()
TOKEN = _cfg.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in _cfg.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
# 친구 등급: 파일 접수만 가능. 텍스트는 명령 실행 경로(run_claude/run_codex)에 닿지 않는다.
# ALLOWED(전권)와 절대 합치지 말 것 — ALLOWED는 이 맥에서 임의 명령 실행과 동급이다.
FRIENDS = {c.strip() for c in _cfg.get("TELEGRAM_FRIEND_CHAT_IDS", "").split(",") if c.strip()}


def _load_contacts():
    """TELEGRAM_CONTACTS='철수:12345,영희:67890' → {'철수': '12345', ...} (발송 대상 지정용)."""
    out = {}
    for pair in _cfg.get("TELEGRAM_CONTACTS", "").split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


CONTACTS = _load_contacts()

# 동적 친구 명부 — 소유자가 텔레그램에서 `등록 <chat_id> <별명>`으로 추가한다.
# .env(정적)와 달리 편집·재시작 없이 즉시 반영되며, 매 조회 시 파일을 다시 읽는다.
REG = ROOT / ".cache" / "telegram-friends.json"


def load_friends():
    if REG.is_file():
        try:
            return json.loads(REG.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_friends(reg):
    REG.parent.mkdir(parents=True, exist_ok=True)
    REG.write_text(json.dumps(reg, ensure_ascii=False, indent=1))


def is_friend(chat_id):
    return str(chat_id) in FRIENDS or str(chat_id) in load_friends()


def friend_name(chat_id):
    return load_friends().get(str(chat_id), {}).get("name", "")


def resolve_contact(name):
    """발송 대상 이름 → chat_id. .env CONTACTS 우선, 동적 명부 별명 폴백."""
    if name in CONTACTS:
        return CONTACTS[name]
    for cid, info in load_friends().items():
        if info.get("name") == name:
            return cid
    return None


def contact_names():
    return sorted(set(CONTACTS)
                  | {i.get("name") for i in load_friends().values() if i.get("name")})
# 로컬 Bot API 서버를 띄우면 20MB 다운로드 한도가 사라진다 (TELEGRAM.md '큰 파일' 절 참조)
API = _cfg.get("TELEGRAM_API_BASE", "").rstrip("/") or "https://api.telegram.org"
IS_LOCAL_API = "api.telegram.org" not in API
# 로컬 서버를 docker로 돌릴 때: getFile이 주는 절대 경로는 컨테이너 안 경로다.
# 볼륨 마운트에 맞춰 '<컨테이너 접두어>:<호스트 접두어>'로 치환한다.
FILE_REMAP = _cfg.get("TELEGRAM_FILE_REMAP", "")


# ── 마크다운 → 텔레그램 HTML ─────────────────────────────────
# 텔레그램은 제목(#)과 표(|)를 지원하지 않는다. 그대로 보내면 원문 기호가 노출되고
# 표는 폰 화면에서 줄바꿈으로 뭉개진다. 표는 행 단위 목록으로 접고, 제목은 굵게 바꾼다.
_TAG = re.compile(r"<[^>]+>")


def _is_sep(row):
    """마크다운 표의 구분선 행인가 (|---|:---:|)."""
    cells = [c.strip() for c in row if c.strip()]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _table_to_lines(rows):
    """[[셀,...], ...] → '• 첫칸 — 헤더: 값 · 헤더: 값' 목록. 헤더 행은 라벨로 쓴다."""
    if not rows:
        return []
    if len(rows) >= 2 and _is_sep(rows[1]):
        head, body = rows[0], rows[2:]
    else:
        head, body = None, [r for r in rows if not _is_sep(r)]
    out = []
    for r in body:
        cells = [c.strip() for c in r]
        if not any(cells):
            continue
        first, rest = cells[0], cells[1:]
        parts = []
        for i, v in enumerate(rest):
            if not v or v == "—":
                continue
            label = head[i + 1].strip() if head and i + 1 < len(head) else ""
            parts.append(f"{label}: {v}" if label else v)
        out.append(f"• <b>{first}</b>" + (" — " + " · ".join(parts) if parts else ""))
    return out


def md_to_html(text):
    """마크다운 답변을 텔레그램 HTML 파스모드용으로 변환한다."""
    blocks = []                                 # 코드펜스는 변환에서 보호

    def stash(m):
        blocks.append(m.group(1))
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```[\w-]*\n(.*?)```", stash, text, flags=re.S)
    text = html.escape(text, quote=False)

    out, table = [], []
    for line in text.split("\n"):
        if line.strip().startswith("|") and line.count("|") >= 2:
            table.append([c for c in line.strip().strip("|").split("|")])
            continue
        if table:
            out += _table_to_lines(table)
            table = []
        s = line.rstrip()
        if re.fullmatch(r"\s*([-*_]\s*){3,}", s):        # 구분선 제거
            continue
        h = re.match(r"^(#{1,6})\s+(.*)", s)
        if h:
            out += ["", f"<b>{h.group(2).strip()}</b>"]
            continue
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)        # 불릿 통일
        s = re.sub(r"^\s*>\s?", "", s)                   # 인용 기호 제거
        out.append(s)
    if table:
        out += _table_to_lines(table)

    t = "\n".join(out)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    for i, b in enumerate(blocks):
        t = t.replace(f"\x00{i}\x00", f"<pre>{html.escape(b, quote=False)}</pre>")
    return t


def strip_html(t):
    """파스모드 전송이 실패했을 때 쓰는 평문 폴백."""
    return html.unescape(_TAG.sub("", t))


def _call(method, payload=None, timeout=70):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 미설정 — 90-Meta/.env 확인")
    req = urllib.request.Request(
        f"{API}/bot{TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"{method} 실패: {str(out)[:200]}")
    return out["result"]


def split_message(text):
    """텔레그램 길이 제한에 맞춰 분할. 가능하면 줄 경계를 지킨다."""
    text = text if text.strip() else "(내용 없음)"
    parts, buf = [], ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > MSG_LIMIT:
            parts.append(buf)
            buf = ""
        while len(line) > MSG_LIMIT:            # 한 줄이 한도를 넘으면 강제 분할
            parts.append(line[:MSG_LIMIT])
            line = line[MSG_LIMIT:]
        buf += line
    if buf:
        parts.append(buf)
    return parts


def send(chat_id, text, convert=True):
    """마크다운을 텔레그램용으로 변환해 전송한다. 길면 분할하고, 실패해도 루프를 죽이지 않는다.

    분할 지점에서 태그가 갈리면 파스모드 전송이 400으로 실패할 수 있으므로,
    실패한 조각만 태그를 걷어내고 평문으로 재전송한다."""
    body = md_to_html(text) if convert else text
    for p in split_message(body):
        try:
            _call("sendMessage", {"chat_id": chat_id, "text": p, "parse_mode": "HTML",
                                  "disable_web_page_preview": True})
            continue
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            print(f"[telegram] HTML 전송 실패, 평문 재시도: {str(e)[:120]}")
        try:
            _call("sendMessage", {"chat_id": chat_id, "text": strip_html(p),
                                  "disable_web_page_preview": True})
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            print(f"[telegram] 전송 실패: {str(e)[:150]}")


def send_document(chat_id, path, caption=""):
    """파일을 sendDocument로 업로드한다 (multipart/form-data, 발송 한도 50MB).

    _call()은 JSON 전용이라 쓸 수 없다. 파트 선두부는 본문에 UTF-8로 들어가므로
    한글 파일명도 그대로 전송된다."""
    path = Path(path)
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 미설정 — 90-Meta/.env 확인")
    if not path.is_file():
        raise RuntimeError(f"파일 없음: {path}")
    limit_mb = 2000 if IS_LOCAL_API else 50
    if path.stat().st_size > limit_mb * 1024 * 1024:
        raise RuntimeError(f"{limit_mb}MB 초과로 봇 API로 보낼 수 없음: {path.name}")
    boundary = "----myjarvis" + uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f'name="{name}"\r\n\r\n{value}\r\n').encode())

    field("chat_id", chat_id)
    if caption:
        field("caption", caption[:1000])
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
                  f'filename="{path.name}"\r\n'
                  f"Content-Type: application/octet-stream\r\n\r\n").encode()
                 + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API}/bot{TOKEN}/sendDocument", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"sendDocument 실패: {str(out)[:200]}")
    return out["result"]


def get_updates(offset, timeout=50):
    return _call("getUpdates", {"offset": offset, "timeout": timeout},
                 timeout=timeout + 20)


class TooBig(RuntimeError):
    """봇 API 다운로드 한도(공식 서버 20MB) 초과."""


def download(file_id, dest: Path):
    """getFile → 실제 파일 다운로드. 한도 초과는 TooBig으로 구분해 올린다."""
    try:
        info = _call("getFile", {"file_id": file_id})
    except (RuntimeError, urllib.error.HTTPError) as e:
        if "too big" in str(e).lower() or "400" in str(e):
            raise TooBig(str(e)[:200]) from e
        raise
    fp = info["file_path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fp.startswith("/"):
        # 로컬 Bot API 서버(--local)는 URL이 아니라 서버 디스크의 절대 경로를 준다.
        # docker 구동이면 컨테이너 안 경로이므로 마운트된 호스트 경로로 치환한다
        if FILE_REMAP and ":" in FILE_REMAP:
            src, dst = FILE_REMAP.split(":", 1)
            if fp.startswith(src):
                fp = dst + fp[len(src):]
        shutil.copyfile(fp, dest)
        return dest
    with urllib.request.urlopen(f"{API}/file/bot{TOKEN}/{fp}", timeout=600) as r, \
            open(dest, "wb") as fh:
        while chunk := r.read(65536):
            fh.write(chunk)
    return dest
