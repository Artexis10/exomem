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

Resolution precedence: `EXOMEM_PROMINENCE` env → the current principal/vault
preference for this request's engagement context → the identity-wide
principal/vault preference → the shared config file (`mode.config_path()`) →
the surface default. The context — `coding` or `conversation` — is derived from
the detected client surface, so one identity can run Balanced while coding and
Maximal in a conversational client. See `context_for_surface`.

One exception sits at the last rung: when the preference record exists but cannot
be READ, the fallback is the generic default rather than the surface default, and
capture drops to `off` for as long as that holds. An unreadable record is not
consent, and the surface default is `maximal` on exactly the clients where the
user has no hook to notice it. `effective_capture_level` is that rule, and every
projection of capture authority — the gate, the served contract, the delegation
envelope's `proactive_capture`, the workflow contract — asks it rather than
`resolve`, because a floor honoured by only some of them grants back in one key
what the others withheld.

Note what this module does NOT do: it never promises that a saved level reaches the
standalone nudge hooks. Those run on the client machine and read only the operator
environment and that machine's config file, which a server has no way to write. A
client that may run them is told so instead — see `hook_cadence`.

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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from . import mode

log = logging.getLogger(__name__)

_REQUEST_PREFERENCE: ContextVar[dict | None] = ContextVar(
    "exomem_request_prominence", default=None
)


def _request_preference(vault_root: Path) -> dict:
    from . import prominence_preferences
    from .cli_ops import OpError
    from .governance.principal import effective_principal

    preference = {"stored": None, "contexts": {}, "revision": "missing"}
    if effective_principal().resolved:
        try:
            preference = prominence_preferences.inspect(vault_root)
        except OpError as exc:
            preference["unavailable"] = exc.code
    return {"vault_root": vault_root, "preference": preference}


@contextmanager
def request_scope(vault_root: Path):
    """Snapshot the addressed user's preference for one canonical invocation."""
    token = _REQUEST_PREFERENCE.set(_request_preference(vault_root))
    try:
        yield
    finally:
        _REQUEST_PREFERENCE.reset(token)


def refresh_request_preference(vault_root: Path) -> None:
    """Refresh the current invocation after its own committed preference change."""
    current = _REQUEST_PREFERENCE.get()
    if current is not None and current["vault_root"] == vault_root:
        _REQUEST_PREFERENCE.set(_request_preference(vault_root))


def _saved_preference() -> dict:
    current = _REQUEST_PREFERENCE.get()
    return current["preference"] if current is not None else {}


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

#: Engagement contexts. Exactly two, both derived from the detected surface: a
#: saved level may differ between a coding client and a conversational one. The
#: context tunes eagerness only — it never selects a principal, vault, storage
#: path or authority ceiling.
CODING_CONTEXT = "coding"
CONVERSATION_CONTEXT = "conversation"
CONTEXTS: tuple[str, ...] = (CODING_CONTEXT, CONVERSATION_CONTEXT)

_PROMINENCE_ENV = "EXOMEM_PROMINENCE"
_SURFACE_ENV = "EXOMEM_SURFACE"
#: Mirrors `hosted_runtime.HOSTED_MODE_ENV`. Read directly rather than importing that
#: module: this one is on the bootstrap hot path and must stay cheap to import.
_HOSTED_CELL_ENV = "EXOMEM_HOSTED_CELL"
_CONFIG_KEY = "prominence"

#: The activation carrier, at `balanced` and `maximal` only.
#:
#: Where the host exposes a prompt lifecycle, the retrieve hook injects the
#: compiled packet before inference and this line is belt and braces. Where it
#: does not — a hosted chat surface is tool-only and the pasted custom-instruction
#: carrier has no headroom — the guidance bootstrap already serves is the ONLY
#: portable carrier there is, so one sentence has to carry the whole contract:
#: call the compiler with the turn, and resolve an `ambiguous` answer by naming
#: the sense rather than guessing at one.
#:
#: Absent at `light` and `off` deliberately: a level whose whole promise is
#: "only when asked" must not then instruct an unprompted call.
#:
#: Two properties are asserted by `tests/test_bootstrap_activation_carrier.py`
#: rather than merely intended. It is byte-identical to the shipped scaffold's
#: recall loop, so a reader of the skill and a reader of the payload follow one
#: contract in one wording. And it is budgeted: the compact bootstrap profile
#: runs within a few hundred bytes of a hard ceiling, so it stays ASCII (a
#: non-ASCII dash costs six JSON bytes) and inside 220.
ACTIVATION_CARRIER_LINE = (
    "Before a substantive turn with no prior context, call `activate_context` "
    "with the turn verbatim; on `ambiguous`, call again with `anchor`."
)

