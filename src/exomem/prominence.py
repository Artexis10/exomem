"""Prominence level — one knob that governs how much Exomem speaks up.

Where `mode` answers "how much of this machine may exomem use?", prominence answers
the orthogonal question: **"how much should Exomem participate in a conversation?"**
The two are deliberately separate. A laptop on battery can still want maximal recall;
a workstation can still want Exomem to stay out of the way.

Four canonical levels, over three behavioural axes — recall, capture, narration:

- **off**      — explicit invocation only. No proactive recall, no proactive capture.
- **light**    — recall only when a turn is clearly on-topic or the user asks;
                 capture only on request. Silent.
- **balanced** — recall on topic match; capture durable conclusions. Quiet, mentions
                 the KB only when it returned something. The default where hooks exist.
- **maximal**  — recall before every substantive turn; capture at every stepping
                 stone; say what was recalled and saved. The default where hooks do not.

Why the default differs by client: on filesystem clients the capture/retrieve hooks
re-arm the check every turn, so `balanced` prose is enough. Web clients have no hooks,
so instruction text is the only lever — and passive prose decays over a long thread,
which is exactly the "auto-save quietly never fires" failure the hooks exist to fix.
There, `maximal` holds the same real-world behaviour `balanced` gets for free
elsewhere. See `default_for_surface`.

Resolution precedence mirrors `mode`: `EXOMEM_PROMINENCE` env → the `prominence` key
in the shared config file (`mode.config_path()`) → the surface default.

The config file is deliberately the SAME one `mode` uses. It is a fixed, shared path
for the same reason documented in `mode.config_path`: the MCP server and the CLI are
often different OS users, and `bootstrap` serves the active level from the server. A
home-relative file would let the CLI write a level the server never reads.

Torch-free and import-cheap by design: `commands.bootstrap` imports this on every
session start.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from . import mode

log = logging.getLogger(__name__)

CANON = ("off", "light", "balanced", "maximal")
_ALIASES = {
    "none": "off",
    "silent": "off",
    "disabled": "off",
    "minimal": "light",
    "low": "light",
    "quiet": "light",
    "default": "balanced",
    "medium": "balanced",
    "normal": "balanced",
    "high": "maximal",
    "max": "maximal",
    "full": "maximal",
    "aggressive": "maximal",
}

#: Where the nudge hooks re-arm the check each turn, prose alone is enough.
DEFAULT_PROMINENCE = "balanced"
#: Where there are no hooks, instruction strength is the only lever.
WEB_DEFAULT_PROMINENCE = "maximal"

#: Surfaces that cannot run hooks: no filesystem to install into, no turn-level
#: re-arming. Everything here defaults to `WEB_DEFAULT_PROMINENCE`.
HOOKLESS_SURFACES = frozenset({"web", "hosted", "chatgpt", "claude-ai", "openai"})

_PROMINENCE_ENV = "EXOMEM_PROMINENCE"
_SURFACE_ENV = "EXOMEM_SURFACE"
#: Mirrors `hosted_runtime.HOSTED_MODE_ENV`. Read directly rather than importing that
#: module: this one is on the bootstrap hot path and must stay cheap to import.
_HOSTED_CELL_ENV = "EXOMEM_HOSTED_CELL"
_CONFIG_KEY = "prominence"

_CAPTURE_EFFECTIVE_TEMPLATE = MappingProxyType(
    {
        "off": MappingProxyType(
            {
                "durable_intent": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": False,
                        "proactive_requires": (),
                    }
                ),
                "observed_outcomes": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": False,
                        "proactive_requires": (),
                    }
                ),
            }
        ),
        "light": MappingProxyType(
            {
                "durable_intent": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": False,
                        "proactive_requires": (),
                    }
                ),
                "observed_outcomes": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": False,
                        "proactive_requires": (),
                    }
                ),
            }
        ),
        "balanced": MappingProxyType(
            {
                "durable_intent": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": True,
                        "proactive_requires": ("authored-proactive", "durable-intent"),
                    }
                ),
                "observed_outcomes": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": True,
                        "proactive_requires": (
                            "authored-proactive",
                            "sufficiently-identified-outcome",
                        ),
                    }
                ),
            }
        ),
        "maximal": MappingProxyType(
            {
                "durable_intent": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": True,
                        "proactive_requires": ("authored-proactive", "durable-intent"),
                    }
                ),
                "observed_outcomes": MappingProxyType(
                    {
                        "authored_explicit": "explicit-user-request",
                        "proactive_permitted": True,
                        "proactive_requires": (
                            "authored-proactive",
                            "sufficiently-identified-outcome",
                        ),
                    }
                ),
            }
        ),
    }
)


@dataclass(frozen=True)
class ProminenceContract:
    """The behavioural contract for one level, over the three axes."""

    level: str
    recall: str
    capture: str
    narration: str
    summary: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "recall": self.recall,
            "capture": self.capture,
            "narration": self.narration,
            "summary": self.summary,
            "effective_capture": effective_capture(self.level),
        }


CONTRACTS: dict[str, ProminenceContract] = {
    "off": ProminenceContract(
        level="off",
        recall="Never search memory unless the user explicitly asks you to.",
        capture="Never write to memory unless the user explicitly asks you to.",
        narration="Say nothing about memory unless asked.",
        summary="Explicit invocation only.",
    ),
    "light": ProminenceContract(
        level="light",
        recall=(
            "Search memory only when the user asks a recall question outright, or "
            "when the turn is unmistakably about a topic the knowledge base covers. "
            "When in doubt, do not search."
        ),
        capture=(
            "Write to memory only when the user asks. Do not capture on your own "
            "judgment, however durable the conclusion looks."
        ),
        narration=(
            "Never narrate memory activity. Fold retrieved facts into the answer "
            "with a citation and nothing more."
        ),
        summary="Recall when asked or clearly on-topic; capture on request; silent.",
    ),
    "balanced": ProminenceContract(
        level="balanced",
        recall=(
            "Search memory first when a turn references a project, a domain, a named "
            "entity, or asks what was concluded, tried, or decided. Skip it for "
            "chit-chat, control messages, and fresh tasks with no prior context."
        ),
        capture=(
            "Capture when the conversation reaches a stepping stone: a durable "
            "conclusion lands, a recurring entity accumulates reusable facts, or a "
            "method was carried out and the user reports how it went. Not "
            "mid-thought exploration, tangents, or unresolved questions. "
            "Route stated intent to Planning and observed outcome to Records. "
            "Transition only on explicit user intent; otherwise leave Planning "
            "unchanged or, under the resolved posture, propose a bounded review."
        ),
        narration=(
            "Stay quiet. Mention memory only when a search returned something you "
            "used, and report one line after a write."
        ),
        summary="Recall on topic match; capture durable conclusions; quiet.",
    ),
    "maximal": ProminenceContract(
        level="maximal",
        recall=(
            "Search memory before answering any substantive turn, not only the ones "
            "that obviously reference prior work. Assume the knowledge base may hold "
            "something relevant until a search says otherwise. Only skip for pure "
            "chit-chat and control messages."
        ),
        capture=(
            "Capture at every stepping stone, and treat the bar for 'durable' as low: "
            "a decision, a resolved problem, a diagnosed failure, a reusable pattern, "
            "a fact about a recurring entity, a method you actually ran and how it "
            "turned out. When torn between capturing and letting it pass, capture. "
            "Prefer a real page over a mental note, and do not wait to be asked. "
            "Route stated intent to Planning and observed outcome to Records. "
            "Transition only on explicit user intent; otherwise leave Planning "
            "unchanged or, under the resolved posture, propose a bounded review."
        ),
        narration=(
            "Say what you did. Name what you recalled and cite it; state one line "
            "after every write. The user should be able to see memory working "
            "without asking."
        ),
        summary="Recall before every substantive turn; capture every stepping stone; says so.",
    ),
}

#: Hook tunables per level, so the level changes behaviour and not only prose.
#: Empty string means "unset this variable and take the hook's own default".
#: Keep in sync with `_hooks/exomem_retrieve_nudge.py` and `_hooks/exomem_capture_nudge.py`.
_HOOK_PRESETS: dict[str, dict[str, str]] = {
    "off": {
        "EXOMEM_RETRIEVE_NUDGE_DISABLE": "1",
        "EXOMEM_CAPTURE_NUDGE_DISABLE": "1",
    },
    "light": {
        "EXOMEM_RETRIEVE_NUDGE_DISABLE": "",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS": "80",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS": "180",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": "900",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": "1800",
        "EXOMEM_CAPTURE_NUDGE_DISABLE": "",
        "EXOMEM_CAPTURE_NUDGE_MIN_CHARS": "800",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": "900",
    },
    "balanced": {
        "EXOMEM_RETRIEVE_NUDGE_DISABLE": "",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS": "20",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS": "180",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": "300",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": "900",
        "EXOMEM_CAPTURE_NUDGE_DISABLE": "",
        "EXOMEM_CAPTURE_NUDGE_MIN_CHARS": "300",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": "300",
    },
    "maximal": {
        "EXOMEM_RETRIEVE_NUDGE_DISABLE": "",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS": "0",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS": "180",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": "0",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": "0",
        "EXOMEM_CAPTURE_NUDGE_DISABLE": "",
        "EXOMEM_CAPTURE_NUDGE_MIN_CHARS": "120",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": "60",
    },
}


def normalize(value: str | None) -> str | None:
    """Canonical level for a raw string (accepting aliases), or None if unknown."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v in CANON:
        return v
    return _ALIASES.get(v)


