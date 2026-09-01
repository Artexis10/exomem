"""The delegation envelope: ceilings, derivation, overrides, and refusals.

Prominence answers "how much should Exomem speak up". The envelope answers the
question underneath it — "what is Exomem allowed to do on its own" — per action
class, under hard ceilings that no level, override or adaptation may lift.

The ceilings are product law; the envelope only ever chooses a disposition
BELOW one. That asymmetry is what these tests pin: the derivation table is
reproduced from the design so a drift is visible as a diff, the write path
refuses everything outside a class range, and the read path never refuses at
all — a stored value written by a newer runtime is reported and ignored,
because reading the envelope must never break bootstrap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import envelope, mode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the shared config at a throwaway file and clear the env overrides."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    return path


def _stored(path: Path) -> dict:
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


# --------------------------------------------------------------- the closed set


def test_the_v1_class_set_is_closed_and_every_class_carries_a_ceiling() -> None:
    assert envelope.ACTION_CLASSES == (
        "hygiene_writes",
        "proactive_capture",
        "link_acceptance",
        "structural_suggestions",
        "restructure_execution",
        "disclosure",
    )
    assert dict(envelope.CEILINGS) == {
        "hygiene_writes": "silent",
        "proactive_capture": "silent-capable",
        "link_acceptance": "confirm",
        "structural_suggestions": "advisory",
        "restructure_execution": "confirm-required",
        "disclosure": "governance-owned",
    }


def test_three_classes_are_ranged_two_are_fixed_and_disclosure_has_no_disposition() -> None:
    assert dict(envelope.RANGES) == {
        "proactive_capture": ("off", "advisory", "silent"),
        "link_acceptance": ("off", "advisory", "confirm-shortcut"),
        "structural_suggestions": ("off", "advisory"),
    }
    assert dict(envelope.FIXED) == {
        "hygiene_writes": "silent",
        "restructure_execution": "confirm",
    }
    assert envelope.GOVERNANCE_OWNED == frozenset({"disclosure"})
    # Every class is accounted for exactly once by one of the three groups.
    covered = set(envelope.RANGES) | set(envelope.FIXED) | set(envelope.GOVERNANCE_OWNED)
    assert covered == set(envelope.ACTION_CLASSES)


def test_confirm_shortcut_is_an_inline_single_action_confirmation() -> None:
    """It sits BELOW the `confirm` ceiling because the step is never skipped."""
    meaning = envelope.CONFIRM_SHORTCUT.lower()

    assert "inline" in meaning
    assert "one" in meaning or "single" in meaning
    assert "never skipped" in meaning


# ------------------------------------------------------------------ derivation


#: The design table, restated so a drift shows up as a diff on this line rather
#: than as a behaviour nobody noticed. Column order is off/light/balanced/maximal.
DESIGN_TABLE = {
    "hygiene_writes": ("silent", "silent", "silent", "silent"),
    "proactive_capture": ("off", "off", "silent", "silent"),
    "link_acceptance": ("off", "advisory", "advisory", "advisory"),
    "structural_suggestions": ("off", "advisory", "advisory", "advisory"),
    "restructure_execution": ("confirm", "confirm", "confirm", "confirm"),
}


@pytest.mark.parametrize("index,level", list(enumerate(("off", "light", "balanced", "maximal"))))
def test_derivation_matches_the_design_table(index: int, level: str) -> None:
    derived = envelope.derive_envelope(level)

    assert derived == {
        action_class: row[index] for action_class, row in DESIGN_TABLE.items()
    }
    assert "disclosure" not in derived


def test_derivation_is_pure_and_touches_no_configuration(tmp_path, monkeypatch) -> None:
    """`derive_envelope` is the attributable half of (level, overrides)."""
    missing = tmp_path / "never-written.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(missing))

    assert envelope.derive_envelope("balanced")["proactive_capture"] == "silent"
    assert not missing.exists()


def test_the_envelope_module_is_import_cheap() -> None:
    """`commands.op_bootstrap` serves this on every session start."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from exomem import envelope\n"
            "assert envelope.derive_envelope('balanced')\n"
            "assert 'torch' not in sys.modules\n"
            "assert 'numpy' not in sys.modules\n",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------- write-time refusals


def test_an_unknown_class_is_refused_with_a_class_specific_error(config) -> None:
    with pytest.raises(ValueError) as error:
        envelope.set_disposition("standing_sweeps", "silent")

    assert "UNKNOWN_ACTION_CLASS" in str(error.value)
    assert "standing_sweeps" in str(error.value)
    assert "envelope" not in _stored(config)