#: The link instruction (`capture-identities-at-write-time` design D2, task
#: 3.1), appended to `balanced` and `maximal` only -- a level that captures
#: only when asked must not be told to link unprompted either. Sized to what
#: was left after paying for it (91 B): all five kinds design D2 names, and
#: "even with no page yet" so the instruction covers the identity that has
#: none, not only the one that already has a page.
LINK_NAMED_IDENTITIES_LINE = (
    "Wikilink named people, places, organisations, equipment and products "
    "even with no page yet."
)

_ARTIFACT_ADOPTION_CAPTURE = (
    " Generated draft stays ephemeral. Selected is not write consent: proactive_capture "
    "preserves exact bytes as Source/Evidence by role, never MIME. No handle means "
    "non-committing handoff. Delivery requires Evidence receipt/Record; no remote byte "
    "inference. Missing schema uses structural_suggestions/restructure_execution; "
    "relations use link_acceptance."
)

#: The episode-completeness pass, appended to `balanced` and `maximal` only.
#: `light` and `off` capture on request, so asking them to sweep would be asking
#: them to capture proactively, which is the thing those levels exist to refuse.
#:
#: The class list is EXAMPLES and says so twice — "for example" opening it and no
#: "only" anywhere. An agent reads a closed list inside a capture contract as a
#: boundary, so an enumeration here would narrow capture rather than complete it,
#: which is the opposite of the point.
#: Cut to the minimum that carries the whole rule, because this text sits inside
#: `engagement` and therefore in FRONT of the action catalogue in the compact
#: payload. The full class list is not lost: the wire block's `consider` key
#: carries all ten, and a client that receives this contract receives that block
#: on its next durable write. What must survive here is the rule, not the roster.
_EPISODE_SWEEP_CAPTURE = (
    " After any capture, make one bounded episode pass over the recent exchange for "
    "anything else that would materially improve a later decision, lookup, task, "
    "comparison or continuation, e.g. an outcome, a preference, a method, an entity "
    "facet or an operational quirk; examples, not a closed set. Never re-write what "
    "the response lists as written recently, and stay silent when nothing qualifies."
)

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

    def as_dict(self, effective_capture: dict | None = None) -> dict:
        """The contract as served. Pass the request's gate when there is one.

        Without one this reports the LEVEL's gate, which is the right answer for
        "what does maximal look like?" and the wrong one for a live request
        under the unreadable-record floor, where the level is `balanced` and
        capture is withheld.
        """
        return {
            "level": self.level,
            "recall": self.recall,
            "capture": self.capture,
            "narration": self.narration,
            "summary": self.summary,
            "effective_capture": (
                effective_capture if effective_capture is not None else capture_gate(self.level)
            ),
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
            ACTIVATION_CARRIER_LINE + " "
            "Search memory for project, domain, entity, or conclusion context. "
            "Skip chit-chat, control messages, and context-free fresh tasks."
        ),
        capture=(
            LINK_NAMED_IDENTITIES_LINE + " "
            "Capture at a stepping stone: a durable conclusion, central or recurring "
            "entity with reusable facts, or method that was carried out with a reported "
            "result. Not "
            "mid-thought exploration, tangents, or unresolved questions. Capture stable "
            "preferences, recurring routines, historical baselines, or durable affiliations "
            "only when stability or recurrence and reusable comparison, interpretation, or "
            "decision value are clear. Route a uniquely resolved Entity facet or affiliation "
            "there; otherwise use one concise compiled observation; use Records only for a "
            "compatible existing measurement. Fleeting preferences, one-offs, incidental "
            "associations, trivia, and tentative claims stay quiet. A concise observation or "
            "narrow Entity facet follows proactive_capture; an affiliation relation requires "
            "link_acceptance; Entity creation or structural change requires confirmed "
            "restructure_execution. Route stated intent to "
            "Planning and observed outcome to Records. "
            "Transition only on explicit user intent; otherwise leave Planning "
            "unchanged or, under the resolved posture, propose a bounded review."
        )
        + _ARTIFACT_ADOPTION_CAPTURE
        + _EPISODE_SWEEP_CAPTURE,
        narration=(
            "Stay quiet; cite useful recall and report a write in one line."
        ),
        summary="Recall on topic match; capture durable conclusions; quiet.",
    ),
    "maximal": ProminenceContract(
        level="maximal",
        recall=(
            ACTIVATION_CARRIER_LINE + " "
            "Search memory before answering any substantive turn, not only the ones "
            "that obviously reference prior work. Assume the knowledge base may hold "
            "something relevant until a search says otherwise. Only skip for pure "
            "chit-chat and control messages."
        ),
        capture=(
            LINK_NAMED_IDENTITIES_LINE + " "
            "Capture at every stepping stone, and treat the bar for 'durable' as low: "
            "a decision, a resolved problem, a diagnosed failure, a reusable pattern, "
            "a fact about a central or recurring entity, or a method you actually ran and "
            "how it turned out. Capture stable preferences, recurring routines, historical "
            "baselines, "
            "or durable affiliations only when stability or recurrence and reusable comparison, "
            "interpretation, or decision value are clear. Route a uniquely resolved Entity "
            "facet or affiliation there; otherwise use one concise compiled observation; use "
            "Records only for a compatible existing measurement. Fleeting preferences, one-offs, "
            "incidental associations, trivia, and tentative claims stay quiet. A concise observation "
            "or narrow Entity facet follows proactive_capture; an affiliation relation requires "
            "link_acceptance; Entity creation or structural change requires confirmed "
            "restructure_execution. When torn between "
            "capturing and letting it pass, capture. "
            "Prefer a real page over a mental note, and do not wait to be asked. "
            "Route stated intent to Planning and observed outcome to Records. "
            "Transition only on explicit user intent; otherwise leave Planning "
            "unchanged or, under the resolved posture, propose a bounded review."
        )
        + _ARTIFACT_ADOPTION_CAPTURE
        + _EPISODE_SWEEP_CAPTURE,
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
    """Detect explicit surface, hosted service, or a known current MCP client.

    Unknown clients keep the generic `balanced` default.
    """
    explicit = os.environ.get(_SURFACE_ENV, "").strip().lower()
    if explicit:
        return explicit
    if _truthy(os.environ.get(_HOSTED_CELL_ENV)):
        return "hosted"
    if _REQUEST_PREFERENCE.get() is None:
        return None
    # Client names affect eagerness only, never authorization. Unknown HTTP
    # clients can be hooked CLIs too, so transport alone is not enough.
    from .command_surface import mcp_caller_identity

    identity = mcp_caller_identity()
    name = (identity.get("client_name") or "").strip().casefold()
    if "codex" in name:
        return "codex"
    if "claude-code" in name or "claude code" in name:
        return "claude-code"
    if name in {"chatgpt", "openai"} or name.startswith("chatgpt/"):
        return "chatgpt"
    if name in {"claude.ai", "claude-ai"} or name.startswith("claude.ai/"):
        return "claude-ai"
    return None


def _truthy(value: str | None) -> bool:
    """Shared truthiness convention (mirrors `mode._truthy`)."""
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def context_for_surface(surface: str | None) -> str:
    """The engagement context one surface belongs to. Pure; no detection.

    Hookless surfaces are conversational; Codex, Claude Code, every other named
    surface, and an absent or unrecognized one are `coding`, which keeps today's
    generic default for unknown clients. Callers that want the live surface pass
    `detect_surface()`; a request argument never supplies the applied context.
    """
    if surface and surface.strip().lower() in HOOKLESS_SURFACES:
        return CONVERSATION_CONTEXT
    return CODING_CONTEXT


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


def _saved_context_level(surface: str | None) -> str | None:
    """The saved level for the context this request belongs to, if any.

    The context is derived from the surface — the detected one when the caller
    passes none — never from a request argument. `surface` is an internal
    "what would this surface get?" knob and is not reachable from the public
    MCP/REST/CLI parameter set.
    """
    contexts = _saved_preference().get("contexts")
    if not isinstance(contexts, dict):
        return None
    applied = context_for_surface(surface if surface is not None else detect_surface())
    return normalize(contexts.get(applied))


def resolve(surface: str | None = None) -> str:
    """Active level: environment → this context's saved value → the identity-wide
    saved value → machine config → surface default."""
    from_env = normalize(os.environ.get(_PROMINENCE_ENV))
    if from_env:
        return from_env

    by_context = _saved_context_level(surface)
    if by_context:
        return by_context

    stored = normalize(_saved_preference().get("stored"))
    if stored:
        return stored

    raw = mode.read_config().get(_CONFIG_KEY)
    from_config = normalize(raw if isinstance(raw, str) else None)
    if from_config:
        return from_config
    if raw not in (None, ""):
        log.warning("ignoring invalid %s=%r in config; using default", _CONFIG_KEY, raw)

    if _saved_preference().get("unavailable"):
        # An unreadable record must never hand a user MORE proactivity than they
        # last chose. The client default is `maximal` on hookless surfaces, so
        # falling through to it turned an identity that may well have saved `off`
        # into the eagerest level Exomem has. The generic default is the honest
        # floor: the record is gone, so the client's own default cannot be the
        # thing that widens it. `effective_capture_level` withholds proactive
        # writes on top, in every projection that speaks about capture authority.
        return DEFAULT_PROMINENCE

    return default_for_surface(surface)


def contract(level: str | None = None, surface: str | None = None) -> ProminenceContract:
    """The behavioural contract for a level (defaults to the active one)."""
    resolved_level = normalize(level) or resolve(surface)
    return CONTRACTS[resolved_level]


def _gate_for(level: str) -> dict:
    """One level's capture gate, straight from the template and nothing else."""
    return {
        kind: {
            "authored_explicit": rule["authored_explicit"],
            "proactive_permitted": rule["proactive_permitted"],
            "proactive_requires": list(rule["proactive_requires"]),
        }
        for kind, rule in _CAPTURE_EFFECTIVE_TEMPLATE[level].items()
    }


def _unreadable_preference_applies(surface: str | None = None) -> bool:
    """True when an unreadable record is what this request actually resolved on.

    Cheap first: a request whose record read fine carries no `unavailable`
    diagnostic at all, so the common path never touches the config file.
    """
    if not _saved_preference().get("unavailable"):
        return False
    return _active_source(surface) == "preference:unavailable"


def effective_capture_level(surface: str | None = None) -> str:
    """The level whose CAPTURE authority this request is under.

    Usually the resolved level. It diverges in one case: an unreadable
    preference record resolves to the generic default so the recall and
    narration contract stays usable, while capture drops to `off`, because the
    identity behind the missing record may be one that saved `off` and an
    unreadable record is not consent to start writing on its behalf.

    Every projection of capture authority -- the gate, the delegation envelope's
    `proactive_capture`, the workflow contract's effective capture -- must ask
    this rather than `resolve`, or one of them grants back what the others
    withheld and the agent is handed a contradiction instead of a policy.
    """
    if _unreadable_preference_applies(surface):
        return "off"
    return resolve(surface)


def capture_gate(level: str | None = None, surface: str | None = None) -> dict:
    """Return the capture gate this request is actually under.

    An explicit `level` asks the detached question -- "what would this level
    give?" -- and is answered from the table untouched, so the payload's own
    level documentation cannot be rewritten by one request's floor. Passing no
    level asks about THIS request, which is where `effective_capture_level`
    applies.
    """
    resolved_level = normalize(level) or effective_capture_level(surface)
    return _gate_for(resolved_level)


def effective_capture(authored_capture: dict[str, str], prominence_level: str) -> dict:
    """Evaluate an authored workflow posture under one active prominence level.

    This is deliberately pure: workflow resolution selects authored policy, while
    public adapters supply the active prominence level that caps it.
    """
    if set(authored_capture) != {"durable_intent", "observed_outcomes"} or any(
        value not in {"explicit", "proactive"} for value in authored_capture.values()
    ):
        raise ValueError("authored capture must name explicit or proactive for both capture kinds")
    if prominence_level not in CANON:
        raise ValueError(f"unknown canonical prominence: {prominence_level!r}")

    gate = _CAPTURE_EFFECTIVE_TEMPLATE[prominence_level]
    return {
        kind: {
            "authored": authored_capture[kind],
            "explicit_user_request_permitted": True,
            "proactive_permitted": (
                authored_capture[kind] == "proactive" and gate[kind]["proactive_permitted"]
            ),
            "proactive_requires": (
                list(gate[kind]["proactive_requires"])
                if authored_capture[kind] == "proactive" and gate[kind]["proactive_permitted"]
                else []
            ),
        }
        for kind in ("durable_intent", "observed_outcomes")
    }


def capture_policy_projection() -> dict:
    """Return the complete, detached level-to-effective-capture table."""
    return {level: _gate_for(level) for level in CANON}


def hook_env(level: str | None = None, surface: str | None = None) -> dict[str, str]:
    """Hook tunables for a level. Empty value means "unset and use the hook default"."""
    resolved_level = normalize(level) or resolve(surface)
    return dict(_HOOK_PRESETS[resolved_level])


def resolved(surface: str | None = None) -> dict:
    """Bootstrap-shaped view of the active prominence policy."""
    level = resolve(surface)
    applied_surface = surface if surface is not None else detect_surface()
    # One gate, computed once, and everything downstream that speaks about
    # capture authority is handed THIS object -- the served contract here, and
    # the delegation envelope at the call site that attaches it.
    gate = capture_gate(surface=surface)
    result = {
        "level": level,
        "source": _active_source(surface),
        "surface": applied_surface,
        "context": context_for_surface(applied_surface),
        "contract": CONTRACTS[level].as_dict(gate),
        "levels": list(CANON),
        "change_with": "exomem prominence <level>",
    }
    cadence = hook_cadence(applied_surface)
    if cadence is not None:
        result["hook_cadence"] = cadence
    if _REQUEST_PREFERENCE.get() is not None:
        result["preference"] = {
            "scope": "principal-and-vault",
            **_saved_preference(),
            "operator_override": normalize(os.environ.get(_PROMINENCE_ENV)),
        }
    return result


def hook_cadence(surface: str | None) -> dict | None:
    """What the standalone nudge hooks read, for a client that may run them.

    The saved preference lives on the SERVER, per identity. The capture and
    retrieve nudges are standalone copies deployed into a hook directory on the
    CLIENT machine, and they resolve from `EXOMEM_PROMINENCE` and that machine's
    exomem configuration file only — they cannot import this package, and on a
    remote client there is no credential with which to ask the service. So a user
    who saves `off` through the agent is told never to write unasked while the
    Stop hook on their own machine keeps injecting the capture reminder.

    Mirroring the saved level into the client's config file is not the fix: the
    server writes its own machine's file, which is the client's file only when
    the two are the same box. What IS available is honesty, so a hook-capable
    client is told what its hooks read and how to change it where they run.

    Gated on the CODING context rather than a two-name allowlist. The hookless
    set is the thing actually known here -- a web connector has no filesystem to
    install a hook into -- and everything else is a client that may well run
    them. `detect_surface` already returns None for an unrecognized client and
    `context_for_surface` already calls that coding, so an allowlist would have
    silenced exactly the hooked clients nobody has named yet: a new CLI, a fork,
    a local install driving the CLI directly. Silence on a hookless surface
    stays, because there is genuinely no cadence there to be out of step with.
    """
    if context_for_surface(surface) != CODING_CONTEXT:
        return None
    return {
        "reads": "operator environment, then this client machine's exomem configuration file",
        "saved_preference_reaches_hooks": False,
        # Scoped deliberately. Without the parenthesis an agent reads the CLI as
        # THE way to set the level and starts telling users to run it for
        # everything, which would undo the identity-scoped preference the
        # agent-accessible control exists to offer.
        "change_with": (
            "exomem prominence <level> on the client machine "
            "(changes hook cadence only; a saved preference still decides what is served)"
        ),
    }


def configuration_route() -> str:
    """The identity-scoped setting route for surfaces exposing configure_memory.

    Deliberately terse: this string rides in the COMPACT bootstrap ahead of the
    action catalogue, where `test_record_public_surface` pins how early `record`
    is reachable. The full contract — levels, expected_revision, the context
    argument — is the tool description's job, not this route hint's.
    """
    return "configure_memory: inspect first; set or clear."


def custom_instructions_route() -> str:
    """The setting route for a surface that serves no preference control.

    A connector-only client has neither `configure_memory` nor a machine to type
    `exomem prominence` on, so naming either is naming a route that user cannot
    take. The copy-paste level blocks in `docs/prominence.md` are what is left,
    and they are the route that surface was always meant to be given.

    Names no agent-callable command on purpose: `_filter_bootstrap_payload`
    deletes any served string that mentions one the active surface lacks, and a
    route that vanished on the surface it exists for would be no route at all.
    """
    return "Paste the level block from the Exomem prominence guide into this client's custom instructions."


def _active_source(surface: str | None = None) -> str:
    """Where the active level came from — useful when a setting appears not to apply."""
    if normalize(os.environ.get(_PROMINENCE_ENV)):
        return "env"
    if _saved_context_level(surface):
        return "preference:context"
    if normalize(_saved_preference().get("stored")):
        return "preference"
    raw = mode.read_config().get(_CONFIG_KEY)
    if isinstance(raw, str) and normalize(raw):
        return "config"
    if _saved_preference().get("unavailable"):
        return "preference:unavailable"
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
