"""Adoption Studio run lifecycle: canonical-file-backed, deterministic, governed.

A durable adoption run is a JSON object under
``Knowledge Base/_Adoption/runs/<run_id>/`` written only through
``vault.batch_atomic_write`` — so it inherits the access-tier backstop and the
writer-lease fence exactly like every other governed write. The run is fully
reconstructable after a process restart with no dependence on any rebuildable
sidecar. Originals outside ``Knowledge Base/`` are never rewritten, moved, or
deleted under any action or failure: imports are copies with provenance, and
scan/select/plan write nothing outside the run object.

``plan`` and ``apply`` are bounded by the caller's request budget. A call that
runs short returns a ``continuation`` cursor and the next call carries on; a
repeated cursor returns the earlier call's answer and writes nothing.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import posixpath
import secrets
import shutil
import stat
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal, get_args
from urllib.parse import urlencode

from . import (
    access,
    adoption_consume,
    adoption_plan,
    call_spans,
    capabilities,
    context_refs,
    due_state,
    duplicates,
    export_expansion,
    frontmatter_update,
    held_fs,
    import_filing,
    import_rename,
    import_stage,
    kept_speech,
    knowledge_packs,
    lexstore,
    note_types,
    rename_journal,
    request_budget,
    reserved_paths,
    source_taxonomy,
)
from . import adopt as adopt_module
from . import overview as overview_module
from .adopt import _TEXT_IMPORT_SUFFIXES
from .governance import raw_protection
from .kbdir import is_in_managed_root, managed_join, managed_root
from .vault import (
    MISSING_CONTENT_HASH,
    ContentHashMismatchError,
    PlannedWrite,
    batch_atomic_write,
    is_import_id,
    kb_root,
    parse_frontmatter,
)
from .writer_lease import _FAST_ACK_DEADLINE_SECONDS

RUNS_DIR = "_Adoption/runs"
SCHEMA_VERSION = 1
# The plan layout `apply` understands. A plan made by an earlier layout is refused as stale.
PLAN_FORMAT = 2
# These are fixed status protocol views, not vault vocabulary.
StatusView = Literal["summary", "inventory", "selection", "plan", "outcomes", "stale", "filing", "full"]
STATUS_PAGE_ITEMS = 100
STATUS_PAGE_BYTES = 32 * 1024
MAX_CANDIDATES_DEFAULT = 5000
# Where Evermatter Cloud stages an uploaded collection (see `cloud_import_job`).
IMPORT_STAGE_DIR = import_stage.STAGE_DIR
# One `apply` batch holds at most this many items or bytes of originals. A batch costs about 3.5 s of fixed
# work plus about 20-60 ms per item (2026-11-25 rehearsal, and a local 25/100/250-page batch stayed linear), so
# the pacer, not this cap, sizes a chunk to its budget share; the cap only bounds one batch's memory and reply.
APPLY_CHUNK_ITEMS = 250
APPLY_CHUNK_BYTES = 64 * 1024 * 1024
# A chunk is also cut so its predicted cost stays at most this share of the call's budget.
APPLY_CHUNK_BUDGET_SHARE = 0.1
# The `stop_reason` of a call that ended after a chunk because a write waited for the boundary.
STOP_FOR_WAITING_WRITE = "interactive_write_waiting"
# The `apply_stop` code of a chunk that the vault's volume has no room for; the person frees space.
_SPACE_STOP = "INSUFFICIENT_SPACE"
# What a call assumes before it has timed a chunk: seconds per item, and per media
# artifact for its extraction request (both pessimistic starts).
INITIAL_ITEM_SECONDS = 0.5
INITIAL_EXTRACTION_SECONDS = 1.0
# ... and per MiB of original bytes (read, hashed, copied, hashed again): pessimistic,
# about 20 MiB/s, so one huge file is never started on the strength of small notes.
INITIAL_SECONDS_PER_MIB = 0.05
# A chunk teaches the per-MiB rate only when its items average at least this many
# MiB; lighter chunks teach the per-item rate, which a huge file would otherwise inflate.
BYTE_RATE_MIN_ITEM_MIB = 1.0
# Work a call owes after its loop, inside the same MCP call, which the loop cannot
# time. Durable custody still needs proof and pending publication after the leaf.
# Media sidecars are indexed by a fan-out deferred until the mutation's
# terminal is persisted (a fast-acknowledgement session moves it behind the reply
# instead); its first graph step waits on the graph the background media worker
# also holds, and the reply names every written path for the egress checks to
# scan. PROVISIONAL, from a 1,100-file collection on the personal profile: seven
# terminal drains took 3.4-15.2 s, with one outlier of 30.4 s on a short final
# call; once the first had run, each further sidecar cost about 0.1 s; reply
# handling took about 2.4 s for 456 written paths (0.005 s each).
TERMINAL_WORK_RESERVE_SECONDS = 20.0
# What a fast-acknowledgement call reserves for its custody handoff is the handoff time measured on
# this vault, between two bounds. The floor is the acknowledgement's own wait bound, which a short
# call's tail can spend whatever the handoff measured; the ceiling is the whole reserve above,
# which also stands until a handoff has been measured.
CUSTODY_RESERVE_FLOOR_SECONDS = _FAST_ACK_DEADLINE_SECONDS
TERMINAL_FANOUT_SECONDS_PER_ARTIFACT = 0.15
REPLY_SECONDS_PER_ITEM = 0.006
PHASES = (
    "selecting",
    "planned",
    "applying",
    "applied",
    "partial",
    "failed",
    "done",
    "cancelled",
)
# Subsets of this module's closed `PHASES`, not vault vocabulary: the steps check them and the `file` driver reads them.
_APPLY_PHASES = ("planned", "applying")  # nosemgrep: ep-word-set -- the run phases in which `apply` has work left.
_FINISH_PHASES = ("applied", "partial")  # nosemgrep: ep-word-set -- the run phases that `finish` accepts.
_FINISHED_PHASES = ("done", "cancelled")  # nosemgrep: ep-word-set -- the run phases that end a page.

_APPLIED_STATUSES = adoption_consume.FILED_STATUSES
# An own-note item whose Source is on disk and whose draft note is still owed. The item is
# not complete: `apply` resumes it at the note, and the phase stays `applying` until it is.
_SOURCE_ONLY = "source-applied"
# The run-object key for the dearest time per made note of the run's last call that made
# one, with the wall-clock time it was recorded.
_NOTE_PRICE = "note_price"
# An agent continues a run within seconds of the last reply. A run resumed later may meet
# another load or another host, so its next call times a note again instead.
_NOTE_PRICE_FRESH_SECONDS = 300.0
# A carried price sizes a call's first batch at this share of the room, so notes that cost
# up to twice the carried price still fit; the call re-prices from that batch.
_CARRIED_PRICE_ROOM_SHARE = 0.5
# The run-object key for the item-chunk timings of the run's last call that filed, with the wall-clock time
# they were recorded. The next call starts from them while they are as fresh as a carried note price.
_APPLY_PACE = "apply_pace"
# The newest timed chunks a call carries to the next: enough for the fit, few enough to follow a slowing vault.
_CARRIED_CHUNKS = 8
# The run-object keys for the filed files that no batch has written to the Sources index and the log yet, as
# a list of {original_path, source_path, original_sha256} dicts in filing order, and for a batch that set out to
# write them and may not have: its log token and the `source_path` of each row it carries, so a settled batch
# drops exactly those rows and no others.
_INDEX_OWED = "index_owed"
_RECORD_INTENT = "record_intent"
# The run-object key that names one full save of `run.json`; a journal holds changes since the save it names.
_GENERATION = "generation"
# The run-object fields that every full save stamps with a fresh value, whatever the save changed. A reader that
# compares a run before and after a step (the tool-selection scorer) leaves these out.
SAVE_STAMPS = ("updated", _GENERATION)
_JOURNAL_FILE = "journal.json"
# Keys that only `start`, `select` and `plan` write, each in a full save: `apply`, `file`, `finish` and `cancel`
# never change them, so a journal leaves them out. A step that replaces one is caught and saves in full; one
# that changes `plan` or `selection` in place, as `apply` must never do, would be lost until the closing save.
_FIXED_KEYS = ("inventory", "plan", "selection", "scan_summary", "pack_suggestions", "plan_draft")  # nosemgrep: ep-word-set -- run-object keys that this module writes, not a vocabulary
_ABSENT = object()
# The values of a rule's `adopt_as` are fixed by the rule schema, not by the vault.
_ADOPT_AS = ("source", "note")  # nosemgrep: ep-word-set -- the closed values of the rule schema's `adopt_as`.
# Per-item failures that mean "the original is not what the plan read".
_DRIFT_CODES = frozenset({"SOURCE_CHANGED", "NOT_FOUND"})
_RECALL_SAMPLE = 10

_REASONS = {
    "NOT_IN_INVENTORY": "path is not among the scanned adoption candidates",
    "UNSUPPORTED_IMPORT_TYPE": "the scan did not mark this file importable",
    "ALREADY_GOVERNED": f"path is already inside {managed_join('')}",
    adoption_plan.OVER_CEILING: "file is larger than the adoption ceiling",
    "NOT_REGULAR_FILE": "only regular files import; links and special files do not",
    "HIDDEN_FOLDER": "hidden folders are not scanned unless include_hidden=true",
    "JUNK": "sync-conflict or zero-byte file; pass include_junk=true or list it in overrides",
}

_LOCK = threading.Lock()


class AdoptionRunError(Exception):
    """Structured failure: ``code`` machine-readable, ``reason`` human-readable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _today(today: dt.date | None) -> dt.date:
    return today or dt.date.today()


def _clean(path: str | None) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _call_budget() -> request_budget.RequestBudget:
    """This call's deadline, which `plan` and `apply` pace themselves against.

    Over MCP, stdio and HTTP alike, the dispatcher binds one `RequestBudget` per
    request (`request_budget.MCP_REQUEST_BUDGET_SECONDS`, 50 s unless the operator
    sets `EXOMEM_MCP_REQUEST_BUDGET_SECONDS`) and `current()` returns it, with the
    time the request has already spent. The REST and CLI doors bind none, so they
    get a fresh budget of the same length that starts here. Either way the call
    stops starting work `request_budget.DELIVERY_RESERVE_SECONDS` before the end.
    """
    return request_budget.current() or request_budget.RequestBudget(
        seconds=request_budget.budget_seconds()
    )


# --------------------------------------------------------------------------- #
# id / fingerprint helpers
# --------------------------------------------------------------------------- #
def inventory_fingerprint(rows: list[dict]) -> str:
    """sha256[:24] over the sorted ``(path|bytes|mtime)`` of eligible rows."""
    eligible = sorted((r for r in rows if r.get("eligible")), key=lambda r: r["path"])
    payload = "\n".join(f"{r['path']}|{r['bytes']}|{r['mtime']!r}" for r in eligible)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def selection_hash(paths: list[str], classification: Mapping[str, Mapping] | None = None) -> str:
    """sha256[:16] over the sorted selected paths and the classification each one carries."""
    payload = "\n".join(sorted(paths))
    if classification:
        payload += "\n--\n" + json.dumps(
            {path: classification[path] for path in sorted(classification)},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def plan_id_for(sel_hash: str, item_hashes: list[str]) -> str:
    """sha256[:16] over the selection hash plus the concatenated per-item hashes."""
    payload = sel_hash + "|" + "".join(item_hashes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def new_run_id(source_root: str, created_iso: str, inventory_fp: str, *, today: dt.date) -> str:
    digest = hashlib.sha256(
        f"{source_root}\n{created_iso}\n{inventory_fp}".encode()
    ).hexdigest()[:8]
    return f"adr-{today.strftime('%Y%m%d')}-{digest}"


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
@dataclass
class _Saved:
    """What a run's `run.json` holds, for a run a step opened to change: what its journal is measured from."""

    #: The full save that `run.json` holds; None when no journal can be measured from it.
    generation: str | None
    #: Each fixed key's value as loaded: a step that replaces one cannot be journaled.
    fixed: dict[str, object]
    #: A copy of every other key as `run.json` holds it.
    rest: dict


class AdoptionRunStore:
    """Load/save the canonical run objects under ``_Adoption/runs/<run_id>/``.

    A page's run object holds its inventory, plan and outcomes, several megabytes for a large page. `save` writes it
    whole and names that save with a fresh `generation`. A step that changes a run several times in one call opens it
    with `load_for_update` and writes each change with `save_changes` (or `save_intent`, or `planned_changes` inside a
    larger batch): `journal.json` beside it then holds every change since the last full save, and `load` folds that
    journal in while its generation matches. Each journal write replaces the last one whole, and a full save names a
    new generation, so a crash leaves either the older state or the newer one.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.base = kb_root(self.vault_root) / "_Adoption" / "runs"
        self._saved: dict[str, _Saved] = {}

    def run_dir(self, run_id: str) -> Path:
        return self.base / run_id

    def _run_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def _journal_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / _JOURNAL_FILE

    def load(self, run_id: str | None) -> dict:
        return self._load(run_id, for_update=False)

    def load_for_update(self, run_id: str | None) -> dict:
        """Load a run that this step will change and save, so its changes can go to the journal."""
        return self._load(run_id, for_update=True)

    def _load(self, run_id: str | None, *, for_update: bool) -> dict:
        if not run_id:
            raise AdoptionRunError("RUN_NOT_FOUND", "an adoption run_id is required")
        folder = self.run_dir(run_id)
        if not (folder / "run.json").exists():
            raise AdoptionRunError("RUN_NOT_FOUND", f"no adoption run {run_id!r}")
        try:
            saved, journal = _read_run_files(folder)
            if for_update and isinstance(saved, dict):
                self._remember(saved)
            return _folded(saved, journal)
        except (OSError, UnicodeError, ValueError) as exc:
            raise AdoptionRunError("RUN_NOT_FOUND", f"cannot read run {run_id!r}: {exc}") from exc

    def _remember(self, run: dict) -> None:
        rest = {key: value for key, value in run.items() if key not in _FIXED_KEYS}
        self._saved[run["run_id"]] = _Saved(
            generation=run.get(_GENERATION),
            fixed={key: run.get(key, _ABSENT) for key in _FIXED_KEYS},
            rest=json.loads(json.dumps(rest, ensure_ascii=False, default=str)),
        )

    def planned_write(self, run: dict) -> PlannedWrite:
        """The whole run object as a write, so a caller can commit it inside a larger batch."""
        run["updated"] = _now_iso()
        run[_GENERATION] = secrets.token_hex(8)
        # Compact: indenting a multi-megabyte run costs real time.
        content = json.dumps(run, ensure_ascii=False, default=str) + "\n"
        return PlannedWrite(path=self._run_file(run["run_id"]), content=content)

    def planned_changes(self, run: dict) -> PlannedWrite:
        """What changed in a run since its last full save, as a write; the whole run when no journal can say it."""
        changes = self._changes(run)
        if changes is None:
            saved = self._saved.get(run["run_id"])
            if saved is not None:
                # The batch that carries this write may still fail, so no journal is measured from it.
                saved.generation = None
            return self.planned_write(run)
        content = json.dumps(changes, ensure_ascii=False, default=str) + "\n"
        return PlannedWrite(path=self._journal_file(run["run_id"]), content=content)

    def _changes(self, run: dict) -> dict | None:
        """The journal of `run`, or None when it was not opened for update or a fixed key was replaced."""
        saved = self._saved.get(run["run_id"])
        if saved is None or saved.generation is None or saved.generation != run.get(_GENERATION):
            return None
        if any(run.get(key, _ABSENT) is not value for key, value in saved.fixed.items()):
            return None
        run["updated"] = _now_iso()
        replaced: dict = {}
        entries: dict[str, dict] = {}
        dropped: dict[str, list[str]] = {}
        for key, value in run.items():
            if key in _FIXED_KEYS:
                continue
            before = saved.rest.get(key, _ABSENT)
            if isinstance(value, dict) and isinstance(before, dict):
                # Keyed maps (outcomes, cursors, invocations) change a few entries per call, so only those are written.
                changed = {name: item for name, item in value.items() if before.get(name, _ABSENT) != item}
                gone = [name for name in before if name not in value]
                if changed:
                    entries[key] = changed
                if gone:
                    dropped[key] = gone
            elif before is _ABSENT or before != value:
                replaced[key] = value
        return {
            "run_id": run["run_id"], _GENERATION: saved.generation, "replaced": replaced, "entries": entries,
            "dropped": dropped, "removed": [key for key in saved.rest if key not in run],
        }

    def save(self, run: dict, *also: PlannedWrite) -> None:
        """Write the whole run object, with an empty journal for it when an earlier one exists, and the writes `also` names."""
        writes = [self.planned_write(run), *also]
        journal = self._journal_file(run["run_id"])
        if journal.exists():
            # A stale journal is already ignored; replacing it keeps an older `run.json` from matching it again.
            empty = {"run_id": run["run_id"], _GENERATION: run[_GENERATION], "replaced": {}, "entries": {},
                     "dropped": {}, "removed": []}
            writes.append(PlannedWrite(path=journal, content=json.dumps(empty) + "\n"))
        batch_atomic_write(writes, vault_root=self.vault_root)
        if run["run_id"] in self._saved:
            self._remember(run)

    def save_changes(self, run: dict) -> None:
        """Write what changed since the run's last full save (see `planned_changes`)."""
        self._write_changes(run)

    def save_intent(self, run: dict) -> None:
        """Freeze recovery scope without claiming that an import already committed."""
        self._write_changes(run, commit_point=False, post_commit_fanout=False)

    def _write_changes(self, run: dict, **batch: bool) -> None:
        write = self.planned_changes(run)
        batch_atomic_write([write], vault_root=self.vault_root, **batch)
        if write.path.name != _JOURNAL_FILE and run["run_id"] in self._saved:
            self._remember(run)

    def load_proposals(self, run_id: str) -> dict:
        f = self.run_dir(run_id) / "proposals.json"
        if not f.exists():
            return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "proposals": []}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "proposals": []}

    def save_proposals(self, run_id: str, payload: dict) -> None:
        content = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
        batch_atomic_write(
            [PlannedWrite(path=self.run_dir(run_id) / "proposals.json", content=content)],
            vault_root=self.vault_root,
        )

    def list_runs(self) -> list[dict]:
        return self.listing()["runs"]

    def _readable_runs(self) -> Iterable[dict]:
        """Every run that loads, in run id order."""
        try:
            names = sorted(folder.name for folder in self.base.iterdir())
        except OSError:
            return
        for name in names:
            try:
                yield self.load(name)
            except AdoptionRunError:
                continue

    def runs_at(self, source_root: str) -> tuple[dict, ...]:
        """Every readable run that scanned `source_root`, whatever its page or phase."""
        return tuple(
            run for run in self._readable_runs() if isinstance(run, dict) and run.get("source_root") == source_root
        )

    def tallies_at(self, source_root: str, *, current: dict) -> list[_Tally]:
        """What a `file` reply counts from every readable run that scanned `source_root`, in run id order.

        `current` is a run as just loaded, counted as it stands. Every other run is read only when its files
        changed since this process last counted it: a reply would otherwise parse every page of the import.
        """
        try:
            folders = sorted(folder for folder in self.base.iterdir() if folder.name != current["run_id"])
        except OSError:
            folders = []
        tallies = {current["run_id"]: _Tally.of(current)}
        for folder in folders:
            tally = _tally_of(folder)
            if tally is not None:
                tallies[folder.name] = tally
        return [tallies[name] for name in sorted(tallies) if tallies[name].source_root == source_root]

    def runs_by_stage(self) -> dict[str, tuple[dict, ...]]:
        """Every readable run, grouped by the import whose stage it started in; the most recently updated first."""
        grouped: dict[str, list[dict]] = {}
        for run in self._readable_runs():
            recorded = run.get("stage") if isinstance(run, dict) else None
            if isinstance(recorded, dict) and isinstance(recorded.get("stage"), str):
                grouped.setdefault(recorded["stage"], []).append(run)
        return {
            import_id: tuple(sorted(runs, key=lambda run: str(run.get("updated") or ""), reverse=True))
            for import_id, runs in grouped.items()
        }

    def runs_of_stage(self, import_id: str) -> tuple[dict, ...]:
        """Every readable run that started in the import's stage, the most recently updated first."""
        return self.runs_by_stage().get(import_id, ())

    def _stage_index(self, import_id: str) -> Path:
        return self.base.parent / _STAGE_RUNS / f"{import_id}.json"

    def _index_state(self, import_id: str) -> tuple[list[str] | None, str | None]:
        """The run ids that the stage's index lists, and the hash that guards a write of it.

        The ids are an empty list for a stage with no index, and None when the index exists and cannot be read.
        """
        try:
            raw = self._stage_index(import_id).read_bytes()
        except FileNotFoundError:
            return [], MISSING_CONTENT_HASH
        except OSError:
            return None, None
        guard = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw)
        except ValueError:
            return None, guard
        ids = document.get("runs") if isinstance(document, dict) else None
        return (ids if isinstance(ids, list) and all(isinstance(run_id, str) for run_id in ids) else None), guard

    def indexed_in_stage(self, run: dict) -> PlannedWrite | None:
        """The write that adds a run to the index of its stage, to commit with the run; None when it has no stage or is listed.

        The index names each stage's runs, so a caller that needs one stage's runs reads that stage's file and
        those runs and never the runs of the vault (`stage_steps`). A damaged index is built again from the runs,
        which loads them all, so that costs once for each damage. The write is guarded by the hash of the index
        that it read: when another process lists a run first, the commit refuses (`ContentHashMismatchError`) and
        `start` reads the index again, so no entry is lost.
        """
        recorded = run.get("stage")
        import_id = recorded.get("stage") if isinstance(recorded, dict) else None
        if not is_import_id(import_id):
            return None
        listed, guard = self._index_state(import_id)
        if listed is None:
            listed = [earlier["run_id"] for earlier in reversed(self.runs_of_stage(import_id))]
        if run["run_id"] in listed:
            return None
        content = json.dumps({"stage": import_id, "runs": [*listed, run["run_id"]]}) + "\n"
        return PlannedWrite(path=self._stage_index(import_id), content=content, expected_hash=guard)

    def stage_steps(self, import_id: str) -> tuple[str, ...] | None:
        """The step that moves each run of the stage on (`_next_filing_step`); empty for a stage no run started in.

        It reads the stage's own index and then one counted run for each run of that stage, so its cost follows the
        stage and never the vault's history of runs. A run counted before and unchanged since is not read again.
        None when the index exists and cannot be read: that is no answer, and a caller treats the stage as still
        open, so that damage never files a page early. The next run that starts in the stage builds the index again.
        A run that started before the index existed is not listed.
        """
        listed, _guard = self._index_state(import_id)
        if listed is None:
            return None
        tallies = (_tally_of(self.run_dir(run_id)) for run_id in listed)
        return tuple(tally.step for tally in tallies if tally is not None and tally.step is not None)

    def listing(self, *, path: str = "") -> dict:
        rows: list[dict] = []
        adopted: set[PurePosixPath] = set()
        complete = True
        scope = _status_path(path) if path else None
        if path and scope is None:
            raise AdoptionRunError("IMPORT_PATH_OUTSIDE_VAULT", "status takes a vault-relative subtree")
        try:
            folders = sorted(self.base.iterdir())
        except FileNotFoundError:
            folders = []
        except OSError:
            if scope is None:
                raise
            folders, complete = [], False
        for folder in folders:
            try:
                if not stat.S_ISDIR(folder.stat().st_mode):
                    continue
                run = _folded(*_read_run_files(folder))
            except (OSError, UnicodeError, ValueError):
                complete = False
                continue
            if not isinstance(run, dict):
                complete = False
                continue
            try:
                rows.append(_compact_row(run))
            except (AttributeError, TypeError):
                # Legacy listings skip malformed rows; scoped counts still inspect their scope.
                pass
            if scope is None:
                continue
            source_root = _status_path(run.get("source_root"))
            if source_root is None:
                complete = False
                continue
            if not (source_root.is_relative_to(scope) or scope.is_relative_to(source_root)):
                continue
            outcomes = run.get("outcomes")
            if not isinstance(outcomes, dict):
                complete = False
                continue
            for original, outcome in outcomes.items():
                original_path = _status_path(original)
                # Apply's writers define these fixed protocol states, not vault vocabulary.
                if (
                    original_path is None or original_path == source_root
                    or not original_path.is_relative_to(source_root) or not isinstance(outcome, dict)
                    or outcome.get("status") not in (*_APPLIED_STATUSES, _SOURCE_ONLY, "failed")
                ):
                    complete = False
                elif original_path.is_relative_to(scope) and outcome.get("status") in _APPLIED_STATUSES:
                    adopted.add(original_path)
        rows.sort(key=lambda r: r.get("created") if isinstance(r.get("created"), str) else "", reverse=True)
        result = {"runs": rows}
        if scope is not None:
            result.update(adopted_files=len(adopted) if complete else None, complete=complete)
        return result


# The folder beside `runs/` that holds, for each stage, the ids of the runs that started in it (`indexed_in_stage`).
_STAGE_RUNS = "stage-runs"
# How often `start` reads a stage's index again when another process lists a run in it first.
_INDEX_ATTEMPTS = 4
# What a `file` reply counts of one outcome, as bits (see `_Tally`).
_FILED, _FAILED, _SKIPPED, _STORED = 1, 2, 4, 8
# The runs a process keeps counted, by folder: enough for the pages of a few large imports.
_TALLY_RUNS = 64
_TALLIES: dict[str, tuple[tuple, _Tally]] = {}
_TALLIES_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Tally:
    """What a `file` reply counts of one run: its scan, its creation, its phase and each outcome as `_FILED`... bits.

    It also keeps the step that moves the run's import on, which an import's other windows read (`stage_steps`).
    """

    source_root: object
    created: str
    phase: object
    outcomes: dict[str, int]
    #: The step that moves the run's import on (`_next_filing_step`); None for a run too damaged to name one.
    step: str | None = None

    @classmethod
    def of(cls, run: dict) -> _Tally:
        outcomes: dict[str, int] = {}
        for path, outcome in (run.get("outcomes") or {}).items():
            status = outcome.get("status")
            outcomes[path] = (
                (_FILED if status in _APPLIED_STATUSES else 0) | (_FAILED if status == "failed" else 0)
                # As `_skipped_duplicates` reads a skipped duplicate.
                | (_SKIPPED if status == "already-applied" and "existing_ref" in outcome else 0)
                | (_STORED if _source_status(outcome) == "applied" else 0)
            )
        try:
            step = _next_filing_step(run)
        except (KeyError, TypeError, AttributeError):
            step = None
        return cls(run.get("source_root"), str(run.get("created") or ""), run.get("phase"), outcomes, step)


