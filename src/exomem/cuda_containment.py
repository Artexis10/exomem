"""Keep a CUDA fault away from the serving worker.

A CUDA library can crash the process that initializes it. Only the disposable media child may
touch the GPU: the service starts the serving worker with no CUDA device, and the media
supervisor hands the child its device list only after a throwaway process has initialized CUDA
without dying. This module imports nothing from the package, so any layer may use it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass

DEVICES_ENV = "CUDA_VISIBLE_DEVICES"
#: The deployment's device list while the serving worker sees none. JSON, so an unset list and an
#: empty one stay different, and only the media child's environment gets the value back.
CHILD_DEVICES_ENV = "EXOMEM_MEDIA_CUDA_VISIBLE_DEVICES"
#: Why the media child has no GPU. Set in the child's environment only after a failed preflight.
PREFLIGHT_FAILURE_ENV = "EXOMEM_MEDIA_GPU_PREFLIGHT_FAILURE"
#: Cold CUDA initialization on a loaded card takes tens of seconds; a hang past this is a fault.
PREFLIGHT_TIMEOUT_SECONDS = 60.0
#: How long to wait for a killed preflight process to be reaped before giving up on it.
_REAP_SECONDS = 5.0

#: Runs in the throwaway process. Each stage is named before it starts, so the last line printed
#: names the library that died. The probe allocates one element and loads no model.
_PREFLIGHT_CODE = """\
def stage(name):
    print(name, flush=True)

stage("importing torch")
try:
    import torch
except ImportError:
    torch = None
if torch is not None:
    stage("torch CUDA initialization")
    if torch.cuda.is_available():
        stage("a torch CUDA operation")
        (torch.ones(1, device="cuda") + 1).item()
stage("importing CTranslate2")
try:
    import ctranslate2
except ImportError:
    ctranslate2 = None
if ctranslate2 is not None:
    stage("the CTranslate2 CUDA device count")
    ctranslate2.get_cuda_device_count()
"""


@dataclass(frozen=True)
class Preflight:
    """The preflight verdict. `ok` means the media child may keep its devices; `tested` is False when no CUDA ran."""

    ok: bool
    reason: str | None = None
    tested: bool = True

    @classmethod
    def untested(cls, why: str) -> Preflight:
        """Nothing initialized CUDA, so nothing can be said about the GPU: the child's environment stays as it is."""
        return cls(True, why, tested=False)

    @property
    def result(self) -> str:
        return "not tested" if not self.tested else "passed" if self.ok else "failed"


def hide_from_worker(env: Mapping[str, str]) -> dict[str, str]:
    """The serving worker's environment: no CUDA device, and the deployment's list kept for the media child.

    Idempotent: an environment that is hidden already keeps the list it saved the first time.
    """
    isolated = dict(env)
    if not hidden_from_worker(env):
        isolated[CHILD_DEVICES_ENV] = json.dumps(env.get(DEVICES_ENV))
    isolated[DEVICES_ENV] = ""
    return isolated


def no_cuda_visible(env: Mapping[str, str] | None = None) -> bool:
    """Whether this environment lists no CUDA device: a serving worker with its devices hidden, or a denied media child."""
    env = os.environ if env is None else env
    return env.get(DEVICES_ENV) == ""


def hide_in_process() -> None:
    """Hide this process's own CUDA devices, for a serving process that no service manager started."""
    hidden = hide_from_worker(os.environ)
    os.environ[CHILD_DEVICES_ENV] = hidden[CHILD_DEVICES_ENV]
    os.environ[DEVICES_ENV] = hidden[DEVICES_ENV]


