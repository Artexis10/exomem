"""The disposable sensor worker: supervisor (service side) and child loop.

Model inference never runs in the service process. The dreamer's loop drives a
`Supervisor`, which starts one child process per vault
(`python -m exomem.sensor_worker_child`, on the `media_worker` pattern). The
child reads the sense queue from the projection file, judges pairs with the
admitted instrument, appends readings to the ledger and exits.

The supervisor is the policy:

* It follows its own gate (`dreamer_policy.sensor_gate`): the setting and
  standby. The child is terminated the moment that gate closes, so its memory
  returns to the host. Quiet mode, a busy service and the dreamer's tick holds
  do not stop it: it is CPU only, one thread, at the lowest priority, in its
  own process.
* It never launches in a Cloud cell (`cloud_placement`): until
  add-sensed-epistemic-model task 9.3's Cloud placement, cells write no readings.
* It launches only while the host's available memory is above the peak resident
  memory the last child recorded for the active instrument (`memory`). A
  running child is never killed for memory. With no such peak it launches, and
  that child records one.
* It spends at most `CPU_SECONDS_PER_HOUR` CPU-seconds (model load included)
  and `JUDGEMENTS_PER_HOUR` judgements per rolling hour. A child that dies
  without reporting its spend is charged its whole allotment.
* One child serves every question an active instrument answers, on one shared
  model. A child that cannot admit a question's instrument records a named
  refusal for that question and serves the others; with none left it exits
  with the refusal. A refused question is not counted in the queue depth, and
  it is tried again only when the setting or that question's instrument
  identity changes. So a refusal never relaunches the child for its own queue.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cloud_cell, runtime_resources, sensed_model, sensing, sensing_ledger

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
#: The last child's peak resident memory and the instrument key it was recorded
#: under. The supervisor owns this file: each child overwrites `SPEND_FILE` from
#: its first report, which would erase it.
PEAK_FILE = "peak-rss.json"


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
        #: Per refused question: its reason, its detail and the refusal key it was
        #: filed under (`_refusal_key`).
        self._refusals: dict[str, tuple[str, str, str]] = {}
        #: The instrument key of the running child, taken at launch, so its peak
        #: is filed under the model it loaded.
        self._launch_key: str | None = None
        self.last_action: str = "off"
        #: The recorded peak as (instrument key, bytes), loaded once from `PEAK_FILE`.
        self._peak: tuple[str, int] | None = None
        self._peak_loaded = False
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

    def _recorded_peak(self, key: str) -> int | None:
        if not self._peak_loaded:
            stored = _read_json(_state_file(self.vault_root, PEAK_FILE)) or {}
            peak = _peak_value(stored.get("peak_rss_bytes"))
            stored_key = stored.get("key")
            self._peak = (stored_key, peak) if peak is not None and isinstance(stored_key, str) else None
            self._peak_loaded = True
        if self._peak is None or self._peak[0] != key:
            return None
        return self._peak[1]

    def peak_rss_bytes(self) -> int | None:
        """The recorded child peak in bytes for the active instrument, else None.

        A peak recorded under another instrument key says nothing about the
        model that would load now, so it is ignored.
        """
        return self._recorded_peak(_instrument_key())

    def _note_peak(self, spend: dict[str, Any]) -> None:
        """Keep a child's reported peak. A final report replaces the last one.

        A report cut off mid-load is a lower bound, so it can only raise it.
        """
        reported = _peak_value(spend.get("peak_rss_bytes"))
        if reported is None:
            return
        key = self._launch_key or _instrument_key()
        recorded = self._recorded_peak(key)
        if recorded is not None and not spend.get("final"):
            reported = max(reported, recorded)
        self._peak = (key, reported)
        with contextlib.suppress(OSError):
            _write_json(
                _state_file(self.vault_root, PEAK_FILE), {"peak_rss_bytes": reported, "key": key}
            )

    def _harvest(self) -> None:
        """Charge a finished child and record why it ended."""
        child = self._child
        if child is None or child.poll() is None:
            return
        self._child = None
        pid = getattr(child, "pid", None)
        spend = self._reported(pid)
        if spend is not None and child.returncode != EXIT_REFUSED:
            # A refused child never loaded the instrument: its peak says nothing
            # about what the next launch needs.
            self._note_peak(spend)
        if spend is not None and spend.get("final"):
            self._charge(float(spend.get("cpu") or 0.0), int(spend.get("judgements") or 0))
        else:
            # It died without a final report: charge everything it was allowed.
            cpu, judgements = self._allotment
            self._charge(cpu, judgements)
        self._note_refusals(pid, whole=child.returncode == EXIT_REFUSED)

    def _note_refusals(self, pid: int | None, *, whole: bool) -> None:
        """Keep the questions the child's status file says it refused.

        `whole` is a child that served nothing. If it left no readable status, every
        answered question is refused for a reason that stays `unknown`.
        """
        status = _read_json(_state_file(self.vault_root, STATUS_FILE)) or {}
        refused = status.get("refused") if status.get("pid") == pid else None
        if not isinstance(refused, dict):
            refused = {}
        if whole and not refused:
            refused = {question: {} for question in sensed_model.answered_questions()}
        for question_type, why in refused.items():
            if question_type not in sensing.QUESTIONS or not isinstance(why, dict):
                continue
            reason = str(why.get("reason") or "unknown")
            self._refusals[question_type] = (
                reason,
                str(why.get("detail") or ""),
                _refusal_key(question_type),
            )
            log.info(
                "sensor worker refused %s (%s); not relaunching it until it can change",
                question_type,
                reason,
            )

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
            self._note_peak(spend)
            since = 0.0
            with contextlib.suppress(OSError):
                since = max(0.0, time.time() - _state_file(self.vault_root, SPEND_FILE).stat().st_mtime)
            cpu = float(spend.get("cpu") or 0.0) + min(since, self._allotment[0])
            self._charge(cpu, int(spend.get("judgements") or 0))
            return
        self._harvest()

    # the step ------------------------------------------------------------

    def step(
        self,
        *,
        gate_run: bool,
        gate_reason: str,
        queue_depth: Callable[[Sequence[str]], int],
    ) -> str:
        """One supervision decision. Returns the action taken, a closed code.

        `queue_depth` counts the queued pairs of the questions it is given: the
        supervisor gives it every answered question that is not refused.

        The codes: `off`, `gate:<reason>`, `cloud_placement`, `running`, `refused`,
        `budget`, `idle`, `memory`, `launch_failed` and `launched`.
        """
        self._harvest()
        if not sensing.enabled():
            self.terminate("off")
            return self._act("off")
        if not gate_run:
            self.terminate(gate_reason)
            return self._act(f"gate:{gate_reason}")
        if cloud_cell.cloud_mode_enabled():
            # Until add-sensed-epistemic-model task 9.3's Cloud placement, cells
            # write no readings (that change's design, R3's export note).
            self.terminate("cloud_placement")
            return self._act("cloud_placement")
        if self.alive():
            return self._act("running")
        answered = sensed_model.answered_questions()
        self._refusals = {
            question_type: refusal
            for question_type, refusal in self._refusals.items()
            if refusal[2] == _refusal_key(question_type)
        }
        open_questions = tuple(q for q in answered if q not in self._refusals)
        if answered and not open_questions:
            return self._act("refused")
        cpu, judgements = self.used()
        cpu_left = CPU_SECONDS_PER_HOUR - cpu
        judgements_left = JUDGEMENTS_PER_HOUR - judgements
        if cpu_left <= 0 or judgements_left <= 0:
            return self._act("budget")
        if queue_depth(open_questions) <= 0:
            return self._act("idle")
        # A child that could not fit is killed by the kernel after it loaded the
        # model for nothing. With no peak on record the first child measures it.
        # A host that reports no available memory launches, as an unmeasurable
        # host builds in `embedding_backend`: refusing would lock sensing out for
        # good on a host that can record a peak but not read free memory.
        peak = self.peak_rss_bytes()
        if peak is not None:
            available = runtime_resources.available_memory()
            if available is not None and available <= peak:
                if self.last_action != "memory":
                    log.info(
                        "sensor worker not launched (memory): peak %d bytes, available %d bytes",
                        peak,
                        available,
                    )
                return self._act("memory")
        self._allotment = (cpu_left, judgements_left)
        self._launch_key = _instrument_key()
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
            "refused": {question: refusal[0] for question, refusal in self._refusals.items()},
            "peak_rss_bytes": self.peak_rss_bytes(),
            "launches": self.launches,
            "kills": self.kills,
        }


def _peak_value(value: Any) -> int | None:
    """A peak in bytes: an int above zero. A bool is an int in Python, and no peak."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _instrument_key() -> str:
    """Every active instrument: what a recorded peak holds for. One child loads one
    model for all of them, so the peak has one key."""
    return json.dumps(sorted(sensed_model.active_instruments()))


