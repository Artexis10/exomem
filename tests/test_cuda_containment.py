"""The CUDA containment seams: what the serving worker hides and what the preflight sees."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from exomem import cuda_containment

DEPLOYMENT = {"CUDA_VISIBLE_DEVICES": "0"}


def test_a_process_hidden_twice_still_gives_the_media_child_the_deployments_device_list() -> None:
    once = cuda_containment.hide_from_worker(DEPLOYMENT)  # the service manager starts the worker
    twice = cuda_containment.hide_from_worker(once)  # `server.run` hides again

    assert twice["CUDA_VISIBLE_DEVICES"] == ""
    assert cuda_containment.give_devices_to_child(twice)["CUDA_VISIBLE_DEVICES"] == "0"


def test_a_damaged_kept_device_list_leaves_the_media_child_no_device_instead_of_an_unstartable_environment() -> None:
    damaged = {"CUDA_VISIBLE_DEVICES": "", cuda_containment.CHILD_DEVICES_ENV: json.dumps("0\x00")}

    # An environment value with a NUL makes `Popen` raise, which would stop the supervisor.
    assert cuda_containment.give_devices_to_child(damaged)["CUDA_VISIBLE_DEVICES"] == ""


def test_a_preflight_that_hangs_returns_in_bounded_time_even_when_a_helper_holds_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cuda_containment,
        "_PREFLIGHT_CODE",
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('torch CUDA initialization', flush=True)\n"
        "time.sleep(60)\n",
    )
    started = time.monotonic()

    verdict = cuda_containment.preflight(os.environ, timeout=1.0)

    assert time.monotonic() - started < 20
    assert not verdict.ok
    assert verdict.reason == "timed out after 1 s during torch CUDA initialization"


def test_the_preflight_loads_libraries_as_the_media_child_does(tmp_path: Path) -> None:
    # A torch that only PYTHONPATH supplies is the one the media child imports, so the preflight must meet it too.
    (tmp_path / "torch.py").write_text("import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PATH": os.environ.get("PATH", "")}

    verdict = cuda_containment.preflight(env, timeout=30.0)

    assert (verdict.ok, verdict.reason) == (False, "died with SIGSEGV during importing torch")
