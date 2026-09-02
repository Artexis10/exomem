"""`logs/ledger.jsonl` — one durable, hash-chained row per MCP tool call.

Exomem already traces every call. `CallTraceMiddleware` emits `tool_start` /
`tool_success` / `tool_failure` / `tool_error` prose lines to the rotating
process log, and three JSONL journals record the *subsets* that are reads,
queries, and mutations. None of that answers the question an operator actually
asks during an incident: **which client made which calls, in what order, and
which of them were refused.**

Four gaps, each verified against a live install:

* **No caller identity anywhere.** Not one of `mutations.jsonl`,
  `reads.jsonl`, `queries.jsonl`, or the server log records which client is
  calling. A vault served to claude.ai, ChatGPT, Codex, and a local CLI at the
  same time produces one undifferentiated stream.
* **Only three tool families journal at all.** A call that is neither a read,
  a query, nor a mutation leaves no structured row — it exists only as a prose
  log line.
* **The prose lines are evictable by volume**, including by the traceback storm
  that accompanies the incident whose calls you need to reconstruct.
* **The one durable per-request store is a replay cache, not a ledger**:
  `.idempotency-<vault>.sqlite` is mutations-only, keyed so a replay overwrites
  the row, and TTL-pruned.

So: one flat row per call, appended host-local *outside the vault and outside
the writer-lease boundary* — a ledger that cannot write during a read-only
incident cannot explain that incident. Rows carry a monotonic `sequence` and a
`prev_hash`/`row_hash` chain, so a dropped, reordered, or edited row is
detectable rather than silent.

Redaction is a property of what the row builder puts in the row, never of a
downstream filter: arguments are recorded as name, byte length, and sha256 of
the value, and the value itself is never written. That has to hold by
construction, because `privacy_log`'s process-wide redactor is gated on
`EXOMEM_HOSTED_CELL` and is simply off for local installs.

**Single writer.** The chain head is held in-process under a cheap lock rather
than behind a cross-process file lock, because every MCP tool call is dispatched
in one server process and `resolve_log_dir()` is host-local — two writers cannot
reach the same ledger. If that ever stops holding, the symptom is not silent:
two processes appending would collide on `sequence`, which is exactly what
`verify` reports.

This module measures. It never scores, ranks, or interprets a call, and it runs
no model.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: The `prev_hash` of the very first row of a fresh ledger.
GENESIS_HASH = "0" * 64

#: Rotate the live file past this size, modelled on `log.md`'s rotation rather
#: than on the unbounded `queries.jsonl` / `reads.jsonl` / `writes.jsonl`.
ROTATE_BYTES_DEFAULT = 8_000_000
#: Rows kept in the live file when rotation fires. The rest move byte-exact
#: into a content-addressed archive; `sequence` never resets.
ROTATE_KEEP_ROWS_DEFAULT = 2_000

#: Bound on any single recorded string, so one pathological argument name or
#: path cannot produce a row too large to write atomically.
_MAX_FIELD_CHARS = 512
#: Bound on how many arguments and target paths a row enumerates.
_MAX_ARGS = 64
_MAX_TARGET_PATHS = 32

_LEDGER_FILENAME = "ledger.jsonl"
_ARCHIVE_DIRNAME = "ledger-archive"

_lock = threading.Lock()
#: Chain head, cached in-process. `None` means "not yet loaded from disk".
_state: dict[str, Any] = {"sequence": None, "prev_hash": None, "path": None}


def enabled() -> bool:
    """The ledger is the durable half of an always-installed tracer, so it is on
    by default; `EXOMEM_DISABLE_CALL_LEDGER` mirrors `EXOMEM_DISABLE_QUERY_LOG`."""
    return not os.environ.get("EXOMEM_DISABLE_CALL_LEDGER")


def ledger_dir() -> Path:
    """Host-local, outside the vault, and outside the writer-lease boundary.

    Defaults to the same directory the other journals use, so `exomem logs` and
    `exomem trace` find it without a second location to explain, and an operator
    already looking at `mutations.jsonl` is looking at the ledger too.
    """
    override = os.environ.get("EXOMEM_CALL_LEDGER_DIR")
    if override:
        return Path(override).expanduser()
    from .logging_config import resolve_log_dir

    return resolve_log_dir()


def ledger_path() -> Path:
    return ledger_dir() / _LEDGER_FILENAME


def archive_dir() -> Path:
    return ledger_dir() / _ARCHIVE_DIRNAME


def canonical_json(payload: dict[str, Any]) -> bytes:
    """The exact bytes a row hashes over: sorted keys, compact, UTF-8."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def row_hash(payload: dict[str, Any]) -> str:
    """sha256 of the row's canonical form, excluding `row_hash` itself."""
    without = {key: value for key, value in payload.items() if key != "row_hash"}
    return hashlib.sha256(canonical_json(without)).hexdigest()


