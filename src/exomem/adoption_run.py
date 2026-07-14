"""Adoption Studio run lifecycle: canonical-file-backed, deterministic, governed.

A durable adoption run is a JSON object under
``Knowledge Base/_Adoption/runs/<run_id>/`` written only through
``vault.batch_atomic_write`` — so it inherits the access-tier backstop and the
writer-lease fence exactly like every other governed write. The run is fully
reconstructable after a process restart with no dependence on any rebuildable
sidecar. Originals outside ``Knowledge Base/`` are never rewritten, moved, or
deleted under any action or failure: imports are copies with provenance, and
scan/select/plan write nothing outside the run object.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

from . import adopt as adopt_module
from . import context_refs, knowledge_packs
from . import overview as overview_module
from .adopt import _TEXT_IMPORT_SUFFIXES
from .kbdir import kb_dirname
from .vault import PlannedWrite, batch_atomic_write, kb_root

RUNS_DIR = "_Adoption/runs"
SCHEMA_VERSION = 1
MAX_CANDIDATES_DEFAULT = 5000
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

_REASONS = {
    "NOT_IN_INVENTORY": "path is not among the scanned adoption candidates",
    "UNSUPPORTED_IMPORT_TYPE": "adoption imports text/markdown-like files only",
    "ALREADY_GOVERNED": f"path is already inside {kb_dirname()}/",
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


# --------------------------------------------------------------------------- #
# id / fingerprint helpers
# --------------------------------------------------------------------------- #
def inventory_fingerprint(rows: list[dict]) -> str:
    """sha256[:24] over the sorted ``(path|bytes|mtime)`` of eligible rows."""
    eligible = sorted((r for r in rows if r.get("eligible")), key=lambda r: r["path"])
    payload = "\n".join(f"{r['path']}|{r['bytes']}|{r['mtime']!r}" for r in eligible)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def selection_hash(paths: list[str]) -> str:
    """sha256[:16] over the sorted selected paths."""
    payload = "\n".join(sorted(paths))
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

    def save(self, run: dict) -> None:
        run["updated"] = _now_iso()
        content = json.dumps(run, indent=2, ensure_ascii=False, default=str) + "\n"
        batch_atomic_write(
            [PlannedWrite(path=self._run_file(run["run_id"]), content=content)],
            vault_root=self.vault_root,
        )

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
        if not self.base.exists():
            return []
        rows: list[dict] = []
        for d in sorted(self.base.iterdir()):
            if not d.is_dir():
                continue
            f = d / "run.json"
            if not f.exists():
                continue
            try:
                run = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.append(_compact_row(run))
        rows.sort(key=lambda r: r.get("created", ""), reverse=True)
        return rows


def _compact_row(run: dict) -> dict:
    outcomes = run.get("outcomes") or {}
    applied = sum(1 for o in outcomes.values() if o.get("status") in _APPLIED_STATUSES)
    return {
        "run_id": run.get("run_id"),
        "run_ref": run.get("run_ref"),
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
def build_inventory(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> tuple[list[dict], int]:
    """Return ``(rows, truncated)`` — every adoption candidate outside the KB.

    Each row carries the vault-relative path, byte size, mtime, an ``eligible``
    flag, and a machine-readable ``reason`` when ineligible (never silently
    dropped). Rows are stat-only (no file reads); per-source sha256 is deferred
    to ``plan``. The list is capped at ``max_candidates`` with the overflow
    reported in the returned ``truncated`` count.
    """
    root = Path(root)
    kb_name = kb_dirname()
    scan_root = root if not _clean(path) else (root / _clean(path))
    rows: list[dict] = []
    if scan_root.is_dir():
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames.sort()
            filenames.sort()
            kept: list[str] = []
            for d in dirnames:
                if not include_hidden and d.startswith("."):
                    continue
                rel_dir = _rel(root, Path(dirpath) / d)
                if rel_dir == kb_name or rel_dir.startswith(kb_name + "/"):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                if not include_hidden and fn.startswith("."):
                    continue
                fpath = Path(dirpath) / fn
                rel = _rel(root, fpath)
                if rel == kb_name or rel.startswith(kb_name + "/"):
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                eligible = fpath.suffix.lower() in _TEXT_IMPORT_SUFFIXES
                rows.append(
                    {
                        "path": rel,
                        "bytes": st.st_size,
                        "mtime": st.st_mtime,
                        "eligible": eligible,
                        "reason": None if eligible else "UNSUPPORTED_IMPORT_TYPE",
                    }
                )
    rows.sort(key=lambda r: r["path"])
    truncated = 0
    if len(rows) > max_candidates:
        truncated = len(rows) - max_candidates
        rows = rows[:max_candidates]
    return rows, truncated


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


def _materialize_selection(
    run: dict,
    include: list[str],
    exclude: list[str],
    overrides: list[str],
    include_junk: bool,
) -> tuple[list[str], list[dict]]:
    """Materialize the concrete file set from folder rules, validating per-path."""
    eligible = {r["path"] for r in run.get("inventory") or [] if r.get("eligible")}
    junk = set(run.get("scan_summary", {}).get("junk_paths") or [])
    accepted: set[str] = set()
    rejected: list[dict] = []

    for inc in include:
        incn = _clean(inc)
        matches = {p for p in eligible if p == incn or p.startswith(incn + "/")}
        if matches:
            accepted |= matches
        else:
            rejected.append(_reject(incn, _classify_path(run, incn)))

    for exc in exclude:
        excn = _clean(exc)
        accepted = {p for p in accepted if not (p == excn or p.startswith(excn + "/"))}

    override_set = {_clean(o) for o in overrides}
    for ov in sorted(override_set):
        if ov in eligible:
            accepted.add(ov)
        else:
            rejected.append(_reject(ov, _classify_path(run, ov)))

    if not include_junk:
        accepted = {p for p in accepted if p not in junk or p in override_set}

    return sorted(accepted), rejected


# --------------------------------------------------------------------------- #
# presentation helpers
# --------------------------------------------------------------------------- #
def _present(root: Path, run: dict, **extra) -> dict:
    out = dict(run)
    out["next_actions"] = _next_actions(run)
    # Whenever the run carries a persisted `verify` block (set once by `apply`
    # right after its post-commit re-hash), surface the SAME recorded counts at
    # the top level of every presented document — never a live re-hash. This is
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
        if (run.get("selection") or {}).get("paths"):
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
            "claude": "claude://new?" + urlencode({"q": prompt_text}),
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
# lifecycle actions
# --------------------------------------------------------------------------- #
def start(
    root: Path,
    *,
    path: str = "",
    include_hidden: bool = False,
    initialize_kb: bool = False,
    today: dt.date | None = None,
) -> dict:
    root = Path(root)
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

    rows, truncated = build_inventory(root, path=path, include_hidden=include_hidden)
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
        "inventory_fingerprint": inv_fp,
        "selection": {
            "paths": [],
            "selection_hash": selection_hash([]),
            "rules": {"include": [], "exclude": [], "overrides": [], "include_junk": False},
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


def status(root: Path, *, run_id: str | None = None) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    if not run_id:
        return {"mode": "adoption", "mutated": False, "runs": store.list_runs()}
    run = store.load(run_id)
    outcomes = run.get("outcomes") or {}
    selected = (run.get("selection") or {}).get("paths") or []
    interrupted = run.get("phase") == "applying" and len(outcomes) < len(selected)
    return _present(
        root,
        run,
        staleness={"stale_paths": probe_staleness(root, run)},
        stale_paths=probe_staleness(root, run),
        interrupted=interrupted,
        proposals_summary=_proposals_summary(store, run_id),
        handoff=_handoff(run),
    )


def select(
    root: Path,
    *,
    run_id: str,
    include: list[str] | None = None,
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
        accepted, rejected = _materialize_selection(
            run, include or [], exclude or [], overrides or [], include_junk
        )
        run["selection"] = {
            "paths": accepted,
            "selection_hash": selection_hash(accepted),
            "rules": {
                "include": list(include or []),
                "exclude": list(exclude or []),
                "overrides": list(overrides or []),
                "include_junk": include_junk,
            },
            "updated": _now_iso(),
        }
        # A selection call always invalidates any existing plan.
        run["plan"] = None
        run["phase"] = "selecting"
        store.save(run)
    return _present(root, run, rejected=rejected)


def plan(root: Path, *, run_id: str, today: dt.date | None = None) -> dict:
    root = Path(root)
    run_date = _today(today)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        # Re-planning is always allowed (it replaces the plan); a fresh plan from
        # `planned` re-reads and re-hashes the current selection.
        if run["phase"] not in ("selecting", "planned"):
            raise AdoptionRunError(
                "INVALID_PHASE", f"plan requires phase 'selecting' or 'planned', got {run['phase']!r}"
            )
        selected = (run.get("selection") or {}).get("paths") or []
        if not selected:
            raise AdoptionRunError("MISSING_SELECTION", "plan requires a non-empty selection")

        items, skipped = adopt_module.plan_import_items(root, selected, today=run_date)
        plan_items: list[dict] = []
        for it in items:
            plan_items.append(
                {
                    "original_path": it.original_path,
                    "original_sha256": it.sha256,
                    "original_bytes": it.bytes,
                    "action": "copy-as-source",
                    "target_path": it.target_rel,
                    "target_ref": context_refs.source_ref(it.target_rel),
                    "title": it.title,
                    "frontmatter": {
                        "type": "source",
                        "source_type": "other",
                        "imported_from": it.original_path,
                        "original_sha256": it.sha256,
                        "original_bytes": it.bytes,
                        "tags": ["imported"],
                    },
                }
            )
            if it.slug_warning:
                skipped.append(
                    {"path": it.original_path, "code": "SLUG_TRUNCATED", "reason": it.slug_warning}
                )
        sel_hash = run["selection"]["selection_hash"]
        run["plan"] = {
            "plan_id": plan_id_for(sel_hash, [pi["original_sha256"] for pi in plan_items]),
            "created": _now_iso(),
            "selection_hash": sel_hash,
            "items": plan_items,
            "skipped": skipped,
            "warnings": [],
        }
        run["phase"] = "planned"
        store.save(run)
    return _present(root, run)


def apply(
    root: Path,
    *,
    run_id: str,
    plan_id: str | None,
    retry_failed: bool = False,
    only_paths: list[str] | None = None,
    today: dt.date | None = None,
) -> dict:
    root = Path(root)
    run_date = _today(today)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        phase = run["phase"]
        # apply is re-runnable (idempotent) from any post-plan, pre-terminal phase:
        # `applying` recovers an interrupted run; `applied`/`partial`/`failed`
        # replay safely because already-applied detection + write-time
        # re-validation make re-entry safe. `retry_failed` gates re-attempting
        # previously failed items.
        if phase not in ("planned", "applying", "applied", "partial", "failed"):
            raise AdoptionRunError("INVALID_PHASE", f"apply is not allowed from phase {phase!r}")
        plan = run.get("plan")
        if not plan:
            raise AdoptionRunError("PLAN_NOT_FOUND", "no plan on this run; call plan first")
        if plan_id != plan.get("plan_id"):
            raise AdoptionRunError("PLAN_STALE", "plan_id does not match the current plan")
        if plan.get("selection_hash") != (run.get("selection") or {}).get("selection_hash"):
            raise AdoptionRunError("PLAN_STALE", "selection changed since the plan was created")

        outcomes = dict(run.get("outcomes") or {})
        plan_items = plan["items"]

        if retry_failed:
            failed = {p for p, o in outcomes.items() if o.get("status") == "failed"}
            if only_paths:
                failed &= {_clean(p) for p in only_paths}
            target_items = [it for it in plan_items if it["original_path"] in failed]
        elif only_paths:
            wanted = {_clean(p) for p in only_paths}
            target_items = [it for it in plan_items if it["original_path"] in wanted]
        else:
            target_items = list(plan_items)

        # Write-time re-validation; already-applied items are idempotent skips.
        validated: list[str] = []
        for it in target_items:
            op = it["original_path"]
            existing = outcomes.get(op)
            if existing and existing.get("status") in _APPLIED_STATUSES and not retry_failed:
                outcomes[op] = {**existing, "status": "already-applied"}
                continue
            if existing and existing.get("status") == "failed" and not retry_failed:
                # Leave a prior failure untouched on a plain re-apply; only
                # retry_failed re-attempts it.
                continue
            fpath = root / op
            if not fpath.exists():
                outcomes[op] = {
                    "status": "failed", "code": "NOT_FOUND",
                    "reason": "original missing at apply time", "at": _now_iso(),
                }
                continue
            if hashlib.sha256(fpath.read_bytes()).hexdigest() != it["original_sha256"]:
                outcomes[op] = {
                    "status": "failed", "code": "SOURCE_CHANGED",
                    "reason": "sha256 mismatch at apply time", "at": _now_iso(),
                }
                continue
            validated.append(op)

        # Run-level stale-conflict refusal, distinct from PLAN_STALE (a plan
        # identity mismatch): a plain, whole-plan apply attempt whose write-time
        # re-validation finds NOTHING left to commit, on a run that has never
        # successfully applied anything, means every requested source drifted or
        # vanished since the plan was captured. Refuse before any write so the
        # still-valid selection survives a re-scan/re-plan (design.md's pinned
        # ADOPTION_SOURCE_CHANGED vocabulary) — a scoped retry_failed/only_paths
        # call that still fails one stubborn item (while others already applied)
        # is NOT this case; that stays a normal partial response.
        has_any_applied = any(o.get("status") in _APPLIED_STATUSES for o in outcomes.values())
        if not retry_failed and not only_paths and target_items and not validated and not has_any_applied:
            raise AdoptionRunError(
                "ADOPTION_SOURCE_CHANGED",
                "every requested source changed or is missing since the plan was captured; "
                "re-scan and re-plan to continue",
            )

        # Persist the transient `applying` phase BEFORE the first item write so a
        # crash mid-apply is visible to `status`.
        run["phase"] = "applying"
        run["outcomes"] = outcomes
        store.save(run)

        if validated:
            fresh_items, _sk = adopt_module.plan_import_items(root, validated, today=run_date)
            try:
                result = adopt_module.commit_import_items(root, fresh_items, today=run_date)
            except Exception as exc:  # noqa: BLE001 — batch rollback is a retryable outcome
                for op in validated:
                    outcomes[op] = {
                        "status": "failed", "code": "BATCH_ROLLED_BACK",
                        "reason": str(exc), "at": _now_iso(),
                    }
            else:
                by_path = {c["original_path"]: c for c in result["copied_sources"]}
                for op in validated:
                    c = by_path.get(op)
                    if c is None:
                        outcomes[op] = {
                            "status": "failed", "code": "NOT_FOUND",
                            "reason": "import produced no Source", "at": _now_iso(),
                        }
                        continue
                    outcomes[op] = {
                        "status": "applied",
                        "target_path": c["source_path"],
                        "source_ref": c["source_ref"],
                        "sha256": c["original_sha256"],
                        "at": _now_iso(),
                    }

        run["outcomes"] = outcomes
        run["phase"] = _recompute_phase(outcomes, plan_items)
        # Post-apply re-hash of applied originals, persisted as the run's single
        # source of truth for verification counts — never recomputed live by a
        # later `status`/`finish` call (see `_present`).
        verified_unchanged, verified_total = _verify_originals(root, plan_items, outcomes)
        run["verify"] = {
            "verified_unchanged": verified_unchanged,
            "verified_total": verified_total,
            "at": _now_iso(),
        }
        store.save(run)
    return _present(
        root,
        run,
        apply_result={"verified_unchanged": verified_unchanged, "verified_total": verified_total},
    )


def cancel(root: Path, *, run_id: str, why: str | None) -> dict:
    root = Path(root)
    store = AdoptionRunStore(root)
    with _LOCK:
        run = store.load(run_id)
        phase = run["phase"]
        if phase == "applying":
            raise AdoptionRunError("CANCEL_DURING_APPLY", "cannot cancel while an apply is in flight")
        if phase in ("applied", "done"):
            raise AdoptionRunError(
                "ALREADY_APPLIED", "run already applied; applied Sources survive (append-only)"
            )
        if phase not in ("selecting", "planned", "partial", "failed"):
            raise AdoptionRunError("INVALID_PHASE", f"cancel is not allowed from phase {phase!r}")
        run["cancel"] = {"at": _now_iso(), "why": (why or "").strip() or None}
        run["phase"] = "cancelled"
        store.save(run)
    return _present(root, run)


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
        # Reuse the SAME real counts the last `apply` call recorded — never a
        # fresh re-hash at finish time, which could disagree with what was
        # actually verified immediately after the commit.
        verify = run.get("verify") or {}
        verified_unchanged = verify.get("verified_unchanged", 0)
        verified_total = verify.get("verified_total", 0)
        manifest_path = None
        if write_manifest:
            manifest_path = _write_run_manifest(
                root,
                run,
                recall=recall,
                verified_unchanged=verified_unchanged,
                verified_total=verified_total,
                today=run_date,
            )
        title = recall["query"]
        run["finish"] = {
            "at": _now_iso(),
            "recall_check": recall,
            "first_question": f'What do my notes say about "{title}"?',
            "route": {"tool": "ask_memory", "args": {"query": title}},
            "handoff": _handoff(run),
            "verified_unchanged": verified_unchanged,
            "verified_total": verified_total,
            "manifest_path": manifest_path,
        }
        run["phase"] = "done"
        store.save(run)
    return _present(root, run)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _recompute_phase(outcomes: dict, plan_items: list[dict]) -> str:
    statuses = [outcomes.get(it["original_path"], {}).get("status") for it in plan_items]
    applied = [s for s in statuses if s in _APPLIED_STATUSES]
    failed = [s for s in statuses if s == "failed"]
    if applied and not failed:
        return "applied"
    if applied and failed:
        return "partial"
    if failed and not applied:
        return "failed"
    return "applied" if applied else "failed"


def _verify_originals(root: Path, plan_items: list[dict], outcomes: dict) -> tuple[int, int]:
    """Post-apply re-hash of applied originals — the UI's honest verification line."""
    total = 0
    unchanged = 0
    for it in plan_items:
        outcome = outcomes.get(it["original_path"])
        if not outcome or outcome.get("status") not in _APPLIED_STATUSES:
            continue
        total += 1
        fpath = root / it["original_path"]
        try:
            current = hashlib.sha256(fpath.read_bytes()).hexdigest()
        except OSError:
            continue
        if current == it["original_sha256"]:
            unchanged += 1
    return unchanged, total


