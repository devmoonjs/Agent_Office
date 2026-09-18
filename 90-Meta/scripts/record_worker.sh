#!/usr/bin/env bash
# record_worker.sh — 00-Inbox/recordings/의 미전사 녹음을 전사해 <id>.transcript.md 생성
#
# 사용법:
#   record_worker.sh            # 대기 중인 녹음 전부 처리
#   record_worker.sh <오디오파일> # 특정 파일만 처리
#
# 전사: mlx-whisper(large-v3-turbo, Apple Silicon). 반복 루프 방지를 위해
#       condition-on-previous-text 비활성화 (2026-07-29 OO동 사례).
# 화자 분리(선택): HF_TOKEN 환경변수 + whisperx 설치 시 SPEAKER_N 라벨 포함.
#       미설정 시 타임스탬프 전용 스크립트를 생성한다 (화자 귀속은 ingest의 호명 단서 추정).
set -euo pipefail

VAULT="$(cd "$(dirname "$0")/../.." && pwd)"
REC="$VAULT/00-Inbox/recordings"
UVX="$(command -v uvx || echo "$HOME/anaconda3/bin/uvx")"
SCRIPTS="$VAULT/90-Meta/scripts"
CONFIG="$VAULT/.agent-office/config.json"

# 전사 모델 결정: 환경변수 > config.json > 기본값(small)
if [ -n "${TRANSCRIBE_MODEL:-}" ]; then
  TX_MODEL="$TRANSCRIBE_MODEL"
elif [ -f "$CONFIG" ]; then
  TX_MODEL="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('transcribeModel','small'))" "$CONFIG" 2>/dev/null || echo small)"
else
  TX_MODEL="small"
fi

# Apple Silicon이면 mlx-whisper, 아니면 faster-whisper 폴백
IS_MLX=false
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  IS_MLX=true
fi

# mlx-whisper 모델명 매핑
if [ "$IS_MLX" = true ]; then
  case "$TX_MODEL" in
    tiny|base|small|medium|large|large-v2|large-v3)
      MLX_MODEL="mlx-community/whisper-$TX_MODEL" ;;
    large-v3-turbo)
      MLX_MODEL="mlx-community/whisper-large-v3-turbo" ;;
    *)
      MLX_MODEL="mlx-community/whisper-$TX_MODEL" ;;
  esac
else
  MLX_MODEL=""
fi

[ -d "$REC" ] || { echo "녹음 폴더 없음: $REC"; exit 0; }

transcribe() {
  local audio="$1"
  local id base tmp
  base="$(basename "$audio")"
  id="${base%.*}"
  local out_md="$REC/$id.transcript.md"
  [ -f "$out_md" ] && { echo "건너뜀(전사본 있음): $base"; return 0; }

  echo "전사 시작: $base"
  tmp="$(mktemp -d)"

  if [ -n "${HF_TOKEN:-}" ] && command -v whisperx >/dev/null 2>&1; then
    # 화자 분리 경로 (SPEAKER_N 라벨 포함)
    whisperx "$audio" --language ko --diarize --hf_token "$HF_TOKEN" \
      --output_dir "$tmp" --output_format srt >/dev/null
  else
    # --verbose True: 세그먼트마다 "[00:03:12.000 --> ...] 텍스트"를 stdout에 흘린다.
    # 서버(meeting_pipeline._transcribe)가 이 타임스탬프를 녹음 길이로 나눠 진행률(%)을 만든다.
    if [ "$IS_MLX" = true ]; then
      PYTHONUNBUFFERED=1 "$UVX" --from mlx-whisper mlx_whisper "$audio" \
        --model "$MLX_MODEL" --language ko --condition-on-previous-text False \
        --output-dir "$tmp" --output-format srt --verbose True
    else
      # faster-whisper 폴백 (Linux/WSL) — transcribe_fw.py가 mlx-whisper와 동일한
      # 세그먼트 형식을 stdout에 출력하므로 서버의 진행률 파싱이 그대로 동작한다.
      PYTHONUNBUFFERED=1 "$UVX" --with faster-whisper python3 "$SCRIPTS/transcribe_fw.py" \
        "$audio" --model "$TX_MODEL" --language ko --output-dir "$tmp"
    fi
  fi

  local srt
  srt="$(ls "$tmp"/*.srt 2>/dev/null | head -1)"
  [ -n "$srt" ] || { echo "오류: srt 미생성 — $base" >&2; rm -rf "$tmp"; return 1; }

  python3 - "$srt" "$out_md" "$id" <<'PY'
import re, sys
srt, out, rid = sys.argv[1], sys.argv[2], sys.argv[3]
blocks = open(srt, encoding="utf-8").read().strip().split("\n\n")
lines = []
for b in blocks:
    rows = b.strip().splitlines()
    if len(rows) < 3:
        continue
    m = re.match(r"(\d{2}:\d{2}:\d{2})", rows[1])
    ts = m.group(1) if m else "??:??:??"
    text = " ".join(r.strip() for r in rows[2:]).strip()
    if text:
        lines.append(f"[{ts}] {text}")
header = (f"---\ntype: transcript\nrecording: {rid}\n"
          "note: 화자 라벨이 없으면 타임스탬프 전용 전사 — 화자 귀속은 호명 단서로 추정하고 컨펌 필요\n---\n\n"
          f"# 전사 스크립트 — {rid}\n\n")
open(out, "w", encoding="utf-8").write(header + "\n".join(lines) + "\n")
print(f"전사 완료: {out} ({len(lines)} 세그먼트)")
PY
  rm -rf "$tmp"

  # meta 상태 갱신
  python3 - "$REC/$id.meta.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if p.is_file():
    m = json.loads(p.read_text())
    m["status"] = "transcribed"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2))
PY
}

if [ $# -ge 1 ]; then
  transcribe "$1"
else
  found=0
  for f in "$REC"/*.webm "$REC"/*.m4a "$REC"/*.mp4 "$REC"/*.wav "$REC"/*.mp3 "$REC"/*.ogg; do
    [ -f "$f" ] || continue
    found=1
    transcribe "$f" || true
  done
  [ "$found" = 1 ] || echo "처리할 녹음이 없습니다: $REC"
fi