def _tally_of(folder: Path) -> _Tally | None:
    """The tally of the run in `folder`, read again only when its run object or journal changed; None if unreadable."""
    try:
        if not stat.S_ISDIR(folder.stat().st_mode):
            return None
        files = [folder / "run.json", folder / _JOURNAL_FILE]
        key = tuple(
            (info.st_ino, info.st_mtime_ns, info.st_size) if info is not None else None
            for info in (_stat_or_none(path) for path in files)
        )
    except OSError:
        return None
    with _TALLIES_LOCK:
        cached = _TALLIES.get(str(folder))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        run = _folded(*_read_run_files(folder))
        tally = _Tally.of(run) if isinstance(run, dict) else None
    except (OSError, UnicodeError, ValueError, AttributeError, KeyError, TypeError):
        return None
    if tally is None:
        return None
    with _TALLIES_LOCK:
        _TALLIES.pop(str(folder), None)
        while len(_TALLIES) >= _TALLY_RUNS:
            _TALLIES.pop(next(iter(_TALLIES)))
        _TALLIES[str(folder)] = (key, tally)
    return tally


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def _read_run_files(folder: Path) -> tuple[object, object]:
    """A run folder's `run.json` and its journal (None when it has none), as JSON."""
    saved = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    try:
        journal = json.loads((folder / _JOURNAL_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        journal = None
    return saved, journal


def _folded(saved: object, journal: object) -> object:
    """The run that `run.json` and its journal hold together (see `AdoptionRunStore`).

    A journal of another full save is ignored: that save already holds its changes. A journal that cannot be read
    raises, because the run it belongs to may be ahead of `run.json`.
    """
    if journal is None or not isinstance(saved, dict):
        return saved
    if not (
        isinstance(journal, dict) and isinstance(journal.get("replaced"), dict)
        and isinstance(journal.get("entries"), dict) and isinstance(journal.get("dropped"), dict)
        and isinstance(journal.get("removed"), list)
    ):
        raise ValueError("the run's journal is not a journal")
    if journal.get(_GENERATION) is None or journal.get(_GENERATION) != saved.get(_GENERATION):
        return saved
    for key in journal["removed"]:
        saved.pop(key, None)
    for key, changed in journal["entries"].items():
        current = saved.get(key)
        if not isinstance(current, dict) or not isinstance(changed, dict):
            raise ValueError(f"the run's journal changes {key!r}, which is not a map")
        current.update(changed)
    for key, gone in journal["dropped"].items():
        current = saved.get(key)
        for name in gone if isinstance(current, dict) else ():
            current.pop(name, None)
    saved.update(journal["replaced"])
    return saved


def _status_path(raw: object) -> PurePosixPath | None:
    """Read historical vault-relative paths without requiring originals to remain on disk."""
    if not isinstance(raw, str) or "\0" in raw:
        return None
    normalized = posixpath.normpath(raw.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if path.is_absolute() or PureWindowsPath(raw).anchor or normalized == ".." or normalized.startswith("../"):
        return None
    return path


def _compact_row(run: dict) -> dict:
    outcomes = run.get("outcomes") or {}
    applied = sum(1 for o in outcomes.values() if o.get("status") in _APPLIED_STATUSES)
    return {
        "run_id": run.get("run_id"),
        "run_ref": run.get("run_ref"),
        "source_root": run.get("source_root"),
        "phase": run.get("phase"),
        "created": run.get("created"),
        "counts": {
            "inventory": len(run.get("inventory") or []),
            "selected": len((run.get("selection") or {}).get("paths") or []),
            "applied": applied,
        },
    }


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def _count_files(folder: Path) -> int:
    return sum(len(files) for _dirpath, _dirs, files in os.walk(folder))


def _hidden(name: str) -> bool:
    """Whether a scan without include_hidden skips this file or folder name (see `walk_inventory`)."""
    return name.startswith(".")


def _recordable(path: str) -> bool:
    """Whether a run's record can hold this path: a name that is not UTF-8 reads back with surrogates, which UTF-8 refuses."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _order_key(row: dict) -> tuple[int, str]:
    """A file row's place in the scan: artifacts first, then text, then rows that never import; each by path.

    Artifacts go first so that a note's embeds resolve against imports that already exist.
    """
    return (0 if row["form"] == "artifact" else 1 if row["form"] == "text" else 2, row["path"])


def build_inventory(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    ceiling: int | None = None,
    after: tuple[int, str] | None = None,
    stage: import_stage.Stage | None = None,
) -> tuple[list[dict], int, int]:
    """Return ``(rows, remaining, hidden_files)`` — one page of the adoption candidates outside the KB.

    The page holds at most ``max_candidates`` file rows that follow ``after`` in the scan's order
    (``_order_key``); ``remaining`` counts the file rows that follow the page. The first page, which has
    no ``after``, also carries the hidden-folder rows; a later page repeats none of them.
    ``hidden_files`` counts every hidden file the scan skipped, whatever the page.
    """
    files, folders, hidden_files = walk_inventory(
        root, path=path, include_hidden=include_hidden, ceiling=ceiling, stage=stage
    )
    if after is not None:
        files, folders = [row for row in files if _order_key(row) > after], []
    return files[:max_candidates] + folders, max(len(files) - max_candidates, 0), hidden_files


def walk_inventory(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    ceiling: int | None = None,
    stage: import_stage.Stage | None = None,
) -> tuple[list[dict], list[dict], int]:
    """Return ``(files, folders, hidden_files)`` — every adoption candidate outside the KB, in scan order.

    Each file row carries the vault-relative path, byte size, mtime, import
    ``form`` (``text`` for a text-like suffix, ``artifact`` for any other file),
    an ``eligible`` flag, and a machine-readable ``reason`` when ineligible
    (never silently dropped). A skipped hidden folder is one ``kind: folder`` row
    with its file count; ``hidden_files`` counts skipped hidden files. Rows are
    stat-only (no file reads); per-source sha256 is deferred to ``plan``. A file above the ceiling stays
    eligible when ``stage`` (a run that consumes) records an identity that its stat equals: adoption files
    it by rename and copies nothing.
    """
    from . import client_artifacts

    root = Path(root)
    limit = client_artifacts.adoption_ceiling() if ceiling is None else ceiling
    scan_root = root if not _clean(path) else (root / _clean(path))
    rows: list[dict] = []
    folders: list[dict] = []
    hidden_files = 0
    if scan_root.is_dir():
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames.sort()
            filenames.sort()
            kept: list[str] = []
            for d in dirnames:
                dpath = Path(dirpath) / d
                rel_dir = _rel(root, dpath)
                if is_in_managed_root(rel_dir):
                    continue
                if dpath.is_symlink():
                    rows.append(_row(rel_dir, 0, 0.0, eligible=False, reason="NOT_REGULAR_FILE"))
                    continue
                if not include_hidden and _hidden(d):
                    try:
                        mtime = dpath.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    folders.append(
                        {
                            "path": rel_dir, "kind": "folder", "bytes": 0, "mtime": mtime,
                            "eligible": False, "junk": False, "form": None,
                            "reason": "HIDDEN_FOLDER", "files": _count_files(dpath),
                        }
                    )
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                if fn == import_stage.SERIES_FILE and import_stage.is_series_file(_rel(root, Path(dirpath) / fn)):
                    continue  # the series file is never an item to file, nor a hidden file to report
                if not include_hidden and _hidden(fn):
                    hidden_files += 1
                    continue
                fpath = Path(dirpath) / fn
                rel = _rel(root, fpath)
                if is_in_managed_root(rel):
                    continue
                try:
                    st = fpath.lstat()
                except OSError:
                    continue
                reason = None
                if not stat.S_ISREG(st.st_mode):
                    reason = "NOT_REGULAR_FILE"
                elif st.st_size > limit and not _stands_for_manifest(stage, rel, st):
                    reason = adoption_plan.OVER_CEILING
                # Stat-level junk classification (no reads), mirroring overview's
                # categories. Rows are the UNCAPPED source of truth for the
                # selection junk filter; overview's junk path lists are
                # display-capped per category and must never gate writes.
                name_l = fn.lower()
                junk = st.st_size == 0 or any(
                    token in name_l for token in overview_module._CONFLICT_TOKENS
                )
                rows.append(
                    _row(rel, st.st_size, st.st_mtime, eligible=reason is None, reason=reason,
                         junk=junk, suffix=fpath.suffix.lower())
                )
    rows.sort(key=_order_key)
    folders.sort(key=lambda r: r["path"])
    return rows, folders, hidden_files


def _stands_for_manifest(stage: import_stage.Stage | None, rel: str, info: os.stat_result) -> bool:
    """Whether the stat of a staged file equals the identity its stage's manifest records."""
    vouched = adoption_consume.vouched(stage, rel)
    return vouched is not None and vouched[0] == import_stage.Identity.of(info)


def has_text_suffix(path: str) -> bool:
    """Whether the importer treats this path's suffix as text-like, whatever its size."""
    return PurePosixPath(path).suffix.lower() in _TEXT_IMPORT_SUFFIXES


def reads_as_text(path: str, size: int) -> bool:
    """Whether the importer files this member as text: a text-like suffix within the inline text limit.

    The importer also needs the bytes to decode as UTF-8, which a status call does not read.
    """
    return has_text_suffix(path) and size <= adoption_plan.INLINE_TEXT_MAX_BYTES


def _row(
    path: str, size: int, mtime: float, *, eligible: bool, reason: str | None,
    junk: bool = False, suffix: str | None = None,
) -> dict:
    form = None
    if eligible:
        form = "text" if suffix in _TEXT_IMPORT_SUFFIXES else "artifact"
    return {
        "path": path, "bytes": size, "mtime": mtime, "eligible": eligible,
        "junk": junk, "form": form, "reason": reason,
    }


def probe_staleness(root: Path, run: dict) -> list[str]:
    """Stat-diff the selected originals against their captured inventory rows."""
    root = Path(root)
    inv = {r["path"]: r for r in run.get("inventory") or []}
    selected = (run.get("selection") or {}).get("paths") or []
    stale: list[str] = []
    reads = _CallReads()
    outcomes = run.get("outcomes") or {}
    items = {item["original_path"]: item for item in (run.get("plan") or {}).get("items") or []}
    for p in selected:
        fpath = root / p
        row = inv.get(p)
        if not fpath.exists():
            # Adoption removes a staged copy after it files it; that copy is not missing, it is consumed.
            # The flag settles it at once; the index is read only for a call that stopped before it saved.
            if not (outcomes.get(p) or {}).get("consumed") and (
                p not in items or _filed_and_gone(root, run, reads, items[p]) is None
            ):
                stale.append(p)
            continue
        st = fpath.stat()
        if row is None or st.st_size != row.get("bytes") or st.st_mtime != row.get("mtime"):
            stale.append(p)
    return sorted(stale)


# --------------------------------------------------------------------------- #
# selection materialization
# --------------------------------------------------------------------------- #
def _classify_path(run: dict, path: str) -> str:
    inv = {r["path"]: r for r in run.get("inventory") or []}
    row = inv.get(path)
    if row is not None:
        if row.get("eligible"):
            return ""
        return row.get("reason") or "UNSUPPORTED_IMPORT_TYPE"
    if is_in_managed_root(path):
        return "ALREADY_GOVERNED"
    return "NOT_IN_INVENTORY"


def _reject(path: str, code: str) -> dict:
    return {"path": path, "code": code, "reason": _REASONS.get(code, code)}


_RULE_FIELDS = ("source_kind", "domain", "adopt_as", "note_type", "project")  # nosemgrep: ep-word-set -- the keys of the `include` rule object schema.


def _rule_key(entry: object) -> tuple[str, dict[str, str]]:
    """``(path, fields)`` of one `include` entry: a path or a rule object.

    ``fields`` holds the rule object's non-empty string fields, stripped.
    """
    if isinstance(entry, str):
        return _clean(entry), {}
    if isinstance(entry, Mapping):
        unknown = sorted(set(entry) - {"path", *_RULE_FIELDS})
        if unknown or not isinstance(entry.get("path"), str) or not _clean(entry["path"]):
            raise AdoptionRunError(
                "INVALID_RULE",
                "an include rule is a path or an object "
                "{path, source_kind?, domain?, adopt_as?, note_type?, project?}"
                + (f"; unknown keys: {', '.join(unknown)}" if unknown else ""),
            )
        fields: dict[str, str] = {}
        for field in _RULE_FIELDS:
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                raise AdoptionRunError("INVALID_RULE", f"{field} must be a string")
            if isinstance(value, str) and value.strip():
                fields[field] = value.strip()
        return _clean(entry["path"]), fields
    raise AdoptionRunError("INVALID_RULE", "an include rule is a path or an object")


def _classify_rule(
    root: Path, taxonomy: source_taxonomy.SourceTaxonomy | None, fields: Mapping[str, str]
) -> dict | None:
    """What one rule object asks for, validated against the registries, or a refusal.

    A `source` rule (the default) classifies its Sources by `source_kind` and `domain`.
    A `note` rule names the `note_type` its Markdown files become; its `project` and
    `domain` are inputs of that note's destination, and its `source_kind` alone
    classifies the Source copy.
    """
    adopt_as = fields.get("adopt_as", "source")
    if adopt_as not in _ADOPT_AS:
        raise AdoptionRunError("INVALID_RULE", "adopt_as is 'source' (the default) or 'note'")
    if adopt_as == "source":
        stray = [field for field in ("note_type", "project") if field in fields]
        if stray:
            raise AdoptionRunError(
                "INVALID_RULE", f"{', '.join(stray)} belong to a rule with adopt_as: note"
            )
        return _resolve_classification(taxonomy, fields.get("source_kind"), fields.get("domain"))
    classification = (
        _resolve_classification(taxonomy, fields["source_kind"], None)
        if "source_kind" in fields else {}
    ) or {}
    return {**classification, "note": _resolve_note_rule(root, fields)}


def _resolve_note_rule(root: Path, fields: Mapping[str, str]) -> dict:
    """The registered compiled note type a rule names, if the note leaf can place it from this rule.

    The registry holds the types and their roles; the leaf holds placement and the
    inputs a type needs. Asking the leaf to validate a draft says whether it can place
    the type, so this module holds neither a type list nor a folder.
    """
    from . import note as note_module

    raw = fields.get("note_type")
    if raw is None:
        raise AdoptionRunError("NOTE_TYPE_REQUIRED", "a note rule names the note_type its files become")
    try:
        registered = note_types.Basis(root).resolve(raw).require()
    except note_types.NoteTypeUnavailable as error:
        raise AdoptionRunError("NOTE_TYPE_UNAVAILABLE", str(error)) from error
    if registered is None:
        raise AdoptionRunError(
            "NOTE_TYPE_UNREGISTERED", f"note_type {raw!r} is not in the note-type registry"
        )
    if not note_types.compiled(registered):
        raise AdoptionRunError(
            "NOTE_TYPE_NOT_COMPILED",
            f"note_type {registered.key!r} is not a compiled type; own notes become compiled notes",
        )
    rule = {"note_type": registered.key}
    for field in ("project", "domain"):
        if field in fields:
            rule[field] = fields[field]
    try:
        note_module.note(
            root, validate_only=True, content="# Probe\n\nProbe.", title="Probe",
            status="draft", **rule,
        )
    except note_module.NoteError as error:
        needs = f" (needs: {', '.join(error.missing)})" if error.missing else ""
        raise AdoptionRunError(
            "NOTE_TYPE_CANNOT_PLACE",
            f"the note leaf cannot place a {registered.key!r} draft from this rule{needs}: {error.reason}",
        ) from error
    return rule


def _resolve_classification(
    taxonomy: source_taxonomy.SourceTaxonomy, kind_raw: str | None, domain_raw: str | None
) -> dict | None:
    """The registered kind and domain a rule names, or a refusal.

    Only a result the registry already holds is accepted: an unknown key, a near
    miss of a known key, a deprecated key, or a kind that cannot file a copied
    file is refused with the resolver's close match when it has one.
    """
    if kind_raw is None and domain_raw is None:
        return None
    if kind_raw is None:
        raise AdoptionRunError("SOURCE_KIND_REQUIRED", "a domain files under a kind: name the source_kind as well")
    try:
        kind = taxonomy.resolve_kind(kind_raw)
    except source_taxonomy.TaxonomyTypoError as exc:
        raise AdoptionRunError("SOURCE_KIND_NEAR_MISS", str(exc)) from exc
    except source_taxonomy.TaxonomyError as exc:
        raise AdoptionRunError("SOURCE_KIND_INVALID", str(exc)) from exc
    if kind.status not in adoption_plan.ACCEPTED_STATUSES:
        if kind.status == "deprecated":
            replacement = f"; use {kind.replaced_by!r}" if kind.replaced_by else ""
            raise AdoptionRunError(
                "SOURCE_KIND_DEPRECATED", f"source_kind {kind.key!r} is deprecated{replacement}"
            )
        raise AdoptionRunError(
            "SOURCE_KIND_UNREGISTERED",
            f"source_kind {kind.key!r} is not in the Source taxonomy. Select again without source_kind or domain "
            "to leave the files unclassified. Registering a new kind in _Schema/source-taxonomy.yaml changes the "
            "person's vocabulary: do it only after they confirm",
        )
    if kind.key in source_taxonomy.UNCHOSEN_KINDS or kind.key == source_taxonomy.EPISODE_KIND:
        raise AdoptionRunError(
            "SOURCE_KIND_NOT_ALLOWED",
            f"source_kind {kind.key!r} records no choice or is reserved; name what the files are",
        )
    if kind.requires_url:
        raise AdoptionRunError(
            "SOURCE_KIND_NOT_ALLOWED",
            f"source_kind {kind.key!r} needs a URL, which copied files lack; "
            "name a kind that needs no URL",
        )
    domain_key = None
    if domain_raw is not None:
        try:
            domain = taxonomy.resolve_domain(domain_raw)
        except source_taxonomy.TaxonomyError as exc:
            raise AdoptionRunError("DOMAIN_INVALID", str(exc)) from exc
        if domain.status not in adoption_plan.ACCEPTED_STATUSES:
            hint = (
                f"; it looks like the registered domain {domain.close_match!r}" if domain.close_match
                else f"; replaced by {domain.replaced_by!r}" if domain.replaced_by else ""
            )
            raise AdoptionRunError(
                "DOMAIN_DEPRECATED" if domain.status == "deprecated" else "DOMAIN_UNREGISTERED",
                f"domain {domain.key!r} is {domain.status}{hint}",
            )
        domain_key = domain.key
    return {"source_kind": kind.key, "domain": domain_key}


def _materialize_selection(
    root: Path,
    run: dict,
    include: list,
    exclude: list[str],
    overrides: list[str],
    include_junk: bool,
    taxonomy: source_taxonomy.SourceTaxonomy | None,
) -> tuple[list[str], dict[str, dict], list[dict]]:
    """Materialize the concrete file set from folder rules, validating per-path.

    Include/exclude entries are subtree (or exact-file) rules applied in
    ascending path depth, so a deeper rule always overrides its ancestor —
    the UI's tri-state contract (uncheck a folder, re-check a subfolder).
    At equal depth an exclude wins over an include of the same path. Each
    selected path records the classification of the deepest include rule that
    selects it. Every selected file that cannot import comes back in the
    rejection list with its code.
    """
    inventory = run.get("inventory") or []
    files = {r["path"]: r for r in inventory if r.get("kind") != "folder"}
    folder_rows = {r["path"]: r for r in inventory if r.get("kind") == "folder"}
    paths = sorted(files)
    rejected: list[dict] = []

    def subtree(prefix: str) -> list[str]:
        return [p for p in paths if p == prefix or p.startswith(prefix + "/")]

    rules: list[tuple[int, int, str, bool, dict | None]] = []
    seen_includes: dict[str, dict | None] = {}
    for entry in include:
        path, fields = _rule_key(entry)
        classification = _classify_rule(root, taxonomy, fields) if fields else None
        if path in seen_includes and seen_includes[path] != classification:
            raise AdoptionRunError(
                "CONFLICTING_RULES", f"{path!r} appears in two include rules with different classification"
            )
        seen_includes[path] = classification
        if subtree(path):
            rules.append((len(path.split("/")), 0, path, True, classification))
        elif path in folder_rows:
            rejected.append(_reject(path, folder_rows[path]["reason"]))
        else:
            rejected.append(_reject(path, _classify_path(run, path)))
    for exc_path in exclude:
        excn = _clean(exc_path)
        rules.append((len(excn.split("/")), 1, excn, False, None))

    chosen: dict[str, dict | None] = {}
    for _depth, _tie, pathn, on, classification in sorted(rules, key=lambda r: r[:3]):
        for p in subtree(pathn):
            if on:
                chosen[p] = classification
            else:
                chosen.pop(p, None)

    override_set = {_clean(o) for o in overrides}
    for ov in sorted(override_set):
        if ov in files:
            chosen.setdefault(ov, None)
        else:
            rejected.append(_reject(ov, _classify_path(run, ov)))

    accepted: list[str] = []
    classification_of: dict[str, dict] = {}
    for p in sorted(chosen):
        row = files[p]
        if not row.get("eligible"):
            rejected.append(_reject(p, row.get("reason") or "UNSUPPORTED_IMPORT_TYPE"))
        elif row.get("junk") and not include_junk and p not in override_set:
            rejected.append(_reject(p, "JUNK"))
        else:
            accepted.append(p)
            if chosen[p]:
                classification_of[p] = chosen[p]
    unique = {(r["path"], r["code"]): r for r in rejected}
    return accepted, classification_of, [unique[key] for key in sorted(unique)]


# --------------------------------------------------------------------------- #
# presentation helpers
# --------------------------------------------------------------------------- #
# Run-object keys that drive the lifecycle but are not part of what a caller reads.
_INTERNAL_KEYS = frozenset({"plan_draft", "cursors", "inflight", "scope", "apply_invocations", _NOTE_PRICE, "sources_seal", _APPLY_PACE, _INDEX_OWED, _RECORD_INTENT, _GENERATION})  # nosemgrep: ep-word-set -- run-object keys that this module writes, not a vocabulary


def _plan_warning_lines(run: dict) -> list[str]:
    """The plan keeps each warning as a record; the top-level list is what a caller's warning count reads."""
    return [f"{warning['code']}: {warning['reason']}" for warning in (run.get("plan") or {}).get("warnings") or ()]


def _present(root: Path, run: dict, **extra) -> dict:
    out = {key: value for key, value in run.items() if key not in _INTERNAL_KEYS}
    if "pack_suggestions" in run:
        # The run stores the spelling of the surface that started it; serve this surface's.
        out["pack_suggestions"] = knowledge_packs.rendered_suggestions(run["pack_suggestions"] or [])
    out["next_actions"] = _next_actions(run)
    if plan_warnings := _plan_warning_lines(run):
        out["warnings"] = plan_warnings
    cursor = _open_cursor(run, "after")
    if cursor:
        out["next_after"] = f"{run['run_id']}:{cursor}"
    # Whenever the run carries a persisted `verify` block (kept current by `apply`
    # from the per-item re-hash of the originals), surface the SAME recorded counts
    # at the top level of every presented document — never a live re-hash. This is
    # what lets a later `status()` call stay honest even if an applied original
    # is mutated afterward: it reports what was actually verified at apply time,
    # not a fabricated fresh number.
    verify = run.get("verify")
    if verify:
        out["verified_unchanged"] = verify.get("verified_unchanged")
        out["verified_total"] = verify.get("verified_total")
        # `finish` removes filed text copies after `apply`, so a finished run reports the count that `finish` recorded.
        out["consumed"] = (run.get("finish") or verify).get("consumed", 0)
    if isinstance(out.get("finish"), dict):
        out["finish"] = _presented_finish(run)
    out.update(extra)
    return out


def _presented_finish(run: dict) -> dict:
    """The stored finish with its handoff and routes spelled for the surface reading it.

    `finish` stores each route in the grouped spelling that the command registry runs, so a
    run finished on one surface and read on another names tools that the reader lists.
    """
    finish = {**run["finish"], "handoff": _handoff(run)}
    if "route" in finish:
        finish["route"] = _spelled_route(finish["route"])
    recall = finish.get("recall_check")
    if isinstance(recall, dict) and "next_action" in recall:
        finish["recall_check"] = {**recall, "next_action": _spelled_route(recall["next_action"])}
    return finish


def _spelled_route(route: dict) -> dict:
    """A stored grouped `{"tool", "args"}` route, spelled for the serving surface, else as stored."""
    try:
        return capabilities.active_call_route(route["tool"], route["args"])
    except ValueError:
        # A route this surface cannot spell must not fail the whole read of the run.
        return route


def _next_actions(run: dict) -> list[dict]:
    phase = run.get("phase")
    actions: list[dict] = []
    if phase == "selecting":
        if run.get("plan_draft"):
            actions.append(
                {"action": "plan", "status": "available",
                 "description": "Planning is unfinished; call plan again with the continuation."}
            )
        elif (run.get("selection") or {}).get("paths"):
            actions.append(
                {"action": "plan", "status": "available",
                 "description": "Preview the exact import actions before anything is written."}
            )
        else:
            actions.append(
                {"action": "select", "status": "available",
                 "description": "Choose folders/files to import (server materializes the set)."}
            )
    elif phase == "planned":
        actions.append(
            {"action": "apply", "status": "available",
             "description": "Commit exactly the previewed imports (echo the plan_id)."}
        )
    elif phase == "applying":
        stop = run.get("apply_stop")
        if stop:
            # Blocked, not available: the continuation that this reply carries stops at the same chunk again.
            actions.append({"action": "apply", "status": "blocked", "description": stop["next"]})
        else:
            actions.append(
                {"action": "apply", "status": "available",
                 "description": "Apply stopped with work left; call apply again with the continuation, yourself, "
                                "not in a background worker. If you stop before remaining is 0, tell the person the "
                                "applied and remaining counts from progress, that the import is unfinished, and that "
                                "apply with the run_id, plan_id and continuation resumes it."}
            )
    elif phase in _FINISH_PHASES:
        actions.append(
            {"action": "finish", "status": "available",
             "description": "Prove recall and get a ready-to-run first question."}
        )
        if phase == "partial":
            actions.append(
                {"action": "apply", "status": "available",
                 "description": "Retry the failed subset with retry_failed=true."}
            )
    elif phase == "done":
        actions.append(
            {"action": "work-item", "status": "available",
             "description": "Load bounded read-only context to submit structured proposals."}
        )
        actions.extend(_residual_actions((run.get("finish") or {}).get("residual")))
    if _open_cursor(run, "after"):
        actions.append(
            {"action": "start", "status": "available",
             "description": "More files remain beyond this page. Call start with after set to next_after, "
                            "and no path or collection. Pages file in order: apply this page before planning the next."}
        )
    return actions


def _residual_actions(residual: dict | None, where: str = "finish.residual") -> list[dict]:
    """Review the files that stay staged with the person, and discard them only after the person agrees; neither while later pages follow.

    `where` names the reply field that holds the residual, so that the review text points at the field the reader has.

    A page that later pages follow leaves the review and the discard to the last page, because the folder
    still holds files that those pages file. A file that stays is never discarded without the person: a staged
    copy may be the only good copy, and `finish` does not verify every file that it reports as filed.
    """
    # A run finished before paging existed has no `more_pages` key, and no later page.
    if not residual or not residual["files"] or residual.get("more_pages"):
        return []
    # A run finished before the unfiled count existed has no summary.
    summary = residual.get("summary") or f"{residual['files']} files stay staged."
    return [
        {"action": "review-residual", "status": "available",
         "description": f"{summary} {residual['files']} files ({residual['bytes']} bytes) stay staged in {residual['folder']}; "
                        f"{where}.by_reason says why. Review them with the person before anything is discarded."},
        {"action": "discard", "status": "available",
         "description": f"Only after the person agrees, and permanently: {residual['discard']}."},
    ]


def _handoff(run: dict) -> dict:
    run_id = run["run_id"]
    run_ref = run["run_ref"]
    work_item = capabilities.active_call_text("adoption_studio", {"action": "work-item", "run_id": run_id})
    propose = capabilities.active_call_text("adoption_studio", {"action": "propose", "run_id": run_id})
    prompt_text = (
        f"Continue my Evermatter adoption run {run_id}. "
        f"Call {work_item} to load the bounded, "
        "read-only context, then submit structured proposals via "
        f"{propose}. Run ref: {run_ref}."
    )
    return {
        "prompt_text": prompt_text,
        "links": {
            "claude": "claude://claude.ai/new?" + urlencode({"q": prompt_text}),
            "codex": f'codex "{prompt_text}"',
            "gemini": f'gemini "{prompt_text}"',
        },
    }


def _proposals_summary(store: AdoptionRunStore, run_id: str) -> dict:
    payload = store.load_proposals(run_id)
    proposals = payload.get("proposals") or []
    summary = {"proposed": 0, "applied": 0, "invalid": 0, "dismissed": 0}
    for p in proposals:
        status = p.get("status")
        if status in summary:
            summary[status] += 1
    return summary


# --------------------------------------------------------------------------- #
# continuation cursors
# --------------------------------------------------------------------------- #
# A cursor is a server-issued token. Passing it to the call that issued it makes
# the call carry on; passing it again returns that call's answer and does no work.
# The cursor, not the call's arguments, is what moves, because an MCP call with
# identical arguments is replayed from the writer's stored result.
def _issue_cursor(run: dict, kind: str) -> str:
    cursors = run.get("cursors")
    if cursors is None:
        cursors = run["cursors"] = {}
    seq = len(cursors) + 1
    token = f"{kind}-{seq:04d}-{secrets.token_hex(4)}"
    cursors[token] = {"kind": kind, "seq": seq, "state": "open"}
    if kind == "apply":
        cursors[token]["scope"] = copy.deepcopy(run.get("scope"))
    return token


def _cursor_entry(run: dict, kind: str, token: str) -> dict:
    entry = (run.get("cursors") or {}).get(token)
    if not isinstance(entry, dict) or entry.get("kind") != kind:
        raise AdoptionRunError(
            "INVALID_CONTINUATION",
            f"this run did not issue that {kind} continuation; call {kind} without one to start again",
        )
    return entry


def _supersede_cursors(run: dict, kind: str) -> None:
    """A fresh call replaces any open cursor: the work it would continue restarts."""
    for entry in (run.get("cursors") or {}).values():
        if entry.get("kind") == kind and entry.get("state") == "open":
            entry["state"] = "used"
            entry["reply"] = {}


def _open_cursor(run: dict, kind: str) -> str | None:
    open_tokens = [
        (entry.get("seq", 0), token)
        for token, entry in (run.get("cursors") or {}).items()
        if entry.get("kind") == kind and entry.get("state") == "open"
    ]
    return max(open_tokens)[1] if open_tokens else None


# --------------------------------------------------------------------------- #
# lifecycle actions
# --------------------------------------------------------------------------- #
def _only_folder(paths: Iterable[str]) -> str | None:
    """The one folder that holds every path, or None; a hidden name is not counted, as in a walk of the stage."""
    visible = [path for path in paths if not path.startswith(".")]
    folders = {path.split("/", 1)[0] for path in visible}
    return next(iter(folders)) if len(folders) == 1 and all("/" in path for path in visible) else None


def _collection_of(root: Path, source_root: str, label: str | None, stage: import_stage.Stage | None = None) -> dict:
    """The collection's label, and the folder its files' collection paths hang from.

    A staged Cloud upload holds its collection in the one folder inside
    ``_Imports/<import_id>/``; a re-upload gets a new import id, so the inner folder
    is what keeps ``collection_path`` stable across uploads.

    A recorded stage (``stage``) gives the folder from its record, not from a walk, so that a window whose
    files adoption has already filed keeps it. The record is the series file's path list, which every window of
    a series carries whole, when it names every member of the manifest, and otherwise the manifest's members.
    The series file's label is the default label.
    """
    parts = source_root.split("/") if source_root else []
    collection_root = source_root
    default = parts[-1] if parts else Path(root).name
    if len(parts) == 2 and parts[0] == IMPORT_STAGE_DIR:
        series = import_stage.read_series(root, parts[1]) if stage is not None else None
        if stage is not None:
            listed = series.listing(stage.members) if series is not None else ()
            folder = _only_folder(listed or stage.members)
        else:
            try:
                entries = [e for e in (root / source_root).iterdir() if not e.name.startswith(".")]
            except OSError:
                entries = []
            folder = entries[0].name if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink() else None
        if folder is not None:
            collection_root, default = f"{source_root}/{folder}", folder
        if series is not None and series.label is not None:
            default = series.label
    chosen = (label or "").strip() or default
    return {"label": adoption_plan.nfc(chosen), "root": collection_root}


def _stage_at(root: Path, source_root: str) -> import_stage.Stage | None:
    """The member manifest of the import stage that `source_root` lies in, or None when it lies in none or the stage has none."""
    parts = source_root.split("/") if source_root else []
    if len(parts) < 2 or parts[0] != IMPORT_STAGE_DIR:
        return None
    return import_stage.read(root, parts[1])


def _origin_of(root: Path, stage: import_stage.Stage | None) -> dict[str, str]:
    """What the series file of the stage states about who sent it, or nothing for a run outside a recorded stage."""
    series = None if stage is None else import_stage.read_series(root, stage.import_id)
    return {} if series is None else dict(series.origin)


def _carry_origin(item: dict, origin: Mapping[str, str], row: Mapping | None) -> None:
    """Put the stage's origin, and the member's file time from its inventory row, on a planned item.

    The time is the member's mtime as the sender set it. A row with no usable time leaves it unstated, never a guess.
    """
    item["origin"] = dict(origin)
    mtime = (row or {}).get("mtime")
    try:
        stamp = dt.datetime.fromtimestamp(mtime, dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return
    item["file_modified"] = stamp


def _carry_item_time(item: dict, derived: import_stage.Derived) -> None:
    """Put the time that an exported item states for itself on the planned item of its transcript.

    A transcript of an item that stated no last update has the time it was written as its modified time, which says
    nothing of the item, so it carries no `file_modified`.
    """
    if derived.created:
        item["item_created"] = derived.created
    if not derived.timed:
        item.pop("file_modified", None)


def _recorded_stage(stage: import_stage.Stage | None) -> dict | None:
    """`{"stage": id, "proof": ...}` for the stage a run starts in, or None when it has no member manifest.

    The manifest itself is read again at each apply, never stored in the run: a discarded stage must stop
    being consumable at once.
    """
    return None if stage is None else {"stage": stage.import_id, "proof": stage.proof}


def _refuse_pending_renames(root: Path, verb: str, *, run_id: str | None = None, under: str | None = None) -> None:
    """Refuse `verb` while a run has renamed staged files into the Knowledge Base and not committed their Source pages.

    Such a file is in no stage and has no page, so a run that is cancelled, re-planned or restarted would leave
    it with no run to finish it. `apply` finishes it. A caller names the run, or a folder of staged files.
    """
    holds = rename_journal.pending(root, run_id=run_id, under=under)
    if holds:
        runs = ", ".join(sorted({hold.run_id for hold in holds}))
        # Only the receipt's manifest lets `apply` settle a renamed file; without it `apply` cannot, and the source stays the only copy.
        settled_by_apply = all(import_stage.read(root, hold.import_id) is not None for hold in holds)
        route = (
            "Call apply on that run first; if apply reports a file unbound, move it, and keep the source."
            if settled_by_apply
            else "The receipt of that import cannot be read, so apply cannot settle them. Move each file, and keep the source."
        )
        raise AdoptionRunError(
            "RENAMES_PENDING",
            f"{verb} is refused: run {runs} renamed {len(holds)} staged file(s) into the Knowledge Base "
            f"and has not committed their Source pages. {route}",
        )


def _refuse_outside_vault(root: Path, path: str) -> None:
    """Refuse a `start` path that names a place outside the vault, before `_clean` can reshape it.

    `_clean` and the overview resolver strip leading slashes, so `/etc` would read as the
    vault-relative `etc` and fail as NOT_FOUND, hiding that the caller meant a folder elsewhere.
    The check is lexical and never touches the filesystem: resolving a caller's absolute or UNC
    value would let a Windows host open a network connection to the host that the value names.
    """
    raw = path or ""
    route = (
        "start scans folders inside the vault; stage a folder elsewhere with "
        "`exomem import <folder>`, then start on the path it prints"
    )
    # Both spellings, whatever the host: a drive letter or UNC share is absolute on Windows
    # and an ordinary relative name on POSIX, and either one means "not a vault path".
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).anchor:
        inside = _vault_relative(root, raw)
        if inside is None:
            raise AdoptionRunError("IMPORT_PATH_OUTSIDE_VAULT", route)
        raise AdoptionRunError(
            "IMPORT_PATH_OUTSIDE_VAULT",
            f"start takes a vault-relative path, not an absolute one; use {inside!r}",
        )
    # Parent steps that climb above the vault root, normalised as text.
    climbed = posixpath.normpath(raw.replace("\\", "/"))
    if climbed == ".." or climbed.startswith("../"):
        raise AdoptionRunError("IMPORT_PATH_OUTSIDE_VAULT", route)


def _vault_relative(root: Path, raw: str) -> str | None:
    """The vault-relative form of an absolute path written in the host's own spelling, or None.

    Only the host's flavour counts: a Windows drive spelling is an ordinary file name on POSIX, not a place in the vault.
    """
    if not PurePath(raw).is_absolute() or not PurePath(root).is_absolute():
        return None
    try:
        relative = PurePath(os.path.normpath(raw)).relative_to(os.path.normpath(root))
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _cursor_holder(root: Path, cursor: str, kind: str) -> tuple[dict, str, dict]:
    """The run that issued a `<run id>:<token>` cursor of `kind`, the token and its entry; a cursor no run issued is refused."""
    run_id, _, token = cursor.partition(":")
    if not run_id or Path(run_id).name != run_id:
        raise AdoptionRunError("RUN_NOT_FOUND", "not a run id")
    run = AdoptionRunStore(root).load(run_id)
    return run, token, _cursor_entry(run, kind, token)


def _page_cursor(root: Path, after: str) -> tuple[dict, str, dict]:
    """The run that issued the `after` cursor, the cursor's token and its entry.

    The cursor is `<run id>:<token>`. The token is one the run object holds, so a guessed or altered
    cursor finds nothing and is refused, the same way a continuation cursor is.
    """
    try:
        return _cursor_holder(root, after, "after")
    except AdoptionRunError as exc:
        raise AdoptionRunError(
            "INVALID_CONTINUATION",
            "no start issued that after cursor; call start without after to scan from the first page",
        ) from exc


def _pinned_scan(entry: dict, path: str, collection: str | None, consume: bool) -> dict:
    """What the first page fixed for every later page, refusing a call that names something else.

    A caller that opts out of consuming on a later page of a consuming scan is refused, because the pinned
    choice would otherwise delete the staged copies that the caller asked to keep.
    """
    pinned = entry["after"]
    if _clean(path) not in ("", pinned["path"]) or (
        (collection or "").strip() and adoption_plan.nfc(collection.strip()) != pinned["collection"]["label"]
    ):
        raise AdoptionRunError(
            "INVALID_CONTINUATION",
            f"that after cursor belongs to the scan of {pinned['path']!r} as collection "
            f"{pinned['collection']['label']!r}; pass the same path and collection, or neither",
        )
    if pinned["consume"] and not consume:
        raise AdoptionRunError(
            "INVALID_CONTINUATION",
            "that after cursor belongs to a scan that removes staged copies once filed; "
            "omit consume, since a later page keeps the choice of the first page",
        )
    return pinned


def start(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    initialize_kb: bool = False,
    collection: str | None = None,
    consume: bool = True,
    after: str | None = None,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    today: dt.date | None = None,
) -> dict:
    """Scan a folder into a run holding one page of candidates; `after` starts the page that follows a cursor.

    While file rows remain beyond `max_candidates`, the reply carries `next_after`. A later page keeps the
    path, hidden-file choice, `consume` choice, collection root and collection label that the first page
    fixed, whatever consuming has since done to the stage.
    """
    root = Path(root)
    source_run = entry = pinned = None
    if after:
        source_run, token, entry = _page_cursor(root, after)
        pinned = _pinned_scan(entry, path, collection, consume)
        path, include_hidden = pinned["path"], pinned["include_hidden"]
        consume, max_candidates = pinned["consume"], pinned["max_candidates"]
    _refuse_outside_vault(root, path)
    _refuse_pending_renames(root, "start", under=_clean(path))
    run_date = _today(today)
    kb = managed_root(root)
    if not kb.is_dir():
        if not initialize_kb:
            raise AdoptionRunError(
                "KB_NOT_INITIALIZED",
                f"{managed_join('')} is required; pass initialize_kb=true to bootstrap the scaffold first",
            )
        from . import init as init_module

        try:
            init_module.init_vault(root)
        except FileExistsError:
            pass

    if source_run is not None and "scan_summary" in source_run and "pack_suggestions" in source_run:
        # A later page goes on with the scan that its first page summarized, so it keeps that summary. Summarizing
        # again read every staged note, about 1 ms per file on each page; only this page's listing is new.
        if not (root / _clean(path)).is_dir():
            raise AdoptionRunError("NOT_FOUND", f"no such vault path: {_clean(path) or '.'}")
        scan_summary, pack_suggestions = source_run["scan_summary"], source_run["pack_suggestions"]
    else:
        try:
            scan = overview_module.overview(root, path=path, include_hidden=include_hidden)
        except overview_module.OverviewError as exc:
            raise AdoptionRunError(exc.code, exc.reason) from exc
        junk = scan.get("junk") or {}
        scan_summary = {
            "totals": scan.get("totals", {}),
            "kb": scan.get("kb", {}),
            "junk_counts": junk.get("counts", {}),
            "junk_paths": sorted({_clean(p) for p in (junk.get("zero_byte") or [])}),
            "skipped": scan.get("skipped", {}),
        }
        pack_suggestions = knowledge_packs.suggest_packs(scan, limit=6)

    stage = _stage_at(root, _clean(path))
    rows, remaining, hidden_files = build_inventory(
        root, path=path, include_hidden=include_hidden, max_candidates=max_candidates,
        after=None if pinned is None else tuple(pinned["after"]), stage=stage if consume else None,
    )
    inv_fp = inventory_fingerprint(rows)
    created_iso = _now_iso()
    source_root = _clean(path)
    run_id = new_run_id(source_root, created_iso, inv_fp, today=run_date)

    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_ref": context_refs.adoption_run_ref(run_id),
        "created": created_iso,
        "updated": created_iso,
        "phase": "selecting",
        "source_root": source_root,
        "collection": pinned["collection"] if pinned else _collection_of(root, source_root, collection, stage),
        "stage": _recorded_stage(stage),
        "origin": _origin_of(root, stage),
        "consume": consume,
        "scan_summary": scan_summary,
        "pack_suggestions": pack_suggestions,
        "inventory": rows,
        "inventory_truncated": remaining,
        "hidden_files_skipped": hidden_files,
        "inventory_fingerprint": inv_fp,
        "selection": {
            "paths": [],
            "selection_hash": selection_hash([]),
            "rules": {"include": [], "exclude": [], "overrides": [], "include_junk": False},
            "classification": {},
            "rejected_counts": {},
            "updated": created_iso,
        },
        "plan": None,
        "outcomes": {},
        "finish": None,
        "cancel": None,
        "errors": [],
    }
    if remaining or pinned:
        run["paging"] = {
            "page": 1 if source_run is None else source_run["paging"]["page"] + 1,
            "previous": None if source_run is None else source_run["run_id"],
            "more": bool(remaining), "include_hidden": include_hidden,
        }
    if remaining:
        last = max((row for row in rows if row.get("kind") != "folder"), key=_order_key)
        issued = _issue_cursor(run, "after")
        run["cursors"][issued]["after"] = {
            "after": list(_order_key(last)), "path": source_root, "include_hidden": include_hidden,
            "consume": consume, "max_candidates": max_candidates, "collection": run["collection"],
        }
    store = AdoptionRunStore(root)
    with _LOCK:
        if source_run is not None:
            # The cursor turns `used` in the same atomic write that saves the page it opened.
            source_run = store.load(source_run["run_id"])
            current = _cursor_entry(source_run, "after", token)
            if current["state"] == "used":
                return _present(root, store.load(current["reply"]["run_id"]))
            current["state"], current["reply"] = "used", {"run_id": run_id}
        for attempt in range(_INDEX_ATTEMPTS):
            # The stage's index of runs commits with the run, so no run is missing from it.
            indexed = store.indexed_in_stage(run)
            index_write = [indexed] if indexed else []
            try:
                if source_run is None:
                    store.save(run, *index_write)
                else:
                    batch_atomic_write(
                        [store.planned_write(run), store.planned_write(source_run), *index_write], vault_root=root,
                    )
            except ContentHashMismatchError as clash:
                # Another process listed a run of this stage between the read and the commit: read the index again.
                if indexed is None or clash.path != indexed.path or attempt == _INDEX_ATTEMPTS - 1:
                    raise
            else:
                break
    return _present(root, run)


def _pending(run: dict) -> list[dict]:
    """Planned items in this call's scope that have no outcome yet, or are owed a note, in plan order."""
    plan = run.get("plan") or {}
    outcomes = run.get("outcomes") or {}
    scope = run.get("scope")
    wanted = None if scope is None else set(scope)
    return [
        item for item in plan.get("items") or []
        if (wanted is None or item["original_path"] in wanted)
        and _is_open(outcomes, item["original_path"])
    ]


def _status_view(run: dict, presented: dict, *, view: StatusView, offset: int,
                 limit: int, revision: str | None) -> dict:
    """Project one persisted run into summary metadata or a revision-bound page."""
    current_revision = hashlib.sha256(json.dumps(
        run, sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()
    if revision is not None and revision != current_revision:
        raise AdoptionRunError("IMPORT_STATUS_CHANGED", "the run changed; read its summary and restart paging")
    outcomes = run.get("outcomes") or {}
    plan = run.get("plan") or {}
    summary = {
        **_compact_row(run), "revision": current_revision, "view": view,
        "plan_id": plan.get("plan_id"), "interrupted": presented["interrupted"],
        # Summary counts cover the whole plan; the last call's subset may already be complete.
        "remaining": sum(_is_open(outcomes, item["original_path"]) for item in plan.get("items") or []),
        "next_actions": presented["next_actions"],
        "proposals_summary": presented["proposals_summary"], "handoff": presented["handoff"],
        "staleness": {"count": len(presented["stale_paths"]), "view": "stale"},
    }
    summary["counts"].update(
        planned=len(plan.get("items") or []),
        failed=sum(outcome.get("status") == "failed" for outcome in outcomes.values()),
        source_applied=sum(outcome.get("status") == _SOURCE_ONLY for outcome in outcomes.values()),
    )
    # These bounded protocol fields retain recorded verification, never a fresh byte claim.
    for key in ("continuation", "progress", "verified_unchanged", "verified_total", "consumed"):
        if key in presented:
            summary[key] = presented[key]
    if view == "summary":
        return summary
    if view == "inventory":
        rows = run.get("inventory") or []
    elif view == "selection":
        rows = (run.get("selection") or {}).get("paths") or []
    elif view == "plan":
        rows = plan.get("items") or []
    elif view == "outcomes":
        rows = [{"original_path": path, **outcomes[path]} for path in sorted(outcomes)]
    else:
        rows = presented["stale_paths"]
    items, omitted = [], []
    used = 0
    next_offset = offset
    for index in range(offset, min(len(rows), offset + limit)):
        row = rows[index]
        size = len(json.dumps(row, default=str).encode("utf-8"))
        if size > STATUS_PAGE_BYTES:
            omitted.append({"offset": index, "code": "ROW_TOO_LARGE", "bytes": size})
        elif used + size > STATUS_PAGE_BYTES:
            break
        else:
            items.append(row)
            used += size
        next_offset = index + 1
    return {
        **summary, "items": items, "omitted": omitted, "offset": offset, "limit": limit,
        "total": len(rows), "next_offset": next_offset if next_offset < len(rows) else None,
    }


def status(root: Path, *, run_id: str | None = None, path: str = "", view: StatusView = "full",
           offset: int = 0, limit: int = STATUS_PAGE_ITEMS, revision: str | None = None) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    if view not in get_args(StatusView):
        raise AdoptionRunError("IMPORT_REQUEST_INVALID", "unknown status view")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= STATUS_PAGE_ITEMS:
        raise AdoptionRunError("IMPORT_REQUEST_INVALID", f"status requires offset >= 0 and limit from 1 to {STATUS_PAGE_ITEMS}")
    if offset and revision is None:
        raise AdoptionRunError("IMPORT_REQUEST_INVALID", "a later status page requires its run revision")
    if view == "filing":
        return _filing_view(root, run_id, path)
    if not run_id:
        return {"mode": "adoption", "mutated": False, **store.listing(path=path)}
    run = store.load(run_id)
    extra: dict = {}
    if run.get("phase") == "applying":
        pending = _pending(run)
        open_cursor = _open_cursor(run, "apply")
        # A budget stop is remaining work with a cursor; only a run that died
        # mid-call, or has no cursor to carry on with, is interrupted.
        extra["interrupted"] = bool(pending) and bool(run.get("inflight") or open_cursor is None)
        extra["remaining"] = len(pending)
        if open_cursor and not extra["interrupted"]:
            extra["continuation"] = open_cursor
    elif run.get("plan_draft") and _open_cursor(run, "plan"):
        extra["continuation"] = _open_cursor(run, "plan")
        draft = run["plan_draft"]
        total = len((run.get("selection") or {}).get("paths") or [])
        extra["interrupted"] = False
        extra["progress"] = {
            "planned": draft["next"], "remaining": total - draft["next"], "total": total
        }
    else:
        extra["interrupted"] = False
    extra["interrupted"] = extra["interrupted"] or any(
        "reply" not in invocation and invocation.get("plan_id") == (run.get("plan") or {}).get("plan_id")
        for invocation in (run.get("apply_invocations") or {}).values()
    )
    stale = probe_staleness(root, run)
    presented = _present(
        root,
        run,
        staleness={"stale_paths": stale},
        stale_paths=stale,
        proposals_summary=_proposals_summary(store, run_id),
        handoff=_handoff(run),
        **extra,
    )
    if view == "full":
        return presented
    return _status_view(run, presented, view=view, offset=offset, limit=limit, revision=revision)


def _filing_view(root: Path, run_id: str | None, path: str) -> dict:
    """The filing record of one staged folder, `_Imports/<id>`: one record read, never a run, inventory or outcome."""
    parts = PurePosixPath(_clean(path)).parts
    if run_id or len(parts) != 2 or parts[0] != IMPORT_STAGE_DIR or not is_import_id(parts[1]):
        raise AdoptionRunError(
            "IMPORT_REQUEST_INVALID", f"the filing view reads one staged folder by its path, {IMPORT_STAGE_DIR}/<id>, and takes no run_id")
    return {"mode": "adoption", "mutated": False, "path": "/".join(parts), **import_filing.read_view(root, parts[1])}


def select(
    root: Path,
    *,
    run_id: str,
    include: list[str | dict] | None = None,
    exclude: list[str] | None = None,
    overrides: list[str] | None = None,
    include_junk: bool = False,
) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        if run["phase"] not in ("selecting", "planned"):
            raise AdoptionRunError(
                "INVALID_PHASE", f"select is not allowed from phase {run['phase']!r}"
            )
        _refuse_pending_renames(root, "select", run_id=run_id)
        # A rule object names a registered kind, domain or note type; resolve before any
        # change so a refused rule leaves the stored selection as it was.
        taxonomy = (
            source_taxonomy.load_taxonomy(root)
            if any(not isinstance(entry, str) for entry in include or [])
            else None
        )
        accepted, classification, rejected = _materialize_selection(
            root, run, include or [], exclude or [], overrides or [], include_junk, taxonomy
        )
        run["selection"] = {
            "paths": accepted,
            "selection_hash": selection_hash(accepted, classification),
            "rules": {
                "include": list(include or []),
                "exclude": list(exclude or []),
                "overrides": list(overrides or []),
                "include_junk": include_junk,
            },
            "classification": classification,
            "rejected_counts": dict(Counter(row["code"] for row in rejected)),
            "updated": _now_iso(),
        }
        # A selection call always invalidates any existing plan, and any plan in progress.
        run["plan"] = None
        run["plan_draft"] = None
        run["phase"] = "selecting"
        store.save(run)
    return _present(root, run, rejected=rejected)


def _unapplied(run: dict) -> bool:
    """Whether the run still has an item to apply: a planned item, or a selected one before a plan, without an applied outcome."""
    plan = run.get("plan")
    paths = (
        [item["original_path"] for item in plan["items"]] if plan
        else (run.get("selection") or {}).get("paths") or []
    )
    outcomes = run.get("outcomes") or {}
    return any((outcomes.get(path) or {}).get("status") not in _APPLIED_STATUSES for path in paths)


def _earlier_pages(store: AdoptionRunStore, run: dict) -> tuple[dict, ...]:
    """The earlier pages of this run's paged scan, nearest first; a page whose record is gone ends the chain."""
    pages: list[dict] = []
    previous = (run.get("paging") or {}).get("previous")
    while previous:
        try:
            earlier = store.load(previous)
        except AdoptionRunError:
            break
        pages.append(earlier)
        previous = (earlier.get("paging") or {}).get("previous")
    return tuple(pages)


def unfiled_members(root: Path, import_id: str, runs: tuple[dict, ...] | None = None) -> list[dict]:
    """The members of an import that adoption did not file or `finish` did not verify, as a residual record lists them.

    Each entry has the path relative to the stage, its bytes and why it stays (see `adoption_consume.residual`).
    The members are the stage manifest's, plus any staged file that the manifest does not list, plus the items and
    exports that expansion could not read in full. A member that
    left the stage, by rename or by hand, keeps the bytes the manifest gives it; the runs that started in the
    stage classify every member, the most recent first, and one that no run scanned is `outside_run`. A member
    that adoption filed, and the series file, are left out: filed is what a run's outcome says, not what the
    stage no longer holds. A caller that lists many windows passes `runs`, the stage's runs from `runs_by_stage`,
    so it reads the run store once.
    """
    pages = AdoptionRunStore(root).runs_of_stage(import_id) if runs is None else runs
    stage = import_stage.read(root, import_id)
    files = dict(import_stage.staged_files(root, import_id))
    for path, (size, _digest) in (stage.members if stage is not None else {}).items():
        files.setdefault(path, size)
    unfiled = [
        import_stage.residual_member(path, size, reason)
        for path, size, reason in adoption_consume.stage_reasons(import_id, list(files.items()), pages, _APPLIED_STATUSES)
        if not adoption_consume.is_filed(reason) and path != import_stage.SERIES_FILE
    ]
    # An item that could not be read and an export that was read in part have no staged file: the expansion record lists them.
    return [*unfiled, *export_expansion.read(root, import_id).unfiled]


def _earlier_page_unapplied(store: AdoptionRunStore, run: dict) -> str | None:
    """The id of the nearest earlier page of this run's collection that still has work to apply, or None.

    A page holds destinations in its plan until it applies them, and a later plan reads the disk, so
    a later page may plan only after the earlier ones have applied. A finished or cancelled page
    holds none.
    """
    for earlier in _earlier_pages(store, run):  # a page whose record is gone holds no work
        if earlier["phase"] not in _FINISHED_PHASES and _unapplied(earlier):
            return earlier["run_id"]
    return None


def _whole_scan(root: Path, run: dict) -> list[str] | None:
    """The collection path of every file the scan holds, for a run of a series or a paged run; None for any other.

    A recorded stage with a series file passes the series' path list, so that a bare name meets the files of
    the windows that have not landed, when the list names every member of the stage's manifest (see
    `Series.listing`). Consumed files of earlier pages are no longer on disk, but their Sources are in the
    import index.
    """
    collection_root = (run.get("collection") or {}).get("root", "")
    stage = import_stage.read(root, run["stage"]["stage"]) if run.get("stage") else None
    listed = ()
    if stage is not None and (series := import_stage.read_series(root, stage.import_id)) is not None:
        listed = series.listing(stage.members)
    if listed:
        prefix = f"{IMPORT_STAGE_DIR}/{stage.import_id}"
        return [
            adoption_plan.collection_path_of(f"{prefix}/{path}", collection_root)
            for path in listed if path != import_stage.SERIES_FILE
        ]
    paging = run.get("paging")
    if not paging:
        return None
    files, _folders, _hidden = walk_inventory(
        root, path=run["source_root"], include_hidden=paging["include_hidden"],
        stage=adoption_consume.recorded(root, run) if run.get("consume", True) else None,
    )
    return [
        adoption_plan.collection_path_of(row["path"], collection_root) for row in files if row["eligible"]
    ]


def _no_kept_speech() -> dict:
    """The plan's running count of transcript pages.

    `staged` pages settle on a recording that this plan stores, `stored` ones on a recording the vault holds, and
    `awaiting` ones wait for a recording that no Source holds yet. `part_pages` counts the further pages of the
    extractions that settle, and `unpaired` counts the pages that stay text by reason, with their part pages
    under `unpaired_part_pages`.
    """
    return {"staged": 0, "stored": 0, "awaiting": 0, "part_pages": 0, "unpaired": {}, "unpaired_part_pages": 0}


def _holds_kept_speech(counts: Mapping[str, object]) -> bool:
    return bool(counts["staged"] or counts["stored"] or counts["awaiting"] or counts["unpaired"])


class _Arrivals:
    """Whether another window of an import's series still has a recording to file, read once for each window.

    A transcript page waits for its recording only while a window of its own series holds that recording unfiled
    and nothing says the window is finished (`expects`). A window that the person discarded or cancelled, one that
    filed its last step, and a recording that failed in a finished window each send nothing: the page files as text.

    The read costs what the series costs and never what the vault's history costs, and it stops at the first
    question that answers no. It checks the recording against the page's own series (one small file) first. It then
    lists the stage folders that the vault holds now, and looks for the recording in each by name. Only a window
    that holds it has its series file read, and only one of the same series has its filing record, its window
    record and its stage's index of runs read (`AdoptionRunStore.stage_steps`). It loads no other run.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._folders: list[str] | None = None
        self._series: dict[str, import_stage.Series | None] = {}
        self._named: dict[str, frozenset[str]] = {}
        self._unfinished: dict[str, bool] = {}

    def expects(self, page_rel: str) -> bool:
        """Whether the recording that the staged page `page_rel` is named for is still to be filed by another window of its series."""
        parts = PurePosixPath(page_rel).parts
        if len(parts) < 3 or parts[0] != IMPORT_STAGE_DIR or not page_rel.lower().endswith(".md"):
            return False
        window, recording = parts[1], "/".join(parts[2:])[: -len(".md")]
        mine = self._read_series(window)
        if mine is None or recording not in self._paths(window):
            return False  # no window of this series holds that recording: nothing else needs reading
        for other in self._stage_folders():
            if other == window or not os.path.lexists(self._root / IMPORT_STAGE_DIR / other / recording):
                continue
            theirs = self._read_series(other)
            if theirs is not None and (theirs.label, theirs.paths) == (mine.label, mine.paths) and self._open(other):
                return True
        return False

    def _stage_folders(self) -> list[str]:
        """The stage folders that the vault holds now, not every window it ever received: a window whose folder is gone has no file left to file."""
        if self._folders is None:
            try:
                with os.scandir(self._root / IMPORT_STAGE_DIR) as entries:
                    self._folders = sorted(entry.name for entry in entries if is_import_id(entry.name))
            except OSError:
                self._folders = []
        return self._folders

    def _read_series(self, window: str) -> import_stage.Series | None:
        if window not in self._series:
            self._series[window] = import_stage.read_series(self._root, window)
        return self._series[window]

    def _paths(self, window: str) -> frozenset[str]:
        if window not in self._named:
            series = self._read_series(window)
            self._named[window] = frozenset(series.paths) if series is not None else frozenset()
        return self._named[window]

    def _open(self, window: str) -> bool:
        """Whether a window still has work that can file its members: not discarded, not filed to the end, not cancelled.

        A window that no run has started in is not planned yet, so it is open. Every run that started in it is read
        by its counted step: one step other than `done` (an open scan or apply, a retry, a finish) keeps it open, as
        `_resumed_page` reads a stage's runs. A run that was cancelled ends in `done`, so nothing files its members.
        An index that exists and cannot be read is no answer: the window stays open, so that damage never files a
        page early (`AdoptionRunStore.stage_steps`).
        """
        if window not in self._unfinished:
            self._unfinished[window] = self._read_open(window)
        return self._unfinished[window]

    def _read_open(self, window: str) -> bool:
        record = import_filing.read_record(self._root, window)
        if record is not None and record["state"] in (import_filing.DONE, import_filing.DISCARDED):
            return False
        landed = import_stage.window_record(self._root, window)
        if landed is None or landed.kind != import_stage.WINDOW_LANDED:
            return False
        steps = AdoptionRunStore(self._root).stage_steps(window)
        return steps is None or not steps or any(step != "done" for step in steps)


def _pair_kept_page(
    root: Path, rel: str, recordings: Mapping[str, dict], counts: dict, arrivals: _Arrivals, *, collection_root: str,
) -> tuple[dict | None, dict | None]:
    """Pair a sidecar-named page with its recording (design D1 of `keep-speech-results-in-cloud`).

    Returns `(item, awaiting)`. `item` is the plan item of a page whose result settles on the Source of its
    recording after apply stores that Source (a recording of this plan) or finds it (one stored before the plan).
    `awaiting` is the pairing record of a page that is complete but whose recording is stored nowhere yet while
    another window of its series still holds it, or while the index cannot say: the page plans as a text Source
    too, and apply files that Source only when no recording is coming. With neither, the page plans as before
    (an unpaired page is counted by reason).
    """
    named = kept_speech.candidate(rel)
    if named is None:
        return None, None
    original, media_type = named
    recording = recordings.get(original)
    expect = (recording["original_sha256"], recording["original_bytes"]) if recording is not None else None
    examined = kept_speech.examine(root, rel, media_type=media_type, expect=expect)
    if isinstance(examined, kept_speech.Unpaired):
        counts["unpaired"][examined.reason] = counts["unpaired"].get(examined.reason, 0) + 1
        counts["unpaired_part_pages"] += examined.part_pages
        return None, None
    if recording is not None:
        target, artifact, planned_as = recording["target_path"], recording.get("artifact_path"), recording["original_path"]
    else:
        try:
            found = kept_speech.find_recording(root, examined.sha256, examined.size)
        except kept_speech.LookupUnavailable:
            counts["awaiting"] += 1  # the index will answer later: the page waits, it is not text for good
            return None, examined.record(awaiting=True)
        if found is None:
            if arrivals.expects(rel):
                counts["awaiting"] += 1
                return None, examined.record(awaiting=True)
            counts["unpaired"][kept_speech.NO_RECORDING] = counts["unpaired"].get(kept_speech.NO_RECORDING, 0) + 1
            counts["unpaired_part_pages"] += examined.part_pages
            return None, None
        target, artifact, planned_as = found + ".md", found, None
    counts["staged" if recording is not None else "stored"] += 1
    counts["part_pages"] += examined.part_pages
    return {
        "original_path": rel, "original_sha256": examined.page_sha256, "original_bytes": examined.page_bytes,
        "form": kept_speech.FORM, "action": kept_speech.KEEPS, "classification": None,
        "collection_path": adoption_plan.collection_path_of(rel, collection_root),
        "target_path": target, "target_ref": context_refs.source_ref(target), "artifact_path": artifact,
        "title": Path(rel).stem, "tags": [], "rejected_tags": [], "aliases": [], "rejected_aliases": [],
        "_refs": [], "kept": examined.record(recording=planned_as),
    }, None


def plan(
    root: Path,
    *,
    run_id: str,
    continuation: str | None = None,
    today: dt.date | None = None,
) -> dict:
    """Preview every import: read, hash, freeze destinations, resolve links.

    Bounded by the request budget: a call that runs short saves its draft in the
    run object and returns a ``continuation``; the plan exists, and the phase is
    ``planned``, only after the last item.
    """
    from . import client_artifacts

    root = Path(root)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        # Re-planning is always allowed (it replaces the plan); a fresh plan from
        # `planned` re-reads and re-hashes the current selection.
        if run["phase"] not in ("selecting", "planned"):
            raise AdoptionRunError(
                "INVALID_PHASE", f"plan requires phase 'selecting' or 'planned', got {run['phase']!r}"
            )
        _refuse_pending_renames(root, "plan", run_id=run_id)
        selection = run.get("selection") or {}
        selected = selection.get("paths") or []
        if not selected:
            raise AdoptionRunError("MISSING_SELECTION", "plan requires a non-empty selection")
        sel_hash = selection["selection_hash"]

        if continuation:
            entry = _cursor_entry(run, "plan", continuation)
            if entry["state"] == "used":
                return _present(root, run, **(entry.get("reply") or {}))
            draft = run.get("plan_draft")
            if not draft or draft.get("selection_hash") != sel_hash:
                raise AdoptionRunError(
                    "INVALID_CONTINUATION",
                    "the selection changed or the plan is finished; call plan without a continuation",
                )
        else:
            earlier = _earlier_page_unapplied(store, run)
            if earlier is not None:
                raise AdoptionRunError(
                    "EARLIER_PAGE_UNAPPLIED",
                    f"run {earlier} holds an earlier page of this collection with work still to apply; "
                    "apply it, or finish or cancel it, before planning this page",
                )
            entry = None
            _supersede_cursors(run, "plan")
            draft = {
                "selection_hash": sel_hash,
                "date": _today(today).isoformat(),
                "items": [],
                "skipped": [],
                "next": 0,
                "kept_speech": _no_kept_speech(),
                # Listed before the first index read below, so a page filed in between breaks the
                # seal at apply instead of hiding from the plan.
                "sources_seal": adoption_plan.sources_seal(adoption_plan.source_page_paths(root)),
            }
            run["plan"] = None
            run["sources_seal"] = None
            run["phase"] = "selecting"

        collection = run.get("collection") or {"label": None, "root": ""}
        classification = selection.get("classification") or {}
        taxonomy = source_taxonomy.load_taxonomy(root) if classification else None
        inventory = {row["path"]: row for row in run.get("inventory") or []}
        origin = run.get("origin") or {}
        ceiling = client_artifacts.adoption_ceiling()
        stage = adoption_consume.recorded(root, run) if run.get("consume", True) else None
        stage_id = _stage_id_of(run["source_root"])
        derived = import_stage.derived_files(root, stage_id) if stage_id is not None else {}
        # A recognised export files as an artifact, so its conversations are indexed once, as the transcripts derived from it.
        exports = set(export_expansion.read(root, stage_id).exports) if stage_id is not None else set()
        stage_prefix = f"{IMPORT_STAGE_DIR}/{stage_id}/"
        index = adoption_plan.read_import_index(root)
        notes = adoption_plan.NoteIndex(root)
        destinations = adoption_plan.Destinations.frozen_by(root, draft["items"])
        seen = adoption_plan.seen_in_plan(draft["items"])
        kept_counts = draft.setdefault("kept_speech", _no_kept_speech())
        arrivals = _Arrivals(root)
        # Artifacts plan before text, so a recording is planned by the time its sidecar-named page is.
        recordings = {item["original_path"]: item for item in draft["items"] if item["form"] == "artifact"}

        budget = _call_budget()
        handled = 0
        with lexstore.shared_reads(root):
            while draft["next"] < len(selected):
                if handled and not budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS):
                    break
                rel = selected[draft["next"]]
                kept_item, awaiting = _pair_kept_page(
                    root, rel, recordings, kept_counts, arrivals, collection_root=collection["root"],
                )
                if kept_item is not None:
                    draft["next"] += 1
                    handled += 1
                    draft["items"].append(kept_item)
                    continue
                result = adoption_plan.plan_item(
                    root, rel,
                    collection=collection["label"], collection_root=collection["root"],
                    classification=classification.get(rel), taxonomy=taxonomy,
                    text_hint=(inventory.get(rel) or {}).get("form", "text") == "text"
                    and rel.removeprefix(stage_prefix) not in exports,
                    date_iso=draft["date"], destinations=destinations, index=index, notes=notes,
                    ceiling=ceiling, vouched=adoption_consume.vouched(stage, rel), seen=seen,
                )
                draft["next"] += 1
                handled += 1
                if isinstance(result, dict):
                    draft["skipped"].append(result)
                else:
                    if origin:
                        _carry_origin(result[0], origin, inventory.get(rel))
                    if awaiting is not None:
                        result[0]["kept"] = awaiting
                    if (item_time := derived.get(rel.removeprefix(stage_prefix))) is not None:
                        _carry_item_time(result[0], item_time)
                    draft["items"].append(result[0])
                    if result[0]["form"] == "artifact":
                        recordings[rel] = result[0]

        progress = {
            "planned": draft["next"],
            "remaining": len(selected) - draft["next"],
            "total": len(selected),
        }
        if draft["next"] < len(selected):
            token = _issue_cursor(run, "plan")
            reply = {"progress": progress, "continuation": token}
            if entry is not None:
                entry["state"], entry["reply"] = "used", reply
            run["plan_draft"] = draft
            store.save(run)
            return _present(root, run, **reply)

        items = draft["items"]
        adoption_plan.resolve_links(
            items, index, collection=collection["label"], whole_scan=_whole_scan(root, run)
        )
        skipped = list(draft["skipped"])
        for planned in items:
            if planned["action"] == "copy-as-source":
                planned["frontmatter"] = adoption_plan.item_frontmatter(planned, collection["label"])
            if planned.get("note"):
                planned["note"]["frontmatter"] = adoption_plan.note_frontmatter(
                    planned, collection["label"]
                )
            if planned.get("slug_warning"):
                skipped.append(
                    {"path": planned["original_path"], "code": "SLUG_TRUNCATED",
                     "reason": planned["slug_warning"]}
                )
            if planned.get("duplicate_check_unavailable"):
                skipped.append(
                    {"path": planned["original_path"], "code": "DUPLICATE_CHECK_UNAVAILABLE",
                     "reason": duplicates.unavailable_warning(planned["original_path"])}
                )
        run["plan"] = {
            "plan_format": PLAN_FORMAT,
            "plan_id": plan_id_for(sel_hash, [adoption_plan.item_digest(i) for i in items]),
            "created": _now_iso(),
            "date": draft["date"],
            "selection_hash": sel_hash,
            "collection": collection["label"],
            "items": items,
            "skipped": skipped,
            "warnings": adoption_plan.root_warnings(
                index, collection=collection["label"], collection_root=collection["root"]
            ),
            "total_bytes": adoption_plan.plan_total_bytes(items),
            "notes": {
                "to_create": sum(1 for i in items if i.get("note")),
                "existing": sum(1 for i in items if i.get("existing_note")),
                "previous": sum(1 for i in items if i.get("previous_note")),
            },
        }
        if _holds_kept_speech(draft["kept_speech"]):
            run["plan"]["kept_speech"] = draft["kept_speech"]
        run["plan_draft"] = None
        run["sources_seal"] = draft.get("sources_seal")
        run["phase"] = "planned"
        reply = {"progress": progress}
        if entry is not None:
            entry["state"], entry["reply"] = "used", reply
        store.save(run)
    return _present(root, run, **reply)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def _failed(code: str, reason: str) -> dict:
    return {"status": "failed", "code": code, "reason": reason, "at": _now_iso()}


def _already_applied(page: str, artifact: str | None, sha256: str) -> dict:
    ref = context_refs.source_ref(page)
    outcome = {
        "status": "already-applied", "target_path": page, "source_ref": ref,
        "existing_ref": ref, "sha256": sha256, "at": _now_iso(),
    }
    if artifact:
        outcome["artifact_path"] = artifact
    return outcome


def _applied(planned: dict) -> dict:
    outcome = {
        "status": "applied", "target_path": planned["target_path"],
        "source_ref": planned["target_ref"], "sha256": planned["original_sha256"],
        "at": _now_iso(),
    }
    if planned.get("artifact_path"):
        outcome["artifact_path"] = planned["artifact_path"]
    if planned.get("stored_own_copy"):
        outcome["stored_own_copy"] = planned["stored_own_copy"]
    return outcome


def _is_open(outcomes: dict, path: str) -> bool:
    """Whether the item still has work: no outcome yet, or its Source is down and its note owed."""
    return path not in outcomes or outcomes[path].get("status") == _SOURCE_ONLY


def _owes_note(planned: dict) -> bool:
    """Whether the item's plan makes a draft note, or expects the one an earlier run made."""
    return bool(planned.get("note") or planned.get("existing_note"))


def _hold_for_note(outcome: dict, planned: dict) -> None:
    """Keep an own-note item open after its Source: the note is still owed."""
    if _owes_note(planned) and outcome["status"] in _APPLIED_STATUSES:
        outcome["source_status"], outcome["status"] = outcome["status"], _SOURCE_ONLY


def _source_status(outcome: dict) -> str:
    """Whether the item's Source was made by this run (`applied`) or found (`already-applied`)."""
    return outcome.get("source_status") or outcome["status"]


def _read_original(
    root: Path, planned: dict, ceiling: int, *, by_rename: bool = False
) -> tuple[str | None, dict | None]:
    """The original's text (None for an artifact), or a failed outcome when it drifted.

    An artifact is only sized here: the batch hashes its bytes once, as it copies them, and refuses
    them when they differ from the plan (`_commit_chunk` then finds which item drifted). A member
    that files by rename is not bound by the ceiling here; `import_rename` applies it to the hash of a
    file whose identity no longer matches, and the size check below still catches a grown file.
    """
    read = adoption_plan.read_facts(
        root, planned["original_path"], text_hint=planned["form"] == "text", hash_artifact=False,
        ceiling=max(ceiling, planned["original_bytes"]) if by_rename else ceiling,
    )
    if isinstance(read, dict):
        code = "SOURCE_CHANGED" if read["code"] == adoption_plan.OVER_CEILING else read["code"]
        return None, _failed(code, read["reason"])
    facts, text = read
    if facts["original_bytes"] != planned["original_bytes"] or (
        planned["form"] == "text"
        and (text is None or facts["original_sha256"] != planned["original_sha256"])
    ):
        return None, _failed("SOURCE_CHANGED", "the original differs from the plan at apply time")
    return text, None


def _original_unchanged(root: Path, planned: dict) -> bool:
    """Re-hash one original: the honest after-the-fact check that it was never touched."""
    read = adoption_plan.read_facts(
        root, planned["original_path"], ceiling=planned["original_bytes"], text_hint=False
    )
    return not isinstance(read, dict) and read[0]["original_sha256"] == planned["original_sha256"]


def _earlier_with_same_bytes(
    root: Path, index: list[adoption_plan.EarlierImport], label: str | None, planned: dict
) -> adoption_plan.EarlierImport | None:
    earlier = adoption_plan.earlier_with_same_bytes(
        index, collection=label, collection_path=planned["collection_path"],
        original_path=planned["original_path"], sha256=planned["original_sha256"],
    )
    if earlier is not None and planned["form"] == "artifact":
        if adoption_plan.artifact_of(root, earlier.path) is None:
            return None
    return earlier


def _settle_on_stored(
    root: Path, planned: dict, *, label: str | None, holder: str | None, stats: Counter
) -> dict | None:
    """Settle an item whose exact bytes the vault already stores: name that Source, write nothing else.

    `holder` is the Source the plan named for the item's bytes. It is read again under the apply's mutation
    guard and named while its stored bytes still hash to the item's, with no help from the duplicate index,
    so a plan that named its holder applies when the index cannot answer. When it names none, or the one it
    named has gone, the duplicate lookup repeats here instead, so a Source that another run or door filed
    since the plan decides. The Source gains this item's path as one mapping, through a write that checks the
    page's content hash and touches no other field. The outcome is `already-applied`, with `mapped: true`
    when this call recorded the mapping, and `mapping_not_recorded` (the reason) when the Source is
    raw-protected and so is never rewritten. A failed outcome means the mapping could not be recorded. None
    means no usable copy holds the bytes: the vault has none, the index cannot answer, or the caller cannot
    write the Source that holds them. The caller then stores the item's own bytes.
    """
    sha256, protected = planned["original_sha256"], raw_protection.marked(planned["target_path"])
    keeps_original = planned["form"] == "artifact"
    hit = None if holder is None else duplicates.named_duplicate(
        root, holder, sha256=sha256, lane=duplicates.SOURCES, protected=protected, keeps_original=keeps_original,
    )
    if hit is None:
        lookup = duplicates.find_duplicate(
            root, sha256=sha256, lane=duplicates.SOURCES, protected=protected, keeps_original=keeps_original
        )
        if not lookup.available:
            stats["duplicate_check_unavailable"] += 1
            return None
        hit = lookup.hit
    if hit is None or access.writable_reason(root, hit.path) is not None:
        return None
    if planned["form"] == "artifact" and not _original_unchanged(root, planned):
        return _failed("SOURCE_CHANGED", "the original differs from the plan at apply time")
    mapping = adoption_plan.import_mapping(
        planned["original_path"], collection=label, collection_path=planned["collection_path"]
    )
    mapped = not adoption_plan.mapping_recorded(hit.payload.frontmatter, mapping)
    # A release grant binds to a protected page's content hash, so this import's path is not written onto it.
    withheld = mapped and hit.payload.protected
    mapped = mapped and not withheld
    if mapped and not frontmatter_update.update_page_frontmatter(
        root, hit.path, lambda frontmatter: adoption_plan.mapping_fields(frontmatter, mapping)
    ):
        return _failed(
            "PLAN_STALE", "the Source that holds these bytes changed or cannot take this path; re-plan this import"
        )
    outcome = _already_applied(hit.path, hit.payload.artifact_path, planned["original_sha256"])
    if mapped:
        outcome["mapped"] = True
        stats["mapped"] += 1
    if withheld:
        outcome["mapping_not_recorded"] = (
            "the Source that holds these bytes is raw-protected, and a rewrite would revoke its release grant"
        )
    return outcome


def _held_by(planned: dict, outcome: dict) -> dict:
    """`planned` as the Source that now holds its bytes records it: its page and artifact, not the frozen ones."""
    return {**planned, "target_path": outcome["target_path"], "artifact_path": outcome.get("artifact_path")}


def _record_stored(
    root: Path, run: dict, reads: _CallReads, planned: dict, outcome: dict, *, settled: list[dict]
) -> None:
    """Keep the outcome of an item `_settle_on_stored` settled; a found Source then settles its staged copy and note."""
    label = (run.get("collection") or {}).get("label")
    run["outcomes"][planned["original_path"]] = outcome
    if outcome["status"] != "already-applied":
        return
    held = _held_by(planned, outcome)
    _hold_for_note(outcome, held)
    settled.append(held)
    if outcome.get("mapped") and reads.index is not None:
        reads.index.append(
            adoption_plan.earlier_import_of(held, collection=label, captured=run["plan"]["date"])
        )


def _own_copy(planned: dict, page: str, artifact: str | None) -> dict:
    """`planned` as an ordinary item that stores its own bytes at `page`, because no copy could stand for it."""
    own = {key: value for key, value in planned.items() if key not in ("duplicate_of", "mapping")}
    own.update(
        action="copy-as-source", target_path=page, target_ref=context_refs.source_ref(page), artifact_path=artifact,
        stored_own_copy="the Source that the plan named for these bytes was missing or changed at apply time, "
        "so they were stored as a Source of their own",
    )
    return own


def _store_own_copy(
    root: Path, planned: dict, destinations: adoption_plan.Destinations, *, date_iso: str
) -> tuple[dict | None, dict | None]:
    """`planned` as an ordinary item with a destination allocated now, or the failed outcome when it has none.

    The fail-open answer when the Source that a plan named for an item's bytes cannot hold them: the item
    files as its own Source, in the folder its own classification gives, never as a failure that a re-plan
    would not cure. A taxonomy that no longer accepts the classification, or a folder the caller cannot write,
    still fails the item.
    """
    classification = planned.get("classification")
    folder = adoption_plan.folder_for(
        root, classification, source_taxonomy.load_taxonomy(root) if classification else None
    )
    if isinstance(folder, str):
        return None, _failed(folder, "the Source taxonomy no longer accepts this rule's kind or domain")
    page, artifact, slug_warning = adoption_plan.allocate_source(planned, folder, destinations, date_iso=date_iso)
    reason = access.writable_reason(root, page)
    if reason is not None:
        return None, _failed("WRITE_REFUSED", reason)
    own = _own_copy(planned, page, artifact)
    if slug_warning:
        own["slug_warning"] = slug_warning
    return own, None


class _ApplyPacer:
    """Decides how much of the plan one `apply` call may start, from what its chunks cost.

    A call never starts a chunk whose predicted cost does not fit in the time left
    before the delivery reserve, after the fan-out the call already owes past the
    loop. A chunk costs its items, its bytes and its media requests. Costs are those
    this call has measured, not a past call's, and the byte and media rates are the
    dearest seen so far: chunk cost swings several-fold with media artifacts and a
    contended index, so the last chunk is a poor guide to the next. A slow stretch shrinks
    the rest of this call's chunks and nothing after it. There are two exceptions. A call starts
    from the item chunks that the run's last call timed while they are fresh (`carried`), so it
    does not spend its first chunks timing small probes again. `_create_notes` sizes a call's
    first batch of notes on the price that the run's last call recorded.

    A commit also costs a fixed part that does not depend on its size, and that part grows
    with the vault. Once two chunks of different sizes are timed, an item is priced at the
    fitted marginal rate and the commit at the dearest fixed part consistent with it. A
    chunk may then last up to twice its fixed part: shrinking a chunk below that only adds
    commits, each paying the fixed part again, until a chunk of one item costs more than
    the call can file.
    """

    def __init__(self, root: Path, budget: request_budget.RequestBudget, *, carried: object = None) -> None:
        from .writer_lease import active_derived_batch_custody

        self._root = root
        self._budget = budget
        self._item_seconds: float | None = None
        self._commit_seconds = 0.0
        self._item_chunks: list[tuple[int, float]] = []
        self._mib_seconds: float | None = None
        self._extraction_seconds: float | None = None
        self._deferred_artifacts = 0
        self._started_items = 0
        # A fast-acknowledgement session runs the sidecar fan-out behind the reply.
        self._owes_fanout = not active_derived_batch_custody(root)
        self._carry(carried)

    def _carry(self, carried: object) -> None:
        """Start from what the run's last call measured, when it is fresh and well formed."""
        if not isinstance(carried, dict):
            return
        try:
            age = dt.datetime.now(dt.UTC).timestamp() - float(carried["at"])
            chunks = [(int(size), float(seconds)) for size, seconds in carried.get("item_chunks") or ()]
        except (KeyError, TypeError, ValueError):
            return
        if not 0 <= age <= _NOTE_PRICE_FRESH_SECONDS or any(size < 1 or seconds < 0 for size, seconds in chunks):
            return
        self._item_chunks = chunks[-_CARRIED_CHUNKS:]
        if self._item_chunks:
            self._price_items()

    def carried(self) -> dict | None:
        """What the next call starts from: the newest timed item chunks, with when they were taken.

        The byte and media rates stay with the call that measured them: they are the dearest seen, so carrying
        one would price every later call of the page at one slow or failed request.
        """
        if not self._item_chunks:
            return None
        return {
            "at": dt.datetime.now(dt.UTC).timestamp(),
            "item_chunks": [list(chunk) for chunk in self._item_chunks[-_CARRIED_CHUNKS:]],
        }

    @staticmethod
    def _tracked(rate: float | None, seen: float) -> float:
        return seen if rate is None else max(rate, seen)

    def _custody_reserve(self, chunk: list[dict]) -> float:
        """Seconds the end of the call needs to hand off custody of the batches so far and this chunk's."""
        from .writer_lease import owed_custody_handoff_seconds

        measured = owed_custody_handoff_seconds(self._root, more_batches=_custody_batches(chunk))
        if measured is None:
            return TERMINAL_WORK_RESERVE_SECONDS
        return min(max(measured, CUSTODY_RESERVE_FLOOR_SECONDS), TERMINAL_WORK_RESERVE_SECONDS)

    def _owed_after_loop(self, chunk: list[dict], artifacts: int) -> float:
        if not self._owes_fanout:
            return self._custody_reserve(chunk)
        if not artifacts:
            return 0.0
        return TERMINAL_WORK_RESERVE_SECONDS + artifacts * TERMINAL_FANOUT_SECONDS_PER_ARTIFACT

    @staticmethod
    def _media(chunk: list[dict]) -> int:
        """How many of `chunk` get an extraction request: media artifacts, by extension."""
        from . import media_processing

        return sum(
            1 for planned in chunk
            if planned["form"] == "artifact" and planned.get("artifact_path")
            and media_processing.classify_media(planned["artifact_path"]) is not None
        )

    @staticmethod
    def _mib(chunk: list[dict]) -> float:
        return sum(planned["original_bytes"] for planned in chunk) / (1024 * 1024)

    def _cost(self, chunk: list[dict]) -> float:
        item = INITIAL_ITEM_SECONDS if self._item_seconds is None else self._item_seconds
        per_mib = INITIAL_SECONDS_PER_MIB if self._mib_seconds is None else self._mib_seconds
        extraction = (
            INITIAL_EXTRACTION_SECONDS if self._extraction_seconds is None
            else self._extraction_seconds
        )
        return (
            self._commit_seconds + len(chunk) * item + self._mib(chunk) * per_mib
            + self._media(chunk) * extraction
        )

    def _room(self, chunk: list[dict]) -> float:
        """Seconds the chunk may take: what is left, less the reserve and the call's debts."""
        return (
            self._budget.remaining() - request_budget.DELIVERY_RESERVE_SECONDS
            - REPLY_SECONDS_PER_ITEM * (self._started_items + len(chunk))
            - self._owed_after_loop(chunk, self._deferred_artifacts + self._media(chunk))
        )

    def next_chunk(self, pending: list[dict], *, first: bool) -> list[dict]:
        """The next chunk, trimmed to fit; empty when none fits. The first is never empty."""
        share = max(APPLY_CHUNK_BUDGET_SHARE * self._budget.seconds, 2 * self._commit_seconds)
        limit = APPLY_CHUNK_ITEMS
        timed = {size for size, _ in self._item_chunks}
        if len(timed) == 1 and min(APPLY_CHUNK_ITEMS, 2 * next(iter(timed))) not in timed:
            # A second size, twice the first, so the fit can tell the fixed part from the per-item rate.
            limit = min(APPLY_CHUNK_ITEMS, 2 * next(iter(timed)))
            share = float("inf")
        chunk = _take_chunk(pending, limit)
        cost, allowed = self._cost(chunk), min(share, self._room(chunk))
        while len(chunk) > 1 and cost > allowed:
            chunk = chunk[: max(1, int(len(chunk) * allowed / cost))]
            cost, allowed = self._cost(chunk), min(share, self._room(chunk))
        return chunk if first or cost <= self._room(chunk) else []

    def next_unit(self, pending: list[dict]) -> tuple[float, int, float]:
        """The full next chunk, before any trim: its predicted seconds, its batches and its room."""
        chunk = _take_chunk(pending, APPLY_CHUNK_ITEMS)
        return self._cost(chunk), _custody_batches(chunk), self._room(chunk)

    def note_fits(self, chunk: list[dict], seconds: float) -> bool:
        """Keep the same terminal headroom between the chunk's individual notes."""
        return seconds <= self._room(chunk)

    def record(self, chunk: list[dict], elapsed: float, extractions: list[float]) -> None:
        """Learn from a finished chunk: `extractions` are its media requests' seconds."""
        self._started_items += len(chunk)
        committing = max(elapsed - sum(extractions), 0.0)
        mib = self._mib(chunk)
        if mib >= BYTE_RATE_MIN_ITEM_MIB * len(chunk):
            self._mib_seconds = self._tracked(self._mib_seconds, committing / mib)
        else:
            self._item_chunks.append((len(chunk), committing))
            self._price_items()
        if extractions:
            self._extraction_seconds = self._tracked(
                self._extraction_seconds, sum(extractions) / len(extractions)
            )
            self._deferred_artifacts += len(extractions)


    def _price_items(self) -> None:
        """Split the item chunks' seconds into a fixed commit part and a marginal per-item rate.

        Until two sizes are timed, the whole cost is priced per item, at the dearest rate seen.
        """
        sizes = [size for size, _ in self._item_chunks]
        if len(set(sizes)) < 2:
            self._item_seconds = max(seconds / size for size, seconds in self._item_chunks)
            self._commit_seconds = 0.0
            return
        mean_size = sum(sizes) / len(sizes)
        mean_seconds = sum(seconds for _, seconds in self._item_chunks) / len(sizes)
        spread = sum((size - mean_size) ** 2 for size in sizes)
        slope = sum((size - mean_size) * (seconds - mean_seconds) for size, seconds in self._item_chunks) / spread
        self._item_seconds = max(slope, 0.0)
        # The dearest fixed part with that rate, never below zero: no timed chunk, and no smaller one, is priced
        # cheaper than it ran, so the room check still stops a call that has run out of time.
        self._commit_seconds = max(
            0.0, *(seconds - size * self._item_seconds for size, seconds in self._item_chunks)
        )


def _custody_batches(chunk: list[dict]) -> int:
    """The custody batches a chunk may commit: one for its Sources, at most one per draft note."""
    return 1 + sum(_owes_note(planned) for planned in chunk)


def _take_chunk(pending: list[dict], limit: int) -> list[dict]:
    """Up to `limit` items, bounded by bytes, always at least one."""
    chunk: list[dict] = []
    total = 0
    for item in pending[:limit]:
        if chunk and total + item["original_bytes"] > APPLY_CHUNK_BYTES:
            break
        chunk.append(item)
        total += item["original_bytes"]
    return chunk


def _space_shortfall(root: Path, chunk: list[dict], stage: import_stage.Stage | None) -> dict | None:
    """Why the chunk cannot start: its bytes and the import reserve do not fit the vault's free space.

    The reserve is the one setting an import already keeps (`cloud_import.reserve_bytes_from_env`); it also
    stands for the chunk's derived state (index, graph and sidecars), which is small beside the copy itself.
    A member that files by rename on one device writes no bytes, so only the reserve covers it.
    """
    from . import cloud_import

    needed = sum(import_rename.written_bytes(root, stage, planned) for planned in chunk)
    reserve = cloud_import.reserve_bytes_from_env()
    free = shutil.disk_usage(root).free
    if needed + reserve <= free:
        return None
    return {
        "code": _SPACE_STOP, "chunk_bytes": needed, "reserve_bytes": reserve, "free_bytes": free,
        "next": "Stop. The continuation stops at this chunk again until the vault's volume has this chunk's bytes "
                "plus the reserve free. Free space and tell the person, then repeat the call with its continuation: it resumes at this chunk.",
    }


def _request_frame_vectors(root: Path, artifact_path: str, stats: Counter) -> None:
    """Queue the frame vectors of a recording that arrived with its transcript: its extraction is complete, so no
    other request would queue the job (`media_processing.queue_frame_vectors`), and `finish` does not repair it."""
    from . import media_processing

    artifact = root / artifact_path
    try:
        media_processing.queue_frame_vectors(root, artifact, media_processing.classify_media(artifact))
    except Exception:  # noqa: BLE001 - committed custody is authoritative; the worker scan queues the job at its next start
        stats["extraction_failed"] += 1


def _settle_kept_pages(
    root: Path, run: dict, pages: list[tuple[dict, str | None]], *, stats: Counter, reads: _CallReads,
) -> set[str]:
    """Settle each transcript page of the chunk on the stored Source of its recording, by the one replacement rule.

    Every Source of the chunk is stored by now, so a page whose recording this plan stores finds it through that
    recording's outcome (an index that has not seen a Source written a moment ago cannot). `pages` pair each page with
    the recording it found at apply, when it waited for one. Returns the recordings of this plan whose page settled.
    """
    outcomes, date_iso = run["outcomes"], run["plan"]["date"]
    settled: set[str] = set()
    for page, found in pages:
        record = page["kept"]
        recording = found or ((outcomes.get(record["recording"]) or {}).get("artifact_path") if record["recording"] else None)
        result = _keep_speech(root, date_iso, record, recording or page.get("artifact_path"), reads=reads)
        outcomes[page["original_path"]] = result
        outcome = (result.get("kept_speech") or {}).get("outcome", kept_speech.FAILED)
        stats["kept_speech_" + outcome] += 1
        if outcome != kept_speech.FAILED and record["recording"]:
            settled.add(record["recording"])
    return settled


def _keep_speech(root: Path, date_iso: str, record: dict, recording: str | None, *, reads: _CallReads) -> dict:
    """The outcome of settling one transcript page on the stored Source of its recording (see `kept_speech.settle`)."""
    try:
        kept = kept_speech.load(root, record)
        found = kept_speech.find_recording(root, kept.sha256, kept.size, prefer=recording)
        if found is None:
            return _failed(
                kept_speech.NO_RECORDING,
                "no stored Source holds the recording of this transcript page; its recording files first, so retry this page then",
            )
        settled = kept_speech.settle(
            root, kept, recording=found, date_iso=date_iso,
            # The apply already holds the mutation guard, as for the extraction request below.
            commit_guard=contextlib.nullcontext,
        )
    except kept_speech.Changed:
        return _failed("SOURCE_CHANGED", "the transcript page or one of its parts differs from the plan at apply time")
    except kept_speech.LookupUnavailable:
        return _failed(
            kept_speech.LOOKUP_UNAVAILABLE,
            "the duplicate index cannot say which Source holds this recording; retry this page when it can",
        )
    except kept_speech.Refused as refusal:
        return _failed(refusal.code, refusal.reason)
    reads.warnings.extend(kept.warnings)
    outcome = _already_applied(settled.source_page, found, record["content_sha256"])
    if settled.outcome != kept_speech.UNCHANGED:
        outcome["status"] = "applied"
        del outcome["existing_ref"]
    outcome["kept_speech"] = {
        "outcome": settled.outcome,
        **({"prior_page": settled.prior_page} if settled.prior_page else {}),
        **({"speech_job": settled.speech_job} if settled.speech_job != "none" else {}),
    }
    return outcome


#: What an apply does with a page that waits for its recording (`_await_recording`).
_PAIRED = "paired"
_HELD = "held"
_FILED_AS_TEXT = "text"


def _await_recording(root: Path, planned: dict, reads: _CallReads) -> tuple[str, str | None]:
    """What a page that waited for its recording does now, as (result, recording or hold code).

    The recording is stored now: the page pairs with it. It is not, and the index cannot say or another window of
    the series still holds it: the page is held, so a later call pairs it. Nothing is coming: the page files as the
    text Source that it also planned as.
    """
    record = planned["kept"]
    try:
        found = kept_speech.find_recording(root, record["recording_sha256"], record["recording_bytes"])
    except kept_speech.LookupUnavailable:
        return _HELD, kept_speech.LOOKUP_UNAVAILABLE
    if found is not None:
        return _PAIRED, found
    if reads.arrivals(root).expects(planned["original_path"]):
        return _HELD, kept_speech.RECORDING_PENDING
    return _FILED_AS_TEXT, None


def _hold_stop(held: Mapping[str, str]) -> dict:
    """The `apply_stop` of a call that held transcript pages: what waits, and what the caller does."""
    pending = kept_speech.RECORDING_PENDING in held.values()
    count = len(held)
    pages = "transcript page waits" if count == 1 else "transcript pages wait"
    if pending:
        return {
            "code": kept_speech.RECORDING_PENDING,
            "reason": f"{count} {pages} for a recording that another window of this import has yet to file.",
            "next": "Stop. File the other windows of this import first, then repeat the call with its continuation: it pairs "
                    "each held page with its recording once that is stored.",
        }
    return {
        "code": kept_speech.LOOKUP_UNAVAILABLE,
        "reason": f"{count} {pages} while the duplicate index cannot say which Source holds its recording.",
        "next": "Stop. Repeat the call with its continuation later: it pairs each held page once the duplicate index can answer.",
    }


def _request_extraction(root: Path, planned: dict, budget: request_budget.RequestBudget,
                        stats: Counter) -> float | None:
    """Ask for text extraction of a committed media artifact; finish repairs what is missed.

    Returns the seconds the request took, or None when none was made.
    """
    from . import media_processing

    artifact = root / planned["artifact_path"]
    if media_processing.classify_media(artifact) is None:
        return None
    if not budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS):
        stats["extraction_deferred"] += 1
        return None
    started = time.monotonic()
    try:
        # The caller already holds the mutation guard, so the sidecar write needs no second one.
        media_processing.reconcile_media(
            root, artifact, explicit=False, commit_guard=contextlib.nullcontext,
            defer_fanout_to_terminal=True,
        )
    except Exception:  # noqa: BLE001 - committed custody is authoritative; finish retries
        stats["extraction_failed"] += 1
    else:
        stats["extraction_requested"] += 1
    return time.monotonic() - started


@dataclass
class _CallReads:
    """What one `apply` call reads from the vault once and keeps current across its chunks.

    Walking every Source for each chunk made a re-run cost grow with the whole vault,
    and walking every Source for each call made a long run's calls slower as it filed.
    Each starts empty and fills on first use; a batch that raised drops the Source
    reads, because what it left on disk is then unknown. A batch writes no note, so
    the note index stays.

    The Source index is read in full only where the run's own state cannot answer. The plan
    seals the Source pages it saw (`run["sources_seal"]`) and every call that files extends the
    seal by its own pages. While the pages listed now carry that seal, nothing else has been
    filed since the plan: an earlier import of a pending original is then only the Source
    the plan froze for it, or one this run wrote, and both sit at the item's `target_path`.
    """

    index: list[adoption_plan.EarlierImport] | None = None
    #: Source pages under the seal: the listing at the call's start, then the pages the call filed.
    pages: list[str] | None = None
    sealed: bool = False
    source_counts: dict[str, int] | None = None
    notes: adoption_plan.NoteIndex | None = None
    stage: import_stage.Stage | None = None
    stage_read: bool = False
    #: Whether a batch of this call has filed anything; only the first records what earlier calls owe (`_commit_chunk`).
    filed: bool = False
    #: What this call removed from a kept transcript (card numbers, credentials), for the reply.
    warnings: list[str] = dataclass_field(default_factory=list)
    #: The transcript pages this call held for their recording, by path, with why (`_await_recording`).
    held: dict[str, str] = dataclass_field(default_factory=dict)
    _arrivals: _Arrivals | None = None
    _kept_recordings: frozenset[str] | None = None

    def kept_recordings(self, run: dict) -> frozenset[str]:
        """The plan's recordings that a transcript page of the plan settles on, read once for each call."""
        if self._kept_recordings is None:
            self._kept_recordings = frozenset(
                item["kept"]["recording"] for item in run["plan"]["items"]
                if item["action"] == kept_speech.KEEPS and item["kept"]["recording"]
            )
        return self._kept_recordings

    def arrivals(self, root: Path) -> _Arrivals:
        """The recordings that other windows have yet to file, read once for each window of this call."""
        if self._arrivals is None:
            self._arrivals = _Arrivals(root)
        return self._arrivals

    def open_sources(self, root: Path, run: dict) -> None:
        """List the Sources once, and read them in full only when the run's seal no longer holds."""
        if self.pages is not None:
            return
        self.pages = adoption_plan.source_page_paths(root)
        seal = run.get("sources_seal")
        self.sealed = seal is not None and adoption_plan.sources_seal(self.pages) == seal
        if not self.sealed and self.index is None:
            # No seal (a plan made before the seal existed), or a Source was filed or removed by
            # someone else since the plan or the run's last call: only the walk can answer.
            self.index = adoption_plan.read_import_index(root)

    def earlier_imports(self, root: Path, run: dict, planned: dict) -> list[adoption_plan.EarlierImport]:
        """The earlier imports that can decide this pending original: all of them, or its own page."""
        self.open_sources(root, run)
        if self.index is not None:
            return self.index
        return adoption_plan.read_import_entries(root, root / planned["target_path"])

    def whole_index(self, root: Path, run: dict) -> list[adoption_plan.EarlierImport]:
        """Every earlier import, walked now: for a batch that raised, which left unknown pages."""
        self.open_sources(root, run)
        self.index = adoption_plan.read_import_index(root)
        return self.index

    def recorded_stage(self, root: Path, run: dict) -> import_stage.Stage | None:
        """The run's stage manifest, read once per call: a manifest can list a million files."""
        if not self.stage_read:
            self.stage, self.stage_read = adoption_consume.recorded(root, run), True
        return self.stage

    def filing_stage(self, root: Path, run: dict) -> import_stage.Stage | None:
        """The stage a run files from by rename; a run that opted out of consuming keeps every staged copy, so none."""
        return self.recorded_stage(root, run) if run.get("consume", True) else None


def _apply_invocation(run: dict) -> dict | None:
    from .writer_lease import active_mutation_recovery_identity

    identity = active_mutation_recovery_identity()
    return (run.get("apply_invocations") or {}).get(identity[0]) if identity else None


def _record_row(item: adopt_module.ImportItem) -> dict[str, str]:
    """How the indexes and the log record one filed file."""
    return {"original_path": item.original_path, "source_path": item.target_rel, "original_sha256": item.sha256}


def _settle_record_intent(root: Path, run: dict) -> None:
    """Resolve a batch that set out to record owed rows and may have stopped: drop its rows if its entry landed.

    The rows go by their Source paths, never by their place in the list, because a later batch may have added
    rows since. A log that cannot be read cannot say whether the entry landed, and recording the rows again could
    log them twice, so the rows stay owed with their intent and the call stops with the log's own code.
    """
    intent = run.get(_RECORD_INTENT)
    if isinstance(intent, dict):
        try:
            landed = adopt_module.import_chunk_logged(root, intent["token"])
        except adopt_module.AdoptError as error:
            raise AdoptionRunError(error.code, error.reason) from error
        if landed:
            recorded = set(intent["sources"])
            run[_INDEX_OWED] = [row for row in run.get(_INDEX_OWED) or () if row["source_path"] not in recorded]
    run.pop(_RECORD_INTENT, None)


def _sources_of(rows: Iterable[Mapping[str, str]]) -> list[str]:
    """The Source path of each row: what names a row when a batch settles."""
    return [row["source_path"] for row in rows]


def _record_owed(root: Path, store: AdoptionRunStore, run: dict, *, today: dt.date) -> None:
    """Record the rows a page still owes, which no filing batch could carry, in a batch of their own."""
    _settle_record_intent(root, run)
    owed = run.get(_INDEX_OWED) or []
    if owed:
        token = hashlib.sha256(json.dumps([run["run_id"], owed], sort_keys=True).encode("utf-8")).hexdigest()
        run[_RECORD_INTENT] = {"token": token, "sources": _sources_of(owed)}
        store.save_intent(run)
        with call_spans.span("adoption.bookkeeping", {"rows": len(owed)}):
            adopt_module.record_imports(root, owed, today=today, token=token)
    run.pop(_RECORD_INTENT, None)
    run.pop(_INDEX_OWED, None)


def _record_owed_or_report(root: Path, store: AdoptionRunStore, run: dict, *, today: dt.date) -> str | None:
    """`_record_owed` for a caller that must finish its own work: a failure keeps the rows owed and is returned."""
    try:
        _record_owed(root, store, run, today=today)
    except Exception as error:  # noqa: BLE001 - the filing is done: a failed record is reported, never raised, and the rows stay owed
        return (
            f"The Sources index and the log could not record the {len(run.get(_INDEX_OWED) or ())} files that "
            f"this page filed ({getattr(error, 'code', type(error).__name__)}); the files are filed, "
            "and the run keeps the rows it still owes"
        )
    return None


def _freeze_import_chunk(
    store: AdoptionRunStore, run: dict, survivors: list[tuple[dict, adopt_module.ImportItem]],
    *, run_date: dt.date, record: list[str] | None,
) -> tuple[dict | None, bool]:
    """Retain exact post-images before a rename or any destination is published.

    `record` is the Source paths of the owed rows the batch records with its own (see `_commit_chunk`), or None.
    """
    invocation = _apply_invocation(run)
    if invocation is None:
        return None, False
    if invocation.get("publication") is not None:
        return invocation["publication"], True
    items = {}
    for planned, item in survivors:
        items[planned["original_path"]] = {
            "target_path": item.target_rel,
            "source_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            "source_id": parse_frontmatter(item.content)[0].get("exomem_id") if item.artifact else None,
            "artifact_path": planned.get("artifact_path"), "original_sha256": item.sha256,
        }
    sequence = invocation["chunk_sequence"] = invocation.get("chunk_sequence", 0) + 1
    publication = {
        "date": run_date.isoformat(), "items": items,
        "operation_token": hashlib.sha256(json.dumps(
            [run["run_id"], invocation["identity_digest"], invocation["command_digest"], sequence, items], sort_keys=True,
        ).encode("utf-8")).hexdigest(),
    }
    publication["record"] = record
    if record:
        run[_RECORD_INTENT] = {"token": publication["operation_token"], "sources": record}
    invocation["publication"] = publication
    with call_spans.span("adoption.save_intent"):
        store.save_intent(run)
    return publication, False


def _commit_chunk(
    root: Path, store: AdoptionRunStore, run: dict,
    survivors: list[tuple[dict, adopt_module.ImportItem]], *,
    run_date: dt.date, label: str | None, reads: _CallReads,
    placed: Mapping[str, import_rename.Placed],
    publication: dict | None = None, reconciling: bool = False,
    record: list[str] | None = None, token: str | None = None,
) -> list[dict]:
    """Commit one chunk's pages, artifacts and the run's changes in one batch.

    Returns the items now on disk. A batch that raises may still have committed, so
    each item is resolved by the same identity check the next call would use; the
    rest fail as retryable, and the call's closing save records them. `placed` holds the items
    whose binary is already at its Knowledge Base path. When the batch fails, a binary that this
    call renamed goes back to its stage; one that an earlier call renamed stays where it is.

    The Sources index, the top index and the log are rewritten whole for any entry, so a batch
    records its files there only when `record` says so: then the owed rows that `record` names
    by Source path and this batch's files, in one log entry named `token` (the frozen chunk's
    own token when it has one). Otherwise the batch adds its files to the rows the run owes.
    """
    outcomes = run["outcomes"]
    for planned, _item in survivors:
        outcomes[planned["original_path"]] = _applied(planned)
        _hold_for_note(outcomes[planned["original_path"]], planned)
    owed = list(run.get(_INDEX_OWED) or [])
    own = [_record_row(item) for _planned, item in survivors]
    recorded: object = record
    if publication is not None:
        token = publication["operation_token"]
        recorded = publication.get("record", _ABSENT)
    rows: list[dict] | None
    if recorded is _ABSENT:
        # A chunk frozen before filings deferred their bookkeeping records its own files, as it would have then.
        rows = None
    elif recorded is None:
        rows = []
        run[_INDEX_OWED] = owed + own
    else:
        named = set(recorded)
        rows = [row for row in owed if row["source_path"] in named] + own
        run[_INDEX_OWED] = [row for row in owed if row["source_path"] not in named]
        run.pop(_RECORD_INTENT, None)
    try:
        invocation = _apply_invocation(run)
        if invocation is not None:
            invocation.pop("publication", None)
        with call_spans.span("adoption.run_changes"):
            changes = store.planned_changes(run)
        committed = adopt_module.commit_import_items(
            root, [item for _planned, item in survivors], today=run_date,
            extra_writes=[changes], source_counts=reads.source_counts,
            reconcile=reconciling, operation_token=token, record=rows,
        )
    except Exception as error:  # noqa: BLE001 - a rolled-back batch is a retryable outcome
        if reconciling:
            raise AdoptionRunError(
                "IMPORT_RECOVERY_UNRESOLVED",
                "the interrupted chunk cannot establish exact custody and canonical bookkeeping; "
                "preserve the run, staging and original mutation identity",
            ) from error
        reads.source_counts = None
        # The pages the batch left are unknown, so only the walk can say which items landed.
        index = reads.whole_index(root, run)
        landed: list[dict] = []
        unlanded: list[tuple[dict, adopt_module.ImportItem]] = []
        for planned, item in survivors:
            earlier = _earlier_with_same_bytes(root, index, label, planned)
            if earlier is not None:
                outcomes[planned["original_path"]] = _already_applied(
                    earlier.path, adoption_plan.artifact_of(root, earlier.path),
                    planned["original_sha256"],
                )
                _hold_for_note(outcomes[planned["original_path"]], planned)
                landed.append(planned)
            else:
                unlanded.append((planned, item))
        # Only the files this batch wrote are owed; whether its log entry landed is checked by the next record.
        filed = {planned["original_path"] for planned in landed}
        owed += [
            row for row in own
            if row["original_path"] in filed and outcomes[row["original_path"]]["target_path"] == row["source_path"]
        ]
        run[_INDEX_OWED] = owed
        if rows:
            run[_RECORD_INTENT] = {"token": token, "sources": _sources_of(rows)}
        if getattr(error, "code", None) == "PATH_GUARD_CONTENT":
            # The batch hashes each artifact as it copies it and names none when one differs from the
            # plan. Only now are the artifacts re-hashed, to fail the one that drifted and commit the rest.
            drifted = [
                planned for planned, _item in unlanded
                if planned["form"] == "artifact" and planned["original_path"] not in placed
                and not _original_unchanged(root, planned)
            ]
            if drifted and len(drifted) < len(unlanded):
                for planned in drifted:
                    outcomes[planned["original_path"]] = _failed(
                        "SOURCE_CHANGED", "the original differs from the plan at apply time"
                    )
                rest = [(planned, item) for planned, item in unlanded if planned not in drifted]
                return landed + _commit_chunk(
                    root, store, run, rest, run_date=run_date, label=label, reads=reads, placed=placed
                )
        if isinstance(error, adopt_module.AdoptError):
            failure = {"code": error.code, "reason": error.reason}
        elif isinstance(
            error, (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)
        ) or getattr(error, "code", None) == "PATH_GUARD_CONTENT":
            failure = adoption_plan.source_failure(error)
        else:
            failure = {"code": "BATCH_ROLLED_BACK", "reason": str(error)}
        for planned, _item in unlanded:
            path = planned["original_path"]
            outcomes[path] = _failed(**failure)
            moved = placed.get(path)
            if moved is not None and not moved.replayed:
                stuck = import_rename.put_back(root, moved)
                if stuck is not None:
                    outcomes[path] = _failed(stuck.code, stuck.reason)
        return landed
    reads.source_counts = committed["source_counts"]
    return [planned for planned, _item in survivors]


def _apply_chunk(
    root: Path, store: AdoptionRunStore, run: dict, chunk: list[dict], *,
    run_date: dt.date, ceiling: int, budget: request_budget.RequestBudget, stats: Counter,
    written: list[str], reads: _CallReads, pacer: _ApplyPacer, page_final: bool,
) -> list[float]:
    """Apply one chunk; returns the seconds each media extraction request took.

    `page_final` marks the chunk that holds every item still pending in the call's scope.
    """
    plan = run["plan"]
    outcomes = run["outcomes"]
    label = (run.get("collection") or {}).get("label")
    settled: list[dict] = []
    # Items that repeat another item of the plan wait for the Source of the item they repeat.
    deferred: list[tuple[dict, str | None]] = []
    # A transcript page settles on its recording's Source, so it waits for the chunk's own Sources. A page that
    # waited for its recording is paired with the recording it found.
    deferred_kept: list[tuple[dict, str | None]] = []
    # The waiting pages that this chunk files as text because no recording is coming; counted once the text is filed.
    as_text: set[str] = set()
    destinations: adoption_plan.Destinations | None = None
    survivors: list[tuple[dict, adopt_module.ImportItem]] = []
    texts: dict[str, str] = {}
    stage = reads.filing_stage(root, run)
    placed: dict[str, import_rename.Placed] = {}
    invocation = _apply_invocation(run)
    publication = invocation.get("publication") if invocation else None
    prework_started = time.monotonic()
    with lexstore.shared_reads(root):
        for planned in chunk:
            path = planned["original_path"]
            if planned["action"] == kept_speech.KEEPS:
                deferred_kept.append((planned, None))
                continue
            frozen = (publication.get("items") or {}).get(path) if publication else None
            if planned.get("kept") and not frozen:  # a frozen item already filed as the text Source: its route is settled
                verdict, found = _await_recording(root, planned, reads)
                if verdict == _PAIRED:
                    deferred_kept.append((planned, found))
                    continue
                if verdict == _HELD:
                    reads.held[path] = found
                    continue
                as_text.add(path)  # nothing is coming: the page files as the text Source it also planned as
            if frozen and planned["action"] != "copy-as-source":
                # Only an item that stored its own bytes in place of a named Source is ever frozen under another action.
                planned = _own_copy(planned, frozen["target_path"], frozen["artifact_path"])
            by_rename = import_rename.renamable(stage, planned)
            text, failure = _read_original(root, planned, ceiling, by_rename=by_rename)
            moved = None
            if failure is not None and failure["code"] == "NOT_FOUND":
                # Not a failure when adoption already filed it and removed the staged copy.
                earlier = None if frozen else _filed_and_gone(root, run, reads, planned)
                if earlier is not None:
                    _release_hold(root, planned)
                    outcomes[path] = _already_applied(
                        earlier.path, adoption_plan.artifact_of(root, earlier.path), planned["original_sha256"]
                    )
                    if run.get("consume", True):
                        outcomes[path]["consumed"] = True
                        stats["consumed"] += 1
                        _queue_offload(root, planned)
                    else:  # a run that opted out claims no removal, and counts its missing copy as changed
                        _record_unchanged(root, planned, outcomes[path], stats)
                    continue
                if by_rename:
                    # An earlier call may have renamed the file and stopped before its Source page committed.
                    replay = import_rename.settle(root, stage, planned, ceiling=ceiling)
                    if isinstance(replay, import_rename.Refused):
                        if frozen:
                            raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "the interrupted artifact cannot establish its rename custody")
                        # The hold stays: the file is unbound with other bytes, and only the person's move
                        # lifts it. The scans leave it alone and a discard of its stage refuses until then.
                        outcomes[path] = _failed(replay.code, replay.reason)
                        continue
                    if replay is not None:
                        moved, failure = replay, None
            if failure is not None:
                if frozen:
                    raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "the interrupted chunk's original bytes are unavailable or changed")
                if by_rename and rename_journal.is_held(root, root / planned["artifact_path"]):
                    # An earlier call renamed this member's file and could not put it back: the retry finds the
                    # staged path changed, and the outcome must still say where the file is.
                    failure = _failed(
                        import_rename.UNBOUND,
                        f"{failure['reason']}; the file stays unbound at {planned['artifact_path']}",
                    )
                outcomes[path] = failure
                continue
            if text is not None and _owes_note(planned):
                texts[path] = text
            if path in outcomes and outcomes[path]["status"] == _SOURCE_ONLY:
                continue  # an earlier call wrote its Source; only the note is owed
            if frozen:
                item = adopt_module.import_item(
                    root, planned, text=text, date_iso=plan["date"], collection=label, source_id=frozen.get("source_id"),
                )
                if (
                    frozen.get("target_path") != item.target_rel
                    or frozen.get("artifact_path") != planned.get("artifact_path")
                    or frozen.get("original_sha256") != item.sha256
                    or frozen.get("source_sha256") != hashlib.sha256(item.content.encode("utf-8")).hexdigest()
                ):
                    raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "the chunk no longer matches its frozen post-images")
                if moved is not None:
                    item.artifact.placed = True
                    placed[path] = moved
                survivors.append((planned, item))
                continue
            index = reads.earlier_imports(root, run, planned)
            if planned["action"] == "already-imported" and not planned.get("mapping"):
                frozen = [entry for entry in index if entry.path == planned["target_path"]]
                if _earlier_with_same_bytes(root, frozen, label, planned) is None:
                    outcomes[path] = _failed(
                        "PLAN_STALE", "the earlier Source or artifact is missing or changed; re-plan this import"
                    )
                    continue
                outcomes[path] = _already_applied(
                    planned["target_path"], planned.get("artifact_path"), planned["original_sha256"]
                )
                _hold_for_note(outcomes[path], planned)
                settled.append(planned)
                continue
            earlier = _earlier_with_same_bytes(root, index, label, planned)
            if earlier is not None:
                outcomes[path] = _already_applied(
                    earlier.path, adoption_plan.artifact_of(root, earlier.path),
                    planned["original_sha256"],
                )
                _hold_for_note(outcomes[path], planned)
                settled.append(planned)
                continue
            if planned["action"] == "duplicate-of":
                deferred.append((planned, text))
                continue
            # An item that was renamed into the vault already sits at its frozen artifact path: its Source completes the filing.
            stored = None if moved is not None else _settle_on_stored(
                root, planned, label=label, stats=stats,
                holder=planned["target_path"] if planned["action"] == "already-imported" else None,
            )
            if stored is not None:
                _record_stored(root, run, reads, planned, stored, settled=settled)
                continue
            if planned["action"] == "already-imported":
                if moved is not None:  # its staged file already sits at the artifact path of the Source that the plan named
                    outcomes[path] = _failed(
                        "PLAN_STALE", "the Source that holds these bytes is missing or changed; re-plan this import"
                    )
                    continue
                # The Source that the plan named for these bytes has gone or changed: this item stores its own.
                destinations = destinations or adoption_plan.Destinations.frozen_by(root, plan["items"])
                planned, failure = _store_own_copy(root, planned, destinations, date_iso=plan["date"])
                if failure is not None:
                    outcomes[path] = failure
                    continue
            occupied = [
                target for target in (planned["target_path"], planned.get("artifact_path"))
                if target and os.path.lexists(root / target)
                and not (moved is not None and target == planned["artifact_path"])
            ]
            if occupied:
                outcomes[path] = _failed(
                    "TARGET_OCCUPIED",
                    f"{occupied[0]} already exists and is not an import of this file",
                )
                continue
            item = adopt_module.import_item(root, planned, text=text, date_iso=plan["date"], collection=label)
            if moved is not None:
                item.artifact.placed = True
                placed[path] = moved
            survivors.append((planned, item))
    call_spans.record_span(
        "adoption.prework", (time.monotonic() - prework_started) * 1000.0, {"items": len(chunk)}
    )
    landed = _file_survivors(
        root, store, run, survivors, stage=stage, placed=placed, run_date=run_date, ceiling=ceiling, reads=reads,
        page_final=page_final,
    )
    own_copies: list[tuple[dict, adopt_module.ImportItem]] = []
    for planned, text in deferred:
        repeated = outcomes.get(planned["duplicate_of"]) or {}
        holder = repeated["target_path"] if repeated.get("status") in (*_APPLIED_STATUSES, _SOURCE_ONLY) else None
        stored = _settle_on_stored(root, planned, label=label, stats=stats, holder=holder)
        if stored is not None:
            _record_stored(root, run, reads, planned, stored, settled=settled)
            continue
        # The Source that the plan named for these bytes did not get written or no longer holds them.
        destinations = destinations or adoption_plan.Destinations.frozen_by(root, plan["items"])
        own, failure = _store_own_copy(root, planned, destinations, date_iso=plan["date"])
        if failure is not None:
            outcomes[planned["original_path"]] = failure
            continue
        own_copies.append((own, adopt_module.import_item(root, own, text=text, date_iso=plan["date"], collection=label)))
    landed += _file_survivors(
        root, store, run, own_copies, stage=stage, placed=placed, run_date=run_date, ceiling=ceiling, reads=reads,
        page_final=page_final,
    )
    landed_paths = {planned["original_path"] for planned in landed}
    stats["kept_unpaired"] += sum(
        1 for path in as_text if (outcomes.get(path) or {}).get("status") in _APPLIED_STATUSES
    )
    with_page = _settle_kept_pages(root, run, deferred_kept, stats=stats, reads=reads)  # recordings whose page settled now
    with call_spans.span("adoption.settle", {"items": len(settled) + len(landed)}):
        for planned in [*settled, *landed]:
            outcome = outcomes[planned["original_path"]]
            # A fresh write was hashed by the batch as it was written; a found copy is read back.
            _settle_staged(
                root, run, reads, planned, outcome, stats,
                durable_verified=planned["original_path"] in landed_paths and _source_status(outcome) == "applied",
            )
    extractions: list[float] = []
    copied: list[str] = []
    for planned in landed:
        if _source_status(outcomes[planned["original_path"]]) == "applied":
            copied.append(planned["target_path"])
        if planned["form"] == "artifact":
            # A recording that a transcript page settles on is asked for no speech job: the settle step above gave it
            # the transcript, and `finish` asks for the extraction of any such recording whose page did not settle.
            if planned["original_path"] not in reads.kept_recordings(run):
                seconds = _request_extraction(root, planned, budget, stats)
                if seconds is not None:
                    extractions.append(seconds)
    for recording in sorted(with_page):
        # Whichever chunk stored the recording: the chunk cap splits a video from its page, which plans after it.
        if artifact_path := (outcomes.get(recording) or {}).get("artifact_path"):
            _request_frame_vectors(root, artifact_path, stats)
    written.extend(copied)
    # The Sources are written without the semantic seam, so the chunk counts them in the
    # backlog itself, before any note does. Each draft note's own commit refreshes its Source.
    due_state.refresh_after_writes(root, [due_state.PageWrite(path) for path in copied])
    if reads.notes is None:
        reads.notes = adoption_plan.NoteIndex(root)
    _create_notes(
        root, store, run, chunk, texts,
        label=label, notes=reads.notes, pacer=pacer, stats=stats, written=written,
    )
    return extractions


