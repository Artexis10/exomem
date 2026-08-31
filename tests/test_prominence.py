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

    assert len(reminder) < 1600, len(reminder)
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
