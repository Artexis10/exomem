"""One bounded broadcast of the vocabulary a session has not been told about.

A session that did not make a promotion learns of it from the bootstrap `new`
marker, which many sessions never read. This block tells such a session once,
where it works: in the activation packet and in the response of a committed
mutating call. It lists at most `MAX_ENTRIES` entries inside `MAX_CHARS`, each
as registry, key, label, a one-line description and the parent where one exists.

Disclosure comes first. The producer takes the registries and instances the
caller is admitted to through the bootstrap's own admission path
(`contract.admission_refusal`, then `contract.history_refusal`) before it hashes,
reads or counts anything. An instance the caller cannot admit therefore changes
neither the digest, the entries, their order nor the `more` count, and a
withheld promotion is indistinguishable from none.

Delivery is recorded on the due-state emission ledger, keyed by session,
audience and vault. The ledger holds `<admitted digest>@<newest save stamp>`:
the next block lists only saves newer than the stamp, and nothing is owed while
the digest is unchanged. Nothing is recorded until a response carries the block.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import due_state

log = logging.getLogger(__name__)

#: The ledger scope that keeps this carrier's value apart from the due-state digests.
SCOPE = "vocabulary_delta"
MAX_ENTRIES = 5
MAX_CHARS = 700
_LABEL_CHARS = 48
_DESCRIPTION_CHARS = 100
#: The terminal key that carries a produced block until the projection delivers it.
PENDING_KEY = "_vocabulary_delta"


@dataclass(frozen=True)
class Pending:
    """A block that is owed to the caller and not yet handed over."""

    block: dict[str, Any]
    claim: str


def size(block: Mapping[str, Any]) -> int:
    """The characters a block costs, as it travels."""
    return len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))


def _line(text: object, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _scopes(root: Path) -> list[str | None]:
    from .vocabulary import instances

    try:
        document = instances.configuration(root)
    except instances.AssignmentRequired:
        document = None
    return [None] if document is None else [None, *sorted(document["private"])]


def _admitted(root: Path) -> list[tuple[Any, Any]]:
    """`(spec, snapshot)` of every registry instance this caller is admitted to."""
    from .vocabulary import contract, instances, registry, registry_specs

    admitted = []
    scopes = _scopes(root)
    for base in registry_specs().values():
        for scope in scopes:
            try:
                spec = instances.select(root, base, scope)
                # Admission precedes every read: a refused instance contributes nothing.
                if contract.admission_refusal(root, spec) is None:
                    admitted.append((spec, registry.load(spec, root)))
            except registry.RegistryError:
                continue
    return admitted


def _digest(admitted: list[tuple[Any, Any]]) -> str:
    rows = sorted((spec.name, spec.instance_id, snapshot.effective_digest) for spec, snapshot in admitted)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:24]


#: Owed rows by (vault, admitted digest, last-told stamp, window start): the digest is the content identity of
#: everything admitted, so a save, a revert or a different audience reads another key. Bounded, cleared when full.
_OWED_ROWS: dict[tuple[str, str, str, str], list[tuple[str, str, Any]]] = {}
_OWED_ROWS_CAP = 16


def _additions(root: Path, admitted: list[tuple[Any, Any]], digest: str, *, after: str) -> list[tuple[str, str, Any]]:
    """`(stamp, registry, entry)` of the admitted additions newer than `after`, newest first."""
    from .vocabulary import bootstrap

    since = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=bootstrap.NEW_WINDOW_DAYS)
    key = (str(root), digest, after, since.isoformat())
    if key not in _OWED_ROWS:
        if len(_OWED_ROWS) >= _OWED_ROWS_CAP:
            _OWED_ROWS.clear()
        _OWED_ROWS[key] = _read_additions(root, admitted, since=since, after=after)
    return _OWED_ROWS[key]


def _read_additions(root: Path, admitted: list[tuple[Any, Any]], *, since: dt.date, after: str) -> list[tuple[str, str, Any]]:
    from .registry_history import HistoryAdmissionRefused
    from .vocabulary import bootstrap, contract

    found: dict[tuple[str, str], tuple[str, Any]] = {}
    for spec, snapshot in admitted:
        if contract.history_refusal(root, spec) is not None:
            continue
        try:
            rows = bootstrap.added_entries(root, spec, snapshot, since=since, after=after)
        except HistoryAdmissionRefused:
            continue
        for stamp, key in rows:
            held = found.get((spec.name, key))
            if held is None or stamp > held[0]:
                found[(spec.name, key)] = (stamp, snapshot.entries[key])
    ordered = [(stamp, name, entry) for (name, _key), (stamp, entry) in sorted(found.items())]
    # A stable reverse sort keeps the registry and key order of saves with one stamp.
    ordered.sort(key=lambda row: row[0], reverse=True)
    return ordered


def _wire(name: str, entry: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"registry": name, "key": entry.key, "label": _line(entry.label or entry.key, _LABEL_CHARS)}
    description = _line(entry.description or entry.guidance, _DESCRIPTION_CHARS)
    if description:
        out["description"] = description
    if entry.parent:
        out["parent"] = entry.parent
    return out


def _fit(rows: list[tuple[str, str, Any]], limit: int) -> dict[str, Any] | None:
    """The newest whole entries that fit `limit`, with a count of the admitted rest."""
    block: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    for _stamp, name, entry in rows[:MAX_ENTRIES]:
        entries = [*entries, _wire(name, entry)]
        trial: dict[str, Any] = {"entries": entries}
        if len(rows) > len(entries):
            trial["more"] = len(rows) - len(entries)
        if size(trial) > limit:
            break
        block = trial
    return block


def pending(vault_root: Path, *, session: str | None = None, limit: int = MAX_CHARS) -> Pending | None:
    """What this caller's conversation is owed, or None.

    Nothing is recorded for a block: `deliver` records it once a response carries it. The one thing
    recorded here is a digest change that owes nothing, so the next call is a compare, not a read.
    """
    root = Path(vault_root)
    if due_state.conversation_session(session) is None:
        return None
    admitted = _admitted(root)
    digest = _digest(admitted)
    held_digest, _, through = (due_state.acknowledged(SCOPE, vault_root=root, session=session) or "").partition("@")
    if held_digest == digest:
        return None
    rows = _additions(root, admitted, digest, after=through)
    claim = f"{digest}@{rows[0][0] if rows else through}"
    block = _fit(rows, min(limit, MAX_CHARS)) if rows else None
    if block is not None:
        return Pending(block, claim)
    if not rows or limit >= MAX_CHARS:
        # Nothing to say, or an entry no block could ever hold: record the digest so the next call is a compare.
        due_state.claim(SCOPE, claim, vault_root=root, session=session)
    return None


def deliver(owed: Pending, vault_root: Path, *, session: str | None = None) -> dict[str, Any] | None:
    """The block, only for the call that records its delivery."""
    return owed.block if due_state.claim(SCOPE, owed.claim, vault_root=Path(vault_root), session=session) else None


def _suggestions_off() -> bool:
    from . import envelope

    return envelope.active().get("structural_suggestions") == "off"


def for_packet(vault_root: Path, packet: dict[str, Any], *, session: str | None = None) -> None:
    """Attach the owed block to an activation packet, counted inside its `limit_chars`. Never raises."""
    try:
        if (packet.get("abstention") or {}).get("reason") == "disabled" or _suggestions_off():
            return
        budget = packet.get("budget") or {}
        used = int(budget.get("used_chars") or 0)
        room = int(budget.get("limit_chars") or 0) - used
        if room <= 0:
            return
        owed = pending(vault_root, session=session, limit=room)
        block = deliver(owed, vault_root, session=session) if owed is not None else None
        if block is not None:
            packet["vocabulary_delta"] = block
            held = packet.setdefault("budget", {})
            held["used_chars"] = used + size(block)
            if "serialized_chars" in held:
                # The packet grew after its length was measured; the field names what is sent.
                from . import context_views

                held.pop("serialized_chars")
                held["serialized_chars"] = context_views.serialized_chars(packet)
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("vocabulary delta unavailable (non-fatal)", exc_info=True)
        packet.pop("vocabulary_delta", None)


def with_pending(vault_root: Path, terminal: Any) -> Any:
    """Carry the owed block on a committed terminal until the projection delivers it. Never raises."""
    from . import mutation_terminal

    if (
        not isinstance(terminal, Mapping)
        or terminal.get("_terminal") != mutation_terminal._TERMINAL_MARKER
        or terminal.get("version") != mutation_terminal._TERMINAL_VERSION
        or terminal.get("state") != "committed"
        or terminal.get("ok") is not True
    ):
        return terminal
    try:
        if _suggestions_off():
            return terminal
        owed = pending(vault_root)
    except Exception:  # noqa: BLE001 - optional guidance cannot change a committed outcome
        log.debug("vocabulary delta unavailable (non-fatal)", exc_info=True)
        return terminal
    if owed is None:
        return terminal
    return {**terminal, PENDING_KEY: {"block": owed.block, "claim": owed.claim, "vault": str(vault_root)}}


def delivered_by(terminal: Mapping[str, Any]) -> dict[str, Any] | None:
    """The block a projected response carries, recorded as delivered; None when it carries none."""
    carried = terminal.get(PENDING_KEY)
    if not isinstance(carried, Mapping):
        return None
    try:
        owed = Pending(dict(carried["block"]), str(carried["claim"]))
        return deliver(owed, Path(str(carried["vault"])))
    except Exception:  # noqa: BLE001 - an ungovernable advisory is not delivered
        log.debug("vocabulary delta delivery unavailable (non-fatal)", exc_info=True)
        return None