def _file_survivors(
    root: Path, store: AdoptionRunStore, run: dict, survivors: list[tuple[dict, adopt_module.ImportItem]], *,
    stage: import_stage.Stage | None, placed: dict[str, import_rename.Placed], run_date: dt.date, ceiling: int,
    reads: _CallReads, page_final: bool,
) -> list[dict]:
    """File `survivors` in one batch: freeze their post-images, rename the staged binaries, then commit.

    Returns the items now on disk. Their Sources are then findable by the duplicate lookups of every later batch.
    `page_final` marks a batch of the chunk that holds every item still pending in the call's scope.
    """
    if not survivors:
        return []
    outcomes = run["outcomes"]
    label = (run.get("collection") or {}).get("label")
    record: list[str] | None = None
    token = None
    held = _apply_invocation(run)
    first, reads.filed = not reads.filed, True
    if (page_final or first and run.get(_INDEX_OWED)) and not (held and held.get("publication")):
        # The call's first batch records what earlier calls filed, and a page's last batch records the rest.
        _settle_record_intent(root, run)
        owed = run.get(_INDEX_OWED) or []
        record = _sources_of(owed)
        if held is None:
            token = hashlib.sha256(json.dumps(
                [run["run_id"], owed, [_record_row(item) for _p, item in survivors]], sort_keys=True,
            ).encode("utf-8")).hexdigest()
            if record:
                # Only rows that earlier calls saved need the intent; this batch's own rows are lost with it.
                run[_RECORD_INTENT] = {"token": token, "sources": record}
                with call_spans.span("adoption.save_intent"):
                    store.save_intent(run)
    publication, reconciling = _freeze_import_chunk(store, run, survivors, run_date=run_date, record=record)
    if stage is not None:
        with call_spans.span("adoption.staging"):
            survivors = _file_by_rename(
                root, stage, survivors, run_id=run["run_id"], ceiling=ceiling, outcomes=outcomes, placed=placed
            )
    if not survivors:
        return []
    landed = _commit_chunk(
        root, store, run, survivors,
        run_date=dt.date.fromisoformat(publication["date"]) if publication else run_date,
        label=label, reads=reads, placed=placed, publication=publication, reconciling=reconciling,
        record=record, token=token,
    )
    for planned in landed:
        _release_hold(root, planned)
    for planned in landed:
        if _source_status(outcomes[planned["original_path"]]) == "applied":
            reads.pages.append(planned["target_path"])
            if reads.index is not None:
                reads.index.append(
                    adoption_plan.earlier_import_of(planned, collection=label, captured=run["plan"]["date"])
                )
    lexstore.record_payload_commit(
        root, [
            planned["target_path"] for planned in landed
            if _source_status(outcomes[planned["original_path"]]) == "applied"
        ],
    )
    return landed


