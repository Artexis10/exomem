"""Prominence level — resolution, persistence, and the hook-preset drift guard.

The nudge hooks are deployed as standalone copies into a client's hook directory, so
they cannot import `exomem.prominence`; each carries its own copy of the preset table.
`test_hook_presets_match_*` is what keeps those copies honest — without it the CLI
would report one cadence while the hooks ran another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import mode, prominence
from exomem._hooks import exomem_capture_nudge as capture_hook
from exomem._hooks import exomem_retrieve_nudge as retrieve_hook


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point mode/prominence at a throwaway config and clear the env overrides."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    return path


# --------------------------------------------------------------------- normalizing


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("maximal", "maximal"),
        ("MAX", "maximal"),
        ("  High  ", "maximal"),
        ("aggressive", "maximal"),
        ("normal", "balanced"),
        ("minimal", "light"),
        ("none", "off"),
        ("", None),
        ("   ", None),
        ("bogus", None),
        (None, None),
    ],
)
def test_normalize(raw, expected):
    assert prominence.normalize(raw) == expected


def test_every_canonical_level_has_a_contract_and_preset():
    assert set(prominence.CONTRACTS) == set(prominence.CANON)
    assert set(prominence._HOOK_PRESETS) == set(prominence.CANON)


def test_aliases_all_resolve_to_canonical_levels():
    for alias, target in prominence._ALIASES.items():
        assert target in prominence.CANON, alias


# --------------------------------------------------------------------- resolution


def test_local_install_defaults_to_balanced(config):
    assert prominence.detect_surface() is None
    assert prominence.resolve() == "balanced"


def test_hosted_cell_defaults_to_maximal(config, monkeypatch):
    """No hooks in a hosted cell, so instruction strength is the only lever."""
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    assert prominence.detect_surface() == "hosted"
    assert prominence.resolve() == "maximal"


@pytest.mark.parametrize("surface", sorted(prominence.HOOKLESS_SURFACES))
def test_every_hookless_surface_defaults_to_maximal(config, surface):
    assert prominence.default_for_surface(surface) == "maximal"


def test_explicit_surface_env_is_honoured(config, monkeypatch):
    monkeypatch.setenv("EXOMEM_SURFACE", "chatgpt")
    assert prominence.resolve() == "maximal"


def test_env_beats_config_and_surface_default(config, monkeypatch):
    prominence.write_prominence("off")
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")
    assert prominence.resolve() == "light"
    assert prominence._active_source() == "env"


def test_config_beats_surface_default(config, monkeypatch):
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    prominence.write_prominence("light")
    assert prominence.resolve() == "light"
    assert prominence._active_source() == "config"


def test_invalid_config_value_degrades_to_default(config):
    config.write_text(json.dumps({"schema": 1, "prominence": "nonsense"}), "utf-8")
    assert prominence.resolve() == "balanced"
    assert prominence._active_source() == "default"


def test_corrupt_config_degrades_to_default(config):
    config.write_text("{not json", "utf-8")
    assert prominence.resolve() == "balanced"


# --------------------------------------------------------------------- persistence


def test_write_prominence_round_trips(config):
    prominence.write_prominence("maximal")
    assert prominence.resolve() == "maximal"


def test_write_prominence_accepts_aliases(config):
    prominence.write_prominence("MAX")
    assert json.loads(config.read_text())["prominence"] == "maximal"


def test_write_prominence_rejects_unknown(config):
    with pytest.raises(ValueError):
        prominence.write_prominence("nonsense")
    assert not config.exists()


def test_prominence_and_mode_share_the_config_without_clobbering(config):
    """Either write must preserve the other's key — they live in one file."""
    mode.write_mode("performance")
    prominence.write_prominence("maximal")
    data = json.loads(config.read_text())
    assert data["mode"] == "performance"
    assert data["prominence"] == "maximal"

    mode.write_mode("quiet")
    data = json.loads(config.read_text())
    assert data["mode"] == "quiet"
    assert data["prominence"] == "maximal", "writing mode dropped prominence"

    prominence.write_prominence("light")
    data = json.loads(config.read_text())
    assert data["mode"] == "quiet", "writing prominence dropped mode"
    assert data["prominence"] == "light"