@pytest.mark.parametrize(
    "action_class,disposition",
    [("link_acceptance", "silent"), ("structural_suggestions", "confirm-shortcut")],
)
def test_an_out_of_range_disposition_is_refused_naming_the_range(
    config, action_class: str, disposition: str
) -> None:
    with pytest.raises(ValueError) as error:
        envelope.set_disposition(action_class, disposition)

    message = str(error.value)
    assert "ENVELOPE_DISPOSITION_OUT_OF_RANGE" in message
    assert action_class in message
    for allowed in envelope.RANGES[action_class]:
        assert allowed in message
    assert "envelope" not in _stored(config)


def test_disclosure_is_refused_naming_the_governance_plane(config) -> None:
    with pytest.raises(ValueError) as error:
        envelope.set_disposition("disclosure", "advisory")

    message = str(error.value)
    assert "DISCLOSURE_IS_GOVERNANCE_OWNED" in message
    assert "governance plane" in message
    assert "envelope" not in _stored(config)


def test_a_fixed_class_that_is_not_restructure_execution_is_refused_as_fixed(config) -> None:
    with pytest.raises(ValueError) as error:
        envelope.set_disposition("hygiene_writes", "off")

    message = str(error.value)
    assert "ENVELOPE_CLASS_FIXED" in message
    assert "silent" in message
    assert "envelope" not in _stored(config)


# ---------------------------------------------------------- read-time tolerance


def test_a_stored_unknown_class_is_reported_and_ignored_rather_than_refused(config) -> None:
    """Written by a newer runtime. Reading the envelope never breaks bootstrap."""
    config.write_text(
        json.dumps(
            {
                "schema": 1,
                "prominence": "balanced",
                "envelope": {"standing_sweeps": "silent", "proactive_capture": "advisory"},
            }
        ),
        "utf-8",
    )

    served = envelope.resolved()

    assert served["classes"]["proactive_capture"]["disposition"] == "advisory"
    assert served["classes"]["proactive_capture"]["provenance"] == "override"
    assert [row["class"] for row in served["ignored"]] == ["standing_sweeps"]
    assert served["ignored"][0]["reason"] == "unknown_class"
    assert set(served["classes"]) == set(envelope.ACTION_CLASSES)


def test_a_stored_out_of_range_disposition_is_reported_and_ignored(config) -> None:
    config.write_text(
        json.dumps({"schema": 1, "envelope": {"structural_suggestions": "silent"}}),
        "utf-8",
    )

    served = envelope.resolved()

    row = served["classes"]["structural_suggestions"]
    assert row["disposition"] == "advisory"
    assert row["provenance"] == "derived"
    assert served["ignored"] == [
        {
            "class": "structural_suggestions",
            "value": "silent",
            "reason": "out_of_range",
        }
    ]


def test_a_malformed_envelope_object_is_ignored_whole(config) -> None:
    config.write_text(json.dumps({"schema": 1, "envelope": ["not", "an", "object"]}), "utf-8")

    served = envelope.resolved()

    assert served["classes"]["proactive_capture"]["provenance"] == "derived"
    assert served["ignored"][0]["reason"] == "malformed_envelope"


# --------------------------------------------------------------- storage (1.2)


def test_an_override_outlives_a_prominence_change_and_a_restart(config) -> None:
    from exomem import prominence

    prominence.write_prominence("maximal")
    envelope.set_disposition("proactive_capture", "advisory")
    prominence.write_prominence("balanced")

    # "Restart" is a fresh read of the file: nothing is cached in the process.
    served = envelope.resolved()

    assert served["classes"]["proactive_capture"] == {
        "ceiling": "silent-capable",
        "disposition": "advisory",
        "provenance": "override",
    }
    for other in ("link_acceptance", "structural_suggestions"):
        assert served["classes"][other]["provenance"] == "derived"
        assert served["classes"][other]["disposition"] == "advisory"


def test_the_override_lives_in_the_shared_config_beside_mode_and_prominence(config) -> None:
    from exomem import prominence

    prominence.write_prominence("light")
    mode.write_mode("quiet")
    envelope.set_disposition("structural_suggestions", "off")

    stored = _stored(config)

    assert stored["mode"] == "quiet"
    assert stored["prominence"] == "light"
    assert stored["envelope"] == {"structural_suggestions": "off"}
    assert envelope.set_disposition("link_acceptance", "off") == mode.config_path()


