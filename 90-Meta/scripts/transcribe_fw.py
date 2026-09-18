#!/usr/bin/env python3
"""transcribe_fw.py — faster-whisper 래퍼.

mlx-whisper의 --verbose True 출력과 동일한 세그먼트 형식을 stdout에 쓴다:
    [00:00:03.000 --> 00:00:07.000]  텍스트

server.py의 _transcribe가 이 타임스탬프 형식을 파싱해 진행률(%)을 계산하므로
포맷이 정확히 일치해야 한다.

SRT 파일도 record_worker.sh가 기대하는 위치($output_dir/*.srt)에 생성한다.

사용법:
    python3 transcribe_fw.py <오디오파일> [--model small] [--language ko] [--output-dir /tmp/xxx]

CUDA 가용 시 device=cuda, 아니면 cpu + compute_type=int8.
"""
import argparse
import sys
from pathlib import Path


def fmt_ts(seconds: float) -> str:
    """초를 HH:MM:SS.mmm 형식으로 변환한다."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_srt_ts(seconds: float) -> str:
    """초를 SRT 타임스탬프(HH:MM:SS,mmm) 형식으로 변환한다."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    parser = argparse.ArgumentParser(description="faster-whisper 전사 래퍼")
    parser.add_argument("audio", help="오디오 파일 경로")
    parser.add_argument("--model", default="small", help="Whisper 모델 (기본: small)")
    parser.add_argument("--language", default="ko", help="언어 코드 (기본: ko)")
    parser.add_argument("--output-dir", dest="output_dir", default=".", help="SRT 출력 디렉토리")
    args = parser.parse_args()

    audio = Path(args.audio)
    if not audio.is_file():
        print(f"오류: 파일을 찾을 수 없다 — {audio}", file=sys.stderr)
        sys.exit(1)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("오류: faster-whisper가 설치되지 않았다. `pip install faster-whisper`", file=sys.stderr)
        sys.exit(1)

    # CUDA 가용 여부 판정
    device = "cpu"
    compute_type = "int8"
    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
            compute_type = "float16"
    except ImportError:
        pass

    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    segments, _info = model.transcribe(
        str(audio),
        language=args.language,
        condition_on_previous_text=False,
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = out_dir / (audio.stem + ".srt")
    srt_lines = []
    idx = 0

    for seg in segments:
        idx += 1
        start_str = fmt_ts(seg.start)
        end_str = fmt_ts(seg.end)
        text = seg.text.strip()

        # mlx-whisper --verbose True 호환 형식을 stdout에 출력
        print(f"[{start_str} --> {end_str}]  {text}", flush=True)

        # SRT 블록 축적
        srt_lines.append(str(idx))
        srt_lines.append(f"{fmt_srt_ts(seg.start)} --> {fmt_srt_ts(seg.end)}")
        srt_lines.append(text)
        srt_lines.append("")

    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    print(f"전사 완료: {srt_path} ({idx} 세그먼트)", flush=True)


if __name__ == "__main__":
    main()