def _file_by_rename(
    root: Path, stage: import_stage.Stage, survivors: list[tuple[dict, adopt_module.ImportItem]], *,
    run_id: str, ceiling: int, outcomes: dict, placed: dict[str, import_rename.Placed],
) -> list[tuple[dict, adopt_module.ImportItem]]:
    """Rename each renamable survivor's staged file into its Knowledge Base path, before the batch.

    A member that the primitive refuses fails here and leaves the batch; a member it cannot rename
    stays a survivor and files by copy, which the adoption ceiling bounds. Each renamed member is added to `placed`.
    """
    candidates = {
        planned["original_path"] for planned, _item in survivors
        if planned["original_path"] not in placed and import_rename.renamable(stage, planned)
    }
    members = [planned for planned, _item in survivors if planned["original_path"] in candidates]
    verdicts = import_rename.place_all(root, stage, members, ceiling=ceiling, run_id=run_id) if members else {}
    kept = []
    for planned, item in survivors:
        path = planned["original_path"]
        verdict = verdicts.get(path)
        if isinstance(verdict, import_rename.Refused):
            outcomes[path] = _failed(verdict.code, verdict.reason)
            continue
        if isinstance(verdict, import_rename.Placed):
            item.artifact.placed = True
            placed[path] = verdict
        elif path in candidates and planned["original_bytes"] > ceiling:
            # `_read_original` let this member past the ceiling because a rename needs no copy; the copy has no such excuse.
            outcomes[path] = _failed(
                adoption_plan.OVER_CEILING,
                f"the original exceeds the {ceiling:,} byte adoption ceiling and could not be renamed into the vault",
            )
            continue
        kept.append((planned, item))
    return kept


