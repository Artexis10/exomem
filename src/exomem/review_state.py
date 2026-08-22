"""Stable Epistemic Inbox identity and portable review decisions."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import context_refs, memory_refs
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
#: Schemas this runtime can READ. A v1 file is migrated in memory on load and
#: rewritten as v2 on the next write; anything newer is refused, which is the
#: correct fail-closed posture for a vault-local file whose writers upgrade
#: together (see the `add-nag-governance-and-metrics-capture` design, D7).
_READABLE_SCHEMA_VERSIONS = frozenset({1, 2})
STATE_FILENAME = ".review-state.json"
#: Raised from 4 MiB with the sectioned schema, and MEASURED rather than picked.
#:
#: The number. At the cardinality the design's stress gate names — 50,000
#: decision records and 150,000 ledger entries, a decade of heavy use — the
#: store measures 41.28 MiB as written (indented) and 32.7 MiB compact. The
#: design proposed 16 MiB; that was chosen before anything was measured and it
#: cannot hold this store. The floor is arithmetic, not encoding style: the keys
#: alone are `review_id:fingerprint`, 49 hex characters plus quoting, so 200,000
#: of them cost 10.1 MiB before a single value, and the leanest plausible record
#: and ledger values take the total to 29.0 MiB. 64 MiB is the measured worst
#: case with roughly 1.5x margin over the written size.
#:
#: What this ceiling does NOT do, stated plainly because the previous wording
#: implied otherwise. It does not bound the store, and neither does compaction.
#: Compaction bounds the LEDGER (an entry with no standing decision behind it
#: goes at 400 days) and lapsed snoozes (90 days). It does not bound standing
#: decisions and it must not: a dismissal is a decision somebody made and
#: nothing here is entitled to forget it. Because the file is a whole-file
#: rewrite, its size grows linearly with the standing decisions a vault
#: accumulates, and no retention rule will ever bring it back down.
#:
#: So the migration trigger is unbounded standing decisions, not this
#: arithmetic. Raising the ceiling buys headroom for the ledger's transient
#: bulk; the day the stress gate fails is the day the sectioned schema has to
#: become append-plus-compaction or SQLite. This constant is a ceiling on a
#: pathological store, not an expectation — a live store compacts past 1 MiB
#: and sits orders of magnitude below it.
_STATE_READ_LIMIT = 64 * 1024 * 1024
#: How much further the RECOVERY path may read.
#:
#: Failing closed on an over-limit store is only safe if there is a way back,
#: and there is exactly one: compaction, which has to read the file first. With
#: a single limit a store that crossed the ceiling could never be compacted, so
#: the refusal would be a permanent lockout rather than a refusal. The reconcile
#: path — operator-invoked, once, whose entire job is to bring the file back
#: under the ceiling — reads at four times the ordinary limit. Four rather than
#: unbounded because a recovery read is still a read into memory; it is the
#: margin that covers a store that drifted past the ceiling, not one that is
#: pathological in a different way.
_RECOVERY_READ_LIMIT_FACTOR = 4


def recovery_read_limit() -> int:
    """The elevated limit the reconcile healer reads at. Derived, never restated."""
    return _STATE_READ_LIMIT * _RECOVERY_READ_LIMIT_FACTOR


REVIEW_PREFIX = "exomem://review/"
#: The family namespace, alongside `corpus_aware`'s write-advisory namespace. A
#: family reference addresses a KIND of signal rather than one occurrence of it.
FAMILY_PREFIX = f"{REVIEW_PREFIX}family/"
VALID_ACTIONS = frozenset({"dismiss", "snooze", "reopen", "competing"})
VALID_VIEWS = frozenset({"open", "all", "snoozed", "dismissed", "competing"})
#: The three dispositions a registered signal family can be in. `normal` is the
#: absence of a record, not a stored value, so setting it clears (mirroring
#: `reopen`).
DISPOSITIONS: tuple[str, ...] = ("normal", "quiet", "off")
DISPOSITION_ACTIONS = frozenset(DISPOSITIONS)
#: The closed triage-reason vocabulary. It rides the existing free-text `why` as
#: a leading colon-terminated token, so no tool input parameter moves for it.
#: `unspecified` is the fallback and is never an error for an item decision; a
#: `quiet` or `off` disposition requires one of the others.
REASON_CODES: tuple[str, ...] = (
    "intentional",
    "false_positive",
    "handled",
    "deferred",
    "too_frequent",
    "unspecified",
)
DEFAULT_REASON = "unspecified"
#: Who wrote a record: a person through the explicit triage surface, or the
#: runtime itself. The manual-maintenance metric is the count of `manual`
#: records in a window, so this has to be on every record rather than inferred.
ORIGINS: tuple[str, ...] = ("manual", "automatic")
MANUAL = "manual"
AUTOMATIC = "automatic"
#: Where a signal was first composed onto a served surface.
SURFACES: tuple[str, ...] = ("review", "carrier", "write")

#: Retention. A snooze whose `until` lapsed this long ago is a decision nothing
#: is waiting on; a ledger entry this old with no standing decision behind it is
#: past every window the paired metrics look at. Both are long on purpose:
#: compaction is the irreversible direction.
_SNOOZE_RETENTION_DAYS = 90
_LEDGER_RETENTION_DAYS = 400
#: Compaction runs on write past either threshold, and on reconcile.
_COMPACT_BYTE_THRESHOLD = 1 * 1024 * 1024
_COMPACT_RECORD_THRESHOLD = 20_000
#: When a scan is worth REPEATING, once a threshold is permanently tripped.
#:
#: Standing dismissals are unbounded by design — nothing is entitled to forget
#: a decision somebody made — so a vault that crosses the record threshold never
#: comes back under it, and a naive "compact on every write past the threshold"
#: rescans 30,000 records on every single decision for the rest of the vault's
#: life, finding nothing, forever. The threshold says the store is big; these
#: say whether anything has changed enough for another walk to be worth it.
#: Growth in either section, or a day elapsed, whichever comes first.
_COMPACT_RESCAN_GROWTH = 0.10
_COMPACT_RESCAN_AFTER = dt.timedelta(days=1)
# Every effective state a decision can resolve to, in report order. `all` is a view
# over these, never a state an item is in.
VALID_STATES: tuple[str, ...] = ("open", "snoozed", "dismissed", "competing")
# Actions that leave a standing record; `reopen` clears rather than records.
_RECORDING_ACTIONS = frozenset({"dismiss", "snooze", "competing"})
_ACTION_STATE: dict[str, str] = {
    "reopen": "open",
    "dismiss": "dismissed",
    "snooze": "snoozed",
    "competing": "competing",
}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReviewDecision:
    action: str
    until: str | None
    why: str | None
    updated_at: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "action": self.action,
            "until": self.until,
            "why": self.why,
            "updated_at": self.updated_at,
        }


def state_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / STATE_FILENAME


def item_id(target_ref: str) -> str:
    return hashlib.sha256(target_ref.encode("utf-8")).hexdigest()[:24]


def review_ref(value: str) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) != 24 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: invalid review item id {value!r}")
    return f"{REVIEW_PREFIX}{clean}"


def parse_review_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.lower().startswith(REVIEW_PREFIX):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: expected {REVIEW_PREFIX}<id>")
    raw_id = raw[len(REVIEW_PREFIX) :].lower()
    if len(raw_id) != 24 or any(char not in "0123456789abcdef" for char in raw_id):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: invalid review reference {value!r}")
    return raw_id


# --------------------------------------------------------------------------
# family references and the registry of valid families
# --------------------------------------------------------------------------


def family_ref(family: str) -> str:
    """Render the family namespace for one registered signal family."""
    return f"{FAMILY_PREFIX}{str(family or '').strip()}"


def is_family_ref(value: str) -> bool:
    return str(value or "").strip().lower().startswith(FAMILY_PREFIX)


def parse_family_ref(value: str) -> str:
    """The family a reference names, validated against the registry.

    Validation lives here rather than at the call site because the registry is
    assembled from three modules and a second opinion about what a family IS is
    exactly how a disposition could be recorded that nothing ever reads.
    """
    raw = str(value or "").strip()
    if not raw.lower().startswith(FAMILY_PREFIX):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: expected {FAMILY_PREFIX}<family>")
    family = raw[len(FAMILY_PREFIX) :].strip()
    registered = registered_families()
    if family not in registered:
        raise ValueError(
            f"INVALID_REVIEW_FAMILY: {family!r} is not a registered signal family. "
            f"Valid: {sorted(registered)}"
        )
    return family


def registered_families() -> frozenset[str]:
    """Every name a disposition may be recorded against.

    The triageable attention categories — the default union plus the registered
    opt-in epistemic queues, which is exactly what a due-state count can hand a
    reference out for — plus the write-advisory kinds the write path emits.
    Assembled from the owning modules rather than restated, so a queue that is
    added or retired cannot leave a stale name here.
    """
    from . import attention as attention_module
    from . import corpus_aware as corpus_aware_module

    return frozenset(
        {
            *attention_module._TRIAGEABLE_CATEGORIES,
            *corpus_aware_module._WRITE_ADVISORY_KINDS,
        }
    )


# --------------------------------------------------------------------------
# the closed reason vocabulary
# --------------------------------------------------------------------------


def parse_reason(why: str | None) -> tuple[str, str | None]:
    """``(reason code, the why verbatim)``. The ONE place that knows the vocabulary.

    The code is the leading colon-terminated token of the free text and must
    match a vocabulary word exactly (case- and space-insensitively). Anything
    else — no colon, an unknown word, a colon that merely punctuates a sentence
    — records `unspecified` and is never an error for an item decision. The
    `why` is stored verbatim regardless, because a closed code alone does not
    say what the user actually meant.
    """
    text = str(why).strip() if why else None
    if not text:
        return DEFAULT_REASON, None
    head, separator, _rest = text.partition(":")
    if not separator:
        return DEFAULT_REASON, text
    candidate = head.strip().lower().replace("-", "_")
    if candidate in REASON_CODES:
        return candidate, text
    return DEFAULT_REASON, text


def refs_for_paths(vault_root: Path, paths: list[str]) -> dict[str, str]:
    """Canonical memory refs where available, portable path refs otherwise."""
    clean = list(dict.fromkeys(str(path).replace("\\", "/").lstrip("/") for path in paths))
    memory = memory_refs.ReferenceIndex(vault_root).refs_for_paths(clean)
    out: dict[str, str] = {}
    for path in clean:
        if memory.get(path):
            out[path] = str(memory[path])
        elif path.startswith(f"{kb_dirname()}/Sources/"):
            out[path] = context_refs.source_ref(path)
        else:
            out[path] = context_refs.vault_ref(path)
    return out


def fingerprint(
    *,
    target_ref: str,
    categories: list[str],
    reasons: list[dict],
    related_refs: list[str],
) -> str:
    reason_versions = []
    for reason in reasons:
        meta = reason.get("meta") or {}
        version = meta.get("signal_version")
        if version is None:
            version = hashlib.sha256(
                str(reason.get("detail") or "").encode("utf-8")
            ).hexdigest()[:16]
        reason_versions.append(
            {
                "category": reason.get("category"),
                "version": str(version),
                "related_paths": sorted(reason.get("related_paths") or []),
            }
        )
    payload = {
        "target_ref": target_ref,
        "categories": sorted(categories),
        "related_refs": sorted(related_refs),
        "reasons": sorted(
            reason_versions,
            key=lambda row: (str(row["category"]), str(row["version"])),
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]



def component_fingerprint(
    *,
    target_ref: str,
    reason: dict,
    related_refs: list[str],
) -> str:
    """Compose the single-signal identity of ONE contributing finding.

    An attention item is a FUSED thing: `_rank` folds every queue that flagged a
    page into one item, so its `fingerprint` is composed over all of those
    categories at once. A counter that walks one category alone -- `due_state` --
    can only see its own finding, and composing that finding's identity by hand
    produced a DIFFERENT fingerprint for the same `item_id`, which
    `effective_state` then read as "the signal materially changed" and re-raised
    forever. A dismissal through the review surface never reached the counter.

    This is the one composer for that per-finding identity. Both sides call it:
    `due_state` to ask "has this been triaged?", and `apply_for_item` to record
    a decision against every component of the fused item the user actually
    triaged. If they ever compose it differently again, dismissal silently stops
    working -- so nothing else may build it privately.
    """
    return fingerprint(
        target_ref=target_ref,
        categories=[str(reason.get("category") or "")],
        reasons=[reason],
        related_refs=sorted(related_refs),
    )


def component_paths(item: Any) -> list[str]:
    """Every vault path one fused item's components need resolved to a ref.

    Separate and public so a caller counting over MANY items can resolve them
    all in one `refs_for_paths` — that call opens a database connection, and
    per-item resolution turned a 103-item view into 103 connections.
    """
    reasons = list(getattr(item, "reasons", None) or [])
    if not reasons:
        return []
    paths = [str(getattr(item, "path", "") or "")]
    for reason in reasons:
        paths.extend(reason.get("related_paths") or [])
    return paths


def component_fingerprints(
    vault_root: Path,
    item: Any,
    *,
    with_category: bool = False,
    refs: dict[str, str] | None = None,
) -> list[Any]:
    """Every per-finding fingerprint folded into one fused attention item.

    Deduplicated and returned in a stable order. The item's own fused
    fingerprint is NOT included -- the caller records that one separately,
    because it is the identity attention itself reports and round-trips.

    `with_category=True` returns `(category, fingerprint)` pairs instead of bare
    fingerprints, in the same order and with the same dedup. It exists so a
    caller that needs to know WHICH family a component belongs to does not
    re-derive the composition by hand: a second derivation that drifts from this
    one produces keys `apply_for_item` never recorded, which is silent
    under-counting rather than a failure.

    `refs` lets a caller that already resolved the paths (see `component_paths`)
    supply the map instead of paying a lookup per item.
    """
    reasons = list(getattr(item, "reasons", None) or [])
    if not reasons:
        return []
    anchor = str(getattr(item, "path", "") or "")
    if refs is None:
        refs = refs_for_paths(vault_root, component_paths(item))
    target_ref = getattr(item, "target_ref", None) or refs.get(anchor)
    if not target_ref:
        return []
    out: list[Any] = []
    for reason in reasons:
        related = sorted(
            {
                path
                for path in (reason.get("related_paths") or [])
                if path != anchor and path in refs
            }
        )
        value = component_fingerprint(
            target_ref=str(target_ref),
            reason=reason,
            related_refs=[refs[path] for path in related],
        )
        entry = (str(reason.get("category") or ""), value) if with_category else value
        if entry not in out:
            out.append(entry)
    return out


def apply_for_item(
    vault_root: Path,
    item: Any,
    *,
    action: str,
    review_id: str | None = None,
    until: str | None = None,
    why: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Record one triage decision for a fused item AND each of its components.

    The fused fingerprint is what attention reports and what a client round-trips
    into `expected_fingerprint`, so it stays the identity in the RESULT and is
    recorded first, unchanged -- attention's own semantics do not move.

    The fan-out exists because single-category consumers (`due_state`) key on a
    component fingerprint that the fused one never equals. Recording the same
    decision, with the same `until` and `why`, against every component is what
    makes "dismiss it in the review surface and the counter goes quiet" true.
    Doing it at DECISION time rather than at read time is deliberate: a reader
    cannot know which fused item a user was looking at, and guessing would let a
    dismissal leak across signals the user never saw.

    `reopen` needs no fan-out -- `apply` clears every record under the item id,
    component records included -- but it still routes through here so there is
    exactly one place that knows this.
    """
    store = ReviewStateStore(vault_root)
    review_id = str(review_id or getattr(item, "item_id", None) or "")
    fused = str(getattr(item, "fingerprint", None) or "")
    result = store.apply(review_id, fused, action=action, until=until, why=why, now=now)
    if str(action or "").strip().lower() == "reopen":
        return result
    components = list(component_fingerprints(vault_root, item))
    # Signals that share this identity but are not folded into the fused item,
    # because they come from registered-but-opt-in queues the default surface
    # does not show. `attention.item_by_ref` attaches them; without this, a ref
    # published by a due-state count could be "dismissed" while the count that
    # published it carried on, or while a different signal was put down instead.
    components.extend(getattr(item, "triage_components", None) or [])
    for component in dict.fromkeys(components):
        if component == fused:
            continue
        store.apply(review_id, component, action=action, until=until, why=why, now=now)
    return result


