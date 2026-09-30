"""The disposable sensor worker: supervisor (service side) and child loop.

Model inference never runs in the service process. The dreamer's loop drives a
`Supervisor`, which starts one child process per vault
(`python -m exomem.sensor_worker_child`, on the `media_worker` pattern). The
child reads the sense queue from the projection file, judges pairs with the
admitted instrument, appends readings to the ledger and exits.

The supervisor is the policy:

* It inherits the dreamer's gate (`dreamer_policy.decide`). The child runs only
  while a dreamer tick could run, and is terminated the moment the gate closes.
* It kills the child in quiet mode, under auto-quiet pressure and in standby,
  so the child's memory returns to the host.
* It spends at most `CPU_SECONDS_PER_HOUR` CPU-seconds (model load included)
  and `JUDGEMENTS_PER_HOUR` judgements per rolling hour. A child that dies
  without reporting its spend is charged its whole allotment.
* A child that cannot admit its instrument exits with a named refusal. It is
  not relaunched until the setting or the instrument identity changes.

Every number states what it prevents, and none has an environment knob.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sensing, sensing_ledger

log = logging.getLogger(__name__)

#: CPU-seconds per rolling hour, model load included: about 8% of one core.
#: Keeps sensing from turning into steady load on a shared host.
CPU_SECONDS_PER_HOUR = 300.0
#: Judgements per rolling hour. Bounds the ledger's growth rate and the work a
#: pathological queue can demand.
JUDGEMENTS_PER_HOUR = 600
WINDOW_SECONDS = 3600.0
#: A child with nothing to sense exits after this long, returning its memory.
IDLE_EXIT_SECONDS = 60.0
#: How often the dreamer's loop re-evaluates the gate while a child lives.
POLL_WHILE_ALIVE_SECONDS = 2.0
#: Grace between terminate and kill.
GRACE_SECONDS = 5.0
#: Pairs per forward pass.
BATCH = 8

EXIT_IDLE = 0
EXIT_BUDGET = 75
EXIT_REFUSED = 78

SPEND_FILE = "spend.json"
STATUS_FILE = "worker-status.json"
ADMISSION_FILE = "admission.json"

def _state_file(vault_root: Path, name: str) -> Path:
    return sensing_ledger.ledger_path(vault_root).with_name(name)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ----------------------------------------------------------------------
# supervisor
# ----------------------------------------------------------------------


@dataclass
class _Charge:
    at: float
    cpu: float
    judgements: int


class Supervisor:
    """One vault's sensor child and its budgets. Driven from the dreamer loop."""

    def __init__(
        self,
        vault_root: Path,
        *,
        launch: Callable[[Path, float, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vault_root = Path(vault_root)
        self._launch = launch or _launch_child
        self._clock = clock
        self._child: Any = None
        self._allotment: tuple[float, int] = (0.0, 0)
        self._window: deque[_Charge] = deque()
        self._refusal: tuple[str, str] | None = None
        self._refusal_key: str | None = None
        self.last_action: str = "off"
        self.launches = 0
        self.kills = 0

    # budget -------------------------------------------------------------

    def _expire(self, now: float) -> None:
        while self._window and now - self._window[0].at >= WINDOW_SECONDS:
            self._window.popleft()

    def used(self) -> tuple[float, int]:
        now = self._clock()
        self._expire(now)
        return (
            sum(entry.cpu for entry in self._window),
            sum(entry.judgements for entry in self._window),
        )

    def _charge(self, cpu: float, judgements: int) -> None:
        self._window.append(_Charge(self._clock(), max(0.0, float(cpu)), max(0, int(judgements))))

    # child ---------------------------------------------------------------

    def alive(self) -> bool:
        child = self._child
        return child is not None and child.poll() is None

    def _reported(self, pid: int | None) -> dict[str, Any] | None:
        spend = _read_json(_state_file(self.vault_root, SPEND_FILE))
        if spend is None or pid is None or spend.get("pid") != pid:
            return None
        return spend

    def _harvest(self) -> None:
        """Charge a finished child and record why it ended."""
        child = self._child
        if child is None or child.poll() is None:
            return
        self._child = None
        pid = getattr(child, "pid", None)
        spend = self._reported(pid)
        if spend is not None and spend.get("final"):
            self._charge(float(spend.get("cpu") or 0.0), int(spend.get("judgements") or 0))
        else:
            # It died without a final report: charge everything it was allowed.
            cpu, judgements = self._allotment
            self._charge(cpu, judgements)
        if child.returncode == EXIT_REFUSED:
            status = _read_json(_state_file(self.vault_root, STATUS_FILE)) or {}
            reason = str(status.get("refused") or "unknown")
            self._refusal = (reason, str(status.get("detail") or ""))
            self._refusal_key = _refusal_key()
            log.info("sensor worker refused (%s); not relaunching until it can change", reason)

    def terminate(self, reason: str) -> None:
        """Stop the child now: terminate, then kill after the grace period."""
        child = self._child
        if child is None:
            return
        if child.poll() is None:
            with contextlib.suppress(OSError):
                child.terminate()
            try:
                child.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    child.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=GRACE_SECONDS)
            self.kills += 1
            log.info("sensor worker stopped (%s)", reason)
        # A terminated child's last report may be stale (mid model load or mid
        # forward pass). Charge what it reported plus the wall time since, which
        # bounds a one-thread child's CPU; with no report at all, `_harvest`
        # charges its whole allotment.
        pid = getattr(child, "pid", None)
        spend = self._reported(pid)
        if spend is not None and not spend.get("final"):
            self._child = None
            since = 0.0
            with contextlib.suppress(OSError):
                since = max(0.0, time.time() - _state_file(self.vault_root, SPEND_FILE).stat().st_mtime)
            cpu = float(spend.get("cpu") or 0.0) + min(since, self._allotment[0])
            self._charge(cpu, int(spend.get("judgements") or 0))
            return
        self._harvest()

    # the step ------------------------------------------------------------

    def step(self, *, gate_run: bool, gate_reason: str, queue_depth: Callable[[], int]) -> str:
        """One supervision decision. Returns the action taken, a closed code."""
        self._harvest()
        if not sensing.enabled():
            self.terminate("off")
            return self._act("off")
        if not gate_run:
            self.terminate(gate_reason)
            return self._act(f"gate:{gate_reason}")
        if self.alive():
            return self._act("running")
        if self._refusal is not None:
            if self._refusal_key == _refusal_key():
                return self._act("refused")
            self._refusal = None
            self._refusal_key = None
        cpu, judgements = self.used()
        cpu_left = CPU_SECONDS_PER_HOUR - cpu
        judgements_left = JUDGEMENTS_PER_HOUR - judgements
        if cpu_left <= 0 or judgements_left <= 0:
            return self._act("budget")
        if queue_depth() <= 0:
            return self._act("idle")
        self._allotment = (cpu_left, judgements_left)
        try:
            self._child = self._launch(self.vault_root, cpu_left, judgements_left)
        except OSError:
            log.warning("sensor worker could not start", exc_info=True)
            self._child = None
            return self._act("launch_failed")
        self.launches += 1
        return self._act("launched")

    def _act(self, action: str) -> str:
        self.last_action = action
        return action

    def status(self) -> dict[str, Any]:
        cpu, judgements = self.used()
        return {
            "action": self.last_action,
            "running": self.alive(),
            "hour_cpu_used": round(cpu, 3),
            "hour_judgements": judgements,
            "refused": None if self._refusal is None else self._refusal[0],
            "launches": self.launches,
            "kills": self.kills,
        }


def _refusal_key() -> str:
    """What must change before a refused child is tried again."""
    from . import sensed_model

    return json.dumps([sensing.setting(), sorted(sensed_model.active_instruments())])


#: The only variables the child inherits: where state, logs and the model
#: cache live, the platform basics, and the locale. The child runs model code on
#: vault text, so it gets no credential, token or service secret.
_CHILD_ENV_ALLOW = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ",
        "TMPDIR", "TMP", "TEMP", "PYTHONPATH", "VIRTUAL_ENV",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
        "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
        "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME", "TORCH_HOME",
        "EXOMEM_STATE_ROOT", "EXOMEM_HOSTED_STATE_ROOT", "EXOMEM_HOSTED_CELL",
        "EXOMEM_KB_DIRNAME", "EXOMEM_LOG_DIR", "EXOMEM_LOG_LEVEL", "EXOMEM_LOG_MAX_MB",
        "EXOMEM_LOG_BACKUPS", "EXOMEM_MODEL_OFFLINE", "EXOMEM_CPU_THREADS",
        # The image's glibc arena bound (bound-cell-memory D2) holds for children too.
        "MALLOC_ARENA_MAX",
    }
)
_CHILD_ENV_DENY = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
#: Set in the child whatever the parent has: offline, CPU only (owner ruling R5)
#: and one inference thread.
_CHILD_ENV_FORCED = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "CUDA_VISIBLE_DEVICES": "",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The sensor child's environment: an allowlist, never a copy of the parent's."""
    env = {
        name: value
        for name, value in parent.items()
        if (name.upper() in _CHILD_ENV_ALLOW or name.upper().startswith("LC_"))
        and not any(word in name.upper() for word in _CHILD_ENV_DENY)
    }
    env.update(_CHILD_ENV_FORCED)
    return env


def _launch_child(vault_root: Path, cpu_allotment: float, judgement_allotment: int) -> Any:
    env = child_env(os.environ)
    args = [
        sys.executable,
        "-m",
        "exomem.sensor_worker_child",
        "--vault",
        str(vault_root),
        "--parent-pid",
        str(os.getpid()),
        "--cpu",
        f"{cpu_allotment:.3f}",
        "--judgements",
        str(int(judgement_allotment)),
        "--idle-seconds",
        str(IDLE_EXIT_SECONDS),
    ]
    log.info("sensor worker: starting disposable child")
    return subprocess.Popen(args, env=env)  # noqa: S603 - fixed interpreter/module command


_SUPERVISORS: dict[str, Supervisor] = {}
_LOCK = threading.Lock()


def supervisor(vault_root: Path) -> Supervisor:
    key = str(Path(vault_root))
    with _LOCK:
        found = _SUPERVISORS.get(key)
        if found is None:
            found = _SUPERVISORS[key] = Supervisor(Path(vault_root))
        return found


def supervise(vault_root: Path, *, gate_run: bool, gate_reason: str) -> str:
    """The dreamer loop's call. Never raises."""
    from . import sensed_model

    try:
        return supervisor(vault_root).step(
            gate_run=gate_run,
            gate_reason=gate_reason,
            queue_depth=lambda: sensed_model.queue_depth(vault_root),
        )
    except Exception:  # noqa: BLE001 - supervision never breaks the dreamer
        log.warning("sensor worker: supervision failed", exc_info=True)
        return "error"