def _queue_offload(root: Path, planned: dict) -> None:
    """Queue the offload job of a filed artifact; the store's classification rule decides whether it is offloadable."""
    from . import media_store

    if not planned.get("artifact_path"):
        return
    try:
        media_store.enqueue_offload(root, root / planned["artifact_path"])
    except Exception:  # noqa: BLE001 - the Source is committed, and the periodic offload scan queues a missed job
        pass


def _create_notes(
    root: Path, store: AdoptionRunStore, run: dict, chunk: list[dict], texts: dict[str, str], *,
    label: str | None, notes: adoption_plan.NoteIndex, pacer: _ApplyPacer,
    stats: Counter, written: list[str],
) -> None:
    """Create the draft note of every own-note item in `chunk` whose Source is on disk.

    The identity check runs first: an item whose note exists settles as found, and one
    whose note cannot be placed fails. The other items commit in batches through the
    governed two-phase creation (`adopt.create_draft_notes`). A call's first batch holds as
    many notes as fit in half the time left at the price that the run's last call to make a
    note recorded, when that call was recent; otherwise it holds one note, to time it. While
    the call has made no note, it makes one note at a time; after that a batch holds as many
    notes as fit at the dearest cost per made note this call has seen, which the call
    records for the next one.
    A refused note does not lower that cost, because a quick refusal would make the next
    batch look free. A batch that refuses makes its notes alone only while one more note fits
    at the price that sized it. The items a batch leaves or does not reach keep their
    `source-applied` outcome and the continuation resumes at them. A call always tries at
    least one note, so a budget below one note's cost still moves.
    """
    outcomes = run["outcomes"]
    owed = [
        planned for planned in chunk
        if planned["original_path"] in outcomes
        and outcomes[planned["original_path"]]["status"] == _SOURCE_ONLY
    ]
    due = [
        planned for planned in owed
        if _note_due(root, planned, outcomes[planned["original_path"]], label=label, notes=notes, stats=stats)
    ]
    while due:
        first_try = not stats["notes_started"]
        if first_try:
            # Pricing each note at the carried price over the share fills only that share of the room.
            price_ms = _carried_note_price_ms(run) / _CARRIED_PRICE_ROOM_SHARE
            size = max(1, _notes_that_fit(pacer, chunk, price_ms, len(due))) if price_ms else 1
        elif not stats["notes_created"]:
            # No note made yet, so nothing prices a batch: one note, paced on the dearest try.
            price_ms = stats["note_try_ms_max"]
            size = 1 if pacer.note_fits(chunk, price_ms / 1000) else 0
        else:
            price_ms = stats["note_ms_max"]
            size = _notes_that_fit(pacer, chunk, price_ms, len(due))
        if not size:
            stats["notes_deferred"] += 1
            break
        batch, due = due[:size], due[size:]
        # A refused batch makes its notes alone; each must still fit at the price that sized it.
        fits_one_note = _fallback_pace(pacer, chunk, price_ms, first_note_free=first_try)
        started = time.monotonic()
        results = adopt_module.create_draft_notes(
            root,
            [
                adopt_module.DraftNote(
                    planned, texts[planned["original_path"]],
                    outcomes[planned["original_path"]]["target_path"],
                )
                for planned in batch
            ],
            collection=label, date_iso=run["plan"]["date"], fits_one_note=fits_one_note,
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        reached = sum(result.reached for result in results)
        stats["notes_started"] += size
        if reached:
            stats["note_try_ms_max"] = max(stats["note_try_ms_max"], int(elapsed_ms / reached))
        made = sum(result.created is not None for result in results)
        if made:
            # The dearest cost per made note: a batch's time, its refusals included, over the notes it made.
            stats["note_ms_max"] = max(stats["note_ms_max"], int(elapsed_ms / made))
            run[_NOTE_PRICE] = {"ms": stats["note_ms_max"], "at": dt.datetime.now(dt.UTC).timestamp()}
        for planned, result in zip(batch, results, strict=True):
            if result.reached:
                _settle_draft_note(
                    root, planned, result, outcomes[planned["original_path"]],
                    label=label, notes=notes, stats=stats, written=written,
                )
        if reached < size:
            # The fallback ran out of time: the notes it did not reach stay `source-applied`.
            stats["notes_deferred"] += 1
            break
    if owed:
        # The outcomes are committed with the chunk's Sources and again here, so a
        # crash costs the identity check at most one chunk of notes.
        with call_spans.span("adoption.run_changes"):
            store.save_changes(run)


def _carried_note_price_ms(run: dict) -> float:
    """The price per made note that the run's last call recorded, or 0 when it is stale."""
    carried = run.get(_NOTE_PRICE) or {}
    age = dt.datetime.now(dt.UTC).timestamp() - carried.get("at", 0.0)
    return carried.get("ms", 0) if 0 <= age <= _NOTE_PRICE_FRESH_SECONDS else 0


def _notes_that_fit(pacer: _ApplyPacer, chunk: list[dict], price_ms: float, most: int) -> int:
    """How many notes, up to `most`, fit in the time the call has left at `price_ms` each."""
    size = most
    while size and not pacer.note_fits(chunk, price_ms * size / 1000):
        size -= 1
    return size


def _fallback_pace(
    pacer: _ApplyPacer, chunk: list[dict], price_ms: int, *, first_note_free: bool
) -> Callable[[], bool]:
    """Whether the per-note fallback may make one more note at `price_ms`.

    On a call's first batch the first note always runs, so a budget below one note's cost
    still moves.
    """
    asked = 0

    def fits_one_note() -> bool:
        nonlocal asked
        asked += 1
        return (first_note_free and asked == 1) or pacer.note_fits(chunk, price_ms / 1000)

    return fits_one_note


def _found_note(
    root: Path, planned: dict, *, label: str | None, notes: adoption_plan.NoteIndex
) -> str | None:
    """The note an earlier run, a retry or a crashed call made for this item, or None."""
    return adoption_plan.find_note(
        root, notes, collection=label, collection_path=planned["collection_path"],
        frozen=(planned.get("note") or {}).get("target_path"),
    )


def _note_due(
    root: Path, planned: dict, outcome: dict, *,
    label: str | None, notes: adoption_plan.NoteIndex, stats: Counter,
) -> bool:
    """Settle an item whose note exists or cannot be placed; True when its note is still owed."""
    frozen = (planned.get("note") or {}).get("target_path")
    existing = _found_note(root, planned, label=label, notes=notes)
    if existing is not None:
        _settle_note(outcome, {"status": "already-applied", "target_path": existing, "at": _now_iso()})
        stats["notes_found"] += 1
        return False
    if frozen is None:
        _fail_note(outcome, "PLAN_STALE", "the note an earlier run made is gone; re-plan this import")
        stats["notes_failed"] += 1
        return False
    if os.path.lexists(root / frozen):
        _fail_note(
            outcome, "TARGET_OCCUPIED", f"{frozen} already exists and is not an import of this file"
        )
        stats["notes_failed"] += 1
        return False
    return True


def _settle_draft_note(
    root: Path, planned: dict, result: adopt_module.DraftNoteOutcome, outcome: dict, *,
    label: str | None, notes: adoption_plan.NoteIndex, stats: Counter, written: list[str],
) -> None:
    """Settle one item on what `adopt.create_draft_notes` reported for its note."""
    from . import note as note_module

    error = result.error
    if error is not None:
        # A failure after the commit still left the note: the identity check records it.
        existing = _found_note(root, planned, label=label, notes=notes)
        if existing is not None:
            _settle_note(outcome, {"status": "already-applied", "target_path": existing, "at": _now_iso()})
            stats["notes_found"] += 1
            return
        if isinstance(error, (adopt_module.AdoptError, note_module.NoteError)):
            failure = {"code": error.code, "reason": error.reason}
        elif isinstance(error, (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)):
            failure = adoption_plan.source_failure(error)
        else:
            failure = {"code": "NOTE_FAILED", "reason": str(error)}
        _fail_note(outcome, failure["code"], failure["reason"])
        stats["notes_failed"] += 1
        return
    created = result.created
    _settle_note(
        outcome,
        {"status": "applied", "target_path": created.path, "note_ref": created.ref, "at": _now_iso()},
    )
    written.append(created.path)
    stats["notes_created"] += 1


def _settle_note(outcome: dict, note: dict) -> None:
    """Complete an own-note item: its Source and its note both exist."""
    outcome["note"] = note
    outcome["status"] = (
        "applied" if "applied" in (outcome.get("source_status"), note["status"]) else "already-applied"
    )


def _fail_note(outcome: dict, code: str, reason: str) -> None:
    """Fail an own-note item at its note; the outcome keeps the Source it made."""
    outcome["note"] = {"status": "failed", "code": code, "reason": reason, "at": _now_iso()}
    outcome.update(_failed(code, reason))


def _record_unchanged(root: Path, planned: dict, outcome: dict, stats: Counter) -> None:
    """Re-hash one applied original; keep the answer on its outcome and in this call's counts."""
    unchanged = _original_unchanged(root, planned)
    outcome["original_unchanged"] = unchanged
    stats["verified_total"] += 1
    stats["verified_unchanged"] += int(unchanged)


def _release_hold(root: Path, planned: dict) -> None:
    """Drop the journal record of an item that is settled: its Source page binds the file, or the run gave it up."""
    if planned.get("artifact_path"):
        rename_journal.release(root, planned["artifact_path"])


def _text_source_holds(root: Path, page_path: str, planned: dict) -> bool:
    """Whether the Source page holds the planned text between its fences, byte for byte.

    The fence body is checked and not the page, which a note's back-reference changes in its front matter.
    A page that is missing, unreadable or not in our format does not hold it.
    """
    try:
        with reserved_paths.open_generic_binary(root, page_path, physical=True) as source:
            data = source.stream.read()
        body = adopt_module.text_source_body(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return False
    if body is None:
        return False
    original = body.encode("utf-8")
    return len(original) == planned["original_bytes"] and hashlib.sha256(original).hexdigest() == planned["original_sha256"]


def _filed_and_gone(
    root: Path, run: dict, reads: _CallReads, planned: dict
) -> adoption_plan.EarlierImport | None:
    """The committed Source of an item whose staged copy a recorded stage lists, or None.

    The Source is the record that adoption filed the item: a copy that is gone from the stage was
    removed by this run, by another run, or by a call that stopped before it saved. The caller has
    already seen that the copy is missing. A text item counts as filed only when the run recorded
    it as filed and its Source page still holds its text between the fences: an item that still
    owes its note is not done, and a text that no longer matches is unfiled.
    """
    stage = reads.recorded_stage(root, run)
    if stage is None or adoption_consume.entry_of(stage, planned["original_path"]) is None:
        return None
    earlier = _earlier_with_same_bytes(
        root, reads.earlier_imports(root, run, planned), (run.get("collection") or {}).get("label"), planned
    )
    if earlier is not None and planned["form"] == "text" and not (
        ((run.get("outcomes") or {}).get(planned["original_path"]) or {}).get("status") in _APPLIED_STATUSES
        and _text_source_holds(root, earlier.path, planned)
    ):
        return None
    return earlier


def _settle_staged(
    root: Path, run: dict, reads: _CallReads, planned: dict, outcome: dict, stats: Counter, *,
    durable_verified: bool = False,
) -> None:
    """Settle one filed item's staged copy: it is gone, removed now, or re-hashed in place.

    A removal needs an artifact item, a recorded stage that lists the file, and a run that did not opt out.
    The copy is hashed against the manifest and the plan as it goes, so a removed copy is already checked
    and only a copy that stays is re-hashed. A removal that fails leaves the item applied and not consumed.
    """
    if outcome.get("consumed"):
        return
    _release_hold(root, planned)  # a call that stopped after the commit left the record
    stage = reads.recorded_stage(root, run)
    if not os.path.lexists(root / planned["original_path"]):
        # A run that opted out claims no removal; its missing copy is recorded as changed below.
        consumed = run.get("consume", True) and _filed_and_gone(root, run, reads, planned) is not None
        if consumed:
            _queue_offload(root, planned)
    else:
        consumed = (
            stage is not None and run.get("consume", True) and planned["form"] == "artifact"
            and (durable_verified or adoption_consume.durable_matches(root, outcome.get("artifact_path"), planned))
            and adoption_consume.remove_staged(root, stage, planned)
        )
    if consumed:
        outcome["consumed"] = True
        outcome.pop("original_unchanged", None)
        stats["consumed"] += 1
        return
    _record_unchanged(root, planned, outcome, stats)


def _progress(run: dict) -> dict:
    plan_items = (run.get("plan") or {}).get("items") or []
    scope = run.get("scope")
    wanted = None if scope is None else set(scope)
    outcomes = run.get("outcomes") or {}
    in_scope = [it for it in plan_items if wanted is None or it["original_path"] in wanted]
    statuses = [outcomes.get(it["original_path"], {}).get("status") for it in in_scope]
    return {
        "applied": sum(1 for s in statuses if s in _APPLIED_STATUSES),
        "failed": sum(1 for s in statuses if s == "failed"),
        "remaining": sum(1 for s in statuses if s is None or s == _SOURCE_ONLY),
        "total": len(in_scope),
    }


def _verify_counts(outcomes: dict) -> tuple[int, int]:
    """Originals re-hashed after their import, and how many were byte-identical."""
    checked = [
        o for o in outcomes.values()
        if o.get("status") in _APPLIED_STATUSES and "original_unchanged" in o
    ]
    return sum(1 for o in checked if o["original_unchanged"]), len(checked)


def _consumed_count(outcomes: dict) -> int:
    """Items filed whose staged copy is gone because adoption filed it."""
    return sum(1 for o in outcomes.values() if o.get("status") in _APPLIED_STATUSES and o.get("consumed"))


def _legacy_apply_invocation(
    root: Path, run: dict, identity: tuple[str, str, bool], *,
    continuation: str | None, retry_failed: bool, only_paths: list[str] | None,
) -> dict:
    """Reconstruct immutable argument scope or the scope held by a current legacy cursor."""
    plan_items = run["plan"]["items"]
    planned_paths = {item["original_path"] for item in plan_items}
    invocation = {
        "identity_digest": identity[0], "command_digest": identity[1],
        "plan_id": run["plan"]["plan_id"], "continuation": continuation,
    }
    if continuation:
        entry = _cursor_entry(run, "apply", continuation)
        saved = entry.get("reply")
        if entry["state"] == "used" and isinstance(saved, dict):
            progress = saved.get("progress")
            # nosemgrep: ep-word-set -- The apply-reply schema fixes these progress counter fields.
            counters = ("applied", "failed", "remaining", "total")
            if (
                isinstance(saved.get("apply_result"), dict) and isinstance(progress, dict)
                and all(type(progress.get(key)) is int and progress[key] >= 0 for key in counters)
                and progress["total"] == sum(progress[key] for key in counters[:3])
            ):
                invocation["reply"] = copy.deepcopy(saved)
                return invocation
        scope = run.get("scope")
        if not (
            run["phase"] == "applying" and run.get("inflight") is True
            and _open_cursor(run, "apply") == continuation and entry["state"] == "open"
            and "scope" in run
            and (scope is None or isinstance(scope, list)
                 and all(isinstance(path, str) and path in planned_paths for path in scope))
        ):
            raise AdoptionRunError(
                "IMPORT_RECOVERY_UNRESOLVED",
                "the legacy continuation no longer proves its original scope; preserve the run and mutation identity",
            )
        # Every fresh apply supersedes this cursor before changing scope; an open current cursor retains it.
        scope = copy.deepcopy(scope)
    elif retry_failed:
        raise AdoptionRunError(
            "IMPORT_RECOVERY_UNRESOLVED",
            "the earlier version did not retain this invocation's original failed subset; "
            "preserve the run, staging and original mutation identity",
        )
    else:
        scope = sorted({_clean(path) for path in only_paths} & planned_paths) if only_paths else None
    selected = set(scope) if scope is not None else None
    label = (run.get("collection") or {}).get("label")
    for planned in plan_items:
        path = planned["original_path"]
        if selected is not None and path not in selected:
            continue
        outcome = (run.get("outcomes") or {}).get(path) or {}
        if outcome.get("status") in _APPLIED_STATUSES or outcome.get("status") == _SOURCE_ONLY:
            earlier = adoption_plan.read_import_entries(root, root / planned["target_path"])
            if _earlier_with_same_bytes(root, earlier, label, planned) is not None:
                continue
            raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "an earlier committed outcome no longer proves Source custody")
        if any(os.path.lexists(root / target) for target in (planned["target_path"], planned.get("artifact_path")) if target):
            raise AdoptionRunError(
                "IMPORT_RECOVERY_UNRESOLVED",
                "the earlier version left an occupied destination without a complete chunk receipt; "
                "its partial publication has no frozen post-image proof",
            )
    invocation["scope"] = scope
    run.setdefault("apply_invocations", {})[identity[0]] = invocation
    return invocation


def apply(
    root: Path,
    *,
    run_id: str,
    plan_id: str | None,
    continuation: str | None = None,
    retry_failed: bool = False,
    only_paths: list[str] | None = None,
    today: dt.date | None = None,
) -> dict:
    """Commit the previewed imports in chunks, bounded by the request budget.

    A call that runs out of budget returns a ``continuation``; the next call,
    carrying it, resumes at the first item without an outcome. Nothing is written
    twice: every item is checked against the earlier imports before it is copied.
    """
    from . import client_artifacts
    from .writer_lease import active_manager, active_mutation_recovery_identity

    root = Path(root)
    run_date = _today(today)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load_for_update(run_id)
        phase = run["phase"]
        # apply is re-runnable from any post-plan phase: `applying` resumes an interrupted
        # or budget-stopped run, and a finished one answers `already-applied`.
        # nosemgrep: ep-word-membership -- This module's run phases are a closed protocol.
        if phase not in (*_APPLY_PHASES, *_FINISH_PHASES, "failed"):
            raise AdoptionRunError("INVALID_PHASE", f"apply is not allowed from phase {phase!r}")
        plan = run.get("plan")
        if not plan:
            raise AdoptionRunError("PLAN_NOT_FOUND", "no plan on this run; call plan first")
        if plan_id != plan.get("plan_id") or plan.get("plan_format") != PLAN_FORMAT:
            raise AdoptionRunError("PLAN_STALE", "plan_id does not match the current plan")
        if plan.get("selection_hash") != (run.get("selection") or {}).get("selection_hash"):
            raise AdoptionRunError("PLAN_STALE", "selection changed since the plan was created")

        identity = active_mutation_recovery_identity()
        invocation = None
        reconciling = identity is not None and identity[2]
        if reconciling:
            invocation = (run.get("apply_invocations") or {}).get(identity[0])
            if invocation is None:
                invocation = _legacy_apply_invocation(
                    root, run, identity, continuation=continuation, retry_failed=retry_failed, only_paths=only_paths,
                )
            if (
                not isinstance(invocation, dict)
                or invocation.get("command_digest") != identity[1]
                or invocation.get("plan_id") != plan_id
                or invocation.get("continuation") != continuation
            ):
                raise AdoptionRunError(
                    "IMPORT_RECOVERY_UNRESOLVED",
                    "the interrupted invocation has no matching durable scope checkpoint; "
                    "preserve this run and its original mutation identity",
                )
            if "reply" in invocation:
                return _present(root, run, **invocation["reply"])

        entry = None
        if continuation:
            entry = _cursor_entry(run, "apply", continuation)
            if entry["state"] == "used" and not reconciling:
                return _present(root, run, **(entry.get("reply") or {}))

        plan_items = plan["items"]
        by_path = {it["original_path"]: it for it in plan_items}
        outcomes = dict(run.get("outcomes") or {})
        before = {
            "phase": phase, "outcomes": dict(outcomes), "scope": run.get("scope"),
            "cursors": copy.deepcopy(run.get("cursors")), "inflight": run.get("inflight"),
        }
        stats: Counter = Counter()
        reads = _CallReads()
        fresh = entry is None and not reconciling
        if fresh:
            _supersede_cursors(run, "apply")
            if retry_failed:
                wanted = {p for p, o in outcomes.items() if o.get("status") == "failed"}
                if only_paths:
                    wanted &= {_clean(p) for p in only_paths}
                for path in wanted:
                    del outcomes[path]
                run["scope"] = sorted(wanted)
            elif only_paths:
                run["scope"] = sorted({_clean(p) for p in only_paths} & set(by_path))
            else:
                run["scope"] = None
        elif reconciling:
            scope = invocation.get("scope")
            if "scope" not in invocation or scope is not None and (
                not isinstance(scope, list)
                or any(not isinstance(path, str) or path not in by_path for path in scope)
            ):
                raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "the frozen import scope is invalid")
            run["scope"] = copy.deepcopy(scope)
        elif "scope" in entry:
            run["scope"] = copy.deepcopy(entry["scope"])
        wanted = None if run.get("scope") is None else set(run["scope"])
        for owner, other in (run.get("apply_invocations") or {}).items():
            if "reply" in other or identity is not None and owner == identity[0]:
                continue
            held = set((other.get("publication") or {}).get("items") or {})
            if held and (wanted is None or held & wanted):
                # Otherwise another call could publish note backrefs or outcomes over an unfinished chunk's post-images.
                raise AdoptionRunError(
                    "IMPORT_RECOVERY_UNRESOLVED",
                    "another interrupted invocation holds this chunk; reconcile its original mutation identity first",
                )
        run["outcomes"] = outcomes
        if identity is not None and not reconciling:
            invocation = {
                "identity_digest": identity[0], "command_digest": identity[1], "plan_id": plan_id,
                "continuation": continuation, "scope": copy.deepcopy(run.get("scope")),
            }
            run.setdefault("apply_invocations", {})[identity[0]] = invocation
            # This intent precedes staged settlement, rename and Source/note publication.
            # It is not evidence that any requested import committed.
            store.save_intent(run)
        elif reconciling:
            store.save_intent(run)

        if fresh:
            scope = None if run["scope"] is None else set(run["scope"])
            # A repeat call re-checks what an earlier call applied: the originals stay unchanged.
            for path in list(outcomes):
                in_scope = scope is None or path in scope
                if in_scope and path in by_path and outcomes[path].get("status") in _APPLIED_STATUSES:
                    outcomes[path] = {**outcomes[path], "status": "already-applied"}
                    _settle_staged(root, run, reads, by_path[path], outcomes[path], stats)
        else:
            # A call that stopped between a chunk's commit and its settlement left those items unsettled.
            scope = None if run.get("scope") is None else set(run["scope"])
            for path, outcome in outcomes.items():
                if (
                    path in by_path and (scope is None or path in scope)
                    and outcome.get("status") in _APPLIED_STATUSES
                    and "consumed" not in outcome and "original_unchanged" not in outcome
                ):
                    _settle_staged(root, run, reads, by_path[path], outcome, stats)
        run.pop("apply_stop", None)
        budget = _call_budget()
        ceiling = client_artifacts.adoption_ceiling()
        written: list[str] = []
        pending = _pending(run)
        if pending:
            # Held in memory until a chunk commits it: a call whose every source drifted
            # refuses below with nothing written, as before.
            run["phase"] = "applying"
            run["inflight"] = True
            # Read once here, before the first chunk's timer, so the pacer does not price it into items.
            reads.open_sources(root, run)

        pacer = _ApplyPacer(root, budget, carried=run.get(_APPLY_PACE))
        stopped = False
        stop_reason: str | None = None
        chunks = 0
        while pending:
            if chunks and active_manager().writers_waiting(root):
                # Each chunk is committed whole, so the call may end here: the writer that waits commits
                # when this call releases the boundary, and the continuation resumes the import.
                stopped, stop_reason = True, STOP_FOR_WAITING_WRITE
                break
            if chunks:
                # Each chunk is committed whole, so a waiting derived proof may hold the boundary here.
                seconds, batches, room = pacer.next_unit(pending)
                active_manager().lend_boundary(
                    root, next_unit_seconds=seconds, next_unit_batches=batches, room_seconds=room
                )
            publication = invocation.get("publication") if invocation else None
            if publication:
                # The frozen batch owns one bookkeeping/log identity; its membership cannot change on retry.
                members = set(publication["items"])
                chunk = [item for item in pending if item["original_path"] in members]
                if len(chunk) != len(members):
                    raise AdoptionRunError("IMPORT_RECOVERY_UNRESOLVED", "the frozen chunk's pending membership changed")
            else:
                chunk = pacer.next_chunk(pending, first=not chunks)
            if not chunk:
                stopped = True
                break
            shortfall = _space_shortfall(root, chunk, reads.filing_stage(root, run))
            if shortfall is not None:
                run["apply_stop"] = shortfall
                stopped = True
                break
            started = time.monotonic()
            extractions = _apply_chunk(
                root, store, run, chunk, run_date=run_date, ceiling=ceiling,
                budget=budget, stats=stats, written=written, reads=reads, pacer=pacer,
                page_final=len(chunk) == len(pending),
            )
            pacer.record(chunk, time.monotonic() - started, extractions)
            completed_paths = {item["original_path"] for item in chunk}
            pending = [item for item in pending if item["original_path"] not in completed_paths]
            chunks += 1
            if stats["notes_deferred"]:
                stopped = True
                break

        if reads.held and "apply_stop" not in run:
            # A held page has no outcome: the continuation comes back to it once its recording is stored.
            run["apply_stop"] = _hold_stop(reads.held)
            stopped = True
        outcomes = run["outcomes"]
        # Every requested source drifted or vanished, and nothing was ever imported: refuse
        # before the run reports a result, so the still-valid selection survives a re-plan.
        if (
            fresh and not retry_failed and not only_paths and not stopped and outcomes
            and all(
                o.get("status") == "failed" and o.get("code") in _DRIFT_CODES
                for o in outcomes.values()
            )
        ):
            run.update(before)
            if identity is not None:
                # This refusal imported nothing; settle its intent before the outer writer releases the uncommitted key.
                run["apply_invocations"].pop(identity[0], None)
                if not run["apply_invocations"]:
                    run.pop("apply_invocations")
                store.save_intent(run)
            raise AdoptionRunError(
                "ADOPTION_SOURCE_CHANGED",
                "every requested source changed or is missing since the plan was captured; "
                "re-scan and re-plan to continue",
            )

        run["phase"] = _recompute_phase(outcomes, plan_items, stopped)
        run["inflight"] = False
        record_failure = None
        if run["phase"] != "applying":
            # A page whose last chunk filed nothing (only duplicates or failures) has no batch left to record the
            # rows that earlier chunks owe. Without this they wait for `finish`, which a caller may never make.
            record_failure = _record_owed_or_report(root, store, run, today=run_date)
        # The run keeps the cumulative counts; this call reports those of the originals it handled.
        unchanged, checked = _verify_counts(outcomes)
        run["verify"] = {
            "verified_unchanged": unchanged, "verified_total": checked,
            "consumed": _consumed_count(outcomes), "at": _now_iso(),
        }
        call_counts = {
            "verified_unchanged": stats["verified_unchanged"],
            "verified_total": stats["verified_total"],
            "consumed": stats["consumed"],
        }
        reply: dict = {
            **call_counts,
            "apply_result": {
                "written": written,
                **call_counts,
                "extraction": {
                    "requested": stats["extraction_requested"],
                    "deferred": stats["extraction_deferred"],
                    "failed": stats["extraction_failed"],
                },
                "notes": {
                    "created": stats["notes_created"],
                    "found": stats["notes_found"],
                    "failed": stats["notes_failed"],
                },
                "duplicates": {
                    "mapped": stats["mapped"],
                    "check_unavailable": stats["duplicate_check_unavailable"],
                },
                "skipped_duplicates": _skipped_duplicates(outcomes),
            },
            "progress": _progress(run),
        }
        if run["plan"].get("kept_speech"):
            reply["apply_result"]["kept_speech"] = {
                **{name: stats[f"kept_speech_{name}"] for name in kept_speech.OUTCOMES},
                "held": len(reads.held),
                # Pages that filed as text Sources because no recording was coming, under the code that says so.
                "unpaired": {kept_speech.NO_RECORDING: stats["kept_unpaired"]} if stats["kept_unpaired"] else {},
            }
        if stopped:
            reply["continuation"] = _issue_cursor(run, "apply")
        if stop_reason is not None:
            reply["stop_reason"] = stop_reason
        # In the reply that a repeated cursor returns again, so that replay shows them too.
        lines = [*reads.warnings]
        if record_failure is not None:
            lines.append(f"{record_failure}; `finish` records them.")  # `finish` or the next `apply` records the rows
        if lines:
            reply["warnings"] = [*_plan_warning_lines(run), *lines]
        if entry is not None:
            entry["state"], entry["reply"] = "used", reply
        if reads.sealed:
            run["sources_seal"] = adoption_plan.sources_seal(reads.pages)
        if invocation is not None:
            invocation["reply"] = copy.deepcopy(reply)
        pace = pacer.carried()
        if pace is not None:
            run[_APPLY_PACE] = pace
        # The one full save of the call: each chunk wrote only its changes.
        with call_spans.span("adoption.run_object"):
            store.save(run)
    return _present(root, run, **reply)