class ReviewStateStore:
    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.path = state_path(vault_root)

    def load(self, *, read_limit: int | None = None) -> dict[str, Any]:
        """The store, or an empty one when the file is absent.

        Raises `REVIEW_STATE_INVALID` on anything else. That is deliberate and
        it is what every caller that reads DECISIONS must inherit: an unreadable
        decision store answered as an empty one silently resurrects every
        dismissal in the vault. `read_limit` raises the ceiling for the one
        recovery path allowed past it (see `recovery_read_limit`).
        """
        from . import reserved_paths

        try:
            with reserved_paths._subsystem_authority_scope("review_state"):
                raw = reserved_paths._read_owner_bytes(
                    self.vault_root,
                    self.path,
                    "review-state",
                    limit=_STATE_READ_LIMIT if read_limit is None else int(read_limit),
                )
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return empty_state()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"REVIEW_STATE_INVALID: cannot read {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") not in _READABLE_SCHEMA_VERSIONS:
            raise ValueError(
                f"REVIEW_STATE_INVALID: unsupported review state schema in {self.path}"
            )
        records = payload.get("records")
        if not isinstance(records, dict):
            raise ValueError(f"REVIEW_STATE_INVALID: records must be an object in {self.path}")
        return _migrated(payload)

    def decision(
        self,
        review_id: str,
        signal_fingerprint: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> ReviewDecision | None:
        state = payload if payload is not None else self.load()
        record = state["records"].get(_record_key(review_id, signal_fingerprint))
        if not isinstance(record, dict):
            return None
        action = str(record.get("action") or "")
        if action not in _RECORDING_ACTIONS:
            return None
        return ReviewDecision(
            action=action,
            until=str(record["until"]) if record.get("until") else None,
            why=str(record["why"]) if record.get("why") else None,
            updated_at=str(record.get("updated_at") or ""),
        )

    def effective_state(
        self,
        review_id: str,
        signal_fingerprint: str,
        *,
        today: dt.date | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, ReviewDecision | None]:
        decision = self.decision(
            review_id,
            signal_fingerprint,
            payload=payload,
        )
        if decision is None:
            return "open", None
        if decision.action == "dismiss":
            return "dismissed", decision
        if decision.action == "competing":
            # A competing-alternatives stance never expires on a clock: it is
            # fingerprint-bound, so editing either rival is what reopens it.
            return "competing", decision
        current = today or dt.date.today()
        until = _parse_until(decision.until)
        return ("snoozed", decision) if until >= current else ("open", decision)

    def apply(
        self,
        review_id: str,
        signal_fingerprint: str,
        *,
        action: str,
        until: str | None = None,
        why: str | None = None,
        now: dt.datetime | None = None,
        origin: str = MANUAL,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"INVALID_REVIEW_ACTION: action must be one of {sorted(VALID_ACTIONS)}"
            )
        parsed_until: str | None = None
        if action == "snooze":
            parsed_until = _parse_until(until).isoformat()
        elif until:
            raise ValueError("INVALID_REVIEW_ACTION: `until` is valid only for snooze")

        key = _record_key(review_id, signal_fingerprint)
        moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        timestamp = _stamp(moment)
        reason, verbatim = parse_reason(why)
        with _LOCK:
            payload = self.load()
            records = payload["records"]
            if action == "reopen":
                for existing in [
                    record_key
                    for record_key in records
                    if record_key.startswith(f"{review_id}:")
                ]:
                    records.pop(existing, None)
                decision = None
            else:
                record = {
                    "item_id": review_id,
                    "fingerprint": signal_fingerprint,
                    "action": action,
                    "until": parsed_until,
                    "why": verbatim,
                    "reason": reason,
                    "origin": origin if origin in ORIGINS else MANUAL,
                    "updated_at": timestamp,
                }
                records[key] = record
                decision = record
            _compact_if_due(payload, now=moment, path=self.path)
            self._write(payload)
        return {
            "item_id": review_id,
            "ref": review_ref(review_id),
            "fingerprint": signal_fingerprint,
            "state": _ACTION_STATE[action],
            "decision": decision,
        }

    # ------------------------------------------------------------------
    # dispositions
    # ------------------------------------------------------------------

    def set_disposition(
        self,
        family: str,
        disposition: str,
        *,
        why: str | None = None,
        now: dt.datetime | None = None,
        origin: str = MANUAL,
    ) -> dict[str, Any]:
        """Record or clear one family's disposition. `normal` clears the record.

        A `quiet` or `off` decision requires a real reason code: silencing a
        whole family with no stated ground is exactly the decision a reader six
        months later cannot evaluate, and the dispositions view exists to be
        read.
        """
        disposition = str(disposition or "").strip().lower()
        if disposition not in DISPOSITION_ACTIONS:
            raise ValueError(
                f"INVALID_REVIEW_ACTION: disposition must be one of {sorted(DISPOSITION_ACTIONS)}"
            )
        reason, verbatim = parse_reason(why)
        if disposition != "normal" and reason == DEFAULT_REASON:
            raise ValueError(
                "INVALID_REVIEW_REASON: quieting a family requires a reason code as the "
                f"leading token of `why`, one of {sorted(set(REASON_CODES) - {DEFAULT_REASON})}"
            )
        moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        timestamp = _stamp(moment)
        with _LOCK:
            payload = self.load()
            dispositions = payload["dispositions"]
            if disposition == "normal":
                record = dispositions.pop(family, None)
                stored: dict[str, Any] = {
                    "family": family,
                    "disposition": "normal",
                    "reason": reason,
                    "why": verbatim,
                    "updated_at": timestamp,
                    "origin": origin if origin in ORIGINS else MANUAL,
                }
                if record is None:
                    # Nothing to clear: still a valid, idempotent decision, and
                    # writing nothing keeps the store free of `normal` rows that
                    # would then have to be filtered out of every read.
                    stored["cleared"] = False
                else:
                    stored["cleared"] = True
                    _compact_if_due(payload, now=moment, path=self.path)
                    self._write(payload)
                return stored
            stored = {
                "family": family,
                "disposition": disposition,
                "reason": reason,
                "why": verbatim,
                "updated_at": timestamp,
                "origin": origin if origin in ORIGINS else MANUAL,
            }
            dispositions[family] = stored
            _compact_if_due(payload, now=moment, path=self.path)
            self._write(payload)
        return dict(stored)

    # ------------------------------------------------------------------
    # retention and compaction
    # ------------------------------------------------------------------

    def compact(
        self,
        *,
        force: bool = False,
        now: dt.datetime | None = None,
        read_limit: int | None = None,
    ) -> dict[str, Any]:
        """Drop what retention allows and report it. Runs under the store lock.

        Never drops a standing dismissal, a competing stance, or a disposition:
        those are decisions somebody made and nothing here is entitled to
        forget them. What it does drop is a snooze whose clock ran out long ago
        and a ledger entry nothing is waiting on.
        """
        moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        with _LOCK:
            payload = self.load(read_limit=read_limit)
            report = _compact_payload(payload, now=moment)
            if force or report["dropped"]["records"] or report["dropped"]["surfaced"]:
                self._write(payload)
        return report

    def _write(self, payload: dict[str, Any]) -> None:
        from . import reserved_paths

        encoded = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        with reserved_paths._subsystem_authority_scope("review_state"):
            reserved_paths._publish_owner_bytes(
                self.vault_root,
                self.path,
                "review-state",
                encoded,
            )


# --------------------------------------------------------------------------
# schema: the sectioned store, and the forward migration into it
# --------------------------------------------------------------------------


def empty_state() -> dict[str, Any]:
    """A fresh v2 payload. One definition, because five readers build this."""
    return {
        "version": SCHEMA_VERSION,
        "records": {},
        "dispositions": {},
        "surfaced": {},
        "stats": {},
    }


def _migrated(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring a loaded payload up to the current schema, in memory.

    A v1 file has one flat `records` section and no reason, origin, disposition
    or ledger. Its records carry `origin: manual` because v1 could only be
    written by the triage surface, so calling them anything else would be a
    lie about who decided. The reason is parsed from the `why` that was already
    stored, so a coded dismissal made before the vocabulary existed is read the
    same way afterwards. Nothing is written here: the rewrite happens on the
    next write, which is the one moment the file is already being replaced.
    """
    for section in ("dispositions", "surfaced", "stats"):
        if not isinstance(payload.get(section), dict):
            payload[section] = {}
    if payload.get("version") == SCHEMA_VERSION:
        return payload
    for record in payload["records"].values():
        if not isinstance(record, dict):
            continue
        record.setdefault("origin", MANUAL)
        if "reason" not in record:
            record["reason"] = parse_reason(record.get("why"))[0]
    payload["version"] = SCHEMA_VERSION
    return payload


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# dispositions: reading them
# --------------------------------------------------------------------------


def disposition_map(payload: dict[str, Any] | None) -> dict[str, str]:
    """``family -> quiet|off`` for every family with a non-default disposition."""
    stored = (payload or {}).get("dispositions") or {}
    if not isinstance(stored, dict):
        return {}
    out: dict[str, str] = {}
    for family, record in stored.items():
        if not isinstance(record, dict):
            continue
        value = str(record.get("disposition") or "").strip().lower()
        if value in {"quiet", "off"}:
            out[str(family)] = value
    return out


def disposition_for(family: str, *, payload: dict[str, Any] | None) -> str:
    return disposition_map(payload).get(str(family), "normal")


def manual_records_since(payload: dict[str, Any] | None, *, since: dt.datetime) -> int:
    """The manual-maintenance metric, computable from the store alone."""
    since = since.astimezone(dt.UTC)
    total = 0
    for record in ((payload or {}).get("records") or {}).values():
        if not isinstance(record, dict) or record.get("origin") != MANUAL:
            continue
        stamped = _parse_stamp(record.get("updated_at"))
        if stamped is not None and stamped >= since:
            total += 1
    return total


def manual_dismissals_by_family(
    payload: dict[str, Any] | None, families: dict[str, list[str]]
) -> dict[str, int]:
    """Per-family manual dismissal counts, from a caller-supplied key index.

    The store keys records by `review_id:fingerprint` and knows nothing about
    which family produced a signal, so the caller that CAN answer that supplies
    the mapping. Guessing it here would put a second, weaker opinion about
    signal identity in the one module that must have exactly one.
    """
    records = (payload or {}).get("records") or {}
    out: dict[str, int] = {}
    for family, keys in families.items():
        count = 0
        for key in keys:
            record = records.get(key)
            if (
                isinstance(record, dict)
                and record.get("action") == "dismiss"
                and record.get("origin", MANUAL) == MANUAL
            ):
                count += 1
        out[family] = count
    return out


# --------------------------------------------------------------------------
# the first-surfaced ledger
# --------------------------------------------------------------------------


def record_surfaced(
    vault_root: Path,
    entries: Any,
    *,
    surface: str,
    now: dt.datetime | None = None,
    known: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Stamp the first time each signal reached a served surface. Best effort.

    Returns `key -> first_surfaced_at` for every entry, whether it was already
    on the ledger or has just been added, so the caller can annotate what it is
    about to return without a second read.

    `known` is a payload the caller has already loaded this pass. When every
    entry is on it there is nothing to add, so the whole store access is
    skipped — which is the steady state for a signal that keeps resurfacing,
    and the reason a large store does not pay a second full read on every
    write. A first surfacing still reloads under the lock, because writing the
    caller's older snapshot back would silently drop any decision recorded in
    between.

    Failure-isolated on purpose: a read surface must not fail, slow past its
    budget, or change its content because the ledger could not be written. An
    unwritable store simply records the entry on a later surfacing, and the
    caller still gets a value to annotate with — the timestamp is what this
    pass observed, which is the honest answer when nothing can be persisted.

    Concurrency, stated rather than implied. `_LOCK` is in-process; the file is
    a whole-file rewrite, so two PROCESSES writing it in the same instant can
    lose one of the writes. That race is the pre-existing one every decision
    write has always had, and this function does not close it. What it does do
    is stay out of it: `known=` makes the steady state — a signal that keeps
    resurfacing — cost no write at all, so the window is opened only by a
    genuine first surfacing, and what is at risk is one ledger row that the
    next surfacing re-adds. A file lock here would put cross-process contention
    on a read surface's latency budget to protect a best-effort measurement.
    """
    if surface not in SURFACES:
        raise ValueError(f"INVALID_SURFACE: surface must be one of {list(SURFACES)}")
    pairs = [
        (str(review_id), str(fingerprint))
        for review_id, fingerprint in entries
        if review_id and fingerprint
    ]
    if not pairs:
        return {}
    timestamp = _stamp(now or dt.datetime.now(dt.UTC))
    stamps: dict[str, str] = {}
    if known is not None:
        cached = _cached_stamps(pairs, known)
        if cached is not None:
            return cached
    store = ReviewStateStore(vault_root)
    try:
        with _LOCK:
            payload = store.load()
            ledger = payload["surfaced"]
            added = False
            for review_id, fingerprint in pairs:
                key = _record_key(review_id, fingerprint)
                existing = ledger.get(key)
                if isinstance(existing, dict) and existing.get("first_surfaced_at"):
                    stamps[key] = str(existing["first_surfaced_at"])
                    continue
                ledger[key] = {
                    "first_surfaced_at": timestamp,
                    "surface": surface,
                    # The runtime decided to show this, not a person.
                    "origin": AUTOMATIC,
                }
                stamps[key] = timestamp
                added = True
            if added:
                store._write(payload)
    except (OSError, ValueError) as error:
        log.debug("first-surfaced ledger not recorded: %s", error)
        for review_id, fingerprint in pairs:
            stamps.setdefault(_record_key(review_id, fingerprint), timestamp)
    return stamps


def _cached_stamps(
    pairs: list[tuple[str, str]], known: dict[str, Any]
) -> dict[str, str] | None:
    """Every pair's existing stamp, or None if any one of them is unrecorded.

    All-or-nothing on purpose: one missing entry means the store has to be
    written anyway, and a partial answer would let a caller annotate half its
    report from a snapshot and half from a fresh read.
    """
    ledger = known.get("surfaced") or {}
    if not isinstance(ledger, dict):
        return None
    cached: dict[str, str] = {}
    for review_id, fingerprint in pairs:
        key = _record_key(review_id, fingerprint)
        existing = ledger.get(key)
        if not isinstance(existing, dict) or not existing.get("first_surfaced_at"):
            return None
        cached[key] = str(existing["first_surfaced_at"])
    return cached


def first_surfaced_map(payload: dict[str, Any] | None) -> dict[str, str]:
    ledger = (payload or {}).get("surfaced") or {}
    if not isinstance(ledger, dict):
        return {}
    return {
        str(key): str(row["first_surfaced_at"])
        for key, row in ledger.items()
        if isinstance(row, dict) and row.get("first_surfaced_at")
    }


# --------------------------------------------------------------------------
# retention and compaction
# --------------------------------------------------------------------------


def _is_standing(record: Any) -> bool:
    """A decision nothing is allowed to forget."""
    return (
        isinstance(record, dict)
        and str(record.get("action") or "") in {"dismiss", "competing"}
    )


def _compact_payload(payload: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
    """Apply retention in place and return the drop report. Caller holds the lock."""
    today = now.date()
    records = payload["records"]
    dropped_records = [
        key
        for key, record in records.items()
        if isinstance(record, dict)
        and str(record.get("action") or "") == "snooze"
        and (until := _safe_date(record.get("until"))) is not None
        and until < today - dt.timedelta(days=_SNOOZE_RETENTION_DAYS)
    ]
    for key in dropped_records:
        records.pop(key, None)

    ledger = payload["surfaced"]
    horizon = now - dt.timedelta(days=_LEDGER_RETENTION_DAYS)
    dropped_ledger = [
        key
        for key, row in ledger.items()
        if isinstance(row, dict)
        and not _is_standing(records.get(key))
        and (stamped := _parse_stamp(row.get("first_surfaced_at"))) is not None
        and stamped < horizon
    ]
    for key in dropped_ledger:
        ledger.pop(key, None)

    report = {
        "at": _stamp(now),
        # Compaction is the runtime deciding, so what it writes says so. The
        # standing decisions it preserves are untouched and keep `manual`.
        "origin": AUTOMATIC,
        "dropped": {"records": len(dropped_records), "surfaced": len(dropped_ledger)},
        "retention_days": {
            "lapsed_snooze": _SNOOZE_RETENTION_DAYS,
            "surfaced": _LEDGER_RETENTION_DAYS,
        },
    }
    # Only a compaction that REMOVED something records itself. A no-op that
    # stamped `stats` would make the payload differ from the file on every
    # scan, so a store that can never drop anything again — the ordinary end
    # state, since standing decisions are permanent — would rewrite itself for
    # the sake of a timestamp saying nothing happened.
    if dropped_records or dropped_ledger:
        payload["stats"]["compaction"] = report
    return report


def _compact_if_due(
    payload: dict[str, Any], *, now: dt.datetime, path: Path | None = None
) -> None:
    """Compact on write past a declared threshold, but never pointlessly. Lock held.

    Two gates, and both are needed.

    The THRESHOLD gate says the store is large enough to be worth walking. The
    size test reads the file already on disk rather than re-encoding the
    payload: re-encoding to decide whether to encode costs a second full
    serialization of a store that can be tens of megabytes, on the one path a
    user is waiting on. The file is the previous write's bytes, which for a
    threshold is exactly as good an answer and free.

    The RESCAN gate says anything has changed since the last walk. It exists
    because the threshold gate is permanently tripped on any vault that reaches
    it: standing dismissals are never dropped, so a store that crossed 20,000
    records stays across it forever, and without this every subsequent decision
    would pay a full scan of every record and every ledger entry to discover
    that retention still allows nothing. Ten percent growth in either section,
    or a day since the last walk, is what makes another one worth its cost.
    """
    if not _past_threshold(payload, path=path):
        return
    if not _rescan_warranted(payload["stats"].get("compaction_scan"), payload, now=now):
        return
    _compact_payload(payload, now=now)
    # The post-compaction counts, deliberately: growth is measured from what the
    # last walk left behind, so a store that drops nothing does not re-arm
    # itself on its own leftovers.
    payload["stats"]["compaction_scan"] = {
        "at": _stamp(now),
        "records": len(payload["records"]),
        "surfaced": len(payload["surfaced"]),
    }


def _past_threshold(payload: dict[str, Any], *, path: Path | None) -> bool:
    if len(payload["records"]) >= _COMPACT_RECORD_THRESHOLD:
        return True
    try:
        size = path.stat().st_size if path is not None else 0
    except OSError:
        size = 0
    return size >= _COMPACT_BYTE_THRESHOLD


def _rescan_warranted(
    marker: Any, payload: dict[str, Any], *, now: dt.datetime
) -> bool:
    """Whether another retention walk can plausibly find anything new."""
    if not isinstance(marker, dict):
        return True
    records = len(payload["records"])
    surfaced = len(payload["surfaced"])
    if records > int(marker.get("records") or 0) * (1 + _COMPACT_RESCAN_GROWTH):
        return True
    if surfaced > int(marker.get("surfaced") or 0) * (1 + _COMPACT_RESCAN_GROWTH):
        return True
    stamped = _parse_stamp(marker.get("at"))
    return stamped is None or (now - stamped) >= _COMPACT_RESCAN_AFTER


def _safe_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _record_key(review_id: str, signal_fingerprint: str) -> str:
    return f"{review_id}:{signal_fingerprint}"


def _parse_until(value: str | None) -> dt.date:
    if not value:
        raise ValueError("INVALID_SNOOZE_DATE: snooze requires `until` as YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("INVALID_SNOOZE_DATE: `until` must be YYYY-MM-DD") from exc
