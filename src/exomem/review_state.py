"""Stable Epistemic Inbox identity and portable review decisions."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import context_refs, memory_refs
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
STATE_FILENAME = ".review-state.json"
REVIEW_PREFIX = "exomem://review/"
VALID_ACTIONS = frozenset({"dismiss", "snooze", "reopen", "competing"})
VALID_VIEWS = frozenset({"open", "all", "snoozed", "dismissed", "competing"})
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


def component_fingerprints(
    vault_root: Path,
    item: Any,
) -> list[str]:
    """Every per-finding fingerprint folded into one fused attention item.

    Deduplicated and returned in a stable order. The item's own fused
    fingerprint is NOT included -- the caller records that one separately,
    because it is the identity attention itself reports and round-trips.
    """
    reasons = list(getattr(item, "reasons", None) or [])
    if not reasons:
        return []
    anchor = str(getattr(item, "path", "") or "")
    paths = [anchor]
    for reason in reasons:
        paths.extend(reason.get("related_paths") or [])
    refs = refs_for_paths(vault_root, paths)
    target_ref = getattr(item, "target_ref", None) or refs.get(anchor)
    if not target_ref:
        return []
    out: list[str] = []
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
        if value not in out:
            out.append(value)
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

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "records": {}}
        from . import vault

        try:
            # Not `read_text`: on Windows an ordinary open pins this file
            # against replacement while it is held, so a status poll or a
            # review-context read overlapping a triage write refused that write
            # with WinError 32 and surfaced it as a bare 500. Review state is
            # derived bookkeeping, not a canonical record whose exact bytes a
            # reader has verified and must hold still.
            payload = json.loads(vault.read_bytes_without_pinning(self.path).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"REVIEW_STATE_INVALID: cannot read {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            raise ValueError(
                f"REVIEW_STATE_INVALID: unsupported review state schema in {self.path}"
            )
        records = payload.get("records")
        if not isinstance(records, dict):
            raise ValueError(f"REVIEW_STATE_INVALID: records must be an object in {self.path}")
        return payload

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
        timestamp = (now or dt.datetime.now(dt.UTC)).astimezone(
            dt.UTC
        ).isoformat().replace("+00:00", "Z")
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
                    "why": str(why).strip() if why else None,
                    "updated_at": timestamp,
                }
                records[key] = record
                decision = record
            self._write(payload)
        return {
            "item_id": review_id,
            "ref": review_ref(review_id),
            "fingerprint": signal_fingerprint,
            "state": _ACTION_STATE[action],
            "decision": decision,
        }

    def _write(self, payload: dict[str, Any]) -> None:
        from . import vault

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{STATE_FILENAME}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # A reader that has not yet let go is a wait, not a failure. Only
            # readers can be concurrent here -- writes to review state run
            # under the writer lease -- so there is no precondition that the
            # wait could invalidate, and nothing to re-prove afterwards.
            vault.replace_tolerating_transient_sharing(
                lambda: os.replace(temp_name, self.path)
            )
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _record_key(review_id: str, signal_fingerprint: str) -> str:
    return f"{review_id}:{signal_fingerprint}"


def _parse_until(value: str | None) -> dt.date:
    if not value:
        raise ValueError("INVALID_SNOOZE_DATE: snooze requires `until` as YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("INVALID_SNOOZE_DATE: `until` must be YYYY-MM-DD") from exc
