"""The maintained due-state projection — what is due, per audience, right now.

Four audit categories detect time-bound obligations: `prediction_window` and
`unfinished_experiments` (shipped in PR #555 under their own changes, consumed here
unchanged) plus `question_aging` and `supersession_integrity`. Detection was never the
problem. Delivery was: those queues run only when someone explicitly asks for them, and
every hookless client — hosted agents, claude.ai, ChatGPT — never asks.

This module is the layer that makes an answer cheap enough to ride on ordinary responses.

**Why maintained rather than computed or cached.** A per-call computation cannot ride on a
mutating response, because the write that would carry it is the write that invalidates it.
And "cache the attention summary" is a cache of something that is rebuilt on every call —
there is nothing there to cache. So the projection is persisted beside the review state and
kept honest four ways: an incremental delta on write for the categories a written page can
participate in; a day-boundary re-bucket that is a date comparison rather than a rescan;
reconcile as the healer after out-of-band edits; and full recomputation as the recovery
path when the persisted state is missing or unreadable.

**Why every entry carries a date.** A `check_by` passes at midnight with nothing happening,
and no generation token, mtime or content hash can see that. The projection therefore stores
the candidates that are NOT yet due alongside the items that are, each with the date it
becomes due, so promotion at a day boundary needs no parse and no audit — only a comparison
against dates already on disk.

**Why the scan runs against a far-future date.** The stored set is "every obligation this
vault will EVER owe", produced by running the shipped predicates with `today` set past every
authored date. That is deliberately not a horizon: a horizon would be one more invented
number, and the alternative — a second, forward-looking copy of each predicate — would be
two definitions of "due" that drift. Running the real predicate once and bucketing by the
authored date it already reports keeps exactly one definition. It is safe because every
finding's `signal_version` is derived from authored state and never from today, so the
review identity and fingerprint a far-future scan produces are the ones the real surface
produces.

**Why the write path never recomputes.** The only post-commit seam that reaches all four
compiled writers runs inside the mutation critical section (it is the seam #538 built for
`structure_suggestion`). A full recompute there would put a vault-wide audit inside the
write lock, which the write-latency gates and the shortened critical section both forbid.
So a write applies a bounded delta and nothing else; when there is no persisted state to
delta, the write stays silent and the next read surface — bootstrap, recall, or reconcile —
performs the recovery outside any lock. A quiet first write is the correct trade against a
mutation whose latency is decided by corpus size.

**Egress before counting, always.** A count is an aggregate, and the governance plane's
silence rule extends to aggregates. The persisted projection is server-internal and is never
served raw: item paths are filtered through the release plane for the requesting audience
and only then counted, ordered and truncated. A withheld item contributes nothing to any
count, reference list or ordering, and the served view is byte-identical to the same vault
with the item absent.

Nothing here judges, ranks, or writes to the vault. The only file it writes is its own
projection state.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from . import review_state as review_state_module
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_FILENAME = ".due-state.json"

#: The four due-state categories, in the tiebreak preference the attention surface
#: already uses. `prediction_window` leads because it fires on a date a human wrote
#: down; `supersession_integrity` follows because it is the only defect among them;
#: the last two report candidates against thresholds nobody authored.
PROJECTION_CATEGORIES: tuple[str, ...] = (
    "prediction_window",
    "supersession_integrity",
    "unfinished_experiments",
    "question_aging",
)

#: The categories a single write can soundly update on its own.
#:
#: `supersession_integrity` is absent on purpose. Its dangling-pointer half is
#: page-local, but its multi-headed-chain half is a property of a CHAIN: whether
#: writing page B forks a chain depends on what other pages point at B's
#: predecessor, and answering that needs a reverse scan the write path cannot
#: afford. Recomputing only the half a page can see would let a delta DELETE a
#: fork the full pass had found, so the category is left whole to reconcile —
#: which is exactly the healer D5 names for cross-page truth.
DELTA_CATEGORIES: tuple[str, ...] = (
    "prediction_window",
    "unfinished_experiments",
    "question_aging",
)

#: How many item references the wire block may carry. Small on purpose: the block
#: is an invitation to consult the review surface, not a replacement for it.
TOP_LIMIT = 5

#: Past every date a human could plausibly author, so one pass over the shipped
#: predicates yields every obligation the vault will ever owe. See the module
#: docstring for why this is a sentinel and not a tuning horizon.
_FAR_FUTURE = dt.date(9999, 12, 31)

_LOCK = threading.Lock()

#: Emission governance state, keyed by (session, audience). In memory by design:
#: it is per-conversation presentation state, not a durable fact about the vault,
#: and persisting it would make a server restart change what an agent is told.
_EMISSION: dict[tuple[str, str], str] = {}
_EMISSION_CAP = 512

#: The fallback session key for stdio and CLI, where the transport supplies no
#: session identity. One key per process lifetime, which is exactly the scope of
#: a stdio conversation.
_PROCESS_SESSION_KEY = f"process:{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def state_path(vault_root: Path) -> Path:
    """Beside the review state, and for the same reason: this is derived bookkeeping."""
    return Path(vault_root) / kb_dirname() / STATE_FILENAME


def load(vault_root: Path) -> dict[str, Any] | None:
    """Return the persisted projection, or None when it is missing or unusable.

    Never raises. A projection is derived state: an unreadable one is a reason to
    recompute, never a reason to fail a caller who asked about something else.
    """
    path = state_path(vault_root)
    if not path.exists():
        return None
    try:
        from . import vault

        payload = json.loads(vault.read_bytes_without_pinning(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        log.debug("due-state projection unreadable at %s; recomputing", path)
        return None
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return None
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        return None
    return payload


def save(vault_root: Path, payload: dict[str, Any]) -> None:
    """Atomically replace the projection state. Best effort; never raises."""
    path = state_path(vault_root)
    try:
        from . import vault

        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_name = tempfile.mkstemp(
            prefix=f".{STATE_FILENAME}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            vault.replace_tolerating_transient_sharing(
                lambda: os.replace(temp_name, path)
            )
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 — a projection write never breaks a caller
        log.debug("could not persist the due-state projection", exc_info=True)


# --------------------------------------------------------------------------
# computation
# --------------------------------------------------------------------------


def _due_since(finding: Any) -> str | None:
    """The date this finding's obligation came (or comes) due, from authored state.

    Derived from each category's own already-published `meta` rather than
    recomputed, so the two #555 categories are consumed exactly as they ship and
    this module never carries a second opinion about when a prediction is due.
    """
    meta = finding.meta or {}
    category = finding.category
    if category == "prediction_window":
        return str(meta.get("check_by") or "") or None
    if category == "unfinished_experiments":
        started = str(meta.get("started") or "")
        duration = meta.get("duration_days")
        if not started or not isinstance(duration, int):
            return None
        try:
            # The window is exceeded, not merely reached, on the day after it
            # closes — `_check_unfinished_experiments` treats the edge as inside.
            return (dt.date.fromisoformat(started) + dt.timedelta(days=duration + 1)).isoformat()
        except ValueError:
            return None
    value = str(meta.get("due_since") or "")
    return value or None


def _entry(vault_root: Path, finding: Any, refs: dict[str, str]) -> dict[str, Any] | None:
    """Compose one stored entry: review identity, fingerprint, path and due date."""
    from . import attention as attention_module

    due = _due_since(finding)
    if due is None:
        return None
    target_ref = refs.get(finding.path)
    if not target_ref:
        return None
    partition = str((finding.meta or {}).get("review_partition") or "")
    identity = f"{target_ref}:{partition}" if partition else target_ref
    review_id = review_state_module.item_id(identity)
    reason = attention_module._reason(finding.category, 1, finding)
    related_paths = sorted(
        {path for path in (finding.paths or []) if path != finding.path}
    )
    related_refs = [refs[path] for path in related_paths if path in refs]
    return {
        "ref": review_state_module.review_ref(review_id),
        "item_id": review_id,
        "fingerprint": review_state_module.fingerprint(
            target_ref=target_ref,
            categories=[finding.category],
            reasons=[reason],
            related_refs=related_refs,
        ),
        # Server-internal: the served view filters on this and never emits it.
        "path": finding.path,
        "due": due,
    }


def _entries_from_findings(
    vault_root: Path, findings: list[Any]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group findings into the persisted `{category: {path: {open, pending}}}` shape."""
    paths = sorted({finding.path for finding in findings} | {
        path for finding in findings for path in (finding.paths or [])
    })
    refs = review_state_module.refs_for_paths(vault_root, paths) if paths else {}
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for finding in findings:
        entry = _entry(vault_root, finding, refs)
        if entry is None:
            continue
        out.setdefault(finding.category, {}).setdefault(finding.path, []).append(entry)
    return out