def test_resolved_payload_shape(config):
    payload = prominence.resolved()
    assert payload["level"] == "balanced"
    assert payload["levels"] == list(prominence.CANON)
    assert set(payload["contract"]) == {
        "level",
        "recall",
        "capture",
        "narration",
        "summary",
        "effective_capture",
    }


# ------------------------------------------------------- hook preset drift guard


def _capture_preset_as_env(level: str) -> dict[str, str]:
    """Render the capture hook's own table in `prominence.hook_env` shape."""
    preset = capture_hook._PROMINENCE_PRESETS[level]
    if preset is None:
        return {"EXOMEM_CAPTURE_NUDGE_DISABLE": "1"}
    min_chars, cooldown = preset
    return {
        "EXOMEM_CAPTURE_NUDGE_DISABLE": "",
        "EXOMEM_CAPTURE_NUDGE_MIN_CHARS": str(min_chars),
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC": str(cooldown),
    }


def _retrieve_preset_as_env(level: str) -> dict[str, str]:
    """Render the retrieve hook's own table in `prominence.hook_env` shape."""
    preset = retrieve_hook._PROMINENCE_PRESETS[level]
    if preset is None:
        return {"EXOMEM_RETRIEVE_NUDGE_DISABLE": "1"}
    min_chars, control_max, cooldown, global_cooldown = preset
    return {
        "EXOMEM_RETRIEVE_NUDGE_DISABLE": "",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS": str(min_chars),
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS": str(control_max),
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": str(cooldown),
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": str(global_cooldown),
    }


@pytest.mark.parametrize("level", prominence.CANON)
def test_hook_presets_match_canonical_table(level):
    """The two standalone hook copies must agree with `prominence._HOOK_PRESETS`."""
    canonical = prominence.hook_env(level)
    rendered = {**_capture_preset_as_env(level), **_retrieve_preset_as_env(level)}
    assert rendered == canonical, (
        f"hook preset table drifted from prominence._HOOK_PRESETS at level {level!r}"
    )


@pytest.mark.parametrize("level", prominence.CANON)
def test_hook_alias_tables_match(level):
    assert capture_hook._PROMINENCE_ALIASES == prominence._ALIASES
    assert retrieve_hook._PROMINENCE_ALIASES == prominence._ALIASES
    assert set(capture_hook._PROMINENCE_PRESETS) == set(prominence.CANON)
    assert set(retrieve_hook._PROMINENCE_PRESETS) == set(prominence.CANON)


@pytest.mark.parametrize("hook", [capture_hook, retrieve_hook])
def test_hook_resolves_level_from_shared_config(config, hook):
    """A hook reading the config the CLI wrote is the whole point of the shared file."""
    prominence.write_prominence("maximal")
    assert hook._prominence() == "maximal"
    assert hook._config_path() == mode.config_path()


@pytest.mark.parametrize("hook", [capture_hook, retrieve_hook])
def test_hook_defaults_to_balanced_not_surface_detected(config, hook, monkeypatch):
    """If a hook is running, the client has hooks — never infer the hookless default."""
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    assert hook._prominence() == "balanced"


@pytest.mark.parametrize("hook", [capture_hook, retrieve_hook])
def test_hook_env_beats_config(config, hook, monkeypatch):
    prominence.write_prominence("off")
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    assert hook._prominence() == "maximal"


def test_off_disables_both_hooks():
    env = prominence.hook_env("off")
    assert env["EXOMEM_CAPTURE_NUDGE_DISABLE"] == "1"
    assert env["EXOMEM_RETRIEVE_NUDGE_DISABLE"] == "1"
    assert capture_hook._PROMINENCE_PRESETS["off"] is None
    assert retrieve_hook._PROMINENCE_PRESETS["off"] is None


def test_maximal_fires_on_every_prompt():
    """Maximal exists to beat instruction decay: no length floor, no cooldown."""
    preset = retrieve_hook._PROMINENCE_PRESETS["maximal"]
    min_chars, _control_max, cooldown, global_cooldown = preset
    assert min_chars == 0
    assert cooldown == 0
    assert global_cooldown == 0