def alive(vault_root: Path) -> bool:
    with _LOCK:
        found = _SUPERVISORS.get(str(Path(vault_root)))
    return found is not None and found.alive()


def shutdown() -> None:
    """Stop every child. Called when the dreamer stops."""
    with _LOCK:
        found = list(_SUPERVISORS.values())
    for item in found:
        with contextlib.suppress(Exception):
            item.terminate("shutdown")


def reset_for_tests() -> None:
    shutdown()
    with _LOCK:
        _SUPERVISORS.clear()


# ----------------------------------------------------------------------
# child
# ----------------------------------------------------------------------


def _parent_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return True
    from .media_jobs import pid_alive

    return pid_alive(parent_pid)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_child(
    vault_root: Path,
    *,
    parent_pid: int,
    cpu_allotment: float,
    judgement_allotment: int,
    idle_seconds: float = IDLE_EXIT_SECONDS,
    instrument: Any = None,
    admit: Callable[[], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    cpu_clock: Callable[[], float] = time.process_time,
) -> int:
    """Sense queued pairs until idle, out of budget, or orphaned. Returns an exit code.

    Reads only the projection's queue and the ledger, and appends only to the
    ledger. Never reads or writes the vault.
    """
    vault_root = Path(vault_root)
    pid = os.getpid()
    started_cpu = cpu_clock()
    judgements = 0

    def report(final: bool) -> None:
        _write_json(
            _state_file(vault_root, SPEND_FILE),
            {
                "pid": pid,
                "cpu": round(max(0.0, cpu_clock() - started_cpu), 3),
                "judgements": judgements,
                "final": final,
            },
        )

    report(False)
    if instrument is None:
        from . import sensing_nli

        try:
            instrument = (admit or (lambda: sensing_nli.admit(
                evidence_path=_state_file(vault_root, ADMISSION_FILE)
            )))()
        except sensing_nli.Refused as refused:
            _write_json(
                _state_file(vault_root, STATUS_FILE),
                {"refused": refused.reason, "detail": refused.detail[:500], "pid": pid},
            )
            report(True)
            return EXIT_REFUSED
    identity: sensing.InstrumentIdentity = instrument.identity
    ledger = sensing_ledger.Ledger(vault_root)
    try:
        lconn = ledger.connect()
    except sensing_ledger.LedgerUnavailable as error:
        _write_json(
            _state_file(vault_root, STATUS_FILE),
            {"refused": "ledger-unavailable", "detail": str(error)[:500], "pid": pid},
        )
        report(True)
        return EXIT_REFUSED
    _write_json(_state_file(vault_root, STATUS_FILE), {"refused": None, "pid": pid})
    done: set[str] = set()
    last_work = clock()
    try:
        while _parent_alive(parent_pid):
            if cpu_clock() - started_cpu >= cpu_allotment or judgements >= judgement_allotment:
                report(True)
                return EXIT_BUDGET
            batch = _next_batch(
                vault_root, lconn, identity, done, min(BATCH, judgement_allotment - judgements)
            )
            if not batch:
                if clock() - last_work >= idle_seconds:
                    report(True)
                    return EXIT_IDLE
                sleep(min(1.0, idle_seconds))
                continue
            last_work = clock()
            try:
                results = instrument.judge([pair.texts for pair in batch])
            except ValueError:
                # Non-finite or misshapen output refuses that output: no reading,
                # and the batch is not retried by this child.
                log.warning("sensor worker: a batch produced unusable output", exc_info=True)
                done.update(pair.pair_key for pair in batch)
                judgements += len(batch)
                report(False)
                continue
            readings = []
            for pair, (ab, ba, reason) in zip(batch, results, strict=True):
                done.add(pair.pair_key)
                reading = sensing.make_reading(
                    sensing.PAIR_RELATION,
                    identity,
                    list(pair.inputs),
                    ab=ab,
                    ba=ba,
                    abstain_reason=reason,
                    sensed_at=_utc_now(),
                )
                if reading is not None:
                    readings.append(reading)
            ledger.append(lconn, readings)
            judgements += len(batch)
            report(False)
        report(True)
        return EXIT_IDLE
    finally:
        lconn.close()


def _next_batch(vault_root, lconn, identity, done: set[str], limit: int) -> list:
    from . import sensed_model

    if limit <= 0:
        return []
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return []
    out = []
    try:
        for pair in sensed_model.queued_pairs(conn, 256):
            if pair.pair_key in done:
                continue
            hashes = [item.text_sha256 for item in pair.inputs]
            if [sensing.text_sha256(text) for text in pair.texts] != hashes:
                done.add(pair.pair_key)
                continue
            rid = sensing.reading_id(sensing.PAIR_RELATION, identity.instrument_id, hashes)
            if sensing_ledger.has_reading(lconn, rid):
                done.add(pair.pair_key)
                continue
            out.append(pair)
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out