def _refusal_key(question_type: str) -> str:
    """The setting and the question's active instruments: a refused question is
    tried again only when this changes."""
    from . import sensed_model

    question = sensing.question(question_type)
    return json.dumps(
        [sensing.setting(), sorted(sensed_model.active_for(sensed_model.active_instruments(), question))]
    )


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
    try:
        return supervisor(vault_root).step(
            gate_run=gate_run,
            gate_reason=gate_reason,
            queue_depth=lambda questions: sensed_model.queue_depth(
                vault_root, question_types=questions
            ),
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


def _by_question(given: Any) -> dict[str, Any]:
    """The instruments `given` (one, or a sequence) by the question their template answers."""
    items = tuple(given) if isinstance(given, (list, tuple)) else (given,)
    return {
        question_type: item
        for item in items
        for question_type, question in sensing.QUESTIONS.items()
        if question.template_version == item.identity.template_version
    }


def run_child(
    vault_root: Path,
    *,
    parent_pid: int,
    cpu_allotment: float,
    judgement_allotment: int,
    idle_seconds: float = IDLE_EXIT_SECONDS,
    instrument: Any = None,
    admit: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    cpu_clock: Callable[[], float] = time.process_time,
) -> int:
    """Sense queued pairs until idle, out of budget, or orphaned. Returns an exit code.

    One child serves every registered question whose instrument it can admit.
    `instrument` is a ready instrument, or a sequence of them, for tests; `admit`
    maps a question type to its admitted instrument. Each question that cannot be
    admitted is recorded in the status file and left unsensed.

    Reads only the projection's queue and the ledger, and appends only to the
    ledger. Never reads or writes the vault.
    """
    vault_root = Path(vault_root)
    pid = os.getpid()
    started_cpu = cpu_clock()
    judgements = 0

    def report(final: bool) -> None:
        spend: dict[str, Any] = {
            "pid": pid,
            "cpu": round(max(0.0, cpu_clock() - started_cpu), 3),
            "judgements": judgements,
            "final": final,
        }
        # Recorded only where the host reports it, so the supervisor can admit
        # the next launch against what this child needed.
        peak = runtime_resources.peak_rss_bytes()
        if peak is not None:
            spend["peak_rss_bytes"] = peak
        _write_json(_state_file(vault_root, SPEND_FILE), spend)

    def refuse(refused: dict[str, dict[str, str]]) -> int:
        _write_json(_state_file(vault_root, STATUS_FILE), {"refused": refused, "pid": pid})
        report(True)
        return EXIT_REFUSED

    report(False)
    instruments: dict[str, Any]
    refused: dict[str, dict[str, str]] = {}
    if instrument is None:
        from . import sensing_nli

        def admit_question(question_type: str) -> Any:
            return sensing_nli.admit(
                evidence_path=_state_file(vault_root, ADMISSION_FILE), question_type=question_type
            )

        instruments = {}
        for question_type in sensing.QUESTIONS:
            try:
                instruments[question_type] = (admit or admit_question)(question_type)
            except sensing_nli.Refused as why:
                refused[question_type] = {"reason": why.reason, "detail": why.detail[:500]}
        if not instruments:
            return refuse(refused)
    else:
        instruments = _by_question(instrument)
    ledger = sensing_ledger.Ledger(vault_root)
    try:
        lconn = ledger.connect()
    except sensing_ledger.LedgerUnavailable as error:
        return refuse(
            {
                question_type: {"reason": "ledger-unavailable", "detail": str(error)[:500]}
                for question_type in instruments
            }
        )
    _write_json(_state_file(vault_root, STATUS_FILE), {"refused": refused, "pid": pid})
    done: set[tuple[str, str]] = set()
    last_work = clock()
    try:
        while _parent_alive(parent_pid):
            if cpu_clock() - started_cpu >= cpu_allotment or judgements >= judgement_allotment:
                report(True)
                return EXIT_BUDGET
            batch = _next_batch(
                vault_root, lconn, instruments, done, min(BATCH, judgement_allotment - judgements)
            )
            if not batch:
                if clock() - last_work >= idle_seconds:
                    report(True)
                    return EXIT_IDLE
                sleep(min(1.0, idle_seconds))
                continue
            last_work = clock()
            grouped: dict[str, list[Any]] = {}
            for pair in batch:
                grouped.setdefault(pair.question_type, []).append(pair)
            readings = []
            for question_type, pairs in grouped.items():
                judge = instruments[question_type]
                try:
                    results = judge.judge([pair.texts for pair in pairs])
                except ValueError:
                    # Non-finite or misshapen output refuses that output: no reading,
                    # and the pairs are not retried by this child.
                    log.warning("sensor worker: a batch produced unusable output", exc_info=True)
                    done.update((question_type, pair.pair_key) for pair in pairs)
                    judgements += len(pairs)
                    continue
                for pair, (ab, ba, reason) in zip(pairs, results, strict=True):
                    done.add((question_type, pair.pair_key))
                    reading = sensing.make_reading(
                        pair.question_type,
                        judge.identity,
                        list(pair.inputs),
                        ab=ab,
                        ba=ba,
                        abstain_reason=reason,
                        sensed_at=_utc_now(),
                    )
                    if reading is not None:
                        readings.append(reading)
                judgements += len(pairs)
            ledger.append(lconn, readings)
            report(False)
        report(True)
        return EXIT_IDLE
    finally:
        lconn.close()


def _next_batch(
    vault_root: Path,
    lconn: Any,
    instruments: Mapping[str, Any],
    done: set[tuple[str, str]],
    limit: int,
) -> list:
    """The next queued pairs of the served questions that the ledger has no reading for."""
    if limit <= 0:
        return []
    conn = sensed_model.open_readonly(vault_root)
    if conn is None:
        return []
    out = []
    try:
        for pair in sensed_model.queued_pairs(conn, 256, question_types=tuple(instruments)):
            marker = (pair.question_type, pair.pair_key)
            if marker in done:
                continue
            hashes = [item.text_sha256 for item in pair.inputs]
            if [sensing.text_sha256(text) for text in pair.texts] != hashes:
                done.add(marker)
                continue
            instrument_id = instruments[pair.question_type].identity.instrument_id
            rid = sensing.reading_id(pair.question_type, instrument_id, hashes)
            if sensing_ledger.has_reading(lconn, rid):
                done.add(marker)
                continue
            out.append(pair)
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out
