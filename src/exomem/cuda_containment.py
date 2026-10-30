"""Keep a CUDA fault away from the serving worker.

A CUDA library can crash the process that initializes it. Only the disposable media child may
touch the GPU: the service starts the serving worker with no CUDA device, and the media
supervisor hands the child its device list only after a throwaway process has initialized CUDA
without dying. This module imports nothing from the package, so any layer may use it.
"""

from __future__ import annotations

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
    ok: bool
    reason: str | None = None


def hide_from_worker(env: Mapping[str, str]) -> dict[str, str]:
    """The serving worker's environment: no CUDA device, and the deployment's list kept for the media child."""
    isolated = dict(env)
    isolated[CHILD_DEVICES_ENV] = json.dumps(env.get(DEVICES_ENV))
    isolated[DEVICES_ENV] = ""
    return isolated


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
    return devices if isinstance(devices, str) or devices is None else ""


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


def preflight_failure(env: Mapping[str, str] | None = None) -> str | None:
    """The reason the supervisor denied this media child the GPU, if it did."""
    return (os.environ if env is None else env).get(PREFLIGHT_FAILURE_ENV) or None


def preflight(env: Mapping[str, str], *, timeout: float = PREFLIGHT_TIMEOUT_SECONDS) -> Preflight:
    """Initialize CUDA once in a throwaway process that has the media child's environment.

    A signal, a timeout or a failure means the child would meet the same fault on a job, so the
    GPU stays away from it. The process runs the same interpreter in isolated mode.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed interpreter and fixed program
            [sys.executable, "-I", "-c", _PREFLIGHT_CODE],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return Preflight(False, f"timed out after {timeout:g} s during {_last_stage(exc.stdout)}")
    except OSError as exc:
        return Preflight(False, f"could not start: {exc}")
    if done.returncode == 0:
        return Preflight(True)
    stage = _last_stage(done.stdout)
    if done.returncode < 0:
        try:
            name = signal.Signals(-done.returncode).name
        except ValueError:
            name = f"signal {-done.returncode}"
        return Preflight(False, f"died with {name} during {stage}")
    detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return Preflight(False, f"exited {done.returncode} during {stage}" + (f": {detail[-1][:200]}" if detail else ""))


def _last_stage(output: bytes | str | None) -> str:
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else (output or "")
    lines = text.strip().splitlines()
    return lines[-1] if lines else "startup"