def test_levels_are_monotonic_in_eagerness():
    """light is strictly less eager than balanced, which is less than maximal."""
    levels = ["light", "balanced", "maximal"]
    retrieve_floors = [retrieve_hook._PROMINENCE_PRESETS[x][0] for x in levels]
    retrieve_cooldowns = [retrieve_hook._PROMINENCE_PRESETS[x][2] for x in levels]
    capture_floors = [capture_hook._PROMINENCE_PRESETS[x][0] for x in levels]
    capture_cooldowns = [capture_hook._PROMINENCE_PRESETS[x][1] for x in levels]
    for series in (retrieve_floors, retrieve_cooldowns, capture_floors, capture_cooldowns):
        assert series == sorted(series, reverse=True), series
# --- the reminder has to name every route it expects to be taken ------------


def test_the_capture_reminder_names_supersession_and_its_tool() -> None:
    """The headline behaviour needs a route from the hook that drives captures.

    "Nothing is deleted, it is superseded" is documented in the shipped schema
    references and is what `replace_memory` exists for -- but the reminder that
    fires on every substantial turn instructed create, edit and entity paths and
    never named it. Observed live: a governed conclusion was contradicted and the
    agent appended a `[correction]` observation beside the original, leaving two
    live versions of one conclusion both reading as current.

    Asserted as the tool plus the distinction it turns on, rather than as a
    frozen sentence, so the wording stays free to improve.
    """
    reminder = capture_hook.REMINDER

    assert "replace_memory" in reminder
    assert "supersede" in reminder
    # The failure mode is specifically appending beside the wrong version, so
    # the reminder has to contrast the two rather than merely offer the tool.
    assert "correction" in reminder


def test_the_capture_reminder_stays_one_paragraph_of_instruction() -> None:
    """It is injected on every substantial turn, so length is a real cost.

    No hard limit worth defending, but a reminder that grows without anyone
    noticing stops being read. This is the tripwire, not the budget.
    """
    reminder = capture_hook.REMINDER

    # 1,800: four capture doctrines plus the entity cadence did not fit in 1,600
    # (2026-09-02); still one paragraph. Carrier budget composition is the follow-up.
    assert len(reminder) < 1800, len(reminder)
    assert reminder.startswith("[Exomem capture check]")


def test_the_deployed_copy_matches_the_packaged_hook() -> None:
    """`plugins/claude-code/hooks/` holds a verbatim copy, not a variant.

    The reminder is the part most likely to be edited in one place only, and a
    client running the deployed copy would then be told something the package
    no longer says.
    """
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py"
    deployed = root / "plugins" / "claude-code" / "hooks" / "exomem_capture_nudge.py"

    assert deployed.read_bytes() == packaged.read_bytes()


# --- the capture predicate has to cover lifecycle consequences ---------------


@pytest.mark.parametrize("level", ["balanced", "maximal"])
def test_the_capture_axis_names_both_lifecycle_classes_and_the_transition_boundary(
    level: str,
) -> None:
    """Stated intent and observed outcomes are capture classes, not magic words.

    The dogfood session that motivated this had the evidence in ordinary
    language every turn -- "let's do the next one", "three done", "Kim posted
    it" -- and the capture predicate was closed over three classes that name
    none of them. The classes are asserted with their ROUTE, because a class
    an agent cannot route is a label rather than an instruction.
    """
    capture = prominence.CONTRACTS[level].capture.lower()

    assert "stated intent" in capture
    assert "planning" in capture
    assert "observed outcome" in capture
    assert "records" in capture
    # An outcome is a Record. A transition needs a distinct explicit user decision;
    # the only non-mutating alternative is the resolved posture's bounded review.
    assert "transition only on explicit user intent" in capture
    assert "leave planning unchanged" in capture
    assert "propose a bounded review" in capture
    assert "record then transition" not in capture
    # The two named non-outcomes -- a tentative claim, and elapsed time -- are
    # stated ONCE, in the bootstrap `intent_boundary` that every client tier
    # reads, rather than in every carrier: see
    # `tests/test_epistemic_bootstrap_contract.py::
    # test_intent_boundary_routes_the_two_lifecycle_classes`. The compact payload
    # projects this level's contract verbatim, so a second copy here is bytes
    # spent on a hookless client's context for a rule it already received.


def test_light_does_not_widen_with_the_lifecycle_classes() -> None:
    """`light` is capture-only-when-asked; naming a proactive class there is a bug."""
    capture = prominence.CONTRACTS["light"].capture.lower()

    assert "stated intent" not in capture
    assert "observed outcome" not in capture
    assert "only when the user asks" in capture


