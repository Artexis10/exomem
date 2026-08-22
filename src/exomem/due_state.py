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

**Why the write path never recomputes.** Cost, not locking. A full recompute is roughly
thirty times a delta and, unlike the delta, scales with the corpus — so putting one on the
write path would make write latency a function of vault size, which is the failure this
projection exists to avoid. (The seam it hangs on is the one #538 built for
`structure_suggestion`. Whether that seam is inside the mutation critical section depends
on the command: `writer_lease._NARROW_BOUNDARY_COMMANDS` release their guards before
`_commit_existing` returns, so for those it is post-lock, and it is inside only for
wide-boundary commands or under `EXOMEM_WIDE_MUTATION_BOUNDARY`. The bound has to hold
either way, which is why it is argued from cost rather than from the lock.) So a write
applies a bounded delta and nothing else; when there is no persisted state to delta, the
write stays silent and the next read surface — bootstrap, recall, or reconcile — performs
the recovery outside any lock. A quiet first write is the correct trade.

**A read surface may write.** Recovery persists: `served_entries` on a vault with no
projection recomputes and saves `.due-state.json` into the KB directory, so a nominally
read-only command can create a file. Where that write is refused the projection is kept in
process instead (see `_remember_unpersisted`) so an unpersistable vault recomputes once per
process rather than once per read.

**Concurrency, and what is deliberately not solved.** The lock here is a `threading.Lock`,
so it orders deltas within one process and not across processes. Two sessions in two
processes can lose one delta (last `os.replace` wins), and a `reconcile` triggered from a
read can overwrite a concurrent delta with a slightly older snapshot. That is accepted
rather than overlooked: every delta is re-derivable from authored state, `reconcile` is the
named healer for exactly this class of drift, and the failure mode is a count that is
briefly stale — never a wrong write to the vault, and never a disclosure. A file lock on the
projection would add a cross-process serialization point to the write path to protect an
advisory counter, which is a worse trade.

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
from collections.abc import Iterator
from contextlib import contextmanager
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

#: The categories a single write can soundly settle in full on its own.
DELTA_CATEGORIES: tuple[str, ...] = (
    "prediction_window",
    "unfinished_experiments",
    "question_aging",
)

#: Categories a write can settle only PARTLY, listed with the defects it may touch.
#:
#: `supersession_integrity` reports two defects with different scopes.
#: `dangling_pointer` is page-local: whether THIS page's `superseded_by` resolves
#: is decided by this page and the link target, so a write that repairs a rotted
#: pointer must clear the finding immediately — it is the only `warn` category
#: here and the one a user acts on at once, so nagging after the fix is the worst
#: possible behaviour. `multi_headed_chain` is a property of a CHAIN: whether
#: writing page B forks a chain depends on what other pages point at B's
#: predecessor, and answering that needs a reverse scan the write path cannot
#: afford. A delta that recomputed the whole category from one page would DELETE
#: a fork the full pass found, so the multi-head half is left to `reconcile` —
#: the healer D5 names for cross-page truth — while the page-local half updates
#: on the write. Stored entries carry their `defect` so the two can be told apart.
DELTA_DEFECTS: dict[str, tuple[str, ...]] = {
    "supersession_integrity": ("dangling_pointer",),
}

#: How many item references the wire block may carry. Small on purpose: the block
#: is an invitation to consult the review surface, not a replacement for it.
TOP_LIMIT = 5

#: Past every date a human could plausibly author, so one pass over the shipped
#: predicates yields every obligation the vault will ever owe. See the module
#: docstring for why this is a sentinel and not a tuning horizon.
_FAR_FUTURE = dt.date(9999, 12, 31)

#: What `supersession_integrity` publishes when the page it flagged carries no
#: authored date at all (`audit` floors it at `dt.date.min`). It is a "due since
#: forever" placeholder, not a real date, so serving sorts it LAST rather than
#: letting it take every slot in a five-item `top` ahead of real overdue work.
_NO_DATE = dt.date.min.isoformat()

_LOCK = threading.Lock()

#: Emission governance state, keyed by (session, audience, vault). In memory by
#: design: it is per-conversation presentation state, not a durable fact about the
#: vault, and persisting it would make a server restart change what an agent is
#: told. The vault is in the key because one process can serve several.
_EMISSION: dict[tuple[str, str, str], str] = {}
_EMISSION_CAP = 512

