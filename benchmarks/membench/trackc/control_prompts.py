"""FROZEN predeclared activation suite for the retrieve-nudge gate (Track C).

The suite encodes the SHIPPED gate contract of
``src/exomem/_hooks/exomem_retrieve_nudge.py`` (all line references below are
into that file), predeclared before any run. Cases are never edited to match a
run: if observed behavior deviates from ``expected``, the driver keeps the
predeclared value and reports the row as a gate limit
(see ``nudge_driver.summarize_results``).

Gate contract this suite measures (read from source, not guessed):

- prompt extraction: first of ``prompt`` / ``user_prompt`` / ``userPrompt`` /
  ``input`` in the stdin event JSON (lines 170-175).
- min-chars gate: ``len(prompt.strip()) < 20`` never fires
  (EXOMEM_RETRIEVE_NUDGE_MIN_CHARS default 20; lines 373, 378-379).
- obvious-control skip: only prompts whose whitespace-normalized length is
  <= 180 chars (EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS default 180; lines
  374, 186-188) AND that match the English-only control regex
  ``_CONTROL_PROMPT_RE`` (lines 92-110) are skipped; a KB-bearing token match
  (``_KB_BEARING_RE``, lines 83-90: kb/notes/remember/decision/...) overrides
  the skip (lines 188-190).
- per-session cooldown: stamp ``retrieve_<session_id>`` under
  ``$EXOMEM_HOOK_HOME/.cache/exomem-nudge`` suppresses repeats within
  EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC (default 300s; lines 194-212, 375,
  384-386).
- client-wide cooldown: stamp ``retrieve_global`` suppresses ANY other session
  in the same hook home within EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC
  (default 900s; lines 215-218, 376, 388-394).
- fire output: one-line JSON ``{"hookSpecificOutput": {"hookEventName":
  "UserPromptSubmit", "additionalContext": ...}}`` on stdout (lines 410-413);
  no-fire = empty stdout, exit 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Gate constants mirrored from exomem_retrieve_nudge.py (defaults; lines 373-376).
MIN_CHARS_DEFAULT = 20
CONTROL_MAX_CHARS_DEFAULT = 180
SESSION_COOLDOWN_SEC_DEFAULT = 300
GLOBAL_COOLDOWN_SEC_DEFAULT = 900

#: home_key shared by the cooldown trio (cp09 arms the stamps; cp13/cp14 probe them).
COOLDOWN_HOME_KEY = "cooldown"
#: session_key shared by cp09 and cp13 (same-session cooldown, lines 384-386).
COOLDOWN_SESSION_A = "cooldown-session-a"
#: distinct session in the SAME home for cp14 (global cooldown, lines 388-390).
COOLDOWN_SESSION_B = "cooldown-session-b"


@dataclass(frozen=True)
class ControlCase:
    """One predeclared activation case.

    ``expected`` encodes the SHIPPED gate contract ('fire' | 'no_fire').
    ``fresh_home`` is True when the case runs in its own fresh
    EXOMEM_HOOK_HOME; False only for the cooldown probes, which deliberately
    reuse the home (and stamps) armed by cp09.
    ``measured_gate_limit`` marks cases where the shipped gate's contractual
    behavior diverges from the ideal ("should this prompt get a KB nudge?")
    so the suite scores the gate's false-positive rate honestly instead of
    hiding it.
    ``session_key`` / ``home_key`` are driver-internal grouping knobs; by
    default every case gets its own home and session (fresh cooldown state).
    """

    id: str
    prompt: str
    expected: str  # 'fire' | 'no_fire'
    fresh_home: bool
    notes: str
    measured_gate_limit: bool = False
    home_key: str = field(default="")
    session_key: str = field(default="")

    def __post_init__(self) -> None:
        if self.expected not in {"fire", "no_fire"}:
            raise ValueError(f"{self.id}: expected must be 'fire'|'no_fire'")
        object.__setattr__(self, "home_key", self.home_key or self.id)
        object.__setattr__(self, "session_key", self.session_key or f"sess-{self.id}")


def _long_control_imperative() -> str:
    """A >180-char imperative that MATCHES the control regex pattern.

    ``_CONTROL_PROMPT_RE`` accepts ``merge`` followed by any number of
    it/then/to/into/main tokens (line 102: ``merge(?:\\s+(?:it|then|to|into|
    main))*``). Repeating that suffix past 180 chars keeps the prompt
    control-shaped while exceeding the control gate's length eligibility
    window (lines 186-188), so the shipped gate FIRES on it — the measured
    bounded-control-window limit cp12 exists to document.
    """
    text = "merge" + " it then into main" * 10  # 5 + 10*18 = 185 chars
    assert len(text) > CONTROL_MAX_CHARS_DEFAULT
    return text


CONTROL_SUITE: tuple[ControlCase, ...] = (
    # --- 8 no-fire controls (cp01-cp08) --------------------------------------
    ControlCase(
        id="cp01",
        prompt="continue",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "8 chars < min-chars 20 (lines 378-379); also matches the control "
            "regex 'continue' alternative (line 101)."
        ),
    ),
    ControlCase(
        id="cp02",
        prompt="merge it",
        expected="no_fire",
        fresh_home=True,
        notes="8 chars < min-chars 20; also control-regex 'merge it' (line 102).",
    ),
    ControlCase(
        id="cp03",
        prompt="done?",
        expected="no_fire",
        fresh_home=True,
        notes="5 chars < min-chars 20; also control-regex 'done' + '?' tail (lines 97, 107).",
    ),
    ControlCase(
        id="cp04",
        prompt="yes",
        expected="no_fire",
        fresh_home=True,
        notes="3 chars < min-chars 20; also control-regex y(es) (line 96).",
    ),
    ControlCase(
        id="cp05",
        prompt="run the tests again",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "19 chars — one below the min-chars 20 gate (lines 373, 378). This one "
            "is length-gated ONLY: 'run ...' matches no control-regex alternative."
        ),
    ),
    ControlCase(
        id="cp06",
        prompt="commit and push",
        expected="no_fire",
        fresh_home=True,
        notes="15 chars < min-chars 20 (no 'commit' control-regex alternative exists).",
    ),
    ControlCase(
        id="cp07",
        prompt="lgtm, ship it",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "13 chars < min-chars 20. ('ship it' is a control alternative at line "
            "102, but the 'lgtm, ' prefix would break the anchored match anyway.)"
        ),
    ),
    ControlCase(
        id="cp08",
        prompt="ok",
        expected="no_fire",
        fresh_home=True,
        notes="2 chars < min-chars 20; also control-regex ok(ay) (line 96).",
    ),
    # --- fire cases (cp09-cp12) ----------------------------------------------
    ControlCase(
        id="cp09",
        prompt="What did we decide about the auth token rotation approach?",
        expected="fire",
        fresh_home=True,
        notes=(
            "58 chars >= 20, no control-regex match -> flows through to the "
            "cooldown gates and fires (lines 378-394). Also arms the cooldown "
            "home's session + global stamps for cp13/cp14."
        ),
        home_key=COOLDOWN_HOME_KEY,
        session_key=COOLDOWN_SESSION_A,
    ),
    ControlCase(
        id="cp10",
        prompt="Millised olid meie järeldused autentimise kohta?",
        expected="fire",
        fresh_home=True,
        notes=(
            "Non-English substantive prompt (Estonian, 48 chars). The control "
            "skip is deliberately an English-only fast path (lines 181-185); "
            "non-English prompts flow through the language-agnostic length gate "
            "and fire."
        ),
    ),
    ControlCase(
        id="cp11",
        prompt="check my notes on refresh tokens",
        expected="fire",
        fresh_home=True,
        notes=(
            "KB-bearing override case: 32 chars >= 20 fires via the length gate; "
            "'notes' additionally matches _KB_BEARING_RE (line 86) which exempts "
            "the prompt from ANY control skip (lines 188-190)."
        ),
    ),
    ControlCase(
        id="cp12",
        prompt=_long_control_imperative(),
        expected="fire",
        fresh_home=True,
        notes=(
            "Long control-flavored imperative (185 chars). It MATCHES "
            "_CONTROL_PROMPT_RE's merge chain (line 102), but "
            "_is_obvious_control_prompt only applies to prompts <= "
            "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS (180; lines 186-188), so "
            "the shipped gate contract is FIRE. Documented limit: the control "
            "skip is length-bounded, so an unusually long control prompt still "
            "triggers the reminder (an unnecessary activation, scored honestly)."
        ),
        measured_gate_limit=True,
    ),
    # --- cooldown probes (cp13-cp14) — reuse the home cp09 armed --------------
    ControlCase(
        id="cp13",
        prompt="What did we decide about the auth token rotation approach?",
        expected="no_fire",
        fresh_home=False,
        notes=(
            "cp09 repeated in the SAME session within the 300s per-session "
            "cooldown: cp09's fire wrote stamp retrieve_<session> (lines 392, "
            "194-212), so this run exits quietly at lines 384-386."
        ),
        home_key=COOLDOWN_HOME_KEY,
        session_key=COOLDOWN_SESSION_A,
    ),
    ControlCase(
        id="cp14",
        prompt="What did we decide about the auth token rotation approach?",
        expected="no_fire",
        fresh_home=False,
        notes=(
            "cp09's prompt from a SECOND session in the same hook home within "
            "the 900s client-wide cooldown: cp09's fire wrote retrieve_global "
            "(lines 393-394, 215-218), so a fresh session is suppressed at "
            "lines 388-390."
        ),
        home_key=COOLDOWN_HOME_KEY,
        session_key=COOLDOWN_SESSION_B,
    ),
    # --- hard negatives (hn01-hn05): substantive-LOOKING prompts with no KB
    # bearing. Where the shipped gate nevertheless fires, expected stays 'fire'
    # with measured_gate_limit=True so the false-positive rate is scored
    # honestly instead of hidden. ----------------------------------------------
    ControlCase(
        id="hn01",
        prompt="What is the capital of Australia, and why was it chosen over Sydney?",
        expected="fire",
        fresh_home=True,
        notes=(
            "Pure general-knowledge question: no prior-KB bearing, so the ideal "
            "gate would skip. The shipped gate has no topicality signal — 68 "
            "chars >= 20 and non-control means FIRE (lines 378-381). Measured "
            "false positive."
        ),
        measured_gate_limit=True,
    ),
    ControlCase(
        id="hn02",
        prompt="Rename the helper function in the open file to snake_case for me.",
        expected="fire",
        fresh_home=True,
        notes=(
            "Edit-this-visible-file instruction: needs no KB recall, so the "
            "ideal gate would skip. Shipped gate fires (66 chars, non-control; "
            "lines 378-381). Measured false positive."
        ),
        measured_gate_limit=True,
    ),
    ControlCase(
        id="hn03",
        prompt="restart the server!!!",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "Substantive-looking ops imperative at 21 chars (>= min-chars, so "
            "the length gate does NOT save it). Correctly skipped by the control "
            "regex 'restart the server' alternative + punctuation tail "
            "(lines 104, 107)."
        ),
    ),
    ControlCase(
        id="hn04",
        prompt="are you done??????????",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "Status-check prompt at 22 chars (>= min-chars). Correctly skipped "
            "by the control regex 'are you done' alternative (line 105) with "
            "the '?' tail (line 107)."
        ),
    ),
    ControlCase(
        id="hn05",
        prompt="Fix the auth bug",
        expected="no_fire",
        fresh_home=True,
        notes=(
            "Substantive imperative that a human might expect to nudge, but at "
            "16 chars it sits under the min-chars 20 gate (lines 378-379): the "
            "shipped contract is no-fire. Documents the short-substantive blind "
            "spot of the length gate."
        ),
    ),
)

#: Cases whose ideal semantic is a relevant KB activation (used for
#: relevant_activation_rate; excludes measured-limit fires, which are
#: unnecessary activations even though the gate contract says 'fire').
RELEVANT_CASE_IDS: frozenset[str] = frozenset(
    case.id
    for case in CONTROL_SUITE
    if case.expected == "fire" and not case.measured_gate_limit
)


def case_by_id(case_id: str) -> ControlCase:
    for case in CONTROL_SUITE:
        if case.id == case_id:
            return case
    raise KeyError(case_id)