@pytest.mark.parametrize("level", prominence.CANON)
def test_effective_capture_pins_explicit_and_proactive_gates(level: str) -> None:
    effective = prominence.capture_gate(level)
    proactive = level in {"balanced", "maximal"}

    assert set(effective) == {"durable_intent", "observed_outcomes"}
    assert effective["durable_intent"] == {
        "authored_explicit": "explicit-user-request",
        "proactive_permitted": proactive,
        "proactive_requires": (
            ["authored-proactive", "durable-intent"] if proactive else []
        ),
    }
    assert effective["observed_outcomes"] == {
        "authored_explicit": "explicit-user-request",
        "proactive_permitted": proactive,
        "proactive_requires": (
            ["authored-proactive", "sufficiently-identified-outcome"] if proactive else []
        ),
    }


def test_effective_capture_projection_is_complete_and_detached() -> None:
    projection = prominence.capture_policy_projection()
    assert list(projection) == list(prominence.CANON)
    projection["balanced"]["durable_intent"]["proactive_requires"].append("mutated")
    assert prominence.capture_policy_projection()["balanced"] == prominence.capture_gate(
        "balanced"
    )


@pytest.mark.parametrize("level", prominence.CANON)
@pytest.mark.parametrize("authored", ("explicit", "proactive"))
def test_effective_capture_applies_authored_posture_under_the_active_level(
    level: str, authored: str
) -> None:
    effective = prominence.effective_capture(
        {"durable_intent": authored, "observed_outcomes": authored}, level
    )
    proactive = authored == "proactive" and level in {"balanced", "maximal"}

    for value in effective.values():
        assert value["authored"] == authored
        assert value["explicit_user_request_permitted"] is True
        assert value["proactive_permitted"] is proactive
        if proactive:
            assert value["proactive_requires"]
        else:
            assert value["proactive_requires"] == []


# Base-superset pins. Two regressions in one tranche came from rewriting a
# carrier's capture text and dropping a sentence nobody's review had a row for:
# a compact-only override lost the Planning/Records transition rule, and the
# balanced rewrite lost main's restraint clause. Per-lane reviews and a parity
# matrix built from the doctrines the tranche ADDED cannot see a doctrine that
# predated it, so the pins below name the base sentences and the structural
# property (one capture text per level across every bootstrap projection).

# The base is origin/main at the revision this branch merged, recorded the way
# the hosted immutability manifest records its source_revision. The list is a
# restated sample of that carrier's sentences, not a derivation of all of them:
# the projection-parity pin below is the structural guard, this one names the
# doctrines a rewrite has already dropped once.
_BASE_CARRIER_REVISION = "fdc9ab4d"
_BASE_CAPTURE_SENTENCES = {
    "balanced": (
        "Not mid-thought exploration, tangents, or unresolved questions.",
        "Route stated intent to Planning and observed outcome to Records.",
        "Transition only on explicit user intent",
    ),
    "maximal": (
        "When torn between capturing and letting it pass, capture.",
        "Route stated intent to Planning and observed outcome to Records.",
        "Transition only on explicit user intent",
    ),
}


@pytest.mark.parametrize("level", sorted(_BASE_CAPTURE_SENTENCES))
def test_capture_text_keeps_the_named_base_carrier_sentences(level: str) -> None:
    capture = prominence.contract(level).capture
    missing = [s for s in _BASE_CAPTURE_SENTENCES[level] if s not in capture]
    assert missing == [], (
        f"{level} capture dropped base doctrine from {_BASE_CARRIER_REVISION}: {missing}"
    )


@pytest.mark.parametrize("level", ("off", "light", "balanced", "maximal"))
def test_every_bootstrap_projection_serves_one_capture_text_per_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """A compact-only rewrite of the capture text is how a doctrine went missing
    from the projection every connector client reads first while full and
    diagnostics still carried it. The three projections must serve prominence's
    own text, byte for byte."""
    from exomem import commands

    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    (tmp_path / "Knowledge Base").mkdir()
    served = {
        profile: commands.op_bootstrap(tmp_path, profile=profile)["engagement"]["contract"][
            "capture"
        ]
        for profile in ("compact", "full", "diagnostics")
    }
    assert set(served.values()) == {prominence.contract(level).capture}, served


# ------------------------------------------------------------- engagement context


