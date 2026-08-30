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
reconcile as the healer after out-of-band edits; and one background recomputation as the
recovery path when the persisted state is missing or unreadable.

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
write stays silent and the next read surface schedules recovery outside any lock. The
advisory remains silent until recovery is ready. A quiet first write and first read are
the correct trade.

**A read surface may schedule a write.** Recovery persists: `served_entries` on a vault
with no projection starts one daemon worker which recomputes and saves `.due-state.json`
into the KB directory. The caller gets no advisory until that work is ready; a derived
counter must never turn an interactive read into a vault-sized audit. Where persistence is
refused the projection is kept in process instead (see `_remember_unpersisted`) so an
unpersistable vault recomputes once per process rather than once per read.

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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import review_state as review_state_module

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
    # Last for the reason it is last in the attention union: it reports an
    # authored binding rather than an authored date, and it is the newest.
    "unreflected_outcomes",
)

#: The categories a single write can soundly settle in full on its own.
DELTA_CATEGORIES: tuple[str, ...] = (
    "prediction_window",
    "unfinished_experiments",
    "question_aging",
    "unreflected_outcomes",
)

#: The subset a PAGE write settles. `unreflected_outcomes` is not one of them:
#: it is a property of a bound COLLECTION PAIR, so an ordinary page write can
#: neither produce it nor prove its absence. Clearing it from the written path
#: would silently drop a finding that only the structured delta below owns —
#: the same reasoning `DELTA_DEFECTS` applies within a split category, applied
#: across two different write shapes.
PAGE_DELTA_CATEGORIES: tuple[str, ...] = (
    "prediction_window",
    "unfinished_experiments",
    "question_aging",
)