def detect_surface() -> str | None:
    """Best-effort surface identity: explicit `EXOMEM_SURFACE`, else hosted detection.

    Returns None when this is an ordinary local install, which is the case that wants
    the hook-backed `balanced` default.
    """
    explicit = os.environ.get(_SURFACE_ENV, "").strip().lower()
    if explicit:
        return explicit
    if _truthy(os.environ.get(_HOSTED_CELL_ENV)):
        return "hosted"
    return None


def _truthy(value: str | None) -> bool:
    """Shared truthiness convention (mirrors `mode._truthy`)."""
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def default_for_surface(surface: str | None = None) -> str:
    """The shipped default for a client surface.

    Hookless surfaces (claude.ai, ChatGPT, hosted) default to `maximal` because
    instruction text is their only lever and it decays over long threads. Everything
    else defaults to `balanced` and leans on the nudge hooks to re-arm.

    Passing no surface auto-detects; pass one explicitly to ask "what would this
    surface get?" without changing the environment.
    """
    resolved_surface = surface if surface is not None else detect_surface()
    if resolved_surface and resolved_surface.strip().lower() in HOOKLESS_SURFACES:
        return WEB_DEFAULT_PROMINENCE
    return DEFAULT_PROMINENCE


def resolve(surface: str | None = None) -> str:
    """Active level: `EXOMEM_PROMINENCE` env → config file → surface default.

    Mirrors `mode.resolve`'s precedence exactly, including reading the config file
    explicitly rather than injecting it into the environment, so an exported
    `EXOMEM_PROMINENCE` always wins.
    """
    from_env = normalize(os.environ.get(_PROMINENCE_ENV))
    if from_env:
        return from_env

    raw = mode.read_config().get(_CONFIG_KEY)
    from_config = normalize(raw if isinstance(raw, str) else None)
    if from_config:
        return from_config
    if raw not in (None, ""):
        log.warning("ignoring invalid %s=%r in config; using default", _CONFIG_KEY, raw)

    return default_for_surface(surface)