def test_reset_restores_pure_derivation_for_the_named_class_only(config) -> None:
    from exomem import prominence

    prominence.write_prominence("balanced")
    envelope.set_disposition("proactive_capture", "off")
    envelope.set_disposition("structural_suggestions", "off")

    envelope.reset_disposition("proactive_capture")
    served = envelope.resolved()

    assert served["classes"]["proactive_capture"]["disposition"] == "silent"
    assert served["classes"]["proactive_capture"]["provenance"] == "derived"
    assert served["classes"]["structural_suggestions"]["provenance"] == "override"


def test_deleting_the_envelope_object_restores_todays_shipped_behaviour(config) -> None:
    """The rollback pin: an absent key IS pure derivation."""
    from exomem import prominence

    prominence.write_prominence("maximal")
    envelope.set_disposition("proactive_capture", "advisory")

    stored = _stored(config)
    stored.pop("envelope")
    config.write_text(json.dumps(stored), "utf-8")

    assert envelope.resolved()["classes"] == {
        action_class: {
            "ceiling": envelope.CEILINGS[action_class],
            "disposition": disposition,
            "provenance": "fixed" if action_class in envelope.FIXED else "derived",
        }
        for action_class, disposition in envelope.derive_envelope("maximal").items()
    } | {
        "disclosure": {
            "ceiling": "governance-owned",
            "disposition": None,
            "provenance": "governance-owned",
        }
    }


def test_reset_of_a_class_that_was_never_overridden_is_a_no_op(config) -> None:
    from exomem import prominence

    prominence.write_prominence("balanced")
    envelope.reset_disposition("link_acceptance")

    assert _stored(config).get("envelope", {}) == {}
    assert envelope.resolved()["classes"]["link_acceptance"]["provenance"] == "derived"


# ------------------------------------------------------ serving it (1.3)


@pytest.fixture
def bare_vault(tmp_path: Path) -> Path:
    root = tmp_path / "served-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    return root


def _served(vault: Path, profile: str = "compact") -> dict:
    from exomem import commands

    return commands.op_bootstrap(vault, profile=profile)["engagement"]["envelope"]


@pytest.mark.parametrize("profile", ["compact", "full", "diagnostics"])
def test_bootstrap_serves_every_class_with_ceiling_and_provenance(
    config, bare_vault: Path, profile: str
) -> None:
    from exomem import prominence

    prominence.write_prominence("balanced")
    envelope.set_disposition("link_acceptance", "off")

    served = _served(bare_vault, profile)

    assert served["level"] == "balanced"
    assert set(served["classes"]) == set(envelope.ACTION_CLASSES)
    assert served["classes"]["hygiene_writes"] == {
        "ceiling": "silent",
        "disposition": "silent",
        "provenance": "fixed",
    }
    assert served["classes"]["proactive_capture"] == {
        "ceiling": "silent-capable",
        "disposition": "silent",
        "provenance": "derived",
    }
    assert served["classes"]["link_acceptance"] == {
        "ceiling": "confirm",
        "disposition": "off",
        "provenance": "override",
    }
    assert served["classes"]["restructure_execution"] == {
        "ceiling": "confirm-required",
        "disposition": "confirm",
        "provenance": "fixed",
    }
    assert served["classes"]["disclosure"] == {
        "ceiling": "governance-owned",
        "disposition": None,
        "provenance": "governance-owned",
    }


def test_the_served_envelope_moves_with_the_active_level(config, bare_vault: Path) -> None:
    from exomem import prominence

    prominence.write_prominence("off")
    assert _served(bare_vault)["classes"]["structural_suggestions"]["disposition"] == "off"

    prominence.write_prominence("maximal")
    assert _served(bare_vault)["classes"]["structural_suggestions"]["disposition"] == "advisory"


def test_a_stored_unknown_class_still_serves_bootstrap_and_names_the_id(
    config, bare_vault: Path
) -> None:
    """Reading the envelope never breaks bootstrap — not even from the future."""
    config.write_text(
        json.dumps({"schema": 1, "envelope": {"standing_sweeps": "silent"}}), "utf-8"
    )

    served = _served(bare_vault)

    assert set(served["classes"]) == set(envelope.ACTION_CLASSES)
    assert served["ignored"] == [
        {"class": "standing_sweeps", "value": "silent", "reason": "unknown_class"}
    ]
