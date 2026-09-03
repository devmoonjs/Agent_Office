#!/usr/bin/env python3
"""UserPromptSubmit 훅 — 프롬프트에 그래프 등재 엔터티(인물/조직/프로젝트)가 감지되면
Neo4j 질의 리마인더를 컨텍스트로 주입한다 (CLAUDE.md '그래프 자동 활용' 규칙의 강제 장치).

- 엔터티 이름은 Neo4j에서 조회하고 .cache/graph-names.json에 캐시(TTL 6시간)
- Neo4j가 내려가 있으면 캐시로 폴백, 캐시도 없으면 조용히 통과
- 훅 실패가 프롬프트 처리를 막지 않도록 모든 예외를 삼키고 exit 0
"""
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache" / "graph-names.json"
CACHE_TTL = 6 * 3600
NEO4J_HTTP = "http://localhost:57474"


def fetch_names():
    pw = ""
    for line in (ROOT / "90-Meta" / "neo4j" / ".env").read_text().splitlines():
        if line.startswith("NEO4J_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
    req = urllib.request.Request(
        f"{NEO4J_HTTP}/db/neo4j/tx/commit",
        data=json.dumps({"statements": [
            {"statement": "MATCH (p:Person) RETURN p.name, coalesce(p.aliases, [])"},
            {"statement": "MATCH (n) WHERE n:Organization OR n:Project RETURN n.name, coalesce(n.aliases, [])"},
        ]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(f"neo4j:{pw}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=1.5) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:200])
    # 인물은 2자 이상, 조직/프로젝트는 3자 이상만 매칭 대상 ('교육', '기타' 같은 일반어 오탐 방지)
    persons, others = set(), set()
    for name, aliases in (d["row"] for d in out["results"][0]["data"]):
        for n in [name] + aliases:
            if n and len(n) >= 2:
                persons.add(n)
    for name, aliases in (d["row"] for d in out["results"][1]["data"]):
        for n in [name] + aliases:
            if n and len(n) >= 3:
                others.add(n)
    return sorted(persons), sorted(others)


def load_names():
    if CACHE.is_file() and time.time() - CACHE.stat().st_mtime < CACHE_TTL:
        d = json.loads(CACHE.read_text())
        return d["persons"], d["others"]
    try:
        persons, others = fetch_names()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"persons": persons, "others": others}, ensure_ascii=False))
        return persons, others
    except Exception:
        if CACHE.is_file():                     # 만료됐어도 있는 캐시가 없는 것보다 낫다
            d = json.loads(CACHE.read_text())
            return d["persons"], d["others"]
        raise


def main():
    prompt = json.load(sys.stdin).get("prompt", "")
    if not prompt or prompt.lstrip().startswith("/graph"):   # 명시 호출은 힌트 불필요
        return
    persons, others = load_names()
    hits = [n for n in persons + others if n in prompt]
    if not hits:
        return
    shown = ", ".join(sorted(set(hits), key=len, reverse=True)[:5])
    print(f"[graph-hint] 질문에 그래프 등재 엔터티 감지: {shown}. "
          f"CLAUDE.md '인물·조직·프로젝트 질문 — 그래프 자동 활용' 규칙을 적용할 것 — "
          f"graph.sh로 Neo4j를 질의하고 관련 vault 문서(60-Sources/meetings 등)와 교차 보강해 답한다.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass                                     # 훅 실패는 조용히 통과
    sys.exit(0)