@pytest.fixture
def saved():
    """Install a request preference snapshot the way `request_scope` would."""
    tokens = []

    def install(stored=None, contexts=None, **extra):
        tokens.append(
            prominence._REQUEST_PREFERENCE.set(
                {
                    "vault_root": Path("/nonexistent"),
                    "preference": {
                        "stored": stored,
                        "contexts": dict(contexts or {}),
                        "revision": "missing",
                        **extra,
                    },
                }
            )
        )

    yield install
    for token in reversed(tokens):
        prominence._REQUEST_PREFERENCE.reset(token)


# ------------------------------------------------------------------ hook cadence


#: What a hook-capable client must be told, verbatim. Pinned here rather than
#: compared to the module constant, because the point of the block is the words
#: the agent reads out to its user: a test that asserts `X == X` would pass
#: through any rewording, including one that stopped being true.
EXPECTED_HOOK_CADENCE = {
    "reads": "operator environment, then this client machine's exomem configuration file",
    "saved_preference_reaches_hooks": False,
    "change_with": (
        "exomem prominence <level> on the client machine "
        "(changes hook cadence only; a saved preference still decides what is served)"
    ),
}

#: Surfaces that reach the block. The two named hooked clients, plus the shapes
#: an unrecognized hooked client actually arrives as -- a new CLI, a fork, a
#: local install driving the CLI directly. `detect_surface` returns None for all
#: of those and `context_for_surface` already calls them coding, so an allowlist
#: of the two names would have silenced exactly the clients nobody has named yet.
CODING_SURFACES = ("claude-code", "codex", "vscode", "some-new-client", None)


@pytest.mark.parametrize("surface", CODING_SURFACES)
def test_a_client_that_may_run_hooks_is_told_what_they_actually_read(config, surface):
    """The saved preference lives on the SERVER; the hooks run on the client.

    A user who saves `off` through the agent is told by the same response never to
    write unasked -- while the Stop hook on their own machine keeps injecting the
    capture reminder, because it reads a config file the server never wrote to.
    """
    assert prominence.resolved(surface)["hook_cadence"] == EXPECTED_HOOK_CADENCE


@pytest.mark.parametrize("surface", sorted(prominence.HOOKLESS_SURFACES))
def test_a_surface_with_no_hooks_is_told_nothing_about_hook_cadence(config, surface):
    """Silence is the honest answer where there are no hooks to be out of step."""
    assert "hook_cadence" not in prominence.resolved(surface)


@pytest.mark.parametrize("surface", [*CODING_SURFACES, *sorted(prominence.HOOKLESS_SURFACES)])
def test_the_cadence_block_follows_the_coding_context_exactly(config, surface):
    """One gate, not two lists that can drift apart."""
    served = "hook_cadence" in prominence.resolved(surface)

    assert served is (prominence.context_for_surface(surface) == prominence.CODING_CONTEXT)


def test_the_cadence_block_names_no_agent_callable_command(config):
    """It rides through `_filter_bootstrap_payload`, which drops command mentions."""
    block = prominence.resolved("claude-code")["hook_cadence"]

    assert "configure_memory" not in json.dumps(block)


def test_the_cadence_route_says_it_moves_only_the_hooks(config):
    """Unscoped, an agent reads the CLI as THE way to set the level.

    It is not: the CLI writes one machine's file, while the agent-accessible
    control saves a preference that follows the identity everywhere. An agent
    that starts recommending the CLI for everything undoes that.
    """
    change_with = prominence.resolved("codex")["hook_cadence"]["change_with"]

    assert "hook cadence only" in change_with
    assert "saved preference still decides what is served" in change_with


@pytest.mark.parametrize("surface", ["claude-code", "codex"])
def test_the_cadence_block_is_told_even_when_the_saved_level_won(config, saved, surface):
    """The mismatch is worst exactly when a preference IS in force."""
    saved(stored="off")

    payload = prominence.resolved(surface)

    assert payload["level"] == "off"
    assert payload["source"] == "preference"
    assert payload["hook_cadence"]["saved_preference_reaches_hooks"] is False


@pytest.mark.parametrize("surface", sorted(prominence.HOOKLESS_SURFACES))
def test_every_hookless_surface_is_the_conversation_context(surface):
    assert prominence.context_for_surface(surface) == "conversation"


