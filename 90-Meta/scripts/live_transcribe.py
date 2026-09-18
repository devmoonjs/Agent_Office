#!/usr/bin/env python3
"""live_transcribe.py — 실시간 회의 보조(답변도우미·참견모드)용 상주 전사기.

stdin으로 오디오 파일 경로를 한 줄씩 받아 전사하고 stdout에 JSON 한 줄로 돌려준다.
record_worker.sh와 달리 프로세스가 살아 있는 동안 모델을 메모리에 유지한다
(mlx_whisper.load_models.load_model이 lru_cache이므로 두 번째 호출부터는 로딩 비용 0).
회의 중 20초 조각마다 모델을 다시 올리면 조각 길이보다 로딩이 오래 걸려 실시간이 깨진다.

실행:
    uv run --with mlx-whisper 90-Meta/scripts/live_transcribe.py
입출력:
    <- /path/to/seg-001.webm
    -> {"ok": true, "path": "...", "text": "..."}

stdout에는 JSON 외의 줄이 섞일 수 있으므로(모델 다운로드 진행 등) 호출자는
'{'로 시작하는 줄만 파싱한다.
"""
import json
import os
import sys
from pathlib import Path

# 전사 모델: 환경변수 > config.json > 기본값(small)
def _resolve_model():
    if os.environ.get("TRANSCRIBE_MODEL"):
        return os.environ["TRANSCRIBE_MODEL"]
    cfg = Path(__file__).resolve().parents[2] / ".agent-office" / "config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8")).get("transcribeModel", "small")
    except (OSError, ValueError):
        return "small"

_TX_MODEL = _resolve_model()
MLX_MODEL = f"mlx-community/whisper-{_TX_MODEL}"

# 엔진 선택: Apple Silicon이면 mlx-whisper, 아니면 faster-whisper
_USE_MLX = sys.platform == "darwin"
_engine = None   # "mlx" | "fw" | None (초기화 전)


def _init_mlx():
    global _engine
    import mlx_whisper  # noqa: F811
    _engine = "mlx"
    return mlx_whisper


def _init_fw():
    global _engine
    from faster_whisper import WhisperModel
    device, ct = "cpu", "int8"
    try:
        import torch
        if torch.cuda.is_available():
            device, ct = "cuda", "float16"
    except ImportError:
        pass
    _init_fw._model = WhisperModel(_TX_MODEL, device=device, compute_type=ct)
    _engine = "fw"
    return _init_fw._model


def main():
    # 엔진 초기화 — 실패 시 사유를 JSON으로 돌려주고 종료한다
    mlx_mod, fw_model = None, None
    if _USE_MLX:
        try:
            mlx_mod = _init_mlx()
        except ImportError:
            print(json.dumps({"ready": False, "error": "mlx-whisper 미설치 — Apple Silicon 전용 실시간 전사 불가"}), flush=True)
            sys.exit(1)
    else:
        try:
            fw_model = _init_fw()
        except ImportError:
            print(json.dumps({"ready": False, "error": "faster-whisper 미설치 — 실시간 전사 미지원. pip install faster-whisper"}), flush=True)
            sys.exit(1)

    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        try:
            if _engine == "mlx":
                # condition_on_previous_text=False — 조각 단위 전사에서 이전 문맥을 물면 반복 루프에 빠진다
                r = mlx_mod.transcribe(path, path_or_hf_repo=MLX_MODEL, language="ko",
                                       condition_on_previous_text=False, verbose=None)
                out = {"ok": True, "path": path, "text": (r.get("text") or "").strip()}
            else:
                segments, _ = fw_model.transcribe(str(path), language="ko",
                                                   condition_on_previous_text=False)
                text = " ".join(seg.text.strip() for seg in segments)
                out = {"ok": True, "path": path, "text": text}
        except Exception as e:                                   # noqa: BLE001 — 어떤 실패든 다음 조각으로 넘어간다
            out = {"ok": False, "path": path, "error": str(e)[:200]}
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