def cancel(root: Path, *, run_id: str, why: str | None) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load_for_update(run_id)
        phase = run["phase"]
        # A run stopped by its budget holds an open cursor and may be cancelled; one with no
        # cursor died mid-call and must be resumed with `apply` first.
        if phase == "applying" and _open_cursor(run, "apply") is None:
            raise AdoptionRunError("CANCEL_DURING_APPLY", "cannot cancel while an apply is in flight")
        if phase in ("applied", "done"):
            raise AdoptionRunError(
                "ALREADY_APPLIED", "run already applied; applied Sources survive (append-only)"
            )
        # nosemgrep: ep-word-membership -- This module's run phases are a closed protocol.
        if phase not in ("selecting", "planned", "applying", "partial", "failed"):
            raise AdoptionRunError("INVALID_PHASE", f"cancel is not allowed from phase {phase!r}")
        _refuse_pending_renames(root, "cancel", run_id=run_id)
        # The Sources this page filed stay, so the indexes and the log record them.
        warning = _record_owed_or_report(root, store, run, today=_today(None))
        run["cancel"] = {"at": _now_iso(), "why": (why or "").strip() or None}
        run["phase"] = "cancelled"
        store.save(run)
    reply = _present(root, run)
    if warning is not None:
        reply["warnings"] = [*reply.get("warnings", ()), f"{warning}."]
    return reply


