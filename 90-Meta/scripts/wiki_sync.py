#!/usr/bin/env python3
"""20-Wiki → Neo4j 단방향 동기화 (Obsidian이 Source of Truth).

온톨로지 관점의 추출물:
  Concept 노드      노트 1개 = 개념 1개. kind(pattern|concept|tool)로 종류를 구분한다
  Topic 노드        MOC-llm.md의 `###` 하위 섹션 = 주제 분류
  IN_TOPIC          (Concept)→(Topic)   MOC 등재 기준
  RELATED_TO        (Concept)→(Concept) 본문 [[백링크]] 기준
  DISCUSSED         (Meeting)→(Concept) frontmatter source가 회의록인 경우

설계 원칙 (20-Wiki/patterns/bi-directional-sync-obsidian-neo4j.md):
  - 단방향만 한다. Neo4j에서 노트를 고쳐 되돌리지 않는다
  - 추가·갱신만 자동. 삭제는 보고만 하고 사람이 판단한다
    (노트가 사라져도 /ingest가 만든 관계가 남아 있을 수 있다)
  - 전부 MERGE 기반이라 재실행해도 중복이 생기지 않는다

사용법:
  python3 90-Meta/scripts/wiki_sync.py [--dry-run] [--quiet]
"""
import argparse
import base64
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "20-Wiki"
MOC = WIKI / "MOC-llm.md"
NEO4J_HTTP = "http://localhost:57474"
SRC = "wiki-sync"
KINDS = {"pattern", "concept", "tool"}

# 노트 출처 repo → 사업(Project.name).
# 코드 리뷰(/daily-review)에서 도출된 개념을 그 코드가 속한 사업에 귀속시킨다.
REPO_PROJECT = {
    # 예시 — repo명: 사업(Project.name)
    "my-service": "내 서비스 프로젝트",
}
# 대응하는 Lego 사업이 없는 repo — 적재하지 않고 보고만 한다 (조용한 누락 방지)
REPO_NO_PROJECT = {"my_jarvis"}


def source_repo(src):
    """source 문자열에서 repo명을 뽑는다. 'repo@해시' 접두가 기본이고,
    문장 안에 repo 경로가 섞인 형태('구두 요청(...) / my-service src/...')도 잡는다."""
    if not isinstance(src, str) or not src.strip():
        return None
    s = src.strip().strip('"')
    m = re.match(r"^([A-Za-z0-9_\-]+)[@ ]", s)
    if m and (m.group(1) in REPO_PROJECT or m.group(1) in REPO_NO_PROJECT):
        return m.group(1)
    if s.startswith(("00-Inbox/", "60-Sources/", "vault-init")):
        return None                                    # 문서 출처 — repo가 아니다
    for repo in sorted(set(REPO_PROJECT) | REPO_NO_PROJECT, key=len, reverse=True):
        if repo in s:                                  # 긴 이름 우선 (…_v2가 …에 먹히지 않도록)
            return repo
    return m.group(1) if m else None                   # 미등록 repo → 상위에서 보고


def cypher(stmts):
    pw = ""
    for line in (ROOT / "90-Meta" / "neo4j" / ".env").read_text().splitlines():
        if line.startswith("NEO4J_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
    req = urllib.request.Request(
        f"{NEO4J_HTTP}/db/neo4j/tx/commit",
        data=json.dumps({"statements": [{"statement": s} for s in stmts]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(f"neo4j:{pw}".encode()).decode()})
    out = json.loads(urllib.request.urlopen(req, timeout=60).read())
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:400])
    return out


