"""Where the speaker-diarization sidecar lives, and how to build it.

The sidecar is its own uv project (`_sidecars/diarizer/`), shipped inside the package so a
wheel install has its `worker.py` and lock. Its venv lives under the machine-local state root,
outside every release venv, so a managed release switch neither drops nor rebuilds it. The
directory is keyed by the lock's hash: a release that changes the sidecar's dependencies looks
for a new venv, and `python -m exomem.diarizer_sidecar` builds it; one that keeps the lock
reuses the venv already built.

The service only locates the venv; it never builds one at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import state_paths

PROJECT = Path(__file__).resolve().parent / "_sidecars" / "diarizer"
_PYTHON = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")

_SMOKE = """
import warnings
warnings.filterwarnings("ignore")
import torch
from pyannote.audio import Pipeline
from faster_whisper.audio import decode_audio
print("diarizer sidecar OK | torch", torch.__version__, "| cuda", torch.cuda.is_available())
"""

_PREWARM = """
import os
from pyannote.audio import Pipeline
model = os.environ.get("EXOMEM_DIARIZE_MODEL", "pyannote/speaker-diarization-3.1")
Pipeline.from_pretrained(model, token=os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN"))
print("prewarmed", model)
"""


def venv_dir() -> Path:
    """The venv directory for this release's sidecar lock."""
    lock = hashlib.sha256((PROJECT / "uv.lock").read_bytes()).hexdigest()[:16]
    return state_paths.state_store_root() / "sidecars" / "diarizer" / lock


def python() -> Path | None:
    """The sidecar interpreter, or None when it is not built for this release's lock.

    `EXOMEM_DIARIZE_SIDECAR_PYTHON` overrides the location.
    """
    override = os.environ.get("EXOMEM_DIARIZE_SIDECAR_PYTHON")
    candidate = Path(override) if override else venv_dir() / _PYTHON
    return candidate if candidate.is_file() else None


def worker_script() -> Path:
    return PROJECT / "worker.py"


def build(*, prewarm: bool = False) -> Path:
    """Build (or refresh) the venv for this lock, smoke-test it, and drop venvs for other locks."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv not found on PATH; install uv first: https://docs.astral.sh/uv/")
    target = venv_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(target)}
    subprocess.run([uv, "sync", "--frozen", "--directory", str(PROJECT)], env=env, check=True)
    interpreter = target / _PYTHON
    subprocess.run([str(interpreter), "-c", _SMOKE], check=True)
    if prewarm:
        if os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN"):
            subprocess.run([str(interpreter), "-c", _PREWARM], check=True)
        else:
            print("HUGGINGFACE_TOKEN/HF_TOKEN not set; skipping prewarm.", file=sys.stderr)
    for other in target.parent.iterdir():
        if other != target and other.is_dir():
            shutil.rmtree(other)
    return interpreter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the speaker-diarization sidecar venv.")
    parser.add_argument("--prewarm", action="store_true", help="download the pyannote weights now")
    parser.add_argument("--if-enabled", action="store_true", help="do nothing unless EXOMEM_DIARIZE is on")
    args = parser.parse_args(argv)
    if args.if_enabled:
        from . import extract  # imported here: extract imports this module

        if not extract._diarize_enabled():
            print("diarizer sidecar: EXOMEM_DIARIZE is off; nothing to build")
            return 0
    print(f"diarizer sidecar ready: {build(prewarm=args.prewarm)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
