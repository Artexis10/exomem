"""Apple Silicon (Metal) acceleration gate for exomem.

Confirms the GPU paths work on this Mac:
- torch sees the MPS (Metal) backend, and accel.select_device() picks it for bge/CLIP
- get_transcriber() selects the mlx-whisper backend (needs the `media-mlx` extra)
- mlx-whisper transcribes a generated silent clip on the Metal GPU

Run: uv run python scripts/verify-mlx.py
Exit 0 = PASS (MPS embeddings + MLX ASR both work); non-zero = something fell back to CPU.
Not meaningful off Apple Silicon (MLX is macOS-arm64 only) — use verify-media-gpu.py on CUDA hosts.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import wave

# Use a tiny model for the gate (fast download) unless the caller pinned one — this
# validates the Metal ASR *path*, not a specific model. Set before importing extract,
# which reads EXOMEM_MLX_WHISPER_MODEL at import.
os.environ.setdefault("EXOMEM_MLX_WHISPER_MODEL", "mlx-community/whisper-tiny")


def main() -> int:
    ok = True

    # --- torch / MPS + the device accel will pick for bge/CLIP ---
    try:
        import torch

        from exomem import accel

        mps = getattr(torch.backends, "mps", None)
        mps_ok = bool(mps and mps.is_available() and mps.is_built())
        device = accel.select_device()
        print(f"torch {torch.__version__} | mps_available={mps_ok} | select_device()={device}")
        if not mps_ok:
            ok = False
            print("  WARN: MPS unavailable — bge/CLIP will run on CPU. Need an arm64 torch wheel.")
        elif device != "mps":
            print(f"  NOTE: select_device()={device} — an override (EXOMEM_TORCH_DEVICE?) is forcing it.")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"torch/MPS check FAILED: {e}")

    # --- which ASR backend is active ---
    try:
        from exomem import extract

        backend = type(extract.get_transcriber()).__name__
        print(f"ASR backend: {backend}")
        if backend != "MlxWhisperBackend":
            ok = False
            print("  WARN: not MLX — install the extra: uv sync --extra media --extra media-mlx")
            print("        (or set EXOMEM_ASR_BACKEND=mlx). ASR would run on CPU faster-whisper.")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"ASR backend check FAILED: {e}")

    # --- mlx-whisper transcribes a silent clip on the Metal GPU (the gate that matters) ---
    try:
        from pathlib import Path

        from exomem import extract

        tmp = os.path.join(tempfile.gettempdir(), "exomem_mlx_gate_silence.wav")
        with wave.open(tmp, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))

        segments, engine = extract.get_transcriber().transcribe(Path(tmp))
        list(segments)  # force the decode so the Metal kernels actually run
        print(f"transcription OK — engine={engine}")
        if not engine.startswith("mlx-whisper:"):
            ok = False
            print("  NOTE: engine is not mlx-whisper — transcription did not use the Metal GPU.")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"MLX transcription check FAILED: {e}")

    print("GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
