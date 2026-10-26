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
import stat
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import urlencode

from . import adopt as adopt_module
from . import (
    adoption_plan,
    context_refs,
    due_state,
    held_fs,
    knowledge_packs,
    note_types,
    request_budget,
    reserved_paths,
    source_taxonomy,
)
from . import overview as overview_module
from .adopt import _TEXT_IMPORT_SUFFIXES
from .kbdir import kb_dirname
from .vault import PlannedWrite, batch_atomic_write, kb_root, parse_frontmatter

RUNS_DIR = "_Adoption/runs"
SCHEMA_VERSION = 1
# The plan layout `apply` understands. A plan made by an earlier layout is refused as stale.
PLAN_FORMAT = 2
MAX_CANDIDATES_DEFAULT = 5000
# Where Exomem Cloud stages an uploaded collection (see `cloud_import_job`).
IMPORT_STAGE_DIR = "_Imports"
# One `apply` batch holds at most this many items or bytes of originals (the
# batch writer re-validates the whole batch per write, so cost grows with size).
# 25 items is about 2-4 s on the measured vaults: a small share of the 50 s call.
APPLY_CHUNK_ITEMS = 25
APPLY_CHUNK_BYTES = 64 * 1024 * 1024
# A chunk is also cut so its predicted cost stays at most this share of the call's budget.
APPLY_CHUNK_BUDGET_SHARE = 0.1
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

_APPLIED_STATUSES = ("applied", "already-applied")
# An own-note item whose Source is on disk and whose draft note is still owed. The item is
# not complete: `apply` resumes it at the note, and the phase stays `applying` until it is.
_SOURCE_ONLY = "source-applied"
# The values of a rule's `adopt_as` are fixed by the rule schema, not by the vault.
_ADOPT_AS = ("source", "note")  # nosemgrep: ep-word-set -- the closed values of the rule schema's `adopt_as`.
# Per-item failures that mean "the original is not what the plan read".
_DRIFT_CODES = frozenset({"SOURCE_CHANGED", "NOT_FOUND"})
_RECALL_SAMPLE = 10

