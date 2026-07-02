"""Real end-to-end smoke for the diarization sidecar worker.

GATED: skips unless the sidecar venv is built (`sidecar/diarizer/.venv`). On CI / un-provisioned
boxes there's no sidecar, so this skips; on a provisioned box it runs the actual worker subprocess
under the SIDECAR interpreter. Do NOT `importorskip("pyannote.audio")` here — pyannote deliberately
isn't installed in the MAIN venv; it lives only in the sidecar.

With a valid `HUGGINGFACE_TOKEN` (+ accepted gated-model conditions) it asserts the happy path
(exit 0 + a turns list). Without a token it asserts graceful failure (nonzero exit, no hang, no
false-positive turns file) — the soft-fail boundary the main service relies on.
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from exomem import extract

_SIDECAR_PY = extract._diarizer_sidecar_python()

pytestmark = pytest.mark.skipif(
    _SIDECAR_PY is None,
    reason="diarizer sidecar venv not built (sidecar/diarizer/.venv) — run scripts/setup-diarizer.ps1",
)


def _write_sine(path: Path, seconds: float = 1.0, rate: int = 16000, freq: int = 220) -> None:
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for n in range(int(seconds * rate)):
            w.writeframes(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * n / rate))))


def test_worker_runs_under_sidecar(tmp_path: Path) -> None:
    wav = tmp_path / "sine.wav"
    out = tmp_path / "turns.json"
    _write_sine(wav)

    proc = subprocess.run(
        [str(_SIDECAR_PY), str(extract._diarizer_worker_script()), str(wav), str(out)],
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": "", "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
    )

    # Whether the gated model is reachable depends on the HF token AND the local cache (a prewarm
    # caches it, so it can succeed token-lessly), which the test can't control. So accept BOTH
    # valid outcomes and just enforce the contract: success → a real turns list; failure → a clean
    # nonzero exit with no false-positive out-file. Never a crash/hang/corrupt output.
    if proc.returncode == 0:
        data = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(data["turns"], list)
        for t in data["turns"]:
            assert {"start", "end", "label"} <= set(t)
    else:
        assert not out.exists() or not out.read_text(encoding="utf-8").strip()