#: Projections for vaults whose state file could not be written (a read-only KB
#: directory, a mount that refuses the replace). Without this, every read surface
#: on such a vault pays a full four-category audit instead of reusing one.
_UNPERSISTED: dict[str, dict[str, Any]] = {}
_UNPERSISTED_CAP = 8

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
                # Compact separators, sorted keys: this file is rewritten and
                # fsynced on every governed write, so its size is a per-write cost.
                # Indentation cost ~2.5x for a machine-read file nobody diffs;
                # `sort_keys` stays for determinism.
                json.dump(
                    payload,
                    handle,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
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
        # The ONE shared composer -- see review_state.component_fingerprint for why
        # composing this by hand here silently broke dismissal for every carrier.
        "fingerprint": review_state_module.component_fingerprint(
            target_ref=target_ref,
            reason=reason,
            related_refs=related_refs,
        ),
        # Server-internal: the served view filters on these and never emits them.
        "path": finding.path,
        "due": due,
        # Present only where a category reports more than one kind of defect;
        # `DELTA_DEFECTS` uses it to update one half of a category on a write.
        **(
            {"defect": str((finding.meta or {}).get("defect"))}
            if (finding.meta or {}).get("defect")
            else {}
        ),
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


def _has_entries(payload: Any) -> bool:
    """Whether a stored projection holds any entry at all, in any category."""
    categories = (payload or {}).get("categories") or {}
    if not isinstance(categories, dict):
        return False
    for pages in categories.values():
        if not isinstance(pages, dict):
            continue
        for entries in pages.values():
            if isinstance(entries, dict) and (
                entries.get("open") or entries.get("pending")
            ):
                return True
    return False


def _page_exists(vault_root: Path, rel_path: str) -> bool:
    """Whether a stored entry's page is still on disk.

    Named rather than inlined so the drop it guards is a mechanism a test can
    remove: patching `Path.exists` wholesale also breaks reading the projection,
    which would make the removal test pass for the wrong reason.
    """
    return (Path(vault_root) / rel_path).exists()


def _unbucket(bucket: Any) -> list[dict[str, Any]]:
    """Turn a stored `{open, pending}` bucket back into plain dated entries."""
    out: list[dict[str, Any]] = []
    if not isinstance(bucket, dict):
        return out
    for key, date_key in (("open", "due_since"), ("pending", "due_on")):
        for row in bucket.get(key) or []:
            if not isinstance(row, dict):
                continue
            entry = dict(row)
            entry["due"] = entry.pop(date_key, None)
            out.append(entry)
    return out


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
    existing = load(vault_root) or _UNPERSISTED.get(str(vault_root))
    payload = recompute(vault_root, today=today)
    # The projection is derived and rebuilt from authored state; the emission
    # ledger is a MEASUREMENT of what this vault has told its callers, and a
    # heal is not a reason to forget it.
    payload["emission"] = _emission_section(existing)
    save(vault_root, payload)
    _remember_unpersisted(vault_root, payload)
    return payload


def _remember_unpersisted(vault_root: Path, payload: dict[str, Any]) -> None:
    """Keep an in-process copy of a projection that could not be written down.

    A KB directory that is read-only (or a vault on a mount that refuses the
    replace) makes `save` a silent no-op — it is best-effort by design. Without
    this, EVERY `ask_memory` and every bootstrap re-ran a four-category audit,
    which on a 600-page vault is ~169 ms against ~5 ms for a delta. Recomputing
    once per process and serving the copy is the honest recovery; the log line
    fires once so an operator can see why a vault is not persisting.
    """
    if state_path(vault_root).exists():
        return
    key = str(vault_root)
    with _LOCK:
        first = key not in _UNPERSISTED
        if len(_UNPERSISTED) >= _UNPERSISTED_CAP:
            _UNPERSISTED.pop(next(iter(_UNPERSISTED)), None)
        _UNPERSISTED[key] = payload
    if first:
        log.warning(
            "due-state projection could not be persisted at %s; serving an "
            "in-process copy and recomputing once per process",
            state_path(vault_root),
        )


def apply_write_delta(
    vault_root: Path, rel_path: str, *, today: dt.date | None = None
) -> dict[str, Any] | None:
    """Recompute one written page's entries and re-aggregate. Bounded, no audit pass.

    Returns the updated projection, or None when there is nothing to delta against
    — a missing or unreadable projection is recovered by a read surface, never by
    turning this write into a vault-wide scan it would then have to pay for.
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
        # Page-local half of a split category: this page's own pointers only.
        findings.extend(
            finding
            for finding in audit_module._check_supersession_integrity(vault_root, pages)
            if str((finding.meta or {}).get("defect") or "")
            in DELTA_DEFECTS["supersession_integrity"]
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
        for category, settleable in DELTA_DEFECTS.items():
            pages_map = dict(categories.get(category) or {})
            # Keep every stored defect this write cannot decide (the chain-scoped
            # half), replace only the page-local ones it just recomputed.
            preserved = [
                entry
                for entry in _unbucket(pages_map.get(rel))
                if str(entry.get("defect") or "") not in settleable
            ]
            entries = preserved + list((grouped.get(category) or {}).get(rel) or [])
            if entries:
                pages_map[rel] = _bucket(entries, today)
            else:
                pages_map.pop(rel, None)
            categories[category] = pages_map
        updated = {
            "version": SCHEMA_VERSION,
            "computed_on": today.isoformat(),
            "categories": categories,
            # One governed write, one projection delta, one tick. This is the
            # denominator the "more automatic" claim is measured against, and
            # it has to be persisted because the emission governor above it is
            # per-process memory no projector can read.
            "emission": _emission_delta(current, writes=1),
        }
        save(vault_root, updated)
    return updated


# --------------------------------------------------------------------------
# the emission ledger
# --------------------------------------------------------------------------


def _emission_section(payload: dict[str, Any] | None) -> dict[str, Any]:
    section = (payload or {}).get("emission")
    if not isinstance(section, dict):
        return {"writes": 0, "emissions": 0, "last_digest": None}
    return {
        "writes": int(section.get("writes") or 0),
        "emissions": int(section.get("emissions") or 0),
        "last_digest": section.get("last_digest"),
    }


def _emission_delta(
    payload: dict[str, Any] | None,
    *,
    writes: int = 0,
    emissions: int = 0,
    last_digest: str | None = None,
) -> dict[str, Any]:
    section = _emission_section(payload)
    section["writes"] += writes
    section["emissions"] += emissions
    if last_digest is not None:
        section["last_digest"] = last_digest
    return section


def _record_emission(vault_root: Path | None, digest: str) -> None:
    """Persist that a block was DELIVERED. Best effort, never on the hot path.

    Called from `mark_emitted`, which the terminal reaches once per response
    that actually carried a block — so this is one small JSON replace per
    delivered advisory, and never inside a mutation critical section.
    """
    if not vault_root:
        return
    try:
        payload = load(vault_root) or _UNPERSISTED.get(str(vault_root))
        if payload is None:
            return
        payload = {
            **payload,
            "emission": _emission_delta(payload, emissions=1, last_digest=digest),
        }
        save(vault_root, payload)
        _remember_unpersisted(vault_root, payload)
    except Exception:  # noqa: BLE001 — a ledger write never breaks a response
        log.debug("could not record the due-state emission", exc_info=True)


def emission_ledger(vault_root: Path) -> dict[str, Any]:
    """The persisted `{writes, emissions, last_digest}` a projector reads."""
    return _emission_section(load(vault_root) or _UNPERSISTED.get(str(vault_root)))


# --------------------------------------------------------------------------
# batch scope
# --------------------------------------------------------------------------

#: Vaults currently inside a batch scope, with the nesting depth. A depth rather
#: than a flag because a batch leaf can call another one, and the inner exit
#: must not un-silence the outer batch. Its own lock: `_LOCK` is not reentrant
#: and the emission governor consults this registry while holding it.
_BATCH: dict[str, int] = {}
_BATCH_LOCK = threading.Lock()


@contextmanager
def batch_scope(vault_root: Path | None) -> Iterator[None]:
    """Suppress emission for the duration of one multi-write command.

    A product command that commits twelve governed writes must not deliver
    twelve counters blocks: the user asked for one batch and would receive
    twelve notifications, which is nagging by another name. Inside the scope the
    per-write projection deltas still apply — the counts stay true — and the
    governor simply refuses to emit. The command's terminal runs after the scope
    has exited and decides once, under the unchanged change-only rule.

    Separate invocations are separate batches by definition: N calls that each
    change the counts legitimately emit N changed lines, which is the cadence
    the design asks for.
    """
    key = str(vault_root or "")
    with _BATCH_LOCK:
        _BATCH[key] = _BATCH.get(key, 0) + 1
    try:
        yield
    finally:
        with _BATCH_LOCK:
            depth = _BATCH.get(key, 1) - 1
            if depth > 0:
                _BATCH[key] = depth
            else:
                _BATCH.pop(key, None)


def batch_active(vault_root: Path | None) -> bool:
    """Whether this vault is inside a batch scope right now."""
    with _BATCH_LOCK:
        return _BATCH.get(str(vault_root or ""), 0) > 0


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
        # An unpersistable vault recomputes ONCE per process, not once per read.
        payload = _UNPERSISTED.get(str(vault_root))
    if payload is None:
        payload = reconcile(vault_root, today=today)

    if not _has_entries(payload):
        # Nothing stored, so nothing to filter, count or order — and no disclosure
        # decision to make, because the answer is the empty list either way. This
        # is not an optimisation of the egress rule; it is the case where the rule
        # has no input. It matters because building the release filter is
        # governance-proportional work, and a vault that owes nothing is the
        # common case on every recall and every bootstrap.
        return []

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
        state_payload = review_state_module.empty_state()
    excluded = _excluded_families(state_payload)

    order = {category: rank for rank, category in enumerate(PROJECTION_CATEGORIES)}
    rows: list[dict[str, Any]] = []
    categories = payload.get("categories") or {}
    for category in PROJECTION_CATEGORIES:
        if category in excluded:
            # A count of things the user asked not to hear about is a nag by
            # another route, so a quiet family contributes nothing to `total`,
            # to `categories`, or to `top` — exactly as a withheld item does.
            # Unlike egress, the absence is explained: `review_memory(
            # mode="dispositions")` says which families are quiet and why.
            continue
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
                    if path and not _page_exists(vault_root, path):
                        # Deleted out of band. The projection is maintained, not
                        # authoritative, and reconcile heals it — but until then a
                        # counter must not report an obligation on a page that is
                        # gone. Cheap enough to pay per served row.
                        continue
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
    # Dated first, oldest first; dateless last. A defect a human authored no date
    # for is reported with a floor date internally, and left to sort naively it
    # would outrank every genuinely overdue prediction in a five-slot `top`.
    rows.sort(
        key=lambda row: (
            row["due_since"] == _NO_DATE,
            row["due_since"],
            order[row["category"]],
            row["ref"],
        )
    )
    _record_served(vault_root, rows, known=state_payload)
    return rows


def _excluded_families(state_payload: dict[str, Any]) -> frozenset[str]:
    """Projection categories a family disposition removes from every carrier.

    Named and separate so it is a mechanism a test can remove. There is no
    second filter in `block_for_write`: that path serves through `served_entries`
    and therefore through this one, and a duplicate would be a second opinion
    about what "quiet" means on the write carrier specifically.
    """
    return frozenset(review_state_module.disposition_map(state_payload))


def _record_served(
    vault_root: Path, rows: list[dict[str, Any]], *, known: dict | None = None
) -> None:
    """Stamp the first surfacing of the references this carrier actually serves.

    `TOP_LIMIT` rather than every row, because `top` is what a carrier puts on
    the wire; a row that only contributed to a count was never shown to anybody.
    Everything here is already past egress, the triage filter and the
    disposition filter, so nothing withheld or excluded can reach the ledger.
    """
    served_rows = rows[:TOP_LIMIT]
    if not served_rows:
        return
    try:
        review_state_module.record_surfaced(
            vault_root,
            [(_ref_id(row["ref"]), row["fingerprint"]) for row in served_rows],
            surface="carrier",
            known=known,
        )
    except Exception:  # noqa: BLE001 — a ledger write never breaks a carrier
        log.debug("first-surfaced ledger not recorded for the carrier", exc_info=True)


def _ref_id(ref: str) -> str:
    return str(ref or "").rsplit("/", 1)[-1]


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

    Called from the post-commit seam, so this does bounded work only: one page
    parse, four page-local predicates, one small JSON replace. It never recomputes.

    The serve runs inside its OWN disclosure boundary. Counting is an egress
    decision per page, and on a governed vault `release_walk_filter` records one
    outcome per due item — joining the mutation's collector would hand the caller
    a governance receipt for a write listing N pages the write never touched.
    Those decisions are real and are collected; they just belong to this advisory
    rather than to the mutation.
    """
    if apply_write_delta(vault_root, rel_path, today=today) is None:
        return None
    from .governance import egress as egress_module

    with egress_module.disclosure_boundary(Path(vault_root), "due_state_advisory"):
        return served(vault_root, today=today)


# --------------------------------------------------------------------------
# emission governance
# --------------------------------------------------------------------------


def emission_key(vault_root: Path | None = None) -> tuple[str, str, str]:
    """(session, audience, vault) — the scope one "already told them" fact belongs to.

    Session identity comes from the MCP transport when it supplies one; stdio and
    CLI have no session concept, so the process lifetime IS the conversation and a
    per-process key is the honest equivalent rather than a degraded one.

    The vault is part of the key because one process can serve more than one of
    them: without it, telling a principal about vault A's four overdue items would
    silence vault B's identical-looking four, which is a wrong answer rather than
    a quiet one.
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
    return session, audience, str(vault_root or "")


def _digest(payload: dict[str, Any]) -> str:
    return json.dumps(
        {"total": payload.get("total"), "categories": payload.get("categories")},
        sort_keys=True,
        separators=(",", ":"),
    )


def would_emit(payload: dict[str, Any] | None, *, vault_root: Path | None = None) -> bool:
    """Whether this block is new to this (session, audience, vault). Records nothing.

    Emit on the first qualifying response of a session, or when the totals change.
    Identical consecutive totals go quiet, because the top product risk of this
    whole programme is the anti-nagging machinery becoming the nag. Deterministic
    and agent-free: the decision is a digest comparison.
    """
    if not payload:
        return False
    if batch_active(vault_root):
        # Inside a batch the answer is always "not now": the leaf's terminal
        # decides once, after the scope exits.
        return False
    with _LOCK:
        return _EMISSION.get(emission_key(vault_root)) != _digest(payload)


def mark_emitted(payload: dict[str, Any] | None, *, vault_root: Path | None = None) -> None:
    """Record that this block was actually DELIVERED to the caller.

    Deliberately separate from `would_emit`, and deliberately called at the point
    of delivery rather than the point of production. Recording at production burnt
    the session's one emission on responses that never carried the block at all --
    a `legacy` detail strips it, and terminal validation can drop it -- so the next
    response with the same totals went quiet about something the caller was never
    told. Produce freely; record only what was handed over.
    """
    if not payload:
        return
    if batch_active(vault_root):
        return
    key = emission_key(vault_root)
    digest = _digest(payload)
    with _LOCK:
        if _EMISSION.get(key) == digest:
            return
        if len(_EMISSION) >= _EMISSION_CAP:
            # Bounded rather than unbounded: a long-lived server must not
            # accumulate one entry per session it has ever seen. Evicting the
            # oldest costs at worst one extra emission for a stale session.
            _EMISSION.pop(next(iter(_EMISSION)), None)
        _EMISSION[key] = digest
    # Outside the lock: the ledger write reads and replaces the projection file,
    # and `_remember_unpersisted` takes this same non-reentrant lock.
    _record_emission(vault_root, digest)


def should_emit(payload: dict[str, Any] | None, *, vault_root: Path | None = None) -> bool:
    """Decide and record in one step. Only for carriers that deliver immediately.

    Recall attaches the block to the object it is about to return, so for it
    production and delivery are the same moment. Pass `vault_root`: the key's third
    component is the vault, and a carrier that omits it both silences a second vault
    and misses what the mutating path recorded.

    The other two carriers do not use this. The mutating path produces before it
    knows whether the response will carry anything, so it must use `would_emit` /
    `mark_emitted` separately. Bootstrap attaches unconditionally rather than
    deciding, so it calls `mark_emitted` alone.
    """
    if not would_emit(payload, vault_root=vault_root):
        return False
    mark_emitted(payload, vault_root=vault_root)
    return True


def reset_emission_state() -> None:
    """Forget what has been emitted. For tests and for a deliberate session reset."""
    with _LOCK:
        _EMISSION.clear()
        _UNPERSISTED.clear()
    with _BATCH_LOCK:
        _BATCH.clear()
