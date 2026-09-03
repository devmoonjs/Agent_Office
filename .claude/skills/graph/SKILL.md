---
name: graph
description: 인물 관계 그래프(Neo4j) 질의 — 자연어 질문을 Cypher로 변환해 실행하고 결과를 해석해 보고한다. 인물·조직·프로젝트·회의·액션 아이템에 관한 질문이면 /graph 명시 호출이 없어도 사용하고, 관련 vault 문서를 함께 검색해 교차 보강한다.
---

# /graph <자연어 질문 | Cypher>

jarvis-neo4j의 인물 관계 그래프를 질의한다.

## 사전 지식

- 스키마: `90-Meta/neo4j/SCHEMA.md`를 먼저 읽는다 (노드 레이블, 관계 타입, 불변 규칙)
- 실행: `bash 90-Meta/scripts/graph.sh query "<cypher>"`
- 상태 확인: `bash 90-Meta/scripts/graph.sh status` (컨테이너가 내려가 있으면 `docker compose -f 90-Meta/neo4j/docker-compose.yml up -d` 안내)

## 절차

1. **질문 해석**: 자연어 질문이면 스키마에 맞는 Cypher로 변환한다. 사용자가 Cypher를 직접 주면 그대로 실행하되, 쓰기 쿼리(`CREATE`/`MERGE`/`DELETE`/`SET`)는 실행 전에 영향 범위를 보고한다
2. **실행**: `graph.sh query`로 실행한다. 결과가 비어 있으면 매칭 실패 원인을 진단한다 (이름 표기 차이 → `aliases` 검색, 관계 방향 오류 등)
3. **해석 보고**: 원시 결과를 그대로 던지지 않고, 질문에 대한 답으로 재구성해 보고한다. 근거 엣지의 `source`(출처 문서)를 함께 제시한다

## 자주 쓰는 질의 패턴

```cypher
-- 특정 인물의 전체 관계
MATCH (p:Person {name: $name})-[r]-(x) RETURN type(r), labels(x)[0], coalesce(x.name, x.id), r.source

-- 같은 회의에 참석한 사람 (추론은 쿼리 시점에 — SCHEMA 불변 규칙 4)
MATCH (a:Person {name: $name})-[:ATTENDED]->(m)<-[:ATTENDED]-(b) RETURN DISTINCT b.name, m.topic

-- 미완료 액션 아이템 담당자별 집계
MATCH (ai:ActionItem {status: 'open'})-[:ASSIGNED_TO]->(p) RETURN p.name, collect(ai.desc)

-- 두 사람의 연결 경로
MATCH path = shortestPath((a:Person {name: $a})-[*..6]-(b:Person {name: $b})) RETURN path
```

## 규칙

- 이름 매칭은 `name` 정확 일치 우선, 실패 시 `aliases` 포함 검색으로 폴백
- 그래프 수정 요청(인물 병합, 관계 정정)은 멱등 쿼리로 작성하고 실행 전 보고한다
- 비밀번호는 graph.sh가 .env에서 읽는다 — Cypher나 보고에 노출 금지
