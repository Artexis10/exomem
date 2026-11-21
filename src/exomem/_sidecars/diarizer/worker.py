#!/usr/bin/env python
"""exomem diarization sidecar — standalone, runs in the isolated CUDA-torch sidecar venv.

Reads an audio/video path + an output-file path, decodes to 16 kHz mono, runs the pyannote
speaker-diarization pipeline (on GPU when available, else CPU), and writes
``{"turns":[{"start","end","label"}, ...]}`` as UTF-8 JSON to the output file. Any failure → a
message on stderr + a nonzero exit; the caller (``extract._run_diarization`` in the main cu132
venv) maps that to ``None`` → plain transcript. This file MUST NOT import ``kb_mcp`` — it runs
under a *different* interpreter/venv.

Why a separate venv: the main service runs a custom torch-2.12+cu132 (Blackwell) build that is
incompatible with the pyannote/torchaudio ecosystem. This sidecar pins a proven stack
(pyannote 4 package loading the 3.1 model, torch 2.9.1+cu130 → sm_120) where pyannote runs on the
GPU. ``KB_MCP_DIARIZE_DEVICE`` = ``cpu`` | ``cuda`` | ``auto`` (default auto → cuda if available).

The result channel is the output file, with completion signaled by an exit code or an acknowledgment
on a private socket. ``main`` redirects stdout to stderr so library output cannot mix with results.
torchcodec's libtorchcodec load failure on Windows is a harmless warning. We
pre-decode and hand pyannote a waveform dict, so its decoder is never used.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import warnings
from functools import lru_cache

# Quiet HF/torchcodec/lightning chatter before the heavy imports. (The parent also sets these in
# the child env; setdefault keeps the worker robust when run directly.)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("PYTHONWARNINGS", "ignore")
warnings.filterwarnings("ignore")


def _decode(path: str):
    """Decode any audio/video to a ``(1, N)`` float32 16 kHz mono torch tensor via faster-whisper's
    PyAV path — the SAME decoder the main ASR uses, so turns share the whisper timebase."""
    import torch
    from faster_whisper.audio import decode_audio

    samples = decode_audio(path, sampling_rate=16000)
    return torch.as_tensor(samples, dtype=torch.float32).unsqueeze(0)


def _device() -> str:
    """Resolve the run device. EXOMEM_DIARIZE_DEVICE (or legacy KB_MCP_DIARIZE_DEVICE)
    = cpu | cuda | mps | auto (default auto → cuda if available, else cpu).

    `auto` never picks MPS: pyannote on this pinned sidecar stack is validated on
    CUDA/CPU, and diarization feeds cross-machine voiceprint attribution, so Apple
    Silicon Metal is an explicit opt-in (`=mps`) rather than an automatic choice.
    """
    import torch

    pref = (
        os.environ.get("EXOMEM_DIARIZE_DEVICE")
        or os.environ.get("KB_MCP_DIARIZE_DEVICE")
        or "auto"
    ).lower()
    if pref == "cpu":
        return "cpu"
    if pref == "mps":
        backend = getattr(torch.backends, "mps", None)
        if backend is not None and backend.is_available():
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            return "mps"
        return "cpu"
    if pref in ("cuda", "gpu", "auto"):
        return "cuda" if torch.cuda.is_available() else "cpu"
    return "cpu"


def _load_pipeline():
    """Load the pretrained pyannote diarization pipeline (model + HF token from env)."""
    from pyannote.audio import Pipeline

    model = (
        os.environ.get("EXOMEM_DIARIZE_MODEL")
        or os.environ.get("KB_MCP_DIARIZE_MODEL")
        or "pyannote/speaker-diarization-3.1"
    )
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    # pyannote 4.x uses `token`; 3.x used `use_auth_token`. Support both.
    try:
        pipeline = Pipeline.from_pretrained(model, token=token)
    except TypeError:
        pipeline = Pipeline.from_pretrained(model, use_auth_token=token)
    # Optional clustering-threshold override (pyannote's clustering threshold). Applied
    # only when set, so default pyannote behaviour is unchanged. Higher → fewer clusters (merge
    # similar voices); lower → more sensitive. Tune to curb over-splitting on conversational audio.
    thr = os.environ.get("EXOMEM_DIARIZE_CLUSTERING_THRESHOLD") or os.environ.get(
        "KB_MCP_DIARIZE_CLUSTERING_THRESHOLD"
    )
    if thr and pipeline is not None:
        try:
            clustering = getattr(pipeline, "clustering", None)
            if clustering is not None and hasattr(clustering, "threshold"):
                clustering.threshold = float(thr)
                print(f"[worker] clustering.threshold={thr}", file=sys.stderr)
        except Exception:  # noqa: BLE001 — an optional knob must never break diarization
            pass
    return pipeline


def _annotation(output):
    """Normalize the pipeline result to an Annotation with ``.itertracks()``.

    pyannote 3.x returns an Annotation directly; pyannote 4.x wraps it in a DiarizeOutput exposing
    ``.exclusive_speaker_diarization`` (non-overlapping, preferred) / ``.speaker_diarization``.
    """
    if hasattr(output, "itertracks"):
        return output
    for attr in ("exclusive_speaker_diarization", "speaker_diarization"):
        ann = getattr(output, attr, None)
        if ann is not None and hasattr(ann, "itertracks"):
            return ann
    return output


def _load_device_pipeline():
    import torch

    pipeline = _load_pipeline()
    if pipeline is None:
        # pyannote returns None (not raises) for an unloadable gated checkpoint — usually a missing
        # HF token or un-accepted conditions. Make that explicit for the caller's stderr log.
        raise RuntimeError(
            "pyannote pipeline failed to load — gated model needs a valid HUGGINGFACE_TOKEN "
            "and accepted conditions for speaker-diarization-3.1 + segmentation-3.0"
        )
    dev = _device()
    if dev in ("cuda", "mps"):
        pipeline.to(torch.device(dev))
    print(f"[worker] device={dev}", file=sys.stderr)
    return pipeline, dev


def _diarize(audio_path: str, load_pipeline=_load_device_pipeline) -> list[dict]:
    waveform = _decode(audio_path)
    pipeline, _ = load_pipeline()
    annotation = _annotation(pipeline({"waveform": waveform, "sample_rate": 16000}))
    return [
        {"start": float(t.start), "end": float(t.end), "label": str(label)}
        for t, _track, label in annotation.itertracks(yield_label=True)
    ]


def _write_turns(out_path: str, turns: list[dict]) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"turns": turns}, fh)


def _serve(fd: int, parent_pid: int) -> None:
    import ctypes
    import signal

    # WSL2 uses Linux prctl. Kill the sidecar even if its owner dies during inference.
    pr_set_pdeathsig = 1
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    if os.getppid() != parent_pid:
        return  # The owner died before prctl was armed.

    import torch

    load_pipeline = lru_cache(maxsize=1)(_load_device_pipeline)
    with socket.socket(fileno=fd) as channel, channel.makefile("rb") as requests:
        for request in requests:
            audio_path, out_path = json.loads(request)
            _write_turns(out_path, _diarize(audio_path, load_pipeline))
            _, device = load_pipeline()
            if device != "cuda":
                channel.sendall(b"\0")  # Report success and exit when CUDA fell back to CPU.
                return
            # Whisper uses the same GPU for the next job. Return free allocator blocks before it starts.
            torch.cuda.empty_cache()
            channel.sendall(b"\n")


def main(argv: list[str]) -> int:
    sys.stdout = sys.stderr
    if len(argv) == 4 and argv[1] == "--serve":
        _serve(int(argv[2]), int(argv[3]))
        return 0
    if len(argv) < 3:
        print("usage: worker.py <audio_path> <out_json_path>", file=sys.stderr)
        return 2
    audio_path, out_path = argv[1], argv[2]
    t0 = time.monotonic()
    turns = _diarize(audio_path)
    _write_turns(out_path, turns)
    print(f"[worker] {len(turns)} turns in {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — any failure → nonzero exit + stderr; caller soft-fails
        print(f"diarizer worker failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