def _skipped_duplicates(outcomes: dict) -> int:
    """The run's items that were not stored because a Source already held their bytes.

    Only `_already_applied` records the Source it found as `existing_ref`. An item the run stored, then a repeat
    call re-marked `already-applied`, keeps no such key, so the count holds across the calls of one run.
    """
    return sum(
        1 for outcome in outcomes.values() if outcome.get("status") == "already-applied" and "existing_ref" in outcome
    )


def _recompute_phase(outcomes: dict, plan_items: list[dict], budget_stopped: bool) -> str:
    """`applied` only when every planned item has an outcome and none failed."""
    statuses = [outcomes.get(it["original_path"], {}).get("status") for it in plan_items]
    statuses = [None if status == _SOURCE_ONLY else status for status in statuses]
    applied = any(s in _APPLIED_STATUSES for s in statuses)
    failed = any(s == "failed" for s in statuses)
    if any(s is None for s in statuses):
        if budget_stopped:
            return "applying"
        return "partial" if applied or failed else "planned"
    if applied and not failed:
        return "applied"
    return "partial" if applied else "failed"


# --------------------------------------------------------------------------- #
# finish and coverage
# --------------------------------------------------------------------------- #
def _same_page(left: str, right: str) -> bool:
    return left.removesuffix(".md") == right.removesuffix(".md")


def _find_paths(root: Path, query: str) -> tuple[list[str] | None, list[str]]:
    """Keep confirmed keyword hits separate from incomplete or failed recall."""
    from . import find as find_module

    degraded: list[str] = []
    try:
        hits = find_module.find(
            root, query=query, mode="keyword", limit=5, graph=False,
            degraded_out=degraded,
        )
    except Exception:  # noqa: BLE001 - recall failure never blocks finish
        return None, degraded
    return [str(getattr(hit, "path", "")) for hit in hits], degraded


def _recall_warming(components: list[str]) -> dict:
    return {"components": sorted(set(components)), "since_s": None}


def _applied_items(run: dict) -> list[tuple[dict, dict]]:
    plan_items = {it["original_path"]: it for it in (run.get("plan") or {}).get("items") or []}
    return [
        (plan_items[path], outcome)
        for path, outcome in (run.get("outcomes") or {}).items()
        if outcome.get("status") in _APPLIED_STATUSES and path in plan_items
        and outcome.get("target_path")
    ]


def _recall_check(root: Path, run: dict) -> dict:
    # A text note names itself in a question; a media file's title is its file name.
    applied = sorted(
        _applied_items(run),
        key=lambda pair: (pair[0].get("form") == "text", pair[0].get("original_bytes", 0)),
        reverse=True,
    )
    if applied:
        item, outcome = applied[0]
        title, target = item.get("title") or item["original_path"], outcome["target_path"]
    else:
        packs = run.get("pack_suggestions") or []
        title, target = (packs[0].get("name") if packs else "") or "", ""
    paths, degraded = _find_paths(root, title) if title else ([], [])
    ok = bool(target) and any(_same_page(path, target) for path in paths or [])
    result: dict = {"query": title, "ok": ok, "hits": paths or []}
    if not ok:
        if paths is None or degraded:
            result.update(ok=None, status="failed" if paths is None else "pending")
            if degraded:
                result["warming"] = _recall_warming(degraded)
        else:
            result["next_action"] = {"tool": "maintain_memory", "args": {"mode": "reconcile"}}
    return result


def _recall_sample(root: Path, run: dict) -> dict:
    """Search for a spread of imported notes: one per top-level folder, then the next of each."""
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for item, outcome in sorted(_applied_items(run), key=lambda pair: pair[0]["collection_path"]):
        folder = item["collection_path"].split("/", 1)[0] if "/" in item["collection_path"] else ""
        groups.setdefault(folder, []).append((item, outcome))
    ordered = [groups[key] for key in sorted(groups)]
    picks: list[tuple[dict, dict]] = []
    depth = 0
    while len(picks) < _RECALL_SAMPLE and any(depth < len(group) for group in ordered):
        for group in ordered:
            if depth < len(group) and len(picks) < _RECALL_SAMPLE:
                picks.append(group[depth])
        depth += 1
    found = 0
    missed: list[str] = []
    pending, failed = 0, 0
    degraded: list[str] = []
    for item, outcome in picks:
        paths, incomplete = _find_paths(root, item.get("title") or item["original_path"])
        if paths is not None and any(_same_page(path, outcome["target_path"]) for path in paths):
            found += 1
        elif paths is None:
            failed += 1
        elif incomplete:
            pending += 1
            degraded.extend(incomplete)
        else:
            missed.append(outcome["target_path"])
    result: dict = {"sampled": len(picks), "found": found, "missed": missed}
    if pending or failed:
        evaluated = found + len(missed)
        result.update(
            status="failed" if failed else "pending", evaluated=evaluated,
            found=found if evaluated else None, pending=pending, failed=failed,
        )
        if degraded:
            result["warming"] = _recall_warming(degraded)
    return result


def _extraction_states(root: Path, run: dict, budget: request_budget.RequestBudget) -> dict:
    """Extraction state of every imported media artifact, after one request for any missing job."""
    from . import media_engines, media_jobs, media_processing

    media: list[tuple[str, str]] = []
    for _item, outcome in _applied_items(run):
        artifact = outcome.get("artifact_path")
        media_type = media_processing.classify_media(root / artifact) if artifact else None
        if media_type is not None:
            media.append((artifact, media_type))
    if not media:
        return {}
    counts: Counter = Counter()
    try:
        jobs = media_jobs.MediaJobStore(root)
    except Exception:  # noqa: BLE001 - an unreadable ledger is "unknown", not a failed finish
        return {"unknown": len(media)}
    for artifact, media_type in media:
        try:
            page = f"{artifact}.md"
            with reserved_paths.open_generic_binary(root, page, physical=True) as source:
                content = source.stream.read().decode("utf-8")
            job = jobs.get_by_binary(root / artifact)
            if (
                job is None and media_engines.stage_enabled(media_type)
                and not media_processing.has_completed_transcript(content, media_type=media_type)
                and budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS)
            ):
                with contextlib.suppress(Exception):
                    media_processing.reconcile_media(
                        root, root / artifact, explicit=False,
                        commit_guard=contextlib.nullcontext, defer_fanout_to_terminal=True,
                    )
                    job = jobs.get_by_binary(root / artifact)
                    with reserved_paths.open_generic_binary(root, page, physical=True) as source:
                        content = source.stream.read().decode("utf-8")
            counts[_extraction_state(content, media_type, job)] += 1
        except Exception:  # noqa: BLE001 - one unreadable page is one unknown
            counts["unknown"] += 1
    return dict(counts)


def _extraction_state(content: str, media_type: str, job: object) -> str:
    from . import media_engines, media_jobs, media_processing

    if media_processing.has_completed_transcript(content, media_type=media_type):
        return "done"
    frontmatter, _body, _raw = parse_frontmatter(content)
    extracted_by = str(frontmatter.get("extracted_by", "")).strip().lower()
    if (
        extracted_by.startswith("failed")
        or frontmatter.get("processing_state") == media_jobs.FAILED
        or (job is not None and job.state == media_jobs.FAILED)
    ):
        return "failed"
    if job is not None and job.state == media_jobs.BLOCKED:
        return "blocked"
    if not media_engines.stage_enabled(media_type):
        return "engine_disabled"
    return "unknown" if job is None else "pending"


def _coverage(root: Path, run: dict, budget: request_budget.RequestBudget) -> dict:
    """What the run did with every file it was pointed at, and what it did not read."""
    plan = run.get("plan") or {}
    selection = run.get("selection") or {}
    outcomes = run.get("outcomes") or {}
    plan_items = plan.get("items") or []
    by_status = Counter(
        outcomes.get(it["original_path"], {}).get("status") or "pending" for it in plan_items
    )
    by_code = Counter(
        o.get("code") for o in outcomes.values() if o.get("status") == "failed" and o.get("code")
    )
    notes = Counter((o["note"] or {}).get("status") for o in outcomes.values() if o.get("note"))
    folders = [row for row in run.get("inventory") or [] if row.get("kind") == "folder"]
    applied_text = [
        item for item, outcome in _applied_items(run) if item["form"] == "text" and not outcome.get("kept_speech")
    ]
    return {
        "selected": len(selection.get("paths") or []),
        "outcomes": {"by_status": dict(by_status), "by_code": dict(by_code)},
        "not_imported": {
            "hidden_folders": {
                "folders": len(folders),
                "files": sum(int(row.get("files") or 0) for row in folders),
            },
            "hidden_files": run.get("hidden_files_skipped", 0),
            "refused_at_selection": selection.get("rejected_counts") or {},
            "refused_at_plan": dict(Counter(row["code"] for row in plan.get("skipped") or [])),
            "inventory_truncated": run.get("inventory_truncated", 0),
        },
        "extraction": _extraction_states(root, run, budget),
        "notes": {
            "planned": sum(1 for it in plan_items if it.get("note") or it.get("existing_note")),
            "created": notes["applied"],
            "already-applied": notes["already-applied"],
            "failed": notes["failed"],
            "left_with_previous_note": sum(1 for it in plan_items if it.get("previous_note")),
        },
        "links": {
            "resolved": sum(len((item.get("links") or {}).get("resolved") or []) for item in applied_text),
            "unresolved": sum(len((item.get("links") or {}).get("unresolved") or []) for item in applied_text),
        },
        "recall": _recall_sample(root, run),
    }


def _consume_filed_text(root: Path, run: dict, budget: request_budget.RequestBudget) -> None:
    """Remove the staged copy of each filed text item whose Source page still holds its text, and mark it consumed.

    `apply` keeps a text item's copy, because the item may still owe its note. By `finish` an item that is
    `applied` or `already-applied` is done. Its copy goes only after the Source page's fence body hashes to the plan
    and the held, hash-checked removal of `adoption_consume.remove_staged` passes. A copy stays and its outcome records
    why in `stays`: `source_differs` when the Source page does not hold the text, so the copy is the only exact one,
    and `copy_changed` when the copy is no longer the file that the manifest and the plan describe. A copy that is
    already gone (a `finish` stopped after its removal) is settled through its Source. The call stops starting
    removals when its budget runs low, and the copies it did not reach stay unverified in the residual.
    """
    if not run.get("consume", True):
        return
    reads = _CallReads()
    stage = reads.recorded_stage(root, run)
    if stage is None:
        return
    outcomes = run.get("outcomes") or {}
    for planned in (run.get("plan") or {}).get("items") or []:
        outcome = outcomes.get(planned["original_path"])
        if (
            planned["form"] != "text" or outcome is None or outcome.get("consumed")
            or outcome.get("status") not in _APPLIED_STATUSES
            or outcome.get("kept_speech")  # a page whose result went to its recording's Source has no text Source to check
        ):
            continue
        if not budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS):
            budget.note_truncated("finish.consume_text")
            return
        if os.path.lexists(root / planned["original_path"]):
            if not _text_source_holds(root, outcome.get("target_path") or planned["target_path"], planned):
                outcome["stays"] = "source_differs"
            elif not adoption_consume.remove_staged(root, stage, planned):
                outcome["stays"] = "copy_changed"
            else:
                outcome["consumed"] = True
        elif _filed_and_gone(root, run, reads, planned) is not None:
            outcome["consumed"] = True