#: The remainder: what a STRUCTURED write settles, keyed by plan-item path rather
#: than by the path the write touched. Derived from the two above rather than
#: restated, so `DELTA_CATEGORIES` stays the single answer to "what can one write
#: settle" and the two shapes partition it instead of drifting apart.
STRUCTURED_DELTA_CATEGORIES: tuple[str, ...] = tuple(
    category for category in DELTA_CATEGORIES if category not in PAGE_DELTA_CATEGORIES
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

#: Vaults whose missing or unreadable projection is currently being rebuilt. A
#: set is enough: callers never wait for the result, and the completed projection
#: itself becomes the durable readiness signal. Entries exist only for the life
#: of one worker, so the registry cannot grow with the number of vaults served.
_WARMING: set[str] = set()

#: The fallback session key for stdio and CLI, where the transport supplies no
#: session identity. One key per process lifetime, which is exactly the scope of
#: a stdio conversation.
_PROCESS_SESSION_KEY = f"process:{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def state_path(vault_root: Path) -> Path:
    """Beside the review state, and for the same reason: this is derived bookkeeping."""
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / STATE_FILENAME


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
        from . import state_paths, vault

        state_paths.ensure_vault_state_dir(vault_root)
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
        # Server-internal, for a family whose finding depends on OTHER pages:
        # the primitives plus the joined `(path, key)` pairs, so serving under a
        # narrower audience can drop the withheld ones and rebuild the finding
        # through the family's own composer. Without it the stored count is the
        # WRITER's count, and an audience that may not see a joined record still
        # reads it in the total.
        **({"component": finding.component} if finding.component else {}),
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


def _survivors_only(
    vault_root: Path, entry: dict[str, Any], keep: Any
) -> dict[str, Any] | None:
    """Re-derive one entry from the joined pages THIS audience may see.

    The projection is shared across audiences and is built once, by whoever
    wrote last. Filtering only the entry's own `path` was therefore not enough
    for a family whose finding is a statement about OTHER pages: an item whose
    joined records are all withheld still contributed a row, a count and a
    review reference to an audience that may not know the records exist.

    So the entry carries the joined `(path, key)` pairs, and the ones this
    audience may not see are dropped HERE, at serve. No survivor is no finding.
    Fewer survivors is a DIFFERENT finding, and it is recomposed through
    `audit.unreflected_component` -- the family's own composer, the same one the
    audit pass calls -- so the fingerprint this audience is offered is the one a
    fresh audit under that audience would produce, and a dismissal taken here
    still matches. Composing it any other way is how dismissal silently breaks.

    Entries with no component (every other category) are returned unchanged.
    """
    component = entry.get("component")
    if not isinstance(component, dict):
        return entry
    stored = [
        (str(pair[0]), str(pair[1]))
        for pair in component.get("joined") or []
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]
    if not stored:
        return entry
    survivors = [pair for pair in stored if keep(pair[0])]
    if len(survivors) == len(stored):
        return entry
    if not survivors:
        return None
    from . import audit as audit_module

    finding = audit_module.unreflected_component(component, survivors)
    if finding is None:
        return None
    paths = sorted({finding.path, *(finding.paths or [])})
    rebuilt = _entry(
        vault_root, finding, review_state_module.refs_for_paths(vault_root, paths)
    )
    if rebuilt is None:
        return None
    return {
        **entry,
        "ref": rebuilt["ref"],
        "item_id": rebuilt["item_id"],
        "fingerprint": rebuilt["fingerprint"],
        "component": rebuilt.get("component"),
    }


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
    """Turn a stored `{open, pending}` bucket back into plain dated entries.

    THE definition -- there was briefly a second one further down the file, and
    because a use sits between the two bindings ruff's F811 never fired while the
    lower one silently won for the page-write path the upper one was written for.
    The two bodies disagreed about exactly the two things a projection must not
    get wrong: a malformed bucket, and a missing date.

    Both answers are kept, and both are deliberate. A bucket that is not a dict
    degrades to `[]`: the projection is derived state and a corrupt one is a
    reason to recompute, never a reason to throw inside a caller's write (the
    blanket `except Exception` above this would have turned it into a page write
    that silently lost all four of its category updates and its `writes` bump).
    A row with no stored date reads as `_NO_DATE` rather than `None`, because
    `_bucket` sorts on that value and `None` would `TypeError` on the way back in.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(bucket, dict):
        return out
    for key, date_key in (("open", "due_since"), ("pending", "due_on")):
        for row in bucket.get(key) or []:
            if not isinstance(row, dict):
                continue
            entry = dict(row)
            entry["due"] = entry.pop(date_key, _NO_DATE)
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
        # The write path may not walk the vault to learn what is bound to what
        # (4.7): a full pass already resolved every binding, so it publishes the
        # answer here for the deltas to consult. Maintained, never authoritative
        # -- a delta that resolves a binding this index does not have yet adds
        # it, and the next reconcile rebuilds the whole thing.
        "bindings": audit_module.outcome_binding_index(Path(vault_root)),
    }


def reconcile(vault_root: Path, *, today: dt.date | None = None) -> dict[str, Any]:
    """Full recompute plus persist — the healer after out-of-band edits."""
    existing = load(vault_root) or _UNPERSISTED.get(str(vault_root))
    payload = recompute(vault_root, today=today)
    # The projection is derived and rebuilt from authored state; the emission
    # ledger is a MEASUREMENT of what this vault has told its callers, and a
    # heal is not a reason to forget it.
    #
    # `due_total` is deliberately NOT written here. It has exactly one
    # definition — the size of the block a caller was actually handed — and a
    # heal hands nobody anything. An earlier version recorded reconcile's own
    # unfiltered count under the same name, which gave the field two meanings
    # and let an anti-vacuity gate read a pre-dismissal number as evidence that
    # a later batch had something to say. One writer, one meaning.
    payload["emission"] = _emission_delta(existing)
    save(vault_root, payload)
    _remember_unpersisted(vault_root, payload)
    return payload


def _schedule_reconcile(
    vault_root: Path, *, today: dt.date
) -> dict[str, Any] | None:
    """Start one fail-soft projection rebuild without delaying the caller."""
    if os.environ.get("EXOMEM_SYNC_DUE_STATE_WARM", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        # The pytest suite creates hundreds of disposable vaults and then tears
        # them down immediately. Let those tests preserve deterministic carrier
        # assertions without leaving daemon workers racing tmpdir cleanup; the
        # focused warm-up tests remove this seam and exercise the production path.
        return reconcile(vault_root, today=today)
    vault_root = Path(vault_root)
    key = str(vault_root)

    with _LOCK:
        if key in _WARMING or key in _UNPERSISTED:
            return
        # A second caller can have observed the old missing state just before the
        # first worker persisted its result. Recheck while schedulers are ordered
        # so that stale observation cannot launch a second vault-wide audit.
        if load(vault_root) is not None:
            return
        _WARMING.add(key)

    def _run() -> None:
        try:
            reconcile(vault_root, today=today)
        except Exception:  # noqa: BLE001
            # Due state is advisory. Its recovery may be retried by a later read,
            # but it must never fail the read that happened to notice the gap.
            log.warning(
                "background due-state projection rebuild failed for %s",
                vault_root,
                exc_info=True,
            )
        finally:
            with _LOCK:
                _WARMING.discard(key)

    try:
        threading.Thread(
            target=_run,
            name="exomem-due-state-warm",
            daemon=True,
        ).start()
    except RuntimeError:
        with _LOCK:
            _WARMING.discard(key)
        log.warning(
            "could not start background due-state projection rebuild for %s",
            vault_root,
            exc_info=True,
        )


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
        for category in PAGE_DELTA_CATEGORIES:
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
            # Carried, not rebuilt. A page write learns nothing about bindings
            # and must not drop the index the structured deltas depend on --
            # losing it here would silently send every later plan write back to
            # doing nothing at all until the next reconcile.
            **(
                {"bindings": current["bindings"]}
                if isinstance(current.get("bindings"), dict)
                else {}
            ),
        }
        save(vault_root, updated)
    return updated


#: The one family whose entries a STRUCTURED write maintains. Named, not derived,
#: because the two deltas below edit entries field by field and only this
#: family's stored `component` makes that possible; a second structured family
#: would need its own edit rules, and silently reusing these would corrupt it.
#: Whether it is maintained AT ALL is still `STRUCTURED_DELTA_CATEGORIES`'
#: decision -- see `_settles_at_write_time`.
_OUTCOME_FAMILY = "unreflected_outcomes"


def _settles_at_write_time() -> bool:
    """Whether one structured write may settle this family, per the declaration.

    `DELTA_CATEGORIES` is the single answer to "what can one write settle", and
    the two subsets partition it. Reading it here rather than assuming it keeps
    the declaration load-bearing: take the family out of `DELTA_CATEGORIES` (or
    hand it to the page loop) and the write path stops maintaining it, which is
    exactly the mechanism-removal probe.
    """
    return _OUTCOME_FAMILY in STRUCTURED_DELTA_CATEGORIES


def _bindings_index(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """The persisted binding index, or an empty one. Never authoritative."""
    rows = (payload or {}).get("bindings")
    return rows if isinstance(rows, dict) else {}


def _entry_item_key(entry: Any) -> str:
    component = (entry or {}).get("component")
    return str((component or {}).get("item_key") or "") if isinstance(component, dict) else ""


def _entry_records(entry: Any) -> str:
    """Which Records collection's binding this entry is about."""
    component = (entry or {}).get("component")
    if not isinstance(component, dict):
        return ""
    return str(component.get("records_collection") or "")


def _entry_binding(entry: Any) -> tuple[str, str]:
    """The stored entry's identity WITHIN a page: the item, and the binding.

    Not the item alone. Two Records collections joined to one Planning collection
    is an ordinary shape -- two sources logging deliveries against one plan -- and
    the audit reports it as two findings, one per binding, each with its own
    joined records and therefore its own fingerprint. A projection keyed by the
    item collapsed them into one entry whose fingerprint was composed over the
    union, which is a value no audit produces: the block reported one finding
    where two were owed, and a dismissal taken against it bound to nothing.
    """
    return (_entry_item_key(entry), _entry_records(entry))


def _stored_joined(entry: Any) -> list[tuple[str, str]]:
    component = (entry or {}).get("component")
    if not isinstance(component, dict):
        return []
    return [
        (str(pair[0]), str(pair[1]))
        for pair in component.get("joined") or []
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]


def _find_outcome_entry(
    pages: dict[str, Any], item_path: str, item_key: str, records_path: str
) -> dict[str, Any] | None:
    for entry in _unbucket(pages.get(item_path)):
        if _entry_binding(entry) == (item_key, records_path):
            return entry
    return None


def _item_entries(pages: dict[str, Any], item_path: str, item_key: str) -> list[dict[str, Any]]:
    """Every binding's entry for ONE plan item, in stored order."""
    return [row for row in _unbucket(pages.get(item_path)) if _entry_item_key(row) == item_key]


def _replace_page_rows(
    pages: dict[str, Any], item_path: str, rows: list[dict[str, Any]], today: dt.date
) -> None:
    if rows:
        pages[item_path] = _bucket(rows, today)
    else:
        pages.pop(item_path, None)


def _put_outcome_entry(
    pages: dict[str, Any],
    item_path: str,
    item_key: str,
    records_path: str,
    entry: dict[str, Any] | None,
    today: dt.date,
) -> None:
    """Replace (or drop) exactly ONE binding's entry for one item. Nothing else moves.

    A Markdown-log Planning collection keeps every item in one file, so a page can
    hold several entries; and one item can be bound to several Records
    collections, so an item can hold several entries too. The unit of replacement
    is therefore the `(item, binding)` pair -- the audit's own unit. Replacing the
    page wholesale deletes other items' findings; replacing the item wholesale
    deletes the other bindings' findings, which is what H1 was.
    """
    rows = [
        row
        for row in _unbucket(pages.get(item_path))
        if _entry_binding(row) != (item_key, records_path)
    ]
    if entry is not None:
        rows.append(entry)
    _replace_page_rows(pages, item_path, rows, today)


def _drop_item_entries(
    pages: dict[str, Any], item_path: str, item_key: str, today: dt.date
) -> None:
    """Drop EVERY binding's entry for one item -- the item left the open state.

    A closed item is not a finding whatever joins to it, so this is the one place
    that is right to work item-wide rather than per binding.
    """
    rows = [row for row in _unbucket(pages.get(item_path)) if _entry_item_key(row) != item_key]
    _replace_page_rows(pages, item_path, rows, today)


def _compose_outcome_entry(
    vault_root: Path, component: Mapping[str, Any], joined: list[tuple[str, str]]
) -> dict[str, Any] | None:
    """One entry, through the family's own composer. No vault read beyond refs."""
    from . import audit as audit_module

    finding = audit_module.unreflected_component(component, joined)
    if finding is None:
        return None
    paths = sorted({finding.path, *(finding.paths or [])})
    return _entry(
        vault_root, finding, review_state_module.refs_for_paths(vault_root, paths)
    )


def _unfiltered_snapshot(vault_root: Path, manifest: Any) -> Any | None:
    """An adapter snapshot with NO release filter, for the write path only.

    Deliberate, and the reason it is safe: the projection is server-internal
    truth, and disclosure is decided once at serve, where `_survivors_only`
    drops what the reading audience may not see. Filtering here as well cost a
    policy decision per item file -- 55% of a 33 s write -- to reach an answer
    the serve boundary reaches anyway, and it made the stored projection depend
    on whoever happened to write last. The audit pass keeps its filter: that one
    IS a read surface.
    """
    from . import record_formats
    from . import structured_collections as collections_module

    try:
        return record_formats.load_adapter(vault_root, manifest).read()
    except (collections_module.CollectionError, OSError, ValueError):
        return None


def _load_manifest(vault_root: Path, path: str) -> Any | None:
    from . import structured_collections as collections_module

    try:
        return collections_module.load_manifest(vault_root, Path(vault_root) / path)
    except Exception:  # noqa: BLE001 -- a binding whose end vanished heals at reconcile
        return None


def _persist_delta(
    vault_root: Path,
    current: dict[str, Any],
    categories: dict[str, Any],
    today: dt.date,
    *,
    bindings: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    updated = {
        "version": SCHEMA_VERSION,
        "computed_on": today.isoformat(),
        "categories": categories,
        # Bumped on EVERY governed structured write, including one into a
        # collection nobody bound. `writes` is the denominator the "how often
        # does the advisory actually fire?" claim divides by; counting only the
        # writes that had something to say makes that ratio measure the wrong
        # population and flatters the governor.
        "emission": _emission_delta(current, writes=1),
        **(
            {"bindings": bindings}
            if bindings is not None
            else (
                {"bindings": current["bindings"]}
                if isinstance(current.get("bindings"), dict)
                else {}
            )
        ),
    }
    save(vault_root, updated)
    return updated


def _prune_missing_joined(
    vault_root: Path, pages: dict[str, Any], today: dt.date
) -> None:
    """Drop joined records whose file is gone, and any entry that empties.

    The mirror of the `_page_exists` check the serve loop already runs on an
    entry's own page, applied to the pages the finding is ABOUT. A record deleted
    out of band leaves an entry claiming events that no longer exist, and the
    bounded delta cannot see that deletion from the write it was handed -- but a
    `stat` per stored pair can, and the cost is bounded by the projection, not
    by the vault. `reconcile` still owns the general out-of-band heal; this is
    the one case a counter must not get wrong, because it inflates a count.
    """
    for item_path in list(pages):
        rows = _unbucket(pages.get(item_path))
        rebuilt: list[dict[str, Any]] = []
        changed = False
        for row in rows:
            stored = _stored_joined(row)
            if not stored:
                rebuilt.append(row)
                continue
            survivors = [pair for pair in stored if _page_exists(vault_root, pair[0])]
            if len(survivors) == len(stored):
                rebuilt.append(row)
                continue
            changed = True
            if not survivors:
                continue
            replacement = _compose_outcome_entry(
                vault_root, row.get("component") or {}, sorted(survivors)
            )
            if replacement is not None:
                rebuilt.append(replacement)
        if not changed:
            continue
        if rebuilt:
            pages[item_path] = _bucket(rebuilt, today)
        else:
            pages.pop(item_path, None)


def apply_record_write_delta(
    vault_root: Path,
    manifest: Any,
    *,
    path: str,
    key: str,
    values: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    """Fold ONE committed record into the projection. Never reads Records.

    The written record is the caller's own committed write, so its join value is
    already in hand; what the delta does not know is which open plan items that
    value lands on. It therefore reads exactly one thing -- the bound Planning
    snapshot -- and edits only the entries for the items whose join key matches
    the record's new value, or (on an update) its previous one.

    What it deliberately does NOT do: discover collections, re-read the Records
    collection to recount, or ask the release plane about anything. Those three
    were 33 s of a 100-item append between them, and none of them is needed to
    know what one record just did.
    """
    from . import audit as audit_module

    today = today or dt.date.today()
    # A cheap existence probe, not a parse: the only question before the lock is
    # "is there a projection at all?", and answering it by decoding a 100 KB file
    # doubled the write's JSON cost to skip a lock nobody else is holding.
    if not state_path(vault_root).exists():
        return None
    with _LOCK:
        current = load(vault_root)
        if current is None:
            return None
        if not _settles_at_write_time():
            return _persist_delta(
                Path(vault_root), current, dict(current.get("categories") or {}), today
            )
        index = _bindings_index(current)
        rows = [
            row
            for row in index.get(str(manifest.path), [])
            if isinstance(row, dict) and row.get("records") == str(manifest.path)
        ]
        registered = None
        if not rows:
            # Not in the index: either nothing is bound (the common case, and it
            # costs one attribute read of the manifest already in hand) or this
            # collection was bound since the last full pass. Resolving it here
            # and registering it is what lets the PLAN side stay walk-free too.
            declared = audit_module.declared_bindings(Path(vault_root), manifest)
            rows = [{"records": str(manifest.path), **row} for row in declared]
            if rows:
                registered = {key_: list(value) for key_, value in index.items()}
                for row in rows:
                    for slot in (row["records"], row["planning"]):
                        bucket = registered.setdefault(slot, [])
                        if row not in bucket:
                            bucket.append(row)
        categories = dict(current.get("categories") or {})
        pages = dict(categories.get(_OUTCOME_FAMILY) or {})
        for row in rows:
            planning = _load_manifest(Path(vault_root), str(row.get("planning") or ""))
            if planning is None:
                continue
            join = dict(row.get("join") or {})
            record_fields = list(join)
            plan_fields = [join[name] for name in record_fields]
            new_key = audit_module.join_key(record_fields, values)
            old_key = (
                audit_module.join_key(record_fields, previous) if previous is not None else None
            )
            if new_key is None and old_key is None:
                continue
            snapshot = _unfiltered_snapshot(Path(vault_root), planning)
            if snapshot is None:
                continue
            for item in snapshot.records:
                if not audit_module.open_plan_item(item.values):
                    continue
                item_key = audit_module.join_key(plan_fields, item.values)
                if item_key is None:
                    continue
                if item_key != new_key and item_key != old_key:
                    continue
                component = audit_module.outcome_component(manifest, planning, join, item)
                # Keyed by the WRITTEN Records collection: this write can only
                # speak for its own binding, and another collection bound to the
                # same item owns an entry of its own.
                records_path = str(manifest.path)
                existing = _find_outcome_entry(
                    pages, item.source.path, item.identity.key, records_path
                )
                joined = set(_stored_joined(existing))
                if item_key == new_key:
                    joined.add((str(path), str(key)))
                else:
                    joined.discard((str(path), str(key)))
                _put_outcome_entry(
                    pages,
                    item.source.path,
                    item.identity.key,
                    records_path,
                    _compose_outcome_entry(Path(vault_root), component, sorted(joined)),
                    today,
                )
        _prune_missing_joined(Path(vault_root), pages, today)
        categories[_OUTCOME_FAMILY] = pages
        return _persist_delta(
            Path(vault_root), current, categories, today, bindings=registered
        )


def apply_plan_write_delta(
    vault_root: Path,
    manifest: Any,
    *,
    path: str,
    key: str,
    values: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    """Fold ONE committed plan write into the projection. Usually reads nothing.

    Three cases, and only the third costs a read:

    * the item left the open state -- its entry goes, and nothing else can have
      changed, because a closed item is not a finding whatever joins to it;
    * the item is still open and no join-side value moved -- the joined records
      it already had are still the joined records it has, so the entry is
      recomposed in place from the stored pairs with the item's current title
      and status (a status change moves the finding's own text, and a stale one
      would serve a fingerprint no fresh pass would produce);
    * the item is new, or a join-side value moved -- only then is the bound
      Records snapshot read, and only to rebuild THIS item's entry.
    """
    from . import audit as audit_module

    today = today or dt.date.today()
    # A cheap existence probe, not a parse: the only question before the lock is
    # "is there a projection at all?", and answering it by decoding a 100 KB file
    # doubled the write's JSON cost to skip a lock nobody else is holding.
    if not state_path(vault_root).exists():
        return None
    with _LOCK:
        current = load(vault_root)
        if current is None:
            return None
        if not _settles_at_write_time():
            return _persist_delta(
                Path(vault_root), current, dict(current.get("categories") or {}), today
            )
        rows = [
            row
            for row in _bindings_index(current).get(str(manifest.path), [])
            if isinstance(row, dict) and row.get("planning") == str(manifest.path)
        ]
        categories = dict(current.get("categories") or {})
        pages = dict(categories.get(_OUTCOME_FAMILY) or {})
        if not rows:
            return _persist_delta(Path(vault_root), current, categories, today)
        if not audit_module.open_plan_item(values):
            # Item-wide, and the only case that is: a closed item is not a
            # finding under ANY binding.
            _drop_item_entries(pages, str(path), str(key), today)
            _prune_missing_joined(Path(vault_root), pages, today)
            categories[_OUTCOME_FAMILY] = pages
            return _persist_delta(Path(vault_root), current, categories, today)
        plan_fields = {str(name) for row in rows for name in dict(row.get("join") or {}).values()}
        moved = previous is None or any(
            audit_module.join_key([name], previous) != audit_module.join_key([name], values)
            for name in sorted(plan_fields)
        )
        if not moved:
            # Each binding's stored entry keeps its OWN joined records; only the
            # item's title and status could have moved, and they move in all of
            # them.
            for existing in _item_entries(pages, str(path), str(key)):
                component = {
                    **(existing.get("component") or {}),
                    "item_path": str(path),
                    "item_key": str(key),
                    "item_title": str(values.get("title") or key),
                    "item_status": str(values.get("status") or "open"),
                }
                _put_outcome_entry(
                    pages,
                    str(path),
                    str(key),
                    _entry_records(existing),
                    _compose_outcome_entry(
                        Path(vault_root), component, sorted(_stored_joined(existing))
                    ),
                    today,
                )
            _prune_missing_joined(Path(vault_root), pages, today)
            categories[_OUTCOME_FAMILY] = pages
            return _persist_delta(Path(vault_root), current, categories, today)
        planning = _load_manifest(Path(vault_root), str(manifest.path)) or manifest
        item = _SyntheticItem(str(path), str(key), dict(values))
        for row in rows:
            # One binding at a time, and each one writes only its own entry --
            # including writing None, which is how a join move that leaves ONE
            # source behind retracts that source's finding without touching the
            # other's.
            records_path = str(row.get("records") or "")
            records = _load_manifest(Path(vault_root), records_path)
            if records is None:
                continue
            join = dict(row.get("join") or {})
            record_fields = list(join)
            plan_side = [join[name] for name in record_fields]
            wanted = audit_module.join_key(plan_side, values)
            snapshot = (
                _unfiltered_snapshot(Path(vault_root), records) if wanted is not None else None
            )
            matched = (
                [
                    record
                    for record in snapshot.records
                    if audit_module.join_key(record_fields, record.values) == wanted
                ]
                if snapshot is not None
                else []
            )
            joined = {(record.source.path, record.identity.key) for record in matched}
            entry = (
                _compose_outcome_entry(
                    Path(vault_root),
                    audit_module.outcome_component(records, planning, join, item),
                    sorted(joined),
                )
                if joined
                else None
            )
            _put_outcome_entry(pages, str(path), str(key), records_path, entry, today)
        _prune_missing_joined(Path(vault_root), pages, today)
        categories[_OUTCOME_FAMILY] = pages
        return _persist_delta(Path(vault_root), current, categories, today)


@dataclass(frozen=True)
class _SyntheticIdentity:
    key: str


@dataclass(frozen=True)
class _SyntheticSource:
    path: str


class _SyntheticItem:
    """The written item in the shape `outcome_component` reads, without a re-read.

    The caller just committed these bytes, so re-loading the collection to hand
    the composer an adapter record would be reading back its own write.
    """

    def __init__(self, path: str, key: str, values: dict[str, Any]) -> None:
        self.source = _SyntheticSource(path)
        self.identity = _SyntheticIdentity(key)
        self.values = values


def block_for_structured_write(
    vault_root: Path,
    manifest: Any,
    *,
    path: str,
    key: str,
    values: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    """The block a structured mutation response may carry, or None.

    Produces only. Emission is decided at the mutation terminal, exactly as it is
    for a page write: the producer cannot know whether the response will actually
    carry the block, and recording delivery here would burn the session's one
    emission on a response that never showed it.
    """
    profile = str(getattr(manifest, "semantic_profile", "") or "")
    delta = apply_plan_write_delta if profile == "planning" else apply_record_write_delta
    if (
        delta(
            vault_root,
            manifest,
            path=path,
            key=key,
            values=values,
            previous=previous,
            today=today,
        )
        is None
    ):
        return None
    from .governance import egress as egress_module

    with egress_module.disclosure_boundary(Path(vault_root), "due_state_advisory"):
        return served(vault_root, today=today)


# --------------------------------------------------------------------------
# the emission ledger
# --------------------------------------------------------------------------


def _emission_section(payload: dict[str, Any] | None) -> dict[str, Any]:
    section = (payload or {}).get("emission")
    if not isinstance(section, dict):
        return {"writes": 0, "emissions": 0, "last_digest": None, "due_total": 0}
    return {
        "writes": int(section.get("writes") or 0),
        "emissions": int(section.get("emissions") or 0),
        "last_digest": section.get("last_digest"),
        # The size of the last block a caller was actually HANDED — written by
        # `_record_emission` and by nothing else. Without a denominator
        # "0 emissions for 12 writes" is unreadable: it is the behaviour the
        # governance is supposed to produce AND the behaviour of a vault that
        # owed nothing, and a metric that cannot tell those apart cannot be
        # used to claim either one. A vault that delivered nothing therefore
        # reports 0 here, which is the honest answer and the one that makes a
        # counter assertion report `unsupported` rather than pass vacuously.
        "due_total": int(section.get("due_total") or 0),
    }


def _emission_delta(
    payload: dict[str, Any] | None,
    *,
    writes: int = 0,
    emissions: int = 0,
    last_digest: str | None = None,
    due_total: int | None = None,
) -> dict[str, Any]:
    section = _emission_section(payload)
    section["writes"] += writes
    section["emissions"] += emissions
    if last_digest is not None:
        section["last_digest"] = last_digest
    if due_total is not None:
        section["due_total"] = int(due_total)
    return section


def _record_emission(
    vault_root: Path | None, digest: str, *, due_total: int | None = None
) -> None:
    """Persist that a block was DELIVERED, and how big it was. Best effort.

    Called from `mark_emitted`, which the terminal reaches once per response
    that actually carried a block — so this is one small JSON replace per
    delivered advisory, and never inside a mutation critical section.

    `due_total` rides along rather than getting its own write because it is a
    property OF the delivery: the number of items in the block that was handed
    over. Recording it anywhere else would give the field a second definition.
    """
    if not vault_root:
        return
    try:
        payload = load(vault_root) or _UNPERSISTED.get(str(vault_root))
        if payload is None:
            return
        payload = {
            **payload,
            "emission": _emission_delta(
                payload, emissions=1, last_digest=digest, due_total=due_total
            ),
        }
        save(vault_root, payload)
        _remember_unpersisted(vault_root, payload)
    except Exception:  # noqa: BLE001 — a ledger write never breaks a response
        log.debug("could not record the due-state emission", exc_info=True)


def emission_ledger(vault_root: Path) -> dict[str, Any]:
    """The persisted `{writes, emissions, last_digest, due_total}` a projector reads."""
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
        payload = _schedule_reconcile(vault_root, today=today)
        if payload is None:
            return []

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
        # Fail closed, exactly as the decision read does — but the carrier's
        # safe direction is SILENCE, not a raise. Every row below is filtered
        # by a triage decision and a family disposition read out of this store;
        # substituting an empty state would serve every dismissed item as due,
        # which is the nag this slice exists to stop, arriving because a file
        # is corrupt. Serving nothing costs the caller an advisory they can get
        # from `review_memory`; serving everything costs them the trust that
        # dismissing works. The write itself is untouched: this is the advisory
        # attached to the response, not the mutation.
        log.debug("review state unreadable; serving no due state", exc_info=True)
        return []
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
                    if keep is not None:
                        entry = _survivors_only(vault_root, entry, keep)
                        if entry is None:
                            # Every page this finding was ABOUT is withheld from
                            # this audience, so under that audience there is no
                            # finding -- not a finding with a smaller count.
                            continue
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
    return rows


def _excluded_families(state_payload: dict[str, Any]) -> frozenset[str]:
    """Projection categories a family disposition removes from every carrier.

    Named and separate so it is a mechanism a test can remove. There is no
    second filter in `block_for_write`: that path serves through `served_entries`
    and therefore through this one, and a duplicate would be a second opinion
    about what "quiet" means on the write carrier specifically.
    """
    return frozenset(review_state_module.disposition_map(state_payload))


def _record_delivered(vault_root: Path | None, block: dict[str, Any] | None) -> None:
    """Stamp the references a DELIVERED block actually put on the wire.

    Called from `mark_emitted` and from nowhere else, because producing a block
    is not surfacing it. Four things between production and delivery can drop
    it — a batch scope, the change-only governor, a `legacy` detail level, and
    terminal validation — and each of them means the caller was shown nothing.
    Stamping at production time recorded a first surfacing for references that
    reached no one, which is a false measurement in the one ledger whose whole
    purpose is to say what reached a person.

    The block's own `top` is the wire content, so it is exactly the right set:
    a row that only contributed to `total` was never shown. Everything in it is
    already past egress, the triage filter and the disposition filter, because
    that is where `served_entries` put it.

    The fingerprints come from the projection the block was built from: the wire
    block deliberately carries refs and dates only, and widening it to carry a
    fingerprint would put an internal identity on a public surface for the sake
    of a ledger write.
    """
    if not vault_root or not block:
        return
    rows = [row for row in (block.get("top") or []) if isinstance(row, dict)]
    if not rows:
        return
    try:
        projection = load(vault_root) or _UNPERSISTED.get(str(vault_root))
        if projection is None:
            return
        fingerprints = _fingerprints_by_ref(projection)
        entries = []
        for row in rows:
            ref = str(row.get("ref") or "")
            finger = fingerprints.get(ref)
            if ref and finger:
                entries.append((_ref_id(ref), finger))
        if not entries:
            return
        review_state_module.record_surfaced(vault_root, entries, surface="carrier")
    except Exception:  # noqa: BLE001 — a ledger write never breaks a carrier
        log.debug("first-surfaced ledger not recorded for the carrier", exc_info=True)


def _fingerprints_by_ref(payload: dict[str, Any]) -> dict[str, str]:
    """``ref -> fingerprint`` over the whole projection. One pass, no ordering.

    **The coupling this depends on.** A `ref` is a page-level identity and a
    fingerprint is a per-finding one, so the map is only well defined while at
    most one projection row per ref carries a fingerprint. That holds today
    because every category except `unfinished_experiments` stamps a
    `review_partition` into its ref, which makes the ref per-finding too. It is
    a property of how the projection composes refs, not an invariant anything
    else enforces — so a new category that omits the partition would silently
    make this map ambiguous and stamp the wrong identity on the ledger.

    Rather than take first-category-wins on a collision, a conflicting ref is
    DROPPED and logged: an unstamped row is a measurement gap, a wrongly
    stamped one is a false record of what a person was shown.
    """
    out: dict[str, str] = {}
    conflicted: set[str] = set()
    for pages in (payload.get("categories") or {}).values():
        if not isinstance(pages, dict):
            continue
        for entries in pages.values():
            for entry in _unbucket(entries):
                ref = str(entry.get("ref") or "")
                finger = str(entry.get("fingerprint") or "")
                if not ref or not finger:
                    continue
                seen = out.get(ref)
                if seen is None:
                    out[ref] = finger
                elif seen != finger:
                    conflicted.add(ref)
    for ref in conflicted:
        log.debug(
            "projection ref %s carries more than one fingerprint; "
            "not stamping the first-surfaced ledger for it",
            ref,
        )
        out.pop(ref, None)
    return out


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


def block_for_batch(
    vault_root: Path, *, today: dt.date | None = None
) -> dict[str, Any] | None:
    """The block a completed multi-write batch may carry, or None.

    The same serve as `block_for_write`, inside the same `due_state_advisory`
    disclosure boundary and with the same produce-only posture — emission is
    decided at the mutation terminal, after the batch scope has exited. It
    differs in one way only: it applies no delta of its own, because a batch's
    per-write deltas have already been applied (the operation leaves call
    `_apply_batch_deltas`, and `reconcile` full-recomputes) by the time the
    invocation reaches its terminal. Serving here therefore reports the
    POST-batch projection; re-deltaing one arbitrary path of the batch would
    report a number that belongs to no moment in particular.
    """
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
    _record_emission(vault_root, digest, due_total=int(payload.get("total") or 0))
    # This is the moment the block became something a person was shown, so it is
    # the moment the first-surfaced ledger may record its refs. See
    # `_record_delivered`.
    _record_delivered(vault_root, payload)


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