@pytest.mark.parametrize(
    "surface", ["codex", "claude-code", "vscode", "some-new-client", "", "   ", None]
)
def test_coding_and_unknown_surfaces_are_the_coding_context(surface):
    assert prominence.context_for_surface(surface) == "coding"


@pytest.mark.parametrize("surface", ["ChatGPT", "  Claude-AI  ", "HOSTED"])
def test_context_detection_ignores_case_and_surrounding_space(surface):
    assert prominence.context_for_surface(surface) == "conversation"


def test_contexts_are_exactly_coding_and_conversation():
    assert prominence.CONTEXTS == ("coding", "conversation")


def test_a_context_value_beats_the_identity_wide_value(config, saved):
    saved(stored="maximal", contexts={"coding": "balanced"})

    assert prominence.resolve("codex") == "balanced"
    assert prominence._active_source("codex") == "preference:context"


def test_the_identity_wide_value_applies_where_that_context_is_unset(config, saved):
    saved(stored="maximal", contexts={"coding": "balanced"})

    assert prominence.resolve("claude-ai") == "maximal"
    assert prominence._active_source("claude-ai") == "preference"


def test_a_context_value_does_not_leak_into_the_other_context(config, saved):
    saved(contexts={"conversation": "off"})

    assert prominence.resolve("codex") == "balanced", "the coding client keeps its default"
    assert prominence._active_source("codex") == "default"
    assert prominence.resolve("claude-ai") == "off"
    assert prominence._active_source("claude-ai") == "preference:context"


def test_the_operator_override_still_beats_a_context_value(config, saved, monkeypatch):
    saved(stored="maximal", contexts={"coding": "balanced"})
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")

    assert prominence.resolve("codex") == "light"
    assert prominence._active_source("codex") == "env"


def test_a_context_value_beats_the_legacy_machine_config(config, saved):
    config.write_text(json.dumps({"schema": 1, "prominence": "off"}), "utf-8")
    saved(contexts={"coding": "balanced"})

    assert prominence.resolve("codex") == "balanced"
    assert prominence._active_source("codex") == "preference:context"


def test_an_unusable_record_resolves_to_the_generic_default(config, saved):
    """An unreadable record must not hand a user MORE proactivity than they chose.

    The hookless surface default is `maximal`, so falling through to it turned an
    identity that may well have saved `off` into the eagerest level Exomem has.
    The generic default is the honest floor: the record is gone, so the client's
    own preference cannot be the thing that widens it.
    """
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    assert prominence.resolve("claude-ai") == "balanced"
    assert prominence._active_source("claude-ai") == "preference:unavailable"


def test_an_unusable_record_never_grants_proactive_capture(config, saved):
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    gate = prominence.capture_gate(surface="claude-ai")

    assert gate["durable_intent"]["proactive_permitted"] is False
    assert gate["observed_outcomes"]["proactive_permitted"] is False
    assert gate["durable_intent"]["authored_explicit"] == "explicit-user-request"
    assert gate["observed_outcomes"]["authored_explicit"] == "explicit-user-request"


def test_an_unusable_record_keeps_its_diagnostic_and_caps_the_served_contract(config, saved):
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    payload = prominence.resolved("claude-ai")

    assert payload["level"] == "balanced"
    assert payload["source"] == "preference:unavailable"
    assert payload["preference"]["unavailable"] == "PREFERENCE_STATE_UNAVAILABLE"
    effective = payload["contract"]["effective_capture"]
    assert effective["observed_outcomes"]["proactive_permitted"] is False
    assert effective["durable_intent"]["proactive_permitted"] is False


# ------------------------------------------- the two capture projections agree


def _proactive_keys(payload: dict) -> tuple[bool, bool, str]:
    """(durable, observed, envelope disposition) from one engagement payload."""
    from exomem import envelope as envelope_module

    effective = payload["contract"]["effective_capture"]
    served = envelope_module.resolved(
        level=payload["level"], capture_gate=effective
    )
    return (
        effective["durable_intent"]["proactive_permitted"],
        effective["observed_outcomes"]["proactive_permitted"],
        served["classes"]["proactive_capture"]["disposition"],
    )


