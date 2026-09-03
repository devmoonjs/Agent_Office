# 인물 관계 그래프 스키마 (jarvis-neo4j)

문서(`/ingest`)에서 추출된 인물·조직 지식을 적재하는 property graph 정의. 모든 쓰기는 `MERGE` 기반 멱등 연산으로 수행한다 — 같은 문서를 두 번 ingest해도 중복 노드/엣지가 생기지 않아야 한다.

## 접속 정보

- Bolt: `bolt://localhost:57687` / Browser UI: `http://localhost:57474`
- 계정: `neo4j` / 비밀번호: `90-Meta/neo4j/.env`의 `NEO4J_PASSWORD` (커밋·노트 기록 금지)
- 실행 래퍼: `90-Meta/scripts/graph.sh`

## 노드 레이블

| 레이블 | 키 속성 (unique) | 기타 속성 |
|---|---|---|
| `Person` | `name` | `team`, `role`, `aliases` (list), `member_id` (운영 DB id) |
| `Organization` | `name` | `kind` (`internal` 부서 \| `external` 기관/협력사), `team_path`, `aliases` (list), `department_id` |
| `Project` | `name` | `repo_path`, `project_id`, `started_at`, `ended_at`, `description`, `task_count`, `last_active_at` |
| `Meeting` | `id` (= `YYYY-MM-DD-<주제 슬러그>`) | `date`, `topic`, `source` (원본 문서 경로) |
| `Decision` | `id` (= `<meeting_id>#d<n>`) | `summary`, `rationale`, `date` |
| `ActionItem` | `id` (= `<meeting_id>#a<n>`) | `desc`, `due`, `status` (`open`/`done`) |
| `Concept` | `name` (위키 노트 파일명과 일치) | `wiki_path`, `kind` (`concept`\|`pattern`\|`tool`), `aliases`, `created`, `synced_at` |
| `Topic` | `name` (MOC-llm.md의 `###` 하위 섹션명) | `source` |

## 관계 타입

| 관계 | 방향 | 속성 |
|---|---|---|
| `ATTENDED` | (Person \| Organization)→(Meeting) | `source` |
| `REPORTS_TO` | (Person)→(Person) | `source`, `recorded_at` |
| `WORKS_WITH` | (Person)→(Person) | `context`, `source`, `recorded_at` |
| `WORKS_ON` | (Person \| Organization)→(Project) | `role` (`PM`/`업무관리자`/`팀원`), `source`, `task_count`, `last_active_at`, `top_category` |
| `DECIDED` | (Person)→(Decision) | `source` |
| `PRODUCED` | (Meeting)→(Decision \| ActionItem) | — |
| `ASSIGNED_TO` | (ActionItem)→(Person \| Organization) | — |
| `AFFECTS` | (Decision)→(Project \| Concept) | — |
| `DISCUSSED` | (Meeting)→(Concept) | — |
| `RELATED_TO` | (Concept)→(Concept) | 위키 본문 `[[백링크]]` 기준. `source`=링크가 있는 노트 경로 |
| `IN_TOPIC` | (Concept)→(Topic) | MOC 등재 기준 |
| `DERIVED_FROM` | (Concept)→(Project) | 노트 `source`의 repo가 속한 사업. `repo` 속성으로 원 repo 보존 |
| `BELONGS_TO` | (Person)→(Organization) | `source` — 조직도 시드 기준 소속 |
| `PART_OF` | (Organization)→(Organization) | — 부서 계층 (하위→상위) |

> **공통 엣지 속성**: 문서 추출 엣지는 위 속성 외에 `source`(원본 경로), `recorded_at`(ISO 날짜), `confidence`(`direct`|`implied`)를 공통으로 가진다.

## 운영 DB(Lego) 동기화

외부 업무 관리 도구의 DB 덤프를 동기화 스크립트가 읽어 **그래프에는 집계만, 상세는 `70-Activity/` 노트로** 반영한다. task 8천여 건을 노드로 적재하면 그래프 뷰가 무의미해지므로 노드화하지 않는다.

- 그래프 반영분: 노드 id(`member_id`/`department_id`/`project_id`), Project 기간·설명, `WORKS_ON`의 `role`·`task_count`·`last_active_at`·`top_category`
- **기존 엣지를 삭제하지 않는다**: 덤프보다 최신인 수동 적재분을 덮어쓰지 않도록 `source`/`recorded_at`/`confidence`는 `coalesce`로 기존 값을 보존한다
- 존재하지 않는 인물을 새로 만들지 않는다 (덤프의 시스템 계정이 인물로 유입되는 것을 차단)
- 긴급업무 프로젝트(팀 단위 버킷)는 그래프 대상에서 제외한다. 단 활동 노트에는 업무 이력으로 남긴다
- 덤프는 vault 밖 경로에서 읽으며 `member.password`는 파싱하지 않는다

## 위키(Obsidian) 동기화 — 단방향

`90-Meta/scripts/wiki_sync.py`가 `20-Wiki/`를 스캔해 온톨로지 층을 적재한다. **Obsidian이 Source of Truth이고 역방향 반영은 하지 않는다** (근거: `20-Wiki/patterns/bi-directional-sync-obsidian-neo4j.md` — 양방향 동기화는 안티패턴).