_REASONS = {
    "NOT_IN_INVENTORY": "path is not among the scanned adoption candidates",
    "UNSUPPORTED_IMPORT_TYPE": "the scan did not mark this file importable",
    "ALREADY_GOVERNED": f"path is already inside {kb_dirname()}/",
    "TOO_LARGE": "file is larger than the artifact ceiling",
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
class AdoptionRunStore:
    """Load/save the canonical run objects under ``_Adoption/runs/<run_id>/``."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.base = kb_root(self.vault_root) / "_Adoption" / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.base / run_id

    def _run_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def load(self, run_id: str | None) -> dict:
        if not run_id:
            raise AdoptionRunError("RUN_NOT_FOUND", "an adoption run_id is required")
        f = self._run_file(run_id)
        if not f.exists():
            raise AdoptionRunError("RUN_NOT_FOUND", f"no adoption run {run_id!r}")
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdoptionRunError("RUN_NOT_FOUND", f"cannot read run {run_id!r}: {exc}") from exc

    def planned_write(self, run: dict) -> PlannedWrite:
        """The run object as a write, so a caller can commit it inside a larger batch."""
        run["updated"] = _now_iso()
        content = json.dumps(run, indent=2, ensure_ascii=False, default=str) + "\n"
        return PlannedWrite(path=self._run_file(run["run_id"]), content=content)

    def save(self, run: dict) -> None:
        batch_atomic_write([self.planned_write(run)], vault_root=self.vault_root)

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
                run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
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


def build_inventory(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    ceiling: int | None = None,
) -> tuple[list[dict], int, int]:
    """Return ``(rows, truncated, hidden_files)`` — every adoption candidate outside the KB.

    Each file row carries the vault-relative path, byte size, mtime, import
    ``form`` (``text`` for a text-like suffix, ``artifact`` for any other file),
    an ``eligible`` flag, and a machine-readable ``reason`` when ineligible
    (never silently dropped). A skipped hidden folder is one ``kind: folder`` row
    with its file count, kept outside the cap; ``hidden_files`` counts skipped
    hidden files. Rows are stat-only (no file reads); per-source sha256 is
    deferred to ``plan``. File rows are capped at ``max_candidates`` with the
    overflow reported in ``truncated``.
    """
    from . import client_artifacts

    root = Path(root)
    kb_name = kb_dirname()
    limit = client_artifacts.fetch_budget().max_file_bytes if ceiling is None else ceiling
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
                if rel_dir == kb_name or rel_dir.startswith(kb_name + "/"):
                    continue
                if dpath.is_symlink():
                    rows.append(_row(rel_dir, 0, 0.0, eligible=False, reason="NOT_REGULAR_FILE"))
                    continue
                if not include_hidden and d.startswith("."):
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
                if not include_hidden and fn.startswith("."):
                    hidden_files += 1
                    continue
                fpath = Path(dirpath) / fn
                rel = _rel(root, fpath)
                if rel == kb_name or rel.startswith(kb_name + "/"):
                    continue
                try:
                    st = fpath.lstat()
                except OSError:
                    continue
                reason = None
                if not stat.S_ISREG(st.st_mode):
                    reason = "NOT_REGULAR_FILE"
                elif st.st_size > limit:
                    reason = "TOO_LARGE"
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
    rows.sort(key=lambda r: r["path"])
    truncated = 0
    if len(rows) > max_candidates:
        truncated = len(rows) - max_candidates
        rows = rows[:max_candidates]
    folders.sort(key=lambda r: r["path"])
    return rows + folders, truncated, hidden_files


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
    for p in selected:
        fpath = root / p
        row = inv.get(p)
        if not fpath.exists():
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
    kb_name = kb_dirname()
    if path == kb_name or path.startswith(kb_name + "/"):
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
            f"source_kind {kind.key!r} is not in the Source taxonomy; "
            "register it in _Schema/source-taxonomy.yaml first",
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
_INTERNAL_KEYS = frozenset({"plan_draft", "cursors", "inflight", "scope"})


def _present(root: Path, run: dict, **extra) -> dict:
    out = {key: value for key, value in run.items() if key not in _INTERNAL_KEYS}
    if "pack_suggestions" in run:
        # The run stores the spelling of the surface that started it; serve this surface's.
        out["pack_suggestions"] = knowledge_packs.rendered_suggestions(run["pack_suggestions"] or [])
    out["next_actions"] = _next_actions(run)
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
    out.update(extra)
    return out


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
        actions.append(
            {"action": "apply", "status": "available",
             "description": "Apply stopped with work left; call apply again with the continuation."}
        )
    elif phase in ("applied", "partial"):
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
    return actions


def _handoff(run: dict) -> dict:
    run_id = run["run_id"]
    run_ref = run["run_ref"]
    prompt_text = (
        f"Continue my Exomem adoption run {run_id}. "
        f'Call adoption_studio(action="work-item", run_id="{run_id}") to load the bounded, '
        "read-only context, then submit structured proposals via "
        f'adoption_studio(action="propose", run_id="{run_id}"). Run ref: {run_ref}.'
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
def _collection_of(root: Path, source_root: str, label: str | None) -> dict:
    """The collection's label, and the folder its files' collection paths hang from.

    A staged Cloud upload holds its collection in the one folder inside
    ``_Imports/<import_id>/``; a re-upload gets a new import id, so the inner folder
    is what keeps ``collection_path`` stable across uploads.
    """
    parts = source_root.split("/") if source_root else []
    collection_root = source_root
    default = parts[-1] if parts else Path(root).name
    if len(parts) == 2 and parts[0] == IMPORT_STAGE_DIR:
        try:
            entries = [e for e in (root / source_root).iterdir() if not e.name.startswith(".")]
        except OSError:
            entries = []
        if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
            collection_root = f"{source_root}/{entries[0].name}"
            default = entries[0].name
    chosen = (label or "").strip() or default
    return {"label": adoption_plan.nfc(chosen), "root": collection_root}


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


def start(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    initialize_kb: bool = False,
    collection: str | None = None,
    today: dt.date | None = None,
) -> dict:
    root = Path(root)
    _refuse_outside_vault(root, path)
    run_date = _today(today)
    kb = root / kb_dirname()
    if not kb.is_dir():
        if not initialize_kb:
            raise AdoptionRunError(
                "KB_NOT_INITIALIZED",
                f"{kb_dirname()}/ is required; pass initialize_kb=true to bootstrap the scaffold first",
            )
        from . import init as init_module

        try:
            init_module.init_vault(root)
        except FileExistsError:
            pass

    try:
        scan = overview_module.overview(root, path=path, include_hidden=include_hidden)
    except overview_module.OverviewError as exc:
        raise AdoptionRunError(exc.code, exc.reason) from exc

    rows, truncated, hidden_files = build_inventory(
        root, path=path, include_hidden=include_hidden
    )
    inv_fp = inventory_fingerprint(rows)
    created_iso = _now_iso()
    source_root = _clean(path)
    run_id = new_run_id(source_root, created_iso, inv_fp, today=run_date)
    junk = scan.get("junk") or {}
    junk_paths = sorted({_clean(p) for p in (junk.get("zero_byte") or [])})

    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_ref": context_refs.adoption_run_ref(run_id),
        "created": created_iso,
        "updated": created_iso,
        "phase": "selecting",
        "source_root": source_root,
        "collection": _collection_of(root, source_root, collection),
        "scan_summary": {
            "totals": scan.get("totals", {}),
            "kb": scan.get("kb", {}),
            "junk_counts": junk.get("counts", {}),
            "junk_paths": junk_paths,
            "skipped": scan.get("skipped", {}),
        },
        "pack_suggestions": knowledge_packs.suggest_packs(scan, limit=6),
        "inventory": rows,
        "inventory_truncated": truncated,
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
    store = AdoptionRunStore(root)
    with _LOCK:
        store.save(run)
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


def status(root: Path, *, run_id: str | None = None, path: str = "") -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
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
    stale = probe_staleness(root, run)
    return _present(
        root,
        run,
        staleness={"stale_paths": stale},
        stale_paths=stale,
        proposals_summary=_proposals_summary(store, run_id),
        handoff=_handoff(run),
        **extra,
    )


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
            entry = None
            _supersede_cursors(run, "plan")
            draft = {
                "selection_hash": sel_hash,
                "date": _today(today).isoformat(),
                "items": [],
                "skipped": [],
                "next": 0,
            }
            run["plan"] = None
            run["phase"] = "selecting"

        collection = run.get("collection") or {"label": None, "root": ""}
        classification = selection.get("classification") or {}
        taxonomy = source_taxonomy.load_taxonomy(root) if classification else None
        inventory = {row["path"]: row for row in run.get("inventory") or []}
        ceiling = client_artifacts.fetch_budget().max_file_bytes
        index = adoption_plan.read_import_index(root)
        notes = adoption_plan.NoteIndex(root)
        destinations = adoption_plan.Destinations(root)
        for planned in draft["items"]:
            frozen_paths = [(planned.get("note") or {}).get("target_path")]
            if planned["action"] == "copy-as-source":
                frozen_paths += [planned["target_path"], planned.get("artifact_path")]
            for frozen in frozen_paths:
                if frozen:
                    destinations.reserve(frozen)

        budget = _call_budget()
        handled = 0
        while draft["next"] < len(selected):
            if handled and not budget.can_afford(request_budget.DELIVERY_RESERVE_SECONDS):
                break
            rel = selected[draft["next"]]
            result = adoption_plan.plan_item(
                root, rel,
                collection=collection["label"], collection_root=collection["root"],
                classification=classification.get(rel), taxonomy=taxonomy,
                text_hint=(inventory.get(rel) or {}).get("form", "text") == "text",
                date_iso=draft["date"], destinations=destinations, index=index, notes=notes,
                ceiling=ceiling,
            )
            draft["next"] += 1
            handled += 1
            if isinstance(result, dict):
                draft["skipped"].append(result)
            else:
                draft["items"].append(result[0])

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
        adoption_plan.resolve_links(items, index, collection=collection["label"])
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
        run["plan_draft"] = None
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


def _read_original(root: Path, planned: dict, ceiling: int) -> tuple[str | None, dict | None]:
    """The original's text (None for an artifact), or a failed outcome when it drifted."""
    read = adoption_plan.read_facts(
        root, planned["original_path"], ceiling=ceiling, text_hint=planned["form"] == "text"
    )
    if isinstance(read, dict):
        code = "SOURCE_CHANGED" if read["code"] == "TOO_LARGE" else read["code"]
        return None, _failed(code, read["reason"])
    facts, text = read
    if (
        facts["original_sha256"] != planned["original_sha256"]
        or facts["original_bytes"] != planned["original_bytes"]
        or (planned["form"] == "text" and text is None)
    ):
        return None, _failed("SOURCE_CHANGED", "sha256 mismatch at apply time")
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


class _ApplyPacer:
    """Decides how much of the plan one `apply` call may start, from what its chunks cost.

    A call never starts a chunk whose predicted cost does not fit in the time left
    before the delivery reserve, after the fan-out the call already owes past the
    loop. A chunk costs its items, its bytes and its media requests. Costs are those
    this call has measured, not a past call's, and each rate is the dearest seen so
    far: chunk cost swings several-fold with media artifacts and a contended index,
    so the last chunk is a poor guide to the next. A slow stretch shrinks the rest
    of this call's chunks and nothing after it.
    """

    def __init__(self, root: Path, budget: request_budget.RequestBudget) -> None:
        from .writer_lease import active_derived_batch_custody

        self._budget = budget
        self._item_seconds: float | None = None
        self._mib_seconds: float | None = None
        self._extraction_seconds: float | None = None
        self._deferred_artifacts = 0
        self._started_items = 0
        # A fast-acknowledgement session runs the sidecar fan-out behind the reply.
        self._owes_fanout = not active_derived_batch_custody(root)

    @staticmethod
    def _tracked(rate: float | None, seen: float) -> float:
        return seen if rate is None else max(rate, seen)

    def _owed_after_loop(self, artifacts: int) -> float:
        if not self._owes_fanout:
            return TERMINAL_WORK_RESERVE_SECONDS
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
        return len(chunk) * item + self._mib(chunk) * per_mib + self._media(chunk) * extraction

    def _room(self, chunk: list[dict]) -> float:
        """Seconds the chunk may take: what is left, less the reserve and the call's debts."""
        return (
            self._budget.remaining() - request_budget.DELIVERY_RESERVE_SECONDS
            - REPLY_SECONDS_PER_ITEM * (self._started_items + len(chunk))
            - self._owed_after_loop(self._deferred_artifacts + self._media(chunk))
        )

    def next_chunk(self, pending: list[dict], *, first: bool) -> list[dict]:
        """The next chunk, trimmed to fit; empty when none fits. The first is never empty."""
        share = APPLY_CHUNK_BUDGET_SHARE * self._budget.seconds
        chunk = _take_chunk(pending, APPLY_CHUNK_ITEMS)
        cost, allowed = self._cost(chunk), min(share, self._room(chunk))
        while len(chunk) > 1 and cost > allowed:
            chunk = chunk[: max(1, int(len(chunk) * allowed / cost))]
            cost, allowed = self._cost(chunk), min(share, self._room(chunk))
        return chunk if first or cost <= self._room(chunk) else []

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
            self._item_seconds = self._tracked(self._item_seconds, committing / len(chunk))
        if extractions:
            self._extraction_seconds = self._tracked(
                self._extraction_seconds, sum(extractions) / len(extractions)
            )
            self._deferred_artifacts += len(extractions)


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

    Walking every Source for each chunk made a re-run cost grow with the whole vault.
    Each starts empty and fills on first use; a batch that raised drops the Source
    reads, because what it left on disk is then unknown. A batch writes no note, so
    the note index stays.
    """

    index: list[adoption_plan.EarlierImport] | None = None
    source_counts: dict[str, int] | None = None
    notes: adoption_plan.NoteIndex | None = None


def _commit_chunk(
    root: Path, store: AdoptionRunStore, run: dict,
    survivors: list[tuple[dict, adopt_module.ImportItem]], *,
    run_date: dt.date, label: str | None, reads: _CallReads,
) -> list[dict]:
    """Commit one chunk's pages, artifacts, indexes, log and the run object in one batch.

    Returns the items now on disk. A batch that raises may still have committed, so
    each item is resolved by the same identity check the next call would use; the
    rest fail as retryable, and the call's closing save records them.
    """
    outcomes = run["outcomes"]
    for planned, _item in survivors:
        outcomes[planned["original_path"]] = _applied(planned)
        _hold_for_note(outcomes[planned["original_path"]], planned)
    try:
        committed = adopt_module.commit_import_items(
            root, [item for _planned, item in survivors], today=run_date,
            extra_writes=[store.planned_write(run)], source_counts=reads.source_counts,
        )
    except Exception as error:  # noqa: BLE001 - a rolled-back batch is a retryable outcome
        reads.source_counts = None
        reads.index = adoption_plan.read_import_index(root)
        landed: list[dict] = []
        for planned, _item in survivors:
            earlier = _earlier_with_same_bytes(root, reads.index, label, planned)
            if earlier is not None:
                outcomes[planned["original_path"]] = _already_applied(
                    earlier.path, adoption_plan.artifact_of(root, earlier.path),
                    planned["original_sha256"],
                )
                _hold_for_note(outcomes[planned["original_path"]], planned)
                landed.append(planned)
            else:
                if isinstance(error, adopt_module.AdoptError):
                    failure = {"code": error.code, "reason": error.reason}
                elif isinstance(
                    error, (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)
                ) or getattr(error, "code", None) == "PATH_GUARD_CONTENT":
                    failure = adoption_plan.source_failure(error)
                else:
                    failure = {"code": "BATCH_ROLLED_BACK", "reason": str(error)}
                outcomes[planned["original_path"]] = _failed(**failure)
        return landed
    reads.source_counts = committed["source_counts"]
    return [planned for planned, _item in survivors]


def _apply_chunk(
    root: Path, store: AdoptionRunStore, run: dict, chunk: list[dict], *,
    run_date: dt.date, ceiling: int, budget: request_budget.RequestBudget, stats: Counter,
    written: list[str], reads: _CallReads, pacer: _ApplyPacer,
) -> list[float]:
    """Apply one chunk; returns the seconds each media extraction request took."""
    plan = run["plan"]
    outcomes = run["outcomes"]
    label = (run.get("collection") or {}).get("label")
    if reads.index is None:
        reads.index = adoption_plan.read_import_index(root)
    index = reads.index
    settled: list[dict] = []
    survivors: list[tuple[dict, adopt_module.ImportItem]] = []
    texts: dict[str, str] = {}
    for planned in chunk:
        path = planned["original_path"]
        text, failure = _read_original(root, planned, ceiling)
        if failure is not None:
            outcomes[path] = failure
            continue
        if text is not None and _owes_note(planned):
            texts[path] = text
        if path in outcomes and outcomes[path]["status"] == _SOURCE_ONLY:
            continue  # an earlier call wrote its Source; only the note is owed
        if planned["action"] == "already-imported":
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
        occupied = [
            target for target in (planned["target_path"], planned.get("artifact_path"))
            if target and os.path.lexists(root / target)
        ]
        if occupied:
            outcomes[path] = _failed(
                "TARGET_OCCUPIED",
                f"{occupied[0]} already exists and is not an import of this file",
            )
            continue
        survivors.append(
            (planned, adopt_module.import_item(
                root, planned, text=text, date_iso=plan["date"], collection=label
            ))
        )
    landed: list[dict] = []
    if survivors:
        landed = _commit_chunk(
            root, store, run, survivors, run_date=run_date, label=label, reads=reads
        )
        for planned in landed:
            if _source_status(outcomes[planned["original_path"]]) == "applied":
                reads.index.append(
                    adoption_plan.earlier_import_of(planned, collection=label, captured=plan["date"])
                )
    for planned in [*settled, *landed]:
        _record_unchanged(root, planned, outcomes[planned["original_path"]], stats)
    extractions: list[float] = []
    copied: list[str] = []
    for planned in landed:
        if _source_status(outcomes[planned["original_path"]]) == "applied":
            copied.append(planned["target_path"])
        if planned["form"] == "artifact":
            seconds = _request_extraction(root, planned, budget, stats)
            if seconds is not None:
                extractions.append(seconds)
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


def _create_notes(
    root: Path, store: AdoptionRunStore, run: dict, chunk: list[dict], texts: dict[str, str], *,
    label: str | None, notes: adoption_plan.NoteIndex, pacer: _ApplyPacer,
    stats: Counter, written: list[str],
) -> None:
    """Create the draft note of every own-note item in `chunk` whose Source is on disk.

    One note at a time, each through the governed two-phase creation. The call stops
    between two notes when the next would not fit in the time left; the items it leaves
    keep their `source-applied` outcome and the continuation resumes at them. A call
    always makes at least one note, so a budget below one note's cost still moves.
    """
    outcomes = run["outcomes"]
    created_any = False
    for planned in chunk:
        path = planned["original_path"]
        if path not in outcomes or outcomes[path]["status"] != _SOURCE_ONLY:
            continue
        if stats["notes_started"] and not pacer.note_fits(chunk, stats["note_ms_max"] / 1000):
            stats["notes_deferred"] += 1
            break
        started = time.monotonic()
        _create_note(
            root, planned, texts[path], outcomes[path],
            label=label, notes=notes, date_iso=run["plan"]["date"], stats=stats, written=written,
        )
        stats["notes_started"] += 1
        stats["note_ms_max"] = max(stats["note_ms_max"], int((time.monotonic() - started) * 1000))
        created_any = True
    if created_any:
        # The outcomes are committed with the chunk's Sources and again here, so a
        # crash costs the identity check at most one chunk of notes.
        store.save(run)


def _create_note(
    root: Path, planned: dict, text: str, outcome: dict, *,
    label: str | None, notes: adoption_plan.NoteIndex, date_iso: str, stats: Counter,
    written: list[str],
) -> None:
    """Find or create one item's draft note, then settle the item on the result."""
    from . import note as note_module

    frozen = (planned.get("note") or {}).get("target_path")
    source_page = outcome["target_path"]

    def found() -> str | None:
        return adoption_plan.find_note(
            root, notes, collection=label, collection_path=planned["collection_path"],
            frozen=frozen,
        )

    existing = found()
    if existing is not None:
        _settle_note(outcome, {"status": "already-applied", "target_path": existing, "at": _now_iso()})
        stats["notes_found"] += 1
        return
    if frozen is None:
        _fail_note(outcome, "PLAN_STALE", "the note an earlier run made is gone; re-plan this import")
        stats["notes_failed"] += 1
        return
    if os.path.lexists(root / frozen):
        _fail_note(
            outcome, "TARGET_OCCUPIED", f"{frozen} already exists and is not an import of this file"
        )
        stats["notes_failed"] += 1
        return
    try:
        created = adopt_module.create_draft_note(
            root, planned, text, source_path=source_page, collection=label, date_iso=date_iso
        )
    except Exception as error:  # noqa: BLE001 - a refused note is this item's outcome; its Source stays
        # A failure after the commit still left the note: the identity check records it.
        existing = found()
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

    root = Path(root)
    run_date = _today(today)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        phase = run["phase"]
        # apply is re-runnable from any post-plan phase: `applying` resumes an interrupted
        # or budget-stopped run, and a finished one answers `already-applied`.
        if phase not in ("planned", "applying", "applied", "partial", "failed"):
            raise AdoptionRunError("INVALID_PHASE", f"apply is not allowed from phase {phase!r}")
        plan = run.get("plan")
        if not plan:
            raise AdoptionRunError("PLAN_NOT_FOUND", "no plan on this run; call plan first")
        if plan_id != plan.get("plan_id") or plan.get("plan_format") != PLAN_FORMAT:
            raise AdoptionRunError("PLAN_STALE", "plan_id does not match the current plan")
        if plan.get("selection_hash") != (run.get("selection") or {}).get("selection_hash"):
            raise AdoptionRunError("PLAN_STALE", "selection changed since the plan was created")

        entry = None
        if continuation:
            entry = _cursor_entry(run, "apply", continuation)
            if entry["state"] == "used":
                return _present(root, run, **(entry.get("reply") or {}))

        plan_items = plan["items"]
        by_path = {it["original_path"]: it for it in plan_items}
        outcomes = dict(run.get("outcomes") or {})
        before = {
            "phase": phase, "outcomes": dict(outcomes), "scope": run.get("scope"),
            "cursors": copy.deepcopy(run.get("cursors")), "inflight": run.get("inflight"),
        }
        stats: Counter = Counter()
        fresh = entry is None
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
            scope = None if run["scope"] is None else set(run["scope"])
            # A repeat call re-checks what an earlier call applied: the originals stay unchanged.
            for path in list(outcomes):
                in_scope = scope is None or path in scope
                if in_scope and path in by_path and outcomes[path].get("status") in _APPLIED_STATUSES:
                    outcomes[path] = {**outcomes[path], "status": "already-applied"}
                    _record_unchanged(root, by_path[path], outcomes[path], stats)
        run["outcomes"] = outcomes

        budget = _call_budget()
        ceiling = client_artifacts.fetch_budget().max_file_bytes
        written: list[str] = []
        pending = _pending(run)
        if pending:
            # Held in memory until a chunk commits it: a call whose every source drifted
            # refuses below with nothing written, as before.
            run["phase"] = "applying"
            run["inflight"] = True

        pacer = _ApplyPacer(root, budget)
        reads = _CallReads()
        stopped = False
        chunks = 0
        while pending:
            chunk = pacer.next_chunk(pending, first=not chunks)
            if not chunk:
                stopped = True
                break
            started = time.monotonic()
            extractions = _apply_chunk(
                root, store, run, chunk, run_date=run_date, ceiling=ceiling,
                budget=budget, stats=stats, written=written, reads=reads, pacer=pacer,
            )
            pacer.record(chunk, time.monotonic() - started, extractions)
            pending = pending[len(chunk):]
            chunks += 1
            if stats["notes_deferred"]:
                stopped = True
                break

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
            raise AdoptionRunError(
                "ADOPTION_SOURCE_CHANGED",
                "every requested source changed or is missing since the plan was captured; "
                "re-scan and re-plan to continue",
            )

        run["phase"] = _recompute_phase(outcomes, plan_items, stopped)
        run["inflight"] = False
        # The run keeps the cumulative counts; this call reports those of the originals it handled.
        unchanged, checked = _verify_counts(outcomes)
        run["verify"] = {
            "verified_unchanged": unchanged, "verified_total": checked, "at": _now_iso()
        }
        call_counts = {
            "verified_unchanged": stats["verified_unchanged"],
            "verified_total": stats["verified_total"],
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
            },
            "progress": _progress(run),
        }
        if stopped:
            reply["continuation"] = _issue_cursor(run, "apply")
        if entry is not None:
            entry["state"], entry["reply"] = "used", reply
        store.save(run)
    return _present(root, run, **reply)