@pytest.mark.parametrize("level", prominence.CANON)
def test_the_capture_gate_and_the_envelope_agree_at_every_level(config, saved, level):
    """The envelope is the authority surface; the gate is the permission surface.

    They are two keys of ONE `engagement` block, and an agent that reads the
    envelope's "silent: act" next to a gate that says no proactive write has
    been handed a contradiction, not a policy. No test asserted they agree,
    which is how a fail-open survived in the key nobody checked.
    """
    saved(stored=level)

    durable, observed, disposition = _proactive_keys(prominence.resolved("codex"))

    assert durable is observed
    assert disposition == ("silent" if durable else "off")


def test_the_capture_gate_and_the_envelope_agree_under_the_unreadable_floor(config, saved):
    """The floor is exactly where the two used to diverge.

    `resolve` reports `balanced` so recall and narration stay usable, and the
    envelope derived `proactive_capture: silent` from that level alone -- which
    is the withheld write authority granted back one key over.
    """
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    durable, observed, disposition = _proactive_keys(prominence.resolved("claude-ai"))

    assert durable is False
    assert observed is False
    assert disposition == "off"


def test_the_floor_reaches_the_envelope_without_being_handed_the_gate(config, saved):
    """A caller that passes no level must pick the floor up on its own.

    `envelope.active` is what `capture_sweep` asks before emitting, and it never
    sees an `engagement` payload to take a gate from.
    """
    from exomem import envelope as envelope_module

    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    assert envelope_module.active()["proactive_capture"] == "off"
    assert envelope_module.resolved()["classes"]["proactive_capture"]["disposition"] == "off"


def test_an_explicit_level_still_asks_what_that_level_would_give(config, saved):
    """The floor caps the REQUEST; it must not rewrite a "what if" lookup.

    `capture_gate("maximal")` and `derive_envelope("maximal")` are how the
    payload documents the levels, and a floor that leaked into them would make
    the level table describe this one broken request instead.
    """
    from exomem import envelope as envelope_module

    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    assert prominence.capture_gate("maximal")["observed_outcomes"]["proactive_permitted"] is True
    assert prominence.capture_gate("off")["observed_outcomes"]["proactive_permitted"] is False
    assert envelope_module.derive_envelope("maximal")["proactive_capture"] == "silent"


def test_an_unusable_record_leaves_the_detached_level_table_alone(config, saved):
    """`capture_policy_projection` documents the levels, not this request."""
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    table = prominence.capture_policy_projection()

    assert table["balanced"]["observed_outcomes"]["proactive_permitted"] is True
    assert table["maximal"]["durable_intent"]["proactive_permitted"] is True
    assert table["off"]["observed_outcomes"]["proactive_permitted"] is False


def test_explicit_machine_config_still_beats_an_unusable_record(config, saved):
    """The unreadable-record floor replaces the CLIENT default, not a real choice."""
    config.write_text(json.dumps({"schema": 1, "prominence": "light"}), "utf-8")
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")

    assert prominence.resolve("claude-ai") == "light"
    assert prominence._active_source("claude-ai") == "config"


def test_the_operator_override_still_beats_an_unusable_record(config, saved, monkeypatch):
    saved(unavailable="PREFERENCE_STATE_UNAVAILABLE")
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")

    assert prominence.resolve("claude-ai") == "maximal"
    assert prominence._active_source("claude-ai") == "env"
    assert prominence.capture_gate()["observed_outcomes"]["proactive_permitted"] is True


def test_resolved_reports_the_applied_context_and_the_saved_map(config, saved):
    saved(stored="maximal", contexts={"coding": "balanced"})

    payload = prominence.resolved("codex")

    assert payload["context"] == "coding"
    assert payload["surface"] == "codex"
    assert payload["source"] == "preference:context"
    assert payload["level"] == "balanced"
    assert payload["preference"]["contexts"] == {"coding": "balanced"}
    assert payload["preference"]["stored"] == "maximal"
    assert payload["preference"]["scope"] == "principal-and-vault"


def test_resolved_reports_a_conversation_context_for_a_hookless_surface(config, saved):
    saved(stored="maximal", contexts={"coding": "balanced"})

    payload = prominence.resolved("claude-ai")

    assert payload["context"] == "conversation"
    assert payload["source"] == "preference"
    assert payload["level"] == "maximal"


def test_resolved_reports_a_context_without_any_request_scope(config):
    payload = prominence.resolved()

    assert payload["context"] == "coding"
    assert "preference" not in payload