def contract(level: str | None = None, surface: str | None = None) -> ProminenceContract:
    """The behavioural contract for a level (defaults to the active one)."""
    resolved_level = normalize(level) or resolve(surface)
    return CONTRACTS[resolved_level]


def effective_capture(level: str | None = None, surface: str | None = None) -> dict:
    """Return one level's detached capture gate for workflow-contract consumers."""
    resolved_level = normalize(level) or resolve(surface)
    return {
        kind: {
            "authored_explicit": rule["authored_explicit"],
            "proactive_permitted": rule["proactive_permitted"],
            "proactive_requires": list(rule["proactive_requires"]),
        }
        for kind, rule in _CAPTURE_EFFECTIVE_TEMPLATE[resolved_level].items()
    }


def capture_policy_projection() -> dict:
    """Return the complete, detached level-to-effective-capture table."""
    return {level: effective_capture(level) for level in CANON}


def hook_env(level: str | None = None, surface: str | None = None) -> dict[str, str]:
    """Hook tunables for a level. Empty value means "unset and use the hook default"."""
    resolved_level = normalize(level) or resolve(surface)
    return dict(_HOOK_PRESETS[resolved_level])


def resolved(surface: str | None = None) -> dict:
    """Bootstrap-shaped view of the active prominence policy."""
    level = resolve(surface)
    return {
        "level": level,
        "source": _active_source(),
        "surface": surface if surface is not None else detect_surface(),
        "contract": CONTRACTS[level].as_dict(),
        "levels": list(CANON),
        "change_with": "exomem prominence <level>",
    }


def _active_source() -> str:
    """Where the active level came from — useful when a setting appears not to apply."""
    if normalize(os.environ.get(_PROMINENCE_ENV)):
        return "env"
    raw = mode.read_config().get(_CONFIG_KEY)
    if isinstance(raw, str) and normalize(raw):
        return "config"
    return "default"


def write_prominence(value: str) -> Path:
    """Persist a level to the config file (atomic). Accepts aliases. Raises on unknown.

    Deliberately mirrors `mode.write_mode`: same file, same `schema` key, same atomic
    swap — so the two settings never fight over the config, and a `prominence` write
    cannot drop a `mode` the user already set.
    """
    canonical = normalize(value)
    if canonical is None:
        raise ValueError(
            f"unknown prominence: {value!r} "
            f"(expected one of {CANON} or an alias {tuple(_ALIASES)})"
        )
    path = mode.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = mode.read_config()
    data.update(schema=1, prominence=canonical)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    os.replace(tmp, path)  # atomic swap
    return path
