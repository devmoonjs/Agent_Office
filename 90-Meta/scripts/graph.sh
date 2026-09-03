#!/usr/bin/env bash
# graph.sh — jarvis-neo4j에 Cypher를 실행하는 래퍼
#
# 사용법:
#   graph.sh status              # 컨테이너/접속 상태 확인
#   graph.sh init                # 스키마 제약조건 적용 (init.cypher)
#   graph.sh query "<cypher>"    # 단일 Cypher 실행
#   graph.sh file <경로>          # Cypher 파일 실행 (멱등 MERGE 배치용)
#
# 인증: 90-Meta/neo4j/.env의 NEO4J_PASSWORD 사용. 출력/로그에 비밀번호를 남기지 않는다.
set -euo pipefail

VAULT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$VAULT/90-Meta/neo4j/.env"
CONTAINER="jarvis-neo4j"

[ -f "$ENV_FILE" ] || { echo "오류: $ENV_FILE 없음" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${NEO4J_PASSWORD:?NEO4J_PASSWORD가 .env에 없음}"

run_cypher() {
  docker exec -i -e NEO4J_PASSWORD "$CONTAINER" \
    cypher-shell -u neo4j -p "$NEO4J_PASSWORD" --format plain "$@"
}

case "${1:-}" in
  status)
    docker ps --filter "name=$CONTAINER" --format '{{.Names}}: {{.Status}}'
    if run_cypher "RETURN 'ok' AS ping" >/dev/null 2>&1; then
      echo "bolt: 접속 정상"
      run_cypher "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC"
    else
      echo "bolt: 접속 실패 (컨테이너 기동 대기 중이거나 인증 오류)" >&2
      exit 1
    fi
    ;;
  init)
    run_cypher < "$VAULT/90-Meta/neo4j/init.cypher"
    echo "제약조건 적용 완료"
    ;;
  query)
    [ -n "${2:-}" ] || { echo "사용법: graph.sh query \"<cypher>\"" >&2; exit 1; }
    run_cypher "$2"
    ;;
  file)
    [ -f "${2:-}" ] || { echo "사용법: graph.sh file <cypher파일>" >&2; exit 1; }
    run_cypher < "$2"
    ;;
  *)
    grep '^#' "$0" | head -12 | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
