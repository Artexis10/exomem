"""Blackwell verification gate for server-side media extraction (plan Task 0).

Confirms the GPU path works for the media engines on this box:
- torch sees CUDA and lists the GPU's compute arch (sm_120 for Blackwell RTX 50-series)
- faster-whisper (ctranslate2) loads on cuda and transcribes a generated silent clip
  — this is the real test of whether CTranslate2 has sm_120 kernels
- pymupdf + pytesseract import; the Tesseract binary is reported separately

Run: uv run python scripts/verify-media-gpu.py
Exit 0 = gate PASS (faster-whisper works on the GPU); non-zero = fall back to torch-Whisper.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import wave


def main() -> int:
    ok = True

    # --- torch / CUDA arch ---
    try:
        import torch

        avail = torch.cuda.is_available()
        arches = torch.cuda.get_arch_list() if avail else []
        name = torch.cuda.get_device_name(0) if avail else "(no cuda)"
        print(f"torch {torch.__version__} | cuda={avail} | device={name}")
        print(f"  arch_list={arches}")
        if avail and not any("120" in a for a in arches):
            print("  WARN: sm_120 not in arch_list — Blackwell kernels may be missing from torch")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"torch check FAILED: {e}")

    # --- faster-whisper on GPU (the gate that matters) ---
    try:
        from exomem import extract

        extract._ensure_cuda_dll_path()  # register nvidia-* CUDA-12 DLLs on Windows

        import ctranslate2
        from faster_whisper import WhisperModel

        n_cuda = ctranslate2.get_cuda_device_count()
        print(f"ctranslate2 {ctranslate2.__version__} | cuda_device_count={n_cuda}")
        device = "cuda" if n_cuda > 0 else "cpu"
        compute_type = "int8_float16" if device == "cuda" else "int8"

        tmp = os.path.join(tempfile.gettempdir(), "kb_gate_silence.wav")
        with wave.open(tmp, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))

        model = WhisperModel("tiny", device=device, compute_type=compute_type)
        segments, _info = model.transcribe(tmp)
        list(segments)  # force the decode so GPU kernels actually run
        print(f"faster-whisper OK on {device} ({compute_type}) — tiny model transcribed a silent clip")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"faster-whisper GPU check FAILED: {e}")

    # --- per-keyframe video CLIP (real sampler + real CLIP encode) ---
    try:
        import av
        import numpy as np

        from exomem import embeddings

        # Synthesize a ~24s clip with a white square that marches across the frame —
        # SPATIAL structure (not just colour) so the luminance pHash sees distinct
        # scenes and the sampler keeps several keyframes. (aHash is level-invariant,
        # so a textured frame is needed; real recordings are textured.)
        tmp_mp4 = os.path.join(tempfile.gettempdir(), "kb_gate_clip.mp4")
        fps, secs, w_, h_, box = 10, 24, 96, 96, 24
        with av.open(tmp_mp4, "w") as out:
            vs = out.add_stream("mpeg4", rate=fps)
            vs.width, vs.height, vs.pix_fmt = w_, h_, "yuv420p"
            n = fps * secs
            for i in range(n):
                rgb = np.full((h_, w_, 3), 30, dtype=np.uint8)
                x = int((w_ - box) * i / max(1, n - 1))
                y = int((h_ - box) * (i % fps) / max(1, fps - 1))
                rgb[y : y + box, x : x + box] = 235  # white box, moving position
                for pkt in vs.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")):
                    out.mux(pkt)
            for pkt in vs.encode():
                out.mux(pkt)

        frames = embeddings.embed_video_frames(__import__("pathlib").Path(tmp_mp4))
        assert frames, "no keyframe vectors produced"
        assert all(v.shape == (embeddings.CLIP_DIM,) for _, v in frames), "wrong vector dim"
        assert all(abs(float(np.linalg.norm(v)) - 1.0) < 1e-3 for _, v in frames), "vectors not L2-normalized"
        assert all(0.0 <= ts <= secs + 1 for ts, _ in frames), "timestamp out of range"
        print(
            f"embed_video_frames OK — {len(frames)} keyframe vector(s), "
            f"ts={[round(ts, 1) for ts, _ in frames]}"
        )
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"per-keyframe video CLIP check FAILED: {e}")

    # --- pymupdf / pytesseract import + Tesseract binary ---
    for mod in ("fitz", "pytesseract"):
        try:
            __import__(mod)
            print(f"{mod} import OK")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"{mod} import FAILED: {e}")
    try:
        import pytesseract

        print(f"tesseract binary: {pytesseract.get_tesseract_version()}")
    except Exception as e:  # noqa: BLE001
        print(f"tesseract binary NOT found (install: winget install UB-Mannheim.TesseractOCR): {e}")

    print("GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