def _clip(value: object) -> str:
    text = str(value)
    return text if len(text) <= _MAX_FIELD_CHARS else text[:_MAX_FIELD_CHARS]


def _argument_shape(arguments: dict[str, Any]) -> tuple[list[str], dict[str, dict], bool]:
    """Names, byte length, and sha256 per argument. Never a value.

    The serialized form is hashed rather than `repr`, so the same argument
    hashes identically across calls and processes -- which is what makes the
    ledger answer "is this client sending the same call over and over?".
    """
    names = sorted(str(name) for name in arguments)
    truncated = len(names) > _MAX_ARGS
    shape: dict[str, dict] = {}
    for name in names[:_MAX_ARGS]:
        try:
            raw = canonical_json({"v": arguments[name]})
        except (TypeError, ValueError):
            raw = repr(arguments[name]).encode("utf-8", "replace")
        shape[_clip(name)] = {
            "len": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return [_clip(name) for name in names[:_MAX_ARGS]], shape, truncated


#: Argument names that address a page rather than carry content. Recorded
#: structurally because "which page did that call touch?" is the first question
#: of any forensic pass, and a path is not content.
_TARGET_ARG_NAMES = ("path", "old_path", "new_path", "paths", "target", "targets", "file")


def _target_paths(arguments: dict[str, Any]) -> tuple[list[str], bool]:
    found: list[str] = []
    for name in _TARGET_ARG_NAMES:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            found.append(_clip(value.strip()))
        elif isinstance(value, (list, tuple)):
            found.extend(_clip(item) for item in value if isinstance(item, str) and item.strip())
    truncated = len(found) > _MAX_TARGET_PATHS
    return found[:_MAX_TARGET_PATHS], truncated


def build_row(
    *,
    sequence: int,
    prev_hash: str,
    request_id: str,
    tool: str,
    outcome: str,
    duration_ms: float | None,
    total_ms: float | None = None,
    error_code: str | None = None,
    arguments: dict[str, Any] | None = None,
    caller_principal_hash: str | None = None,
    client_name: str | None = None,
    client_version: str | None = None,
    transport: str | None = None,
    session_id: str | None = None,
    spans: list[dict[str, Any]] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Assemble one complete, self-hashing ledger row."""
    arg_names, args, args_truncated = _argument_shape(arguments or {})
    targets, targets_truncated = _target_paths(arguments or {})
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "prev_hash": prev_hash,
        "ts_utc": timestamp
        or dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
        "request_id": request_id,
        "session_id": _clip(session_id) if session_id else None,
        "client_name": _clip(client_name) if client_name else None,
        "client_version": _clip(client_version) if client_version else None,
        "transport": _clip(transport) if transport else None,
        "caller_principal_hash": _clip(caller_principal_hash)
        if caller_principal_hash
        else None,
        "tool": _clip(tool),
        "arg_names": arg_names,
        "args": args,
        "target_paths": targets,
        "outcome": outcome,
        "error_code": _clip(error_code) if error_code else None,
        # Two clocks, deliberately. `duration_ms` is the leaf -- the number the
        # prose trace and `exomem_tool_duration_ms` have always reported.
        # `total_ms` is the wall clock the caller actually waited, guard and
        # argument normalization included. When they diverge, the gap *is* the
        # finding: it says the cost was in admission, not in the work.
        "duration_ms": duration_ms,
        "total_ms": total_ms if total_ms is not None else duration_ms,
        # The size of what was sent, so a slow call is interpretable rather than
        # merely slow. Recorded from the already-computed argument shape, which
        # is why it costs nothing and still leaks nothing.
        "request_bytes": sum(int(shape["len"]) for shape in args.values()),
        # Where the time went, aggregated per phase. Two boundary clocks can
        # prove a call was slow and cannot say why: a live `edit_memory` showed
        # total_ms=24,394 against boundary_hold_ms=3,348, which ruled out lock
        # contention and left 21 seconds unattributed. These are the spans that
        # would have named them. Empty for an uninstrumented path -- absence
        # means "nothing reported", never "nothing happened".
        "spans": _clip_spans(spans),
        "truncated": bool(args_truncated or targets_truncated),
    }
    row["row_hash"] = row_hash(row)
    return row


#: Phases kept in one row. The producer already bounds distinct names per call;
#: this is the independent bound, because the row is hash-chained and an
#: unbounded field would let one pathological call dominate the ledger file.
_MAX_SPANS = 32


def _clip_spans(spans: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize reported phases into a bounded, canonical shape.

    Slowest first, so truncation drops what matters least. Rebuilt field by
    field rather than passed through, because these rows are hashed: an
    unexpected key from a future caller would otherwise change a row's identity
    without any reader knowing what it meant.
    """
    if not spans:
        return []
    shaped: list[dict[str, Any]] = []
    for entry in spans:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            ms = round(float(entry.get("ms", 0.0)), 2)
            count = int(entry.get("count", 1))
        except (TypeError, ValueError):
            continue
        shaped.append({"name": _clip(name), "count": count, "ms": ms})
    shaped.sort(key=lambda item: item["ms"], reverse=True)
    return shaped[:_MAX_SPANS]


def _read_chain_head(path: Path) -> tuple[int, str]:
    """Resume the chain from the live file's last row.

    A fresh process must not restart `sequence` at zero, or every restart would
    look like the tampering the sequence exists to detect.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return 0, GENESIS_HASH
        last = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    last = stripped
        if last is None:
            return 0, GENESIS_HASH
        record = json.loads(last)
        return int(record["sequence"]), str(record["row_hash"])
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable tail must not stop the ledger recording *new* calls; the
        # break stays visible as a chain mismatch at exactly this point.
        return 0, GENESIS_HASH


def _rotate_if_needed(path: Path, *, rotate_bytes: int, keep_rows: int) -> None:
    """Move the oldest rows byte-exact into a content-addressed archive.

    `sequence` does not reset and the chain spans the boundary: the archived
    segment's last `row_hash` is still the first retained row's `prev_hash`, so
    a verifier walks archive then live continuously.
    """
    try:
        if not path.exists() or path.stat().st_size <= rotate_bytes:
            return
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        if len(lines) <= keep_rows:
            return
        cut = len(lines) - keep_rows
        head, tail = b"".join(lines[:cut]), b"".join(lines[cut:])
        destination = archive_dir()
        destination.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(head).hexdigest()[:16]
        archive = destination / f"ledger-{digest}.jsonl"
        if not archive.exists():
            archive.write_bytes(head)
            _restrict(archive)
        temporary = path.with_name(path.name + ".rotating")
        temporary.write_bytes(tail)
        _restrict(temporary)
        os.replace(temporary, path)
    except OSError:
        return


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass  # Windows and some mounts have no meaningful mode; not a failure.


def append_row(row: dict[str, Any], *, path: Path | None = None) -> None:
    """One bounded, newline-terminated buffer, one `os.write`, `O_APPEND`.

    `O_APPEND` advances the position atomically, so a row stays intact even if a
    child process inherited the descriptor. No fsync: this is diagnostic, the
    budget is microseconds, and a crash-lost tail shows up as a `sequence` gap
    rather than as silence.
    """
    target = path or ledger_path()
    payload = canonical_json(row) + b"\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_APPEND)
    except FileNotFoundError:
        # First row of a fresh ledger. Creating the directory and forcing the
        # mode here, rather than on every append, keeps the steady-state cost of
        # a row at one open/write/close -- this sits in every call's critical
        # section, so a redundant `mkdir` and `chmod` per row is not free.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        _restrict(target)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def record_call(
    *,
    request_id: str,
    tool: str,
    outcome: str,
    duration_ms: float | None,
    total_ms: float | None = None,
    error_code: str | None = None,
    arguments: dict[str, Any] | None = None,
    caller_principal_hash: str | None = None,
    client_name: str | None = None,
    client_version: str | None = None,
    transport: str | None = None,
    session_id: str | None = None,
    spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Append exactly one ledger row. Never raises into the call path.

    Returns the row for tests and callers that want it; `None` when the ledger
    is disabled or the append could not be made. A ledger that breaks a call it
    was only supposed to describe is worse than no ledger.
    """
    if not enabled():
        return None
    try:
        rotate_bytes = _int_env("EXOMEM_CALL_LEDGER_ROTATE_BYTES", ROTATE_BYTES_DEFAULT)
        keep_rows = _int_env("EXOMEM_CALL_LEDGER_KEEP_ROWS", ROTATE_KEEP_ROWS_DEFAULT)
        path = ledger_path()
        with _lock:
            if _state["sequence"] is None or _state["path"] != str(path):
                sequence, prev = _read_chain_head(path)
                _state.update(sequence=sequence, prev_hash=prev, path=str(path))
            _rotate_if_needed(path, rotate_bytes=rotate_bytes, keep_rows=keep_rows)
            row = build_row(
                sequence=int(_state["sequence"]) + 1,
                prev_hash=str(_state["prev_hash"]),
                request_id=request_id,
                tool=tool,
                outcome=outcome,
                duration_ms=duration_ms,
                total_ms=total_ms,
                error_code=error_code,
                arguments=arguments,
                caller_principal_hash=caller_principal_hash,
                client_name=client_name,
                client_version=client_version,
                transport=transport,
                session_id=session_id,
                spans=spans,
            )
            append_row(row, path=path)
            _state.update(sequence=row["sequence"], prev_hash=row["row_hash"])
            return row
    except Exception:  # noqa: BLE001 - a ledger write must never break a call
        return None


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def reset_chain_cache() -> None:
    """Drop the in-process chain head. For tests that move the ledger path."""
    with _lock:
        _state.update(sequence=None, prev_hash=None, path=None)


def verify(path: Path | None = None) -> list[str]:
    """Every way the on-disk chain fails to verify, named. Empty means intact.

    Reports rather than raises: a broken chain is a finding to show an operator,
    and the ledger is most useful precisely when something has gone wrong.
    """
    target = path or ledger_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        return [f"ledger unreadable: {error}"]
    return verify_lines(text.splitlines())


def verify_lines(lines: list[str], *, anchored: bool = False) -> list[str]:
    """`verify` over an already-assembled row stream.

    Separate from `verify` because the chain spans rotation: an operator has to
    be able to walk archive-then-live as *one* sequence, or a break at a segment
    boundary -- the one rotation itself could cause -- would go unreported.

    `anchored` says the stream is the ledger's *whole* history, so its first row
    must be sequence 1 hanging off the genesis hash. Without it a dropped oldest
    segment verifies clean: nothing precedes the first row to contradict, so the
    chain simply appears to start later than it did. A single file read
    mid-stream cannot make that claim, hence the default.
    """
    problems: list[str] = []
    previous_sequence: int | None = None
    previous_hash: str | None = None
    first = True
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            problems.append(f"line {number}: not valid JSON")
            continue
        expected = row_hash(record)
        if record.get("row_hash") != expected:
            problems.append(f"line {number}: row_hash does not match the row's contents")
        sequence = record.get("sequence")
        if first:
            first = False
            if anchored and (sequence != 1 or record.get("prev_hash") != GENESIS_HASH):
                problems.append(
                    f"line {number}: the ledger starts at sequence {sequence} rather than "
                    "the genesis row, so an older segment is missing"
                )
        if previous_sequence is not None and sequence != previous_sequence + 1:
            problems.append(
                f"line {number}: sequence {sequence} does not follow {previous_sequence}"
            )
        if previous_hash is not None and record.get("prev_hash") != previous_hash:
            problems.append(f"line {number}: prev_hash does not match the preceding row")
        previous_sequence = sequence if isinstance(sequence, int) else previous_sequence
        previous_hash = record.get("row_hash")
    return problems


# --------------------------------------------------------------------------- #
# Derived-work phases and diagnostics
#
# The change moves the expensive half of a governed write behind durable
# custody, so the two boundary clocks above can now prove a call got faster
# while saying nothing about whether the work it deferred ever converged. These
# are the measurements that answer that, and they are content-free by
# construction rather than by a downstream filter: closed phase names, closed
# counter names, and counts, ages and depths -- never a path, title, excerpt or
# exception text.
# --------------------------------------------------------------------------- #

#: Closed phase vocabulary. A producer cannot name a phase this set does not
#: contain, which is what makes a phase name incapable of carrying a path.
DERIVED_PHASES: frozenset[str] = frozenset(
    {
        "derived.receipt_prepare",
        "derived.receipt_proof",
        "derived.canonical_commit",
        "derived.pending_visibility",
        "derived.acknowledgement",
        "derived.component_dispatch",
        "derived.component_completion",
        "derived.advisory_execute",
        # Not a disjoint phase but the interval the fixed budget governs: from
        # the canonical commit to the end of the acknowledgement. Named because
        # it is the number the SLO is written against, and a benchmark forced
        # to infer it by subtracting other spans would be approximating the one
        # measurement that must not be approximate.
        "derived.post_canonical",
    }
)

#: Closed counter vocabulary, for the same reason.
DERIVED_COUNTERS: frozenset[str] = frozenset(
    {
        "advisory_vectors_reused",
        "advisory_vectors_encoded",
        "advisory_published",
        "advisory_replayed",
        "advisory_failed",
        "component_completed",
        "component_retried",
        "receipt_prepared",
        "receipt_superseded",
    }
)

_derived_lock = threading.Lock()
_derived_counts: dict[str, int] = dict.fromkeys(DERIVED_COUNTERS, 0)


def note_derived_event(name: str) -> None:
    """Count one closed derived-work event. Never raises into a worker."""
    if name not in DERIVED_COUNTERS:
        raise ValueError("unknown derived counter")
    with _derived_lock:
        _derived_counts[name] += 1


def derived_counters() -> dict[str, int]:
    with _derived_lock:
        return dict(_derived_counts)


def reset_derived_counters() -> None:
    with _derived_lock:
        _derived_counts.update(dict.fromkeys(DERIVED_COUNTERS, 0))


def derived_diagnostics(vault_root: Any) -> dict[str, Any]:
    """One content-free view of exact derived custody for this cell.

    Every field is a count, an age in seconds, a closed code or a closed state
    name. Nothing here reads or reports canonical content, and every store
    question goes through the receipt protocol's own public seams rather than
    through its SQLite, so a diagnostic can never see more than a consumer can.

    Never raises: a diagnostic that breaks the call it describes is worse than
    an absent one, so an unreadable section reports itself as unavailable.
    """
    from pathlib import Path as _Path

    root = _Path(vault_root)
    diagnostics: dict[str, Any] = {
        "fast_durable_ack": "inactive",
        "due_components": 0,
        "recoverable_batches": 0,
        "counters": derived_counters(),
        "pending_visibility": {},
        "last_drain_pass": {},
        "unavailable": [],
    }
    try:
        from .writer_lease import fast_durable_ack_active

        diagnostics["fast_durable_ack"] = (
            "active" if fast_durable_ack_active() else "inactive"
        )
    except Exception:  # noqa: BLE001 - a diagnostic never breaks its caller
        diagnostics["unavailable"].append("fast_durable_ack")
    try:
        from . import derived_receipts

        diagnostics["due_components"] = derived_receipts.due_component_count(root)
        diagnostics["recoverable_batches"] = derived_receipts.recoverable_batch_count(
            root
        )
    except Exception:  # noqa: BLE001
        diagnostics["unavailable"].append("custody_depth")
    try:
        from . import pending_recall

        diagnostics["pending_visibility"] = pending_recall.status(root)
    except Exception:  # noqa: BLE001
        diagnostics["unavailable"].append("pending_visibility")
    try:
        from . import derived_drain

        diagnostics["last_drain_pass"] = derived_drain.last_pass_observation(root)
    except Exception:  # noqa: BLE001
        diagnostics["unavailable"].append("last_drain_pass")
    return diagnostics