def _recall_check(root: Path, run: dict) -> dict:
    from . import find as find_module

    outcomes = run.get("outcomes") or {}
    plan_items = {it["original_path"]: it for it in (run.get("plan") or {}).get("items") or []}
    applied = [
        (op, o) for op, o in outcomes.items() if o.get("status") in _APPLIED_STATUSES
    ]
    applied.sort(key=lambda t: plan_items.get(t[0], {}).get("original_bytes", 0), reverse=True)

    if applied:
        op, outcome = applied[0]
        title = plan_items.get(op, {}).get("title") or op
        target = outcome.get("target_path") or ""
    else:
        packs = run.get("pack_suggestions") or []
        title = (packs[0].get("name") if packs else "") or ""
        target = ""

    hits_paths: list[str] = []
    ok = False
    if title:
        try:
            hits = find_module.find(root, query=title, mode="keyword", limit=5, graph=False)
            hits_paths = [str(getattr(h, "path", "")) for h in hits]
            ok = any(
                target and (target in hp or hp in target or Path(hp).name == Path(target).name)
                for hp in hits_paths
            )
        except Exception:  # noqa: BLE001 — recall failure never blocks finish
            hits_paths = []
            ok = False

    result: dict = {"query": title, "ok": ok, "hits": hits_paths}
    if not ok:
        result["next_action"] = {"tool": "maintain_memory", "args": {"mode": "reconcile"}}
    return result


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