def esc(s):
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def frontmatter(text):
    """--- 블록의 단순 key: value 파싱 (stdlib만 사용 — yaml 의존 없음)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
        fm[k] = v
    return fm, text[end + 4:]


def parse_moc():
    """MOC의 `###` 하위 섹션 → {노트명: 주제}. 섹션이 없는 `##` 아래 항목은 주제 없음."""
    topics = {}
    if not MOC.is_file():
        return topics
    cur = None
    for line in MOC.read_text().splitlines():
        h3 = re.match(r"^###\s+(.*)", line)
        h2 = re.match(r"^##\s+(.*)", line)
        if h3:
            cur = h3.group(1).strip()
            continue
        if h2:
            cur = None                       # 상위 섹션은 kind와 중복이라 주제로 쓰지 않는다
            continue
        if cur:
            for name in re.findall(r"\[\[([^\]|#]+)", line):
                topics[name.strip()] = cur
    return topics


def scan():
    notes, links, meetings = {}, [], []
    topics = parse_moc()
    for p in sorted(WIKI.rglob("*.md")):
        if p.stem.startswith("MOC-"):
            continue
        fm, body = frontmatter(p.read_text())
        kind = fm.get("type") if fm.get("type") in KINDS else None
        aliases = fm.get("aliases") if isinstance(fm.get("aliases"), list) else []
        raw_src = fm.get("source", "")
        notes[p.stem] = {
            "name": p.stem, "kind": kind or "concept",
            "wiki_path": str(p.relative_to(ROOT)),
            "aliases": [a for a in aliases if a],
            "created": fm.get("created", ""), "confidence": fm.get("confidence", ""),
            "topic": topics.get(p.stem, ""),
            "repo": source_repo(raw_src) if isinstance(raw_src, str) else None,
            "source": raw_src if isinstance(raw_src, str) else "",
        }
        body = re.sub(r"```.*?```", "", body, flags=re.S)      # 코드블록 내 링크 제외
        for tgt in re.findall(r"\[\[([^\]|#]+)", body):
            tgt = tgt.strip()
            if tgt and tgt != p.stem:
                links.append((p.stem, tgt, str(p.relative_to(ROOT))))
        src = fm.get("source", "")
        if isinstance(src, str) and "meetings/" in src:
            mid = Path(src.split()[0].strip('"')).stem
            meetings.append((mid, p.stem))
    return notes, links, meetings


def build(notes, links, meetings, known_meetings, known_projects):
    today = date.today().isoformat()
    stmts, stats = [], defaultdict(int)

    for n in notes.values():
        sets = [f"c.kind = '{esc(n['kind'])}'", f"c.wiki_path = '{esc(n['wiki_path'])}'",
                f"c.synced_at = '{today}'"]
        if n["aliases"]:
            sets.append("c.aliases = [" + ", ".join(f"'{esc(a)}'" for a in n["aliases"]) + "]")
        for k in ("created", "confidence"):
            if n[k]:
                sets.append(f"c.{'note_' + k if k == 'confidence' else k} = '{esc(n[k])}'")
        stmts.append(f"MERGE (c:Concept {{name: '{esc(n['name'])}'}}) SET " + ", ".join(sets))
        stats["concept"] += 1

    for t in sorted({n["topic"] for n in notes.values() if n["topic"]}):
        stmts.append(f"MERGE (t:Topic {{name: '{esc(t)}'}}) SET t.source = '{SRC}'")
        stats["topic"] += 1
    for n in notes.values():
        if not n["topic"]:
            continue
        stmts.append(
            f"MATCH (c:Concept {{name: '{esc(n['name'])}'}}), (t:Topic {{name: '{esc(n['topic'])}'}}) "
            f"MERGE (c)-[r:IN_TOPIC]->(t) "
            f"SET r.source = '{SRC}', r.recorded_at = '{today}', r.confidence = 'direct'")
        stats["in_topic"] += 1

    dangling = []
    for a, b, path in links:
        if b not in notes:
            dangling.append((a, b))
            continue
        stmts.append(
            f"MATCH (a:Concept {{name: '{esc(a)}'}}), (b:Concept {{name: '{esc(b)}'}}) "
            f"MERGE (a)-[r:RELATED_TO]->(b) "
            f"SET r.source = '{esc(path)}', r.recorded_at = '{today}', r.confidence = 'direct'")
        stats["related_to"] += 1

    # 개념 → 사업 귀속 (repo 출처 기준). 없는 Project를 새로 만들지 않는다
    unmapped = defaultdict(list)
    for n in notes.values():
        repo = n["repo"]
        if not repo:
            continue
        proj = REPO_PROJECT.get(repo)
        if not proj:
            unmapped[repo].append(n["name"])
            continue
        if proj not in known_projects:
            stats["project_missing"] += 1
            continue
        stmts.append(
            f"MATCH (c:Concept {{name: '{esc(n['name'])}'}}), (p:Project {{name: '{esc(proj)}'}}) "
            f"MERGE (c)-[r:DERIVED_FROM]->(p) "
            f"SET r.repo = '{esc(repo)}', r.source = '{esc(n['source'])[:200]}', "
            f"r.recorded_at = '{today}', r.confidence = 'direct'")
        stats["derived_from"] += 1

    for mid, cname in meetings:
        if mid not in known_meetings:              # 없는 Meeting을 새로 만들지 않는다
            stats["meeting_missing"] += 1
            continue
        stmts.append(
            f"MATCH (m:Meeting {{id: '{esc(mid)}'}}), (c:Concept {{name: '{esc(cname)}'}}) "
            f"MERGE (m)-[r:DISCUSSED]->(c) "
            f"SET r.source = '{SRC}', r.recorded_at = '{today}', r.confidence = 'direct'")
        stats["discussed"] += 1
    return stmts, stats, dangling, unmapped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    notes, links, meetings = scan()
    pre = cypher(["MATCH (m:Meeting) RETURN collect(m.id)",
                  "MATCH (c:Concept) WHERE c.wiki_path IS NOT NULL RETURN collect(c.name)",
                  "MATCH (c:Concept) RETURN count(c)",
                  "MATCH (p:Project) RETURN collect(p.name)"])
    known_meetings = set(pre["results"][0]["data"][0]["row"][0])
    synced_before = set(pre["results"][1]["data"][0]["row"][0])
    before = pre["results"][2]["data"][0]["row"][0]
    known_projects = set(pre["results"][3]["data"][0]["row"][0])

    stmts, stats, dangling, unmapped = build(notes, links, meetings,
                                             known_meetings, known_projects)
    # 삭제는 자동으로 하지 않는다 — 노트가 사라진 Concept은 보고만 한다
    orphans = sorted(synced_before - set(notes))

    if not args.dry_run:
        for i in range(0, len(stmts), 100):
            cypher(stmts[i:i + 100])
    post = cypher(["MATCH (c:Concept) RETURN count(c)",
                   "MATCH ()-[r:RELATED_TO]->() RETURN count(r)",
                   "MATCH (t:Topic) RETURN count(t)",
                   "MATCH (:Concept)-[r:DERIVED_FROM]->(p:Project) "
                   "RETURN p.name, count(r) ORDER BY count(r) DESC"])
    g = lambda i: post["results"][i]["data"][0]["row"][0]

    lines = [
        f"위키 동기화{' (dry-run)' if args.dry_run else ''} — 노트 {len(notes)}개 스캔",
        f"Concept {before}→{g(0)}개 · RELATED_TO {g(1)}개 · Topic {g(2)}개",
        f"세부: 개념 {stats['concept']} · 주제연결 {stats['in_topic']} · "
        f"링크 {stats['related_to']} · 회의연결 {stats['discussed']} · "
        f"사업귀속 {stats['derived_from']}",
    ]
    byproj = post["results"][3]["data"]
    if byproj:
        lines.append("사업별 개념: " + " · ".join(f"{r['row'][0]} {r['row'][1]}" for r in byproj))
    if unmapped:
        lines.append("매핑 없는 repo — 적재 안 함: " +
                     ", ".join(f"{k}({len(v)}건)" for k, v in sorted(unmapped.items())))
    if stats["project_missing"]:
        lines.append(f"그래프에 없는 사업 참조 {stats['project_missing']}건 — 건너뜀")
    if dangling:
        uniq = sorted({b for _, b in dangling})
        lines.append(f"대상 없는 링크 {len(uniq)}종 (스텁 후보): {', '.join(uniq[:5])}")
    if orphans:
        lines.append(f"노트가 사라진 Concept {len(orphans)}건 — 자동 삭제하지 않음: "
                     f"{', '.join(orphans[:5])}")
    if stats["meeting_missing"]:
        lines.append(f"그래프에 없는 회의 참조 {stats['meeting_missing']}건 — 건너뜀")
    out = "\n".join(lines)
    if not args.quiet:
        print(out)
    return out


if __name__ == "__main__":
    main()