def finish(
    root: Path,
    *,
    run_id: str,
    write_manifest: bool = True,
    today: dt.date | None = None,
) -> dict:
    root = Path(root)
    run_date = _today(today)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load_for_update(run_id)
        if run["phase"] not in _FINISH_PHASES:
            raise AdoptionRunError(
                "INVALID_PHASE", f"finish requires phase 'applied' or 'partial', got {run['phase']!r}"
            )
        recall = _recall_check(root, run)
        budget = _call_budget()
        coverage = _coverage(root, run, budget)
        _consume_filed_text(root, run, budget)
        # Empty folders go only for a run that consumes; the residual is reported for any recorded stage.
        removed_folders = adoption_consume.remove_empty_folders(root, run) if run.get("consume", True) else 0
        stage_residual = adoption_consume.residual(root, run, _APPLIED_STATUSES, _earlier_pages(store, run))
        if stage_residual is not None:
            stage_residual["removed_empty_folders"] = removed_folders
        # The counts the last `apply` recorded from its own re-hash — never a fresh
        # re-hash at finish time, which could disagree with what was verified then.
        verify = run.get("verify") or {}
        verified_unchanged = verify.get("verified_unchanged", 0)
        verified_total = verify.get("verified_total", 0)
        # Before the manifest, so the log reads in filing order: what the page's last batches filed, then its manifest.
        _record_owed(root, store, run, today=run_date)
        manifest_path = None
        if write_manifest:
            manifest_path = _write_run_manifest(
                root, run, recall=recall, verified_unchanged=verified_unchanged,
                verified_total=verified_total, today=run_date,
            )
        title = recall["query"]
        # A question is ready only once the search finds the page it asks about.
        ready = (
            {
                "first_question": f'What do my notes say about "{title}"?',
                "route": {"tool": "search_memory", "args": {"query": title}},
            }
            if recall["ok"] is True
            else {"indexing": _indexing(recall, coverage["recall"])}
        )
        run["finish"] = {
            "at": _now_iso(),
            "recall_check": recall,
            "coverage": coverage,
            **ready,
            "handoff": _handoff(run),
            "verified_unchanged": verified_unchanged,
            "verified_total": verified_total,
            "consumed": _consumed_count(run.get("outcomes") or {}),
            "residual": stage_residual,
            "manifest_path": manifest_path,
        }
        run["phase"] = "done"
        store.save(run)
    return _present(root, run, coverage=coverage)


def _indexing(recall: dict, sample: dict) -> dict:
    """Why finish offers no first question: the recall check's state and the recall sample's counts.

    The state is `pending` or `failed` when the search could not answer, and `missed` when it
    answered without the page. `found` is None when no sampled search answered.
    """
    return {
        "state": recall.get("status") or "missed",
        "sampled": sample["sampled"],
        "found": sample["found"],
        "missed": len(sample["missed"]),
        "pending": sample.get("pending", 0),
        "failed": sample.get("failed", 0),
    }


def _write_run_manifest(
    root: Path,
    run: dict,
    *,
    recall: dict,
    verified_unchanged: int,
    verified_total: int,
    today: dt.date,
) -> str:
    summary = {
        "run_id": run["run_id"],
        "run_ref": run["run_ref"],
        "phase": "done",
        "selection": (run.get("selection") or {}).get("paths", []),
        "outcomes": run.get("outcomes") or {},
        "recall_check": recall,
        "verified_unchanged": verified_unchanged,
        "verified_total": verified_total,
    }
    writes, rel, _warnings = adopt_module.run_manifest_writes(
        root, run_id=run["run_id"], summary=summary, today=today
    )
    batch_atomic_write(writes, vault_root=root)
    return rel


# --------------------------------------------------------------------------- #
# file: the whole lifecycle behind one repeatable call
# --------------------------------------------------------------------------- #
# The closed protocol of a `file` reply, which the Cloud CLI reads as well: its states, and the counts in `progress`.
FILING_STATES = ("working", "stopped", "done")  # nosemgrep: ep-word-set -- the closed states of a `file` reply.
FILING_COUNTS = ("filed", "skipped_duplicates", "failed", "remaining", "pages_done")  # nosemgrep: ep-word-set -- the closed counts of a `file` reply.
_WORKING, _STOPPED, _DONE = FILING_STATES
# The driver's own steps that publish: a reconciling call runs only the apply that its identity stored.
_APPLY_STEPS = ("apply", "retry")  # nosemgrep: ep-word-set -- the `file` driver's closed step names that call `apply`.
# The driver's own steps that open a page: the first page of a scan, a later page, or a new scan of a finished one.
_SCAN_STEPS = ("start", "rescan")  # nosemgrep: ep-word-set -- the `file` driver's closed step names that call `start`.
# What a done `file` reply carries from the last `finish` reply, which reports on that page alone.
_PAGE_REPORT = ("coverage", "first_question", "route", "indexing")  # nosemgrep: ep-word-set -- `finish`'s closed reply fields.


def _chain_end(store: AdoptionRunStore, run: dict) -> dict:
    """The newest page of the paged scan that `run` belongs to: follow each used `after` cursor to the page it opened."""
    seen = {run["run_id"]}
    while True:
        opened = next(
            (
                entry["reply"]["run_id"] for entry in (run.get("cursors") or {}).values()
                if entry.get("kind") == "after" and entry.get("state") == "used"
                and isinstance(entry.get("reply"), dict) and entry["reply"].get("run_id")
            ),
            None,
        )
        if opened is None or opened in seen:
            return run
        try:
            run = store.load(opened)
        except AdoptionRunError:
            return run
        seen.add(opened)


def _other_collection(run: dict, collection: str | None) -> bool:
    """Whether the call names a collection, and it is not the label that the run's scan fixed."""
    return bool((collection or "").strip()) and adoption_plan.nfc(collection.strip()) != run["collection"]["label"]


def _resumed_page(store: AdoptionRunStore, runs: Iterable[dict]) -> dict | None:
    """The page that a call with only the path carries on, or None when no run has scanned the folder.

    It is the newest of `runs` that has a step left, else the newest of them, followed to the newest page of its
    scan. So a retried first call never makes a second run, and a finished import carries on from its last page,
    which answers `done` unless staged files came after its scan (see `_filing_step`).
    """
    runs = tuple(runs)
    if not runs:
        return None
    unfinished = [run for run in runs if _next_filing_step(run) != "done"]
    return _chain_end(store, max(unfinished or runs, key=lambda run: str(run.get("created") or "")))


def filing_done(root: Path, path: str, runs: Iterable[dict]) -> bool:
    """Whether a `file` call on the staged folder `path` answers `done`; a folder that no run scanned is not done.

    `runs` are the runs of the folder's stage, as `AdoptionRunStore.runs_by_stage` groups them.
    """
    store = AdoptionRunStore(root)
    page = _resumed_page(store, (run for run in runs if run.get("source_root") == path))
    return page is not None and _filing_step(root, store, page) == "done"


def _filing_page(root: Path, store: AdoptionRunStore, path: str, collection: str | None, continuation: str | None) -> dict | None:
    """The newest page that `file` carries on, or None when the call starts a new scan.

    A continuation names the run that issued it, and the call carries on that run's scan. When that scan is done,
    the call carries on the newest scan of the path instead, so the repeat of a call that stopped right after it
    started a new scan carries on that scan rather than starting another. A path alone carries on the newest scan
    of that path (see `_resumed_page`). A call that names another collection than the scan's is refused, because
    the scan keeps the label that its first page fixed.
    """
    source_root = _clean(path)
    if source_root and (len(PurePosixPath(source_root).parts) < 2 or PurePosixPath(source_root).parts[0] != IMPORT_STAGE_DIR):
        raise AdoptionRunError(
            "IMPORT_REQUEST_INVALID", f"file takes a staged folder, {IMPORT_STAGE_DIR}/<id>; use start for any other folder"
        )
    if continuation:
        try:
            issuer, _token, _entry = _cursor_holder(root, continuation, "file")
        except AdoptionRunError as exc:
            raise AdoptionRunError(
                "INVALID_CONTINUATION",
                "no file call issued that continuation; call file with the path and no continuation",
            ) from exc
        if source_root not in ("", issuer["source_root"]) or _other_collection(issuer, collection):
            raise AdoptionRunError(
                "INVALID_CONTINUATION",
                f"that continuation belongs to the import of {issuer['source_root']!r} as collection "
                f"{issuer['collection']['label']!r}; pass the same path and collection, or neither",
            )
        end = _chain_end(store, issuer)
        return end if _next_filing_step(end) != "done" else _resumed_page(store, store.runs_at(issuer["source_root"]))
    if not source_root:
        raise AdoptionRunError("IMPORT_REQUEST_INVALID", f"file takes the path of the staged folder, {IMPORT_STAGE_DIR}/<id>")
    page = _resumed_page(store, store.runs_at(source_root))
    if page is not None and _other_collection(page, collection):
        raise AdoptionRunError(
            "IMPORT_REQUEST_INVALID",
            f"{source_root!r} is imported as collection {page['collection']['label']!r}; "
            "pass that collection, or no collection, to carry on with it",
        )
    return page


def _retry_owed(run: dict) -> bool:
    """Whether this page holds failed files that no `apply` has retried since its full `apply`.

    `apply` with `retry_failed` leaves `scope` set to the failed files that it retried; a full `apply` leaves it None.
    """
    return run.get("scope") is None and any(
        outcome.get("status") == "failed" for outcome in (run.get("outcomes") or {}).values()
    )


def _next_filing_step(run: dict) -> str:
    """The step that moves this page on, from its saved phase alone; `done` ends the import.

    A page that an `apply` limited to some files (`only_paths`) left with planned files that have no outcome applies
    them first. A page with failed files retries them once (`retry`). Then a page with a filed file finishes. A page
    whose every file failed is closed (`close`): `finish` refuses it, and a later page cannot plan while it stays open.
    """
    phase = run["phase"]
    if phase == "selecting":
        return "plan" if run.get("plan_draft") or (run.get("selection") or {}).get("paths") else "select"
    if phase in _APPLY_PHASES:
        return "apply"
    outcomes = run.get("outcomes") or {}
    # nosemgrep: ep-word-membership -- This module's run phases are a closed protocol.
    if phase in (*_FINISH_PHASES, "failed") and any(
        item["original_path"] not in outcomes for item in (run.get("plan") or {}).get("items") or ()
    ):
        return "apply"
    # nosemgrep: ep-word-membership -- This module's run phases are a closed protocol.
    if phase in (*_FINISH_PHASES, "failed") and _retry_owed(run):
        return "retry"
    if phase in _FINISH_PHASES:
        return "finish"
    if phase == "failed":
        return "close"
    return "start" if _open_cursor(run, "after") else "done"


def _unscanned(root: Path, store: AdoptionRunStore, page: dict) -> int:
    """How many staged files under the page's folder no page of its scan classifies: the residual's `outside_run`.

    Such a file came after the scan, such as one that a rerun of a local import copied, or one put in the stage by
    hand. The classification is the residual's own (`adoption_consume.stage_reasons` over the pages of the scan), so
    a `done` reply's residual reads the same pages. A hidden file does not count: a scan skips it by design, so a new
    scan would not classify it either. Nor does a file whose name is not UTF-8: no run can record it, so a new scan
    would fail on it at every call. The residual lists both.
    """
    parts = PurePosixPath(page["source_root"]).parts
    if len(parts) < 2 or parts[0] != IMPORT_STAGE_DIR:
        return 0
    inner = parts[2:]  # the folder that the scan covered inside the stage, when it did not cover the whole stage
    files = [
        (path, size) for path, size in import_stage.staged_files(root, parts[1])
        if PurePosixPath(path).parts[:len(inner)] == inner
        and not any(_hidden(name) for name in PurePosixPath(path).parts[len(inner):]) and _recordable(path)
    ]
    pages = (page, *_earlier_pages(store, page))
    reasons = adoption_consume.stage_reasons(parts[1], files, pages, _APPLIED_STATUSES)
    return sum(1 for _path, _size, reason in reasons if reason == adoption_consume.OUTSIDE_RUN)


def _reading_exports(root: Path, run: dict) -> bool:
    """Whether an app export of the run's stage is still being read, so a new scan must meet what the next slice derives."""
    stage_id = _stage_id_of(run["source_root"])
    return stage_id is not None and bool(export_expansion.read(root, stage_id).held)


def _filing_step(root: Path, store: AdoptionRunStore, run: dict) -> str:
    """The step that moves the import on: the page's own step, or `rescan` when its scan is done and missed staged files.

    `done` ends the import only when no staged file came after the scan; else a new scan of the folder files them.
    """
    step = _next_filing_step(run)
    return "rescan" if step == "done" and (_unscanned(root, store, run) or _reading_exports(root, run)) else step


def _held_apply(store: AdoptionRunStore, run: dict, identity_digest: str) -> tuple[str, dict] | None:
    """The page that holds this mutation identity's apply invocation, and the invocation; None when the attempt never reached apply.

    `apply` saves the invocation before it publishes anything, so an identity without one published nothing.
    """
    for page in (run, *_earlier_pages(store, run)):
        invocation = (page.get("apply_invocations") or {}).get(identity_digest)
        if isinstance(invocation, dict):
            return page["run_id"], invocation
    return None


def _stage_id_of(source_root: str) -> str | None:
    """The import ID of a staged folder `_Imports/<id>[/...]`, or None for any other path."""
    parts = _clean(source_root).split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == IMPORT_STAGE_DIR and is_import_id(parts[1]) else None


def _export_rules(root: Path, run: dict) -> tuple[list[str | dict], list[str]]:
    """The `select` rules of a stage that holds app exports: transcripts file as the registry's Source kind, and an export still being read waits.

    An export that expansion has not finished is left out of this selection, because filing consumes it and the next
    call still reads it; the call that finishes it selects it. A Source kind that the vault's taxonomy lacks leaves its
    transcripts unclassified rather than stopping the filing.
    """
    source_root = run["source_root"]
    stage_id = _stage_id_of(source_root)
    include: list[str | dict] = [source_root]
    if stage_id is None:
        return include, []
    reading = export_expansion.read(root, stage_id)
    prefix = f"{IMPORT_STAGE_DIR}/{stage_id}/"
    scanned = [row["path"] for row in run.get("inventory") or [] if row.get("kind") != "folder"]
    taxonomy = source_taxonomy.load_taxonomy(root) if reading.kinds else None
    for folder, kind in sorted(reading.kinds.items()):
        if not any(path.startswith(f"{prefix}{folder}/") for path in scanned):
            continue
        try:
            _resolve_classification(taxonomy, kind, None)
        except AdoptionRunError:
            continue
        include.append({"path": f"{prefix}{folder}", "source_kind": kind})
    return include, [f"{prefix}{export}" for export in reading.held]


def _file_step(
    root: Path, store: AdoptionRunStore, run: dict | None, step: str, *,
    path: str, collection: str | None, today: dt.date | None, budget: request_budget.RequestBudget | None = None,
) -> dict:
    """Run one lifecycle step through its own function and return the page it leaves."""
    if step == "rescan" or (step == "start" and run is None):
        # A scan lists the stage as it is, so the exports are expanded first and the scan meets their transcripts.
        # A scan that continues a paged one does not expand: new files would move the pages under it.
        stage_id = _stage_id_of(run["source_root"] if run is not None else path)
        if stage_id is not None:
            held = budget or _call_budget()
            export_expansion.expand(
                root, stage_id, keep_going=lambda: held.can_afford(request_budget.DELIVERY_RESERVE_SECONDS),
                rewalk=step == "rescan")
    if step == "start":
        if run is None:
            # Read here, not as a default argument, so a test or an operator can change the page size.
            opened = start(
                root, path=_clean(path), collection=collection, max_candidates=MAX_CANDIDATES_DEFAULT, today=today
            )
        else:
            opened = start(root, after=f"{run['run_id']}:{_open_cursor(run, 'after')}", today=today)
        return store.load(opened["run_id"])
    if step == "rescan":
        # The new scan keeps the label of the finished one, so a file that the finished scan filed is not filed twice,
        # and its `consume` choice, so staged copies that the import keeps stay.
        opened = start(
            root, path=run["source_root"], collection=run["collection"]["label"], consume=run.get("consume", True),
            max_candidates=MAX_CANDIDATES_DEFAULT, today=today,
        )
        return store.load(opened["run_id"])
    run_id = run["run_id"]
    if step == "select":
        include, exclude = _export_rules(root, run)
        select(root, run_id=run_id, include=include, exclude=exclude)
        page = store.load(run_id)
        if not page["selection"]["paths"]:
            # Nothing on this page can import, so no plan can exist; closing it lets the scan move to the next page.
            cancel(root, run_id=run_id, why="nothing on this page is eligible to file")
            page = store.load(run_id)
        return page
    if step == "plan":
        plan(root, run_id=run_id, continuation=_open_cursor(run, "plan") if run.get("plan_draft") else None, today=today)
    elif step == "apply":
        apply(
            root, run_id=run_id, plan_id=run["plan"]["plan_id"],
            continuation=_open_cursor(run, "apply") if run["phase"] == "applying" else None, today=today,
        )
    elif step == "retry":
        apply(root, run_id=run_id, plan_id=run["plan"]["plan_id"], retry_failed=True, today=today)
    elif step == "close":
        # The failed outcomes stay on the page, so `progress` counts them and the residual lists their staged files.
        cancel(root, run_id=run_id, why="every file on this page failed, also when retried once")
    else:
        finish(root, run_id=run_id, today=today)
    return store.load(run_id)


def file(
    root: Path,
    *,
    path: str = "",
    collection: str | None = None,
    continuation: str | None = None,
    today: dt.date | None = None,
) -> dict:
    """File a staged folder with as many lifecycle steps as one call affords, and say what comes next.

    Every step is the module function of that name, chosen from the saved phase of the newest page, so a call
    carries on from wherever the run stands, however it got there. A page with failed files retries them once
    with `apply`, then finishes, or is closed when every file failed, and the scan goes on to the next page.
    A call ends after an `apply` or `finish`, when `plan` needs another call, when the next step is `start`
    of another page and the call has run a step, or when the budget cannot cover the delivery reserve. The
    reply's `continuation` is fresh each time: a call that repeats an earlier one's arguments gets that call's
    stored answer and does no work.

    A call that retries an interrupted attempt of its own mutation identity repeats that attempt's `apply`
    exactly, or runs no `apply` when the attempt never reached one. Another identity that meets an
    interrupted chunk is refused by `apply`, and the reply stops with its code. An error before any run
    exists is a bad argument and raises. An error after one stops the reply with that error's code, so the
    work already committed is still reported.
    """
    from .writer_lease import active_mutation_recovery_identity

    root = Path(root)
    store = AdoptionRunStore(root)
    run = _filing_page(root, store, path, collection, continuation)
    identity = active_mutation_recovery_identity()
    reconciling = identity is not None and identity[2]
    budget = _call_budget()
    steps = 0
    try:
        held = _held_apply(store, run, identity[0]) if reconciling and run is not None else None
        if held is not None:
            page_id, invocation = held
            apply(
                root, run_id=page_id, plan_id=invocation.get("plan_id"),
                continuation=invocation.get("continuation"), today=today,
            )
            return _filing_reply(root, store, _chain_end(store, store.load(page_id)))
        while True:
            step = "start" if run is None else _filing_step(root, store, run)
            if step == "done":
                break
            if steps and (step in _SCAN_STEPS or not budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS)):
                break
            if step in _APPLY_STEPS and reconciling:
                # Only the identity's own held apply may run under reconciliation; this attempt published nothing.
                break
            run = _file_step(root, store, run, step, path=path, collection=collection, today=today, budget=budget)
            steps += 1
            if step in _APPLY_STEPS or step == "finish" or (step == "plan" and run["phase"] == "selecting"):
                break
    except AdoptionRunError as exc:
        if run is None:
            if exc.code == "KB_NOT_INITIALIZED":
                # `file` has no such argument, so `start`'s own advice would send the agent to one it cannot pass.
                begin = capabilities.active_call_text(
                    "adoption_studio", {"action": "start", "path": _clean(path), "initialize_kb": True}
                )
                raise AdoptionRunError(
                    exc.code,
                    f"{managed_join('')} is required; run {begin}, then repeat the file call: it carries on that run",
                ) from exc
            raise
        return _filing_reply(root, store, run, stop={"code": exc.code, "reason": exc.reason})
    return _filing_reply(root, store, run)


def _filing_state(root: Path, store: AdoptionRunStore, run: dict) -> tuple[str, dict | None]:
    """`done`, `stopped` for space with why, or `working`, from the newest page and its stage."""
    if _filing_step(root, store, run) == "done":
        return _DONE, None
    stop = run.get("apply_stop")
    if stop:
        return _STOPPED, {
            "code": stop["code"],
            "reason": stop.get("reason") or (
                f"The next chunk needs {stop['chunk_bytes']} bytes and a reserve of {stop['reserve_bytes']} bytes; "
                f"{stop['free_bytes']} bytes are free."
            ),
        }
    return _WORKING, None


def _page_remaining(run: dict) -> int:
    """Files of this page that have neither been filed nor failed."""
    if run["phase"] in _FINISHED_PHASES:
        return 0
    if run.get("plan"):
        # Every planned file without an outcome, also one outside the last `apply`'s `only_paths`: the next step applies it.
        outcomes = run.get("outcomes") or {}
        return sum(_is_open(outcomes, item["original_path"]) for item in run["plan"]["items"])
    selected = (run.get("selection") or {}).get("paths")
    return len(selected) if selected else sum(1 for row in run.get("inventory") or [] if row.get("eligible"))


def _page_number(run: dict) -> int:
    """The page's number in its paged scan; a scan that fits one page has no paging and is page 1."""
    return (run.get("paging") or {}).get("page", 1)


def _page_position(run: dict) -> dict:
    """Where the newest page stands: its number, its phase and how many selected files `plan` has read so far."""
    draft = run.get("plan_draft")
    planned = draft["next"] if draft else len((run.get("selection") or {}).get("paths") or []) if run.get("plan") else 0
    return {"number": _page_number(run), "phase": run["phase"], "planned": planned}


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _filing_summary(filing: str, filed: int, skipped: int, failed: int, remaining: int, partial: int = 0) -> str:
    """One sentence built from the counts and nothing else: it claims no more than the outcomes record.

    It names the skipped duplicates only when there are some, so an import without any reads as before. It names an
    app export that was read in part, so a `done` import never reads as one with every item filed.
    """
    duplicates = f"{skipped} duplicate{'' if skipped == 1 else 's'} skipped, " if skipped else ""
    counts = f"{_files(filed)} filed, {duplicates}{failed} failed, {remaining} left"
    if partial:
        counts += f"; {partial} export{' was' if partial == 1 else 's were'} read in part"
    if filing == _DONE:
        return f"Filing is done: {counts}."
    return f"Filing is {filing}: {counts}."


def _filing_reply(root: Path, store: AdoptionRunStore, run: dict, *, stop: dict | None = None) -> dict:
    """The compact reply of a `file` call: where the import stands, what comes next and what only the person can do.

    `progress` and `summary` count the whole import: every page of every scan of its folder, with each file's
    newest outcome, so a file that a new scan finds filed already counts once. A `done` reply's `filed` holds the
    stage's residual and, under `last_finished_page`, what the last `finish` reported about its own page alone.
    """
    if stop is not None:
        # A step that raised may have saved the page before it did: only the stored page says where it stands.
        run = store.load(run["run_id"])
    filing, derived = _filing_state(root, store, run)
    if stop is not None:
        filing, derived = _STOPPED, stop
    scans = sorted(store.tallies_at(run["source_root"], current=run), key=lambda tally: tally.created)
    newest = {path: marks for tally in scans for path, marks in tally.outcomes.items()}
    # A skipped duplicate is a file whose newest outcome names the Source that already held its bytes, as
    # `_skipped_duplicates` reads it. A file whose Source a page of this import stored stays filed when a later
    # scan meets that Source again, so each file counts once: as filed or as a skipped duplicate.
    stored = {path for tally in scans for path, marks in tally.outcomes.items() if marks & _STORED}
    skipped = sum(1 for path, marks in newest.items() if path not in stored and marks & _SKIPPED)
    filed = sum(1 for marks in newest.values() if marks & _FILED) - skipped
    stage_id = _stage_id_of(run["source_root"])
    expansion = export_expansion.read(root, stage_id) if stage_id is not None else None
    # An item that expansion could not read has no staged file and no outcome, so it is counted here.
    failed = sum(1 for marks in newest.values() if marks & _FAILED) + (expansion.failed if expansion else 0)
    remaining = _page_remaining(run) + int(run.get("inventory_truncated") or 0)
    if _next_filing_step(run) == "done":
        remaining += _unscanned(root, store, run)  # staged files that came after the scan, which a new scan files
        remaining += len(expansion.held) if expansion else 0  # an export still being read stays unfiled until its last item
    pages_done = sum(1 for tally in scans if tally.phase in _FINISHED_PHASES)
    person: list[dict] = []
    if stop is None and filing == _STOPPED and (run.get("apply_stop") or {}).get("code") == _SPACE_STOP:
        person.append({"need": "free_space", "text": run["apply_stop"]["next"]})
    if stop is not None and stop["code"] == "IMPORT_RECOVERY_UNRESOLVED":
        person.append({"need": "report_to_operator", "text": stop["reason"]})
    reply: dict = {
        "run_id": run["run_id"],
        "collection": run["collection"]["label"],
        "filing": filing,
        "progress": {
            **dict(zip(FILING_COUNTS, (filed, skipped, failed, remaining, pages_done), strict=True)),
            "page": _page_position(run),
        },
        "summary": _filing_summary(filing, filed, skipped, failed, remaining, expansion.partial if expansion else 0),
    }
    if derived is not None:
        reply["stop"] = derived
    if filing == _DONE:
        pages = (run, *_earlier_pages(store, run))
        finished = next((page for page in pages if page.get("finish")), None)
        # Read now from the stage, as `_unscanned` reads it, so a file put in the stage after the last `finish` is listed.
        residual = adoption_consume.residual(root, run, _APPLIED_STATUSES, pages[1:])
        done: dict = {}
        if finished is not None:
            finish = _presented_finish(finished)
            # A `finish` describes its own page, so it sits under a key that names that page, never as the import's totals.
            done["last_finished_page"] = {"page": _page_number(finished), "run_id": finished["run_id"], **{
                key: finish[key] for key in _PAGE_REPORT if key in finish
            }}
        if residual is not None:
            done["residual"] = residual
        reply["filed"] = done
        for action in _residual_actions(residual, where="filed.residual"):
            person.append({"need": action["action"].replace("-", "_"), "text": action["description"]})
    reply["person"] = person
    if filing == _WORKING or (filing == _STOPPED and run.get("apply_stop") and stop is None):
        with _LOCK:
            latest = store.load_for_update(run["run_id"])
            token = _issue_cursor(latest, "file")
            store.save_changes(latest)
        reply["continuation"] = f"{run['run_id']}:{token}"
    return reply
