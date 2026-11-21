"""Where the speaker-diarization sidecar lives, and how to build it.

The sidecar is its own uv project (`_sidecars/diarizer/`), shipped inside the package so a
wheel install has its `worker.py` and lock. Its venv lives under the machine-local state root,
outside every release venv, so a managed release switch neither drops nor rebuilds it. The
directory is keyed by the lock's hash: a release that changes the sidecar's dependencies looks
for a new venv, and `python -m exomem.diarizer_sidecar` builds it; one that keeps the lock
reuses the venv already built. Venvs for other locks stay: a retained release may roll back to
one, and another checkout or service may share the state root.

The service only locates the venv; it never builds one at runtime.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from contextvars import ContextVar
from pathlib import Path

from . import state_paths

log = logging.getLogger(__name__)
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


class WorkerSession:
    """One isolated CUDA diarizer owned by a disposable WSL media worker."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._channel: socket.socket | None = None

    def run(self, command: list[str], *, env: dict[str, str], timeout: float) -> None:
        """Send one audio path and receive confirmation that its output file is ready."""
        deadline = time.monotonic() + timeout
        if self._process is None or self._process.poll() is not None:
            self.close()
            self._start(command[:2], env)
        channel = self._channel
        if channel is None:
            raise OSError("previous diarizer is still shutting down")
        channel.settimeout(max(0, deadline - time.monotonic()))
        channel.sendall(json.dumps(command[2:]).encode("utf-8") + b"\n")
        channel.settimeout(max(0, deadline - time.monotonic()))
        reply = channel.recv(1)
        if reply == b"\0":
            self.close()  # Turns are ready, and a CPU fallback must release its model after the job.
        elif reply != b"\n":
            raise OSError("diarizer stopped before returning speaker turns")

    def _start(self, command: list[str], env: dict[str, str]) -> None:
        parent, worker = socket.socketpair()
        try:
            with worker:
                fd = worker.fileno()
                self._process = subprocess.Popen(  # noqa: S603 - fixed interpreter and worker script
                    [*command, "--serve", str(fd), str(os.getpid())],
                    env=env,
                    pass_fds=(fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,  # Native library output must stay out of MCP stdout.
                )
        except BaseException:
            parent.close()
            raise
        self._channel = parent
        log.info("started reusable CUDA diarizer")

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        process = self._process
        if process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("diarizer did not exit after it was killed")
            return  # Keep its handle so another request cannot start a second sidecar.
        self._process = None


current_worker_session: ContextVar[WorkerSession | None] = ContextVar("diarizer_worker_session", default=None)


@contextlib.contextmanager
def reuse_for_media_worker():
    from . import cloud_cell

    # WSL2 kernel releases end in this suffix. Other platforms retain one-shot workers.
    wsl = sys.platform == "linux" and os.uname().release.lower().endswith("-wsl2")
    if not wsl or cloud_cell.cloud_mode_enabled() or cloud_cell.hosted_mode_enabled():
        yield
        return
    session = WorkerSession()
    token = current_worker_session.set(session)
    try:
        yield
    finally:
        current_worker_session.reset(token)
        session.close()


def build(*, prewarm: bool = False) -> Path:
    """Build (or refresh) the venv for this lock and smoke-test it."""
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
    return interpreter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the speaker-diarization sidecar venv.")
    parser.add_argument("--prewarm", "-p", action="store_true", help="download the pyannote weights now")
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