- 노트 1개 = `Concept` 1개. frontmatter `type`이 `kind` 속성이 되어 개념·패턴·도구를 구분한다
- MOC-llm.md의 `###` 하위 섹션이 `Topic` 노드가 되고 `IN_TOPIC`으로 연결된다 (`##` 상위 섹션은 `kind`와 중복이라 쓰지 않는다)
- 본문 `[[백링크]]`는 `RELATED_TO`가 된다. 링크가 본문에 실재하므로 `confidence: direct`
- **추가·갱신만 자동, 삭제는 보고만 한다**: 노트가 사라져도 Concept을 지우지 않는다. `/ingest`가 만든 `DISCUSSED` 엣지가 함께 사라지는 사고를 막기 위해서다
- 대상 노트가 없는 링크는 적재하지 않고 스텁 후보로 보고한다
- **개념의 사업 귀속**: 노트 `source`가 `<repo>@<커밋>` 형태면 `wiki_sync.REPO_PROJECT` 표로 사업을 찾아 `DERIVED_FROM`을 만든다. `/daily-review`가 코드에서 뽑은 개념을 그 코드가 속한 사업에 귀속시키는 경로다. 표에 없는 repo는 **적재하지 않고 보고만** 한다(새 repo가 조용히 누락되지 않도록). 대응 사업이 없는 repo는 `REPO_NO_PROJECT`에 명시한다
- 실행: 매일 17:00 (`com.myjarvis.daily-sync.plist`) 또는 텔레그램 `/sync`

인물 관계는 이 경로로 적재하지 않는다 — 아래 컨펌 규약을 그대로 따른다.

## 근거 강도 등급 (confidence)

추출되는 모든 엣지는 근거 강도를 `confidence` 속성으로 기록한다.

- `direct`: 문서에 관계를 **직접 진술하는 문장**이 존재한다. `evidence`는 그 문장 인용. 검토표에서 기본 포함(체크).
- `implied`: 직접 진술은 없으나 **문서 정황상 강하게 시사**된다. `evidence`는 근거 정황 + 추론 한 줄. 검토표에서 **기본 제외(체크 해제)** 상태로 올려 사용자가 명시적으로 포함을 선택해야 적재된다.
- 인용도 정황도 댈 수 없는 무근거 추측은 등급 부여 대상이 아니며 추출 단계에서 버린다.

적재 후 `implied` 엣지만 조회: `MATCH ()-[r]->() WHERE r.confidence = 'implied' RETURN r`.

## 불변 규칙

1. **모든 엣지는 출처와 근거 강도를 가진다**: 문서에서 추출된 관계는 `source`(원본 문서 경로), `recorded_at`(ISO 날짜), `confidence`(`direct`|`implied`)를 반드시 기록한다. 근거 없는 엣지 금지 — Daily 노트의 코드 인용 규칙과 동일한 원칙
2. **인물 동일성은 `name` 기준**: 동명이인이 식별되면 `name`에 구분자를 붙이고(`김철수(백엔드)`) `aliases`에 원명을 보존한다
2-1. **조직/팀/기관은 Person이 아니라 `Organization`으로 적재한다**: 내부 부서(kind=internal)는 조직도 시드와 이름 대조, 외부 기관·협력사(kind=external)는 신규 생성. 문서에서 추출된 명칭이 인물인지 조직인지 애매하면 검토 단계에서 사용자에게 질문한다
3. **삭제 대신 상태 전이**: ActionItem 완료는 노드 삭제가 아니라 `status: 'done'` 갱신
4. **추론 관계 금지**: 문서에 명시된 관계만 적재한다. "같은 회의에 참석했으니 WORKS_WITH" 같은 추론은 쿼리 시점에 수행한다 (예: `MATCH (a)-[:ATTENDED]->(m)<-[:ATTENDED]-(b)`)

## Cypher 파일 작성 규칙 (ingest 적재용)

cypher-shell은 `;`로 구분된 문장 간에 **변수 바인딩을 유지하지 않는다**. 앞 문장에서 선언한 `p1`을 뒷 문장의 관계 MERGE에서 쓰면 미바인딩 변수로 취급되어 레이블 없는 익명 노드가 생성된다(2026-06-10 검증에서 실제 발생). 따라서:

1. **노드 먼저, 관계 나중**: 파일 상단에 노드 MERGE를 모두 배치하고, 관계 MERGE는 그 뒤에 둔다
2. **관계 MERGE는 한 문장에서 완결**: 같은 문장 안에서 endpoint를 MATCH로 재바인딩한다

```cypher
-- 올바름
MATCH (p:Person {name: '김철수'}), (m:Meeting {id: '2026-06-10-주간회의'})
MERGE (p)-[r:ATTENDED]->(m) SET r.source = '60-Sources/meetings/2026-06-10-주간회의.md';

-- 금지: p, m이 이 문장에서 미바인딩 → 익명 노드 생성됨
MERGE (p)-[r:ATTENDED]->(m);
```

3. **검증 습관**: 적재 후 `MATCH (n) WHERE size(labels(n)) = 0 RETURN count(n)`이 0인지 확인한다

## 제약조건 (init.cypher로 적용)

Person.name, Project.name, Concept.name, Meeting.id, Decision.id, ActionItem.id에 uniqueness constraint.