def deployment_devices(env: Mapping[str, str] | None = None) -> str | None:
    """The device list the deployment set (None when it set none), whether or not this process hides it."""
    env = os.environ if env is None else env
    kept = env.get(CHILD_DEVICES_ENV)
    if kept is None:
        return env.get(DEVICES_ENV)
    try:
        devices = json.loads(kept)
    except ValueError:
        return ""  # unreadable: the safe reading is no device
    if devices is not None and not isinstance(devices, str):
        return ""
    # A NUL cannot be part of an environment value; a list that holds one is damaged, so no device.
    return "" if devices is not None and "\x00" in devices else devices


def hidden_from_worker(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process runs with its CUDA devices hidden by `hide_from_worker`."""
    env = os.environ if env is None else env
    return CHILD_DEVICES_ENV in env and env.get(DEVICES_ENV) == ""


def give_devices_to_child(env: Mapping[str, str]) -> dict[str, str]:
    """The media child's environment: the deployment's device list back in place."""
    child = dict(env)
    if CHILD_DEVICES_ENV not in child:
        return child
    devices = deployment_devices(child)
    del child[CHILD_DEVICES_ENV]
    if devices is None:
        child.pop(DEVICES_ENV, None)
    else:
        child[DEVICES_ENV] = devices
    return child


def deny_gpu_to_child(env: Mapping[str, str], failure: str) -> dict[str, str]:
    """The media child's environment after a failed preflight: no device, and the reason."""
    child = dict(env)
    child[DEVICES_ENV] = ""
    child[PREFLIGHT_FAILURE_ENV] = failure
    return child


def apply_verdict(env: dict[str, str], verdict: Preflight, *, until: str) -> dict[str, str]:
    """`env` as the media child gets it: with its devices after a passed preflight, without them and with the reason after a failed one.

    `until` names what lifts the denial, in the reason the child reports.
    """
    if verdict.ok:
        return env
    return deny_gpu_to_child(
        env, f"GPU preflight {verdict.reason}; media processing runs without the GPU until {until}"
    )


def preflight_failure(env: Mapping[str, str] | None = None) -> str | None:
    """The reason the supervisor denied this media child the GPU, if it did."""
    return (os.environ if env is None else env).get(PREFLIGHT_FAILURE_ENV) or None


def preflight(env: Mapping[str, str], *, timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> Preflight:
    """Initialize CUDA once in a throwaway process that runs as the media child will.

    A signal, a timeout or a failure means the child would meet the same fault on a job, so the
    GPU stays away from it. The process has the child's interpreter, flags and environment, so
    both see the same libraries. It leads its own session: a timeout kills the whole group, and
    the wait after the kill has a limit too, so a stuck driver cannot hang the caller.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and fixed program
            [sys.executable, "-c", _PREFLIGHT_CODE],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return Preflight(False, f"could not start: {exc}")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stage = _last_stage(exc.stdout)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            return Preflight(False, f"timed out after {timeout:g} s during {stage}, and the process did not exit when killed")
        return Preflight(False, f"timed out after {timeout:g} s during {stage}")
    if process.returncode == 0:
        return Preflight(True)
    stage = _last_stage(stdout)
    if process.returncode < 0:
        return Preflight(False, f"died with {signal_name(-process.returncode)} during {stage}")
    detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return Preflight(False, f"exited {process.returncode} during {stage}" + (f": {detail[-1][:200]}" if detail else ""))


def check_gpu(env: Mapping[str, str], *, driver_present: bool, timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> Preflight:
    """The verdict on a GPU, for the media supervisor and for doctor: one rule, so they never disagree.

    Without a visible CUDA device or an NVIDIA driver there is nothing to initialize, and the verdict
    says "not tested", never "passed". `driver_present` is the caller's lookup, because this module
    imports nothing from the package.
    """
    if no_cuda_visible(env):
        return Preflight.untested("no CUDA device visible")
    if not driver_present:
        return Preflight.untested("no NVIDIA driver found")
    return preflight(env, timeout=timeout)


def signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _last_stage(output: bytes | str | None) -> str:
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else (output or "")
    lines = text.strip().splitlines()
    return lines[-1] if lines else "startup"