def cancel(root: Path, *, run_id: str, why: str | None) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
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
        run["cancel"] = {"at": _now_iso(), "why": (why or "").strip() or None}
        run["phase"] = "cancelled"
        store.save(run)
    return _present(root, run)


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


def _find_paths(root: Path, query: str) -> list[str] | None:
    """Paths of the first keyword hits for `query`, or None when search failed."""
    from . import find as find_module

    try:
        hits = find_module.find(root, query=query, mode="keyword", limit=5, graph=False)
    except Exception:  # noqa: BLE001 - recall failure never blocks finish
        return None
    return [str(getattr(hit, "path", "")) for hit in hits]


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
    paths = _find_paths(root, title) if title else None
    ok = bool(target) and any(_same_page(path, target) for path in paths or [])
    result: dict = {"query": title, "ok": ok, "hits": paths or []}
    if not ok:
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
    for item, outcome in picks:
        paths = _find_paths(root, item.get("title") or item["original_path"])
        if paths is not None and any(_same_page(path, outcome["target_path"]) for path in paths):
            found += 1
        else:
            missed.append(outcome["target_path"])
    return {"sampled": len(picks), "found": found, "missed": missed}


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
    applied_text = [item for item, _outcome in _applied_items(run) if item["form"] == "text"]
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
        "limits": [
            "Inline #tags in a note body are not read; only front-matter tags and aliases are kept."
        ],
    }


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
        run = store.load(run_id)
        if run["phase"] not in ("applied", "partial"):
            raise AdoptionRunError(
                "INVALID_PHASE", f"finish requires phase 'applied' or 'partial', got {run['phase']!r}"
            )
        recall = _recall_check(root, run)
        coverage = _coverage(root, run, _call_budget())
        # The counts the last `apply` recorded from its own re-hash — never a fresh
        # re-hash at finish time, which could disagree with what was verified then.
        verify = run.get("verify") or {}
        verified_unchanged = verify.get("verified_unchanged", 0)
        verified_total = verify.get("verified_total", 0)
        manifest_path = None
        if write_manifest:
            manifest_path = _write_run_manifest(
                root, run, recall=recall, verified_unchanged=verified_unchanged,
                verified_total=verified_total, today=run_date,
            )
        title = recall["query"]
        run["finish"] = {
            "at": _now_iso(),
            "recall_check": recall,
            "coverage": coverage,
            "first_question": f'What do my notes say about "{title}"?',
            "route": {"tool": "search_memory", "args": {"query": title}},
            "handoff": _handoff(run),
            "verified_unchanged": verified_unchanged,
            "verified_total": verified_total,
            "manifest_path": manifest_path,
        }
        run["phase"] = "done"
        store.save(run)
    return _present(root, run, coverage=coverage)


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