def _bucket(
    entries: list[dict[str, Any]], today: dt.date
) -> dict[str, list[dict[str, Any]]]:
    """Split one page's entries into what is due and what is merely coming."""
    open_rows: list[dict[str, Any]] = []
    pending_rows: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry)
        due = row.pop("due")
        if _date(due) is not None and _date(due) > today:
            pending_rows.append({**row, "due_on": due})
        else:
            open_rows.append({**row, "due_since": due})
    open_rows.sort(key=lambda row: (row["due_since"], row["path"], row["ref"]))
    pending_rows.sort(key=lambda row: (row["due_on"], row["path"], row["ref"]))
    return {"open": open_rows, "pending": pending_rows}


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def recompute(vault_root: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Rebuild the whole projection from canonical state. Read-only over the vault.

    Runs the four shipped predicates once with a far-future `today` so the result
    is every obligation the vault will ever owe, then buckets each entry against
    the real `today`. Callers must be outside the mutation critical section.
    """
    from . import audit as audit_module

    today = today or dt.date.today()
    report = audit_module.audit(
        Path(vault_root),
        categories=sorted(PROJECTION_CATEGORIES),
        today=_FAR_FUTURE,
    )
    grouped = _entries_from_findings(vault_root, list(report.findings))
    categories: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for category in PROJECTION_CATEGORIES:
        pages = grouped.get(category) or {}
        categories[category] = {
            path: _bucket(entries, today) for path, entries in sorted(pages.items())
        }
    return {
        "version": SCHEMA_VERSION,
        "computed_on": today.isoformat(),
        "categories": categories,
    }


def reconcile(vault_root: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Full recompute plus persist — the healer after out-of-band edits."""
    payload = recompute(vault_root, today=today)
    save(vault_root, payload)
    return payload


def apply_write_delta(
    vault_root: Path, rel_path: str, *, today: dt.date | None = None
) -> dict[str, Any] | None:
    """Recompute one written page's entries and re-aggregate. Bounded, no audit pass.

    Returns the updated projection, or None when there is nothing to delta against
    — a missing or unreadable projection is recovered by a read surface outside the
    mutation critical section, never by turning this write into a vault-wide scan.
    """
    from . import audit as audit_module
    from . import find as find_module

    today = today or dt.date.today()
    payload = load(vault_root)
    if payload is None:
        return None

    vault_root = Path(vault_root)
    rel = str(rel_path).replace("\\", "/").lstrip("/")
    page = find_module._CACHE.get(vault_root / rel, vault_root)
    pages = [page] if page is not None else []

    findings: list[Any] = []
    if pages:
        findings.extend(
            audit_module._check_prediction_window(vault_root, pages, today=_FAR_FUTURE)
        )
        findings.extend(
            audit_module._check_unfinished_experiments(
                vault_root, pages, today=_FAR_FUTURE
            )
        )
        findings.extend(
            audit_module._check_question_aging(vault_root, pages, today=_FAR_FUTURE)
        )
    grouped = _entries_from_findings(vault_root, findings)

    with _LOCK:
        current = load(vault_root) or payload
        categories = dict(current.get("categories") or {})
        for category in DELTA_CATEGORIES:
            pages_map = dict(categories.get(category) or {})
            entries = (grouped.get(category) or {}).get(rel)
            if entries:
                pages_map[rel] = _bucket(entries, today)
            else:
                pages_map.pop(rel, None)
            categories[category] = pages_map
        updated = {
            "version": SCHEMA_VERSION,
            "computed_on": today.isoformat(),
            "categories": categories,
        }
        save(vault_root, updated)
    return updated


# --------------------------------------------------------------------------
# serving — egress before counting
# --------------------------------------------------------------------------


def served_entries(
    vault_root: Path,
    *,
    today: dt.date | None = None,
    principal: Any = None,
    purpose: str | None = None,
) -> list[dict[str, Any]]:
    """Every open item this audience may see, most-overdue first.

    The order of operations is the contract, not an implementation detail:
    re-bucket against today, drop what this audience may not see, drop what the
    reader has already triaged, and only then count and order. Filtering after
    ordering, or counting before filtering, would let a withheld item shape the
    result it is supposed to be absent from.
    """
    from .governance import egress as egress_module

    today = today or dt.date.today()
    payload = load(vault_root)
    if payload is None:
        payload = reconcile(vault_root, today=today)

    keep = None
    try:
        keep = egress_module.release_walk_filter(
            Path(vault_root), principal=principal, purpose=purpose
        )
    except Exception:  # noqa: BLE001
        # A release plane that cannot decide must not be read as "release
        # everything". Fail closed: serve nothing rather than count something
        # this audience may not be allowed to know exists.
        log.debug("release filter unavailable; serving no due state", exc_info=True)
        return []

    store = review_state_module.ReviewStateStore(vault_root)
    try:
        state_payload = store.load()
    except ValueError:
        state_payload = {"version": review_state_module.SCHEMA_VERSION, "records": {}}

    order = {category: rank for rank, category in enumerate(PROJECTION_CATEGORIES)}
    rows: list[dict[str, Any]] = []
    categories = payload.get("categories") or {}
    for category in PROJECTION_CATEGORIES:
        pages = categories.get(category) or {}
        if not isinstance(pages, dict):
            continue
        for entries in pages.values():
            if not isinstance(entries, dict):
                continue
            for bucket_key, date_key in (("open", "due_since"), ("pending", "due_on")):
                for entry in entries.get(bucket_key) or []:
                    if not isinstance(entry, dict):
                        continue
                    due = _date(entry.get(date_key))
                    if due is None or due > today:
                        continue  # not yet due — the day-boundary re-bucket
                    path = str(entry.get("path") or "")
                    if keep is not None and path and not keep(path):
                        continue  # withheld: contributes to nothing, anywhere
                    effective, _decision = store.effective_state(
                        str(entry.get("item_id") or ""),
                        str(entry.get("fingerprint") or ""),
                        today=today,
                        payload=state_payload,
                    )
                    if effective != "open":
                        continue  # already triaged; a counter must not re-raise it
                    rows.append(
                        {
                            "category": category,
                            "ref": str(entry.get("ref") or ""),
                            "due_since": due.isoformat(),
                            "fingerprint": str(entry.get("fingerprint") or ""),
                            "path": path,
                        }
                    )
    rows.sort(key=lambda row: (row["due_since"], order[row["category"]], row["ref"]))
    return rows


def block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bounded wire block, or None when there is nothing to report.

    Absent rather than empty: an advisory that arrives saying nothing is still a
    thing the reader has to read.
    """
    if not rows:
        return None
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {
        "total": len(rows),
        "categories": {
            category: counts[category]
            for category in PROJECTION_CATEGORIES
            if category in counts
        },
        "top": [
            {
                "category": row["category"],
                "ref": row["ref"],
                "due_since": row["due_since"],
            }
            for row in rows[:TOP_LIMIT]
        ],
    }


def served(
    vault_root: Path,
    *,
    today: dt.date | None = None,
    principal: Any = None,
    purpose: str | None = None,
) -> dict[str, Any] | None:
    """The wire block for the requesting audience, or None when nothing is due."""
    return block(
        served_entries(
            vault_root, today=today, principal=principal, purpose=purpose
        )
    )


def block_for_write(
    vault_root: Path, rel_path: str, *, today: dt.date | None = None
) -> dict[str, Any] | None:
    """Apply the per-write delta and return the block this response may carry.

    Called from the post-commit seam, which is inside the mutation critical
    section, so this does bounded work only: one page parse, three page-local
    predicates, one small JSON replace. It never recomputes.
    """
    if apply_write_delta(vault_root, rel_path, today=today) is None:
        return None
    return served(vault_root, today=today)


# --------------------------------------------------------------------------
# emission governance
# --------------------------------------------------------------------------


def emission_key() -> tuple[str, str]:
    """(session, audience) — the scope one "already told them" fact belongs to.

    Session identity comes from the MCP transport when it supplies one; stdio and
    CLI have no session concept, so the process lifetime IS the conversation and a
    per-process key is the honest equivalent rather than a degraded one.
    """
    session = _PROCESS_SESSION_KEY
    try:
        from .command_surface import mcp_caller_identity

        value = mcp_caller_identity().get("session_id")
        if value:
            session = f"session:{value}"
    except Exception:  # noqa: BLE001 — outside an MCP call there is simply no session
        pass
    audience = "unknown"
    try:
        from .governance.principal import effective_principal

        audience = str(effective_principal().audience_id or "unknown")
    except Exception:  # noqa: BLE001
        pass
    return session, audience


def should_emit(payload: dict[str, Any] | None) -> bool:
    """Whether this response may carry the block, and record that it did.

    Emit on the first qualifying response of a session, or when the totals change.
    Identical consecutive totals go quiet, because the top product risk of this
    whole programme is the anti-nagging machinery becoming the nag. Deterministic
    and agent-free: the decision is a digest comparison.
    """
    if not payload:
        return False
    digest = json.dumps(
        {"total": payload.get("total"), "categories": payload.get("categories")},
        sort_keys=True,
        separators=(",", ":"),
    )
    key = emission_key()
    with _LOCK:
        if _EMISSION.get(key) == digest:
            return False
        if len(_EMISSION) >= _EMISSION_CAP:
            # Bounded rather than unbounded: a long-lived server must not
            # accumulate one entry per session it has ever seen. Evicting the
            # oldest costs at worst one extra emission for a stale session.
            _EMISSION.pop(next(iter(_EMISSION)), None)
        _EMISSION[key] = digest
    return True


def reset_emission_state() -> None:
    """Forget what has been emitted. For tests and for a deliberate session reset."""
    with _LOCK:
        _EMISSION.clear()
