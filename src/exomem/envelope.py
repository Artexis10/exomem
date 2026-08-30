"""The delegation envelope — per-action-class authority under hard ceilings.

Where `prominence` answers "how much should Exomem participate in a
conversation?", the envelope answers the question underneath it: **"what is
Exomem allowed to do on its own, for this KIND of action?"**

Three things are deliberately separate, and keeping them separate is the whole
design:

- **Ceilings are product law.** Every action class carries a hard ceiling. No
  prominence level, override, configuration value or adaptation may authorize
  behaviour above it. `restructure_execution` is confirm-required at every
  level, in v1, always.
- **The envelope chooses a disposition BELOW the ceiling.** Three classes carry
  a range; two are fixed; `disclosure` carries none at all and is served marked
  governance-owned, because the governance plane owns cross-boundary release.
- **Prominence only sets the defaults.** Absent an explicit override, each
  configurable disposition is a pure derivation from the active level
  (`derive_envelope`), so the served envelope is always attributable to
  (level, overrides) and nothing else.

Storage rides the same shared per-machine config file `mode` and `prominence`
use (`mode.config_path()`), for the same reason: the MCP server and the CLI are
often different OS users, and `bootstrap` serves the active envelope from the
server. What travels with the vault is the family-disposition store (portable
review state); machine posture stays here. An absent `envelope` object means
pure derivation, which is exactly today's shipped behaviour — so rollback is
deleting a key rather than a migration.

**Write-time strictness, read-time tolerance.** An unknown class id or an
out-of-range disposition is refused at write with a named, class-specific
error. The same thing found in the STORED file — written, say, by a newer
runtime — is reported and ignored at read. Reading the envelope must never
break bootstrap.

Torch-free and import-cheap by design: `commands.op_bootstrap` serves this on
every session start.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import mode

log = logging.getLogger(__name__)

#: The closed v1 set. A new action class arrives only through a spec change: an
#: unclassified future behaviour has no envelope cell and therefore no
#: authority, which is the fail-closed posture the authority matrix requires.
ACTION_CLASSES: tuple[str, ...] = (
    "hygiene_writes",
    "proactive_capture",
    "link_acceptance",
    "structural_suggestions",
    "restructure_execution",
    "disclosure",
)

#: The hard ceiling per class. Product law: nothing below may exceed it.
CEILINGS: dict[str, str] = {
    "hygiene_writes": "silent",
    "proactive_capture": "silent-capable",
    "link_acceptance": "confirm",
    "structural_suggestions": "advisory",
    "restructure_execution": "confirm-required",
    "disclosure": "governance-owned",
}

#: The three configurable classes and the closed range each one may take.
RANGES: dict[str, tuple[str, ...]] = {
    "proactive_capture": ("off", "advisory", "silent"),
    "link_acceptance": ("off", "advisory", "confirm-shortcut"),
    "structural_suggestions": ("off", "advisory"),
}

#: The two classes whose disposition is not configurable at all.
FIXED: dict[str, str] = {
    "hygiene_writes": "silent",
    "restructure_execution": "confirm",
}

#: Carries no disposition; the governance plane owns it end to end.
GOVERNANCE_OWNED: frozenset[str] = frozenset({"disclosure"})

#: Why `confirm-shortcut` sits BELOW the `confirm` ceiling rather than beside it.
CONFIRM_SHORTCUT: str = (
    "an inline single-action confirmation rendered with the surfaced item — one "
    "action approving that one named acceptance. The confirmation step itself is "
    "never skipped"
)

#: The sole specified error for `restructure_execution`. Standing delegation
#: ("do this kind of thing from now on") would be an envelope cell ABOVE the
#: current ceiling; it does not exist in v1, and only a deliberate founder
#: ratification may ever create one. Pinned as its own refusal so it cannot be
#: shadowed by the generic range refusal, and so the capability cannot drift in
#: as a convenience.
FOUNDER_GATE: str = (
    "standing delegation of restructure execution would be an envelope cell above "
    "the current ceiling. It does not exist in v1, and only a deliberate founder "
    "ratification may ever create one"
)

#: The confirm-required contract, served rather than implied.
#:
#: Three tiers bind it: this marker, the agent contract's in-conversation
#: confirmation, and the server-side gates that exist today. Only two of the
#: four `restructure_execution` surfaces HAVE a server-side gate, and the
#: contract says so instead of letting an agent infer one — v1 adds no new
#: confirmation parameter, because that is a tool-schema change behind the
#: documented two-phase rollout.
#:
#: Command-free on purpose, exactly like the epistemic commitments:
#: `commands._filter_bootstrap_payload` deletes any string naming a command the
#: active surface cannot call, and a ceiling that vanished on a reduced surface
#: would be a ceiling nobody was told about.
CONFIRM_REQUIRED: str = (
    "restructure_execution is confirm-required at every level: obtain explicit user "
    "confirmation before a restructure application, supersession commit, entity "
    "creation or deletion. Deletion has a server-side confirm parameter and adoption "
    "apply is preview-first; supersession and entity creation have no server-side "
    "gate today — named future work, not an implied one"
)

_CONFIG_KEY = "envelope"


def _normalize_class(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalize_disposition(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def derive_envelope(level: str) -> dict[str, str]:
    """``action class -> disposition`` for one prominence level. Pure; no I/O.

    The design's derivation table, and the only place it exists. `disclosure`
    is absent because it carries no disposition to derive.

    Two rows are worth stating rather than reading off the table. `hygiene_writes`
    and `restructure_execution` are fixed, so they do not vary by level — they
    are returned anyway, because a caller asking "what is the envelope at this
    level" wants the whole envelope, not the configurable part of it.
    `proactive_capture` is `silent` at `balanced`, not `advisory`: balanced has
    captured stepping stones on its own since the prominence contract shipped,
    and `silent` here means "acts without asking" — narration remains the
    prominence axis it already is. Tightening it would turn every routine
    capture into a question, which is a nag increase inside the programme that
    exists to remove nags.
    """
    resolved = str(level or "").strip().lower()
    proactive = "silent" if resolved in {"balanced", "maximal"} else "off"
    advisory = "off" if resolved == "off" else "advisory"
    return {
        "hygiene_writes": FIXED["hygiene_writes"],
        "proactive_capture": proactive,
        "link_acceptance": advisory,
        "structural_suggestions": advisory,
        "restructure_execution": FIXED["restructure_execution"],
    }


def stored_overrides() -> tuple[dict[str, str], list[dict]]:
    """``(usable overrides, ignored rows)`` read from the shared config file.

    Never raises and never refuses. An unknown class id or an out-of-range
    value — a newer runtime's, most plausibly — is reported in the second
    element and dropped from the first, because bootstrap serves this payload
    and a config it cannot fully understand must still produce an envelope.
    """
    raw = mode.read_config().get(_CONFIG_KEY)
    if raw in (None, ""):
        return {}, []
    if not isinstance(raw, dict):
        log.warning("ignoring malformed %s in config: %r", _CONFIG_KEY, type(raw).__name__)
        return {}, [{"class": None, "value": None, "reason": "malformed_envelope"}]

    usable: dict[str, str] = {}
    ignored: list[dict] = []
    for key in sorted(raw):
        action_class = _normalize_class(key)
        value = raw[key]
        disposition = _normalize_disposition(value) if isinstance(value, str) else None
        if action_class not in ACTION_CLASSES:
            ignored.append({"class": str(key), "value": value, "reason": "unknown_class"})
            continue
        if action_class in GOVERNANCE_OWNED:
            ignored.append(
                {"class": action_class, "value": value, "reason": "governance_owned"}
            )
            continue
        if action_class in FIXED:
            if disposition != FIXED[action_class]:
                ignored.append({"class": action_class, "value": value, "reason": "fixed"})
            continue
        if disposition not in RANGES[action_class]:
            ignored.append({"class": action_class, "value": value, "reason": "out_of_range"})
            continue
        usable[action_class] = disposition
    return usable, ignored


def active(level: str | None = None) -> dict[str, str]:
    """``action class -> the disposition in force``, overrides applied."""
    resolved_level = level or _active_level()
    derived = derive_envelope(resolved_level)
    overrides, _ignored = stored_overrides()
    derived.update(overrides)
    return derived


def resolved(level: str | None = None, surface: str | None = None) -> dict:
    """Bootstrap-shaped view: every class, its ceiling, disposition and provenance.

    `disclosure` appears with a null disposition and `governance-owned`
    provenance rather than being omitted: a client that cannot see the class at
    all would have no way to learn that the class exists and is somebody else's
    to decide.
    """
    from . import prominence as prominence_module

    resolved_level = prominence_module.normalize(level) or prominence_module.resolve(surface)
    derived = derive_envelope(resolved_level)
    overrides, ignored = stored_overrides()

    classes: dict[str, dict] = {}
    for action_class in ACTION_CLASSES:
        if action_class in GOVERNANCE_OWNED:
            classes[action_class] = {
                "ceiling": CEILINGS[action_class],
                "disposition": None,
                "provenance": "governance-owned",
            }
            continue
        if action_class in FIXED:
            # BEFORE the override lookup, deliberately. `stored_overrides`
            # already drops a stored value for a fixed class, so this branch is
            # the second of two independent refusals — and it is the one on the
            # path that actually reaches a client. A ceiling that held only
            # because the storage reader happened to filter correctly would be
            # an accident rather than product law.
            provenance = "fixed"
            disposition = FIXED[action_class]
        elif action_class in overrides:
            provenance = "override"
            disposition = overrides[action_class]
        else:
            provenance = "derived"
            disposition = derived[action_class]
        classes[action_class] = {
            "ceiling": CEILINGS[action_class],
            "disposition": disposition,
            "provenance": provenance,
        }
    served: dict = {
        "level": resolved_level,
        "classes": classes,
        "confirm_required": CONFIRM_REQUIRED,
    }
    if ignored:
        # Present only when there is something to report. An always-present empty
        # list would spend bytes on every session start to say nothing, and this
        # payload is the entire contract a hookless client receives.
        served["ignored"] = ignored
    return served


def _active_level() -> str:
    from . import prominence as prominence_module

    return prominence_module.resolve()


# --------------------------------------------------------------- write surface


def set_disposition(action_class: str, disposition: str) -> Path:
    """Persist one explicit override (atomic). Refuses everything out of bounds.

    The refusal order is deliberate. `restructure_execution` is checked before
    the value is looked at at all, so the founder-gate refusal is the SOLE error
    that class can produce — a generic range message would let a standing
    delegation request read as a typo.
    """
    name = _normalize_class(action_class)
    if name not in ACTION_CLASSES:
        raise ValueError(
            f"UNKNOWN_ACTION_CLASS: {action_class!r} is not a v1 envelope action class. "
            f"Valid: {list(ACTION_CLASSES)}"
        )
    if name in GOVERNANCE_OWNED:
        raise ValueError(
            f"DISCLOSURE_IS_GOVERNANCE_OWNED: {name} is not envelope-configurable; the "
            "governance plane owns cross-boundary disclosure"
        )
    if name == "restructure_execution":
        raise ValueError(f"STANDING_DELEGATION_REFUSED: {FOUNDER_GATE}")
    if name in FIXED:
        raise ValueError(
            f"ENVELOPE_CLASS_FIXED: {name} is fixed at {FIXED[name]!r} and carries no range"
        )
    value = _normalize_disposition(disposition)
    if value not in RANGES[name]:
        raise ValueError(
            f"ENVELOPE_DISPOSITION_OUT_OF_RANGE: {name} accepts {list(RANGES[name])}, "
            f"not {disposition!r}"
        )
    stored = mode.read_config().get(_CONFIG_KEY)
    envelope_object = dict(stored) if isinstance(stored, dict) else {}
    envelope_object[name] = value
    return _write_envelope(envelope_object)


def reset_disposition(action_class: str) -> Path:
    """Drop one override so the class returns to pure derivation."""
    name = _normalize_class(action_class)
    if name not in ACTION_CLASSES:
        raise ValueError(
            f"UNKNOWN_ACTION_CLASS: {action_class!r} is not a v1 envelope action class. "
            f"Valid: {list(ACTION_CLASSES)}"
        )
    stored = mode.read_config().get(_CONFIG_KEY)
    envelope_object = dict(stored) if isinstance(stored, dict) else {}
    if name not in envelope_object:
        # Nothing to clear. Writing anyway would rewrite the shared config file
        # for a no-op, which is exactly the kind of write that races with
        # another process's `mode` change.
        return mode.config_path()
    envelope_object.pop(name)
    return _write_envelope(envelope_object)


def _write_envelope(envelope_object: dict[str, str]) -> Path:
    """Atomic swap into the shared config, mirroring `prominence.write_prominence`.

    Same file, same `schema` key, same read-modify-write — so an envelope write
    can never drop a `mode` or `prominence` the user already set. An envelope
    with no overrides left removes the key entirely rather than storing `{}`,
    because "absent means derived" is the rollback contract and an empty object
    should not be a second spelling of it.
    """
    path = mode.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = mode.read_config()
    data["schema"] = 1
    if envelope_object:
        data[_CONFIG_KEY] = envelope_object
    else:
        data.pop(_CONFIG_KEY, None)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, path)  # atomic swap
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path
