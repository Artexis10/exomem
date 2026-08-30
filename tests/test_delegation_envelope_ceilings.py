"""Ceilings are product law — the most permissive envelope cannot lift one.

Confirm-required binds at three tiers and all three are pinned here, because
each one alone is removable and the removal reads as a convenience:

1. the SERVED envelope marks `restructure_execution` confirm-required, however
   permissive the rest of the envelope is;
2. deletion still refuses without its explicit server-side `confirm`;
3. the adoption apply surface still refuses to commit anything that was not
   previewed first.

`test_the_serving_path_refuses_a_forbidden_override_even_if_storage_lets_one_through`
is the standing mechanism proof for tier 1: an assertion that cannot fail proves
nothing, so the marker's presence is shown to depend on the code that puts it
there rather than on the class name being spelled the same way twice.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import adoption_run, commands, envelope, prominence

TODAY = dt.date(2026, 7, 14)


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    return path


def _as_permissive_as_the_ranges_allow() -> None:
    """`maximal` prominence and every configurable class at the top of its range."""
    prominence.write_prominence("maximal")
    for action_class, allowed in envelope.RANGES.items():
        envelope.set_disposition(action_class, allowed[-1])


def _legacy_vault(root: Path) -> Path:
    vault = root / "vault"
    old = vault / "Old Notes"
    old.mkdir(parents=True)
    (old / "quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nShip the envelope this quarter.\n", encoding="utf-8"
    )
    kb_root = vault / "Knowledge Base"
    (kb_root / "Notes").mkdir(parents=True)
    sources = kb_root / "Sources"
    sources.mkdir(parents=True)
    (sources / "index.md").write_text(
        "# Sources - Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8"
    )
    (kb_root / "index.md").write_text(
        "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n",
        encoding="utf-8",
    )
    (kb_root / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    return vault


# ------------------------------------------------------- tier 1: the served marker


def test_maximal_prominence_with_every_override_still_marks_confirm_required(
    config, tmp_path: Path
) -> None:
    _as_permissive_as_the_ranges_allow()
    root = tmp_path / "served-vault"
    (root / "Knowledge Base").mkdir(parents=True)

    served = commands.op_bootstrap(root, profile="compact")["engagement"]["envelope"]

    assert served["level"] == "maximal"
    # The permissive settings really did land, so the assertion below is about
    # the ceiling rather than about an override that quietly failed to write.
    assert served["classes"]["proactive_capture"]["disposition"] == "silent"
    assert served["classes"]["link_acceptance"]["disposition"] == "confirm-shortcut"
    assert served["classes"]["link_acceptance"]["provenance"] == "override"

    assert served["classes"]["restructure_execution"] == {
        "ceiling": "confirm-required",
        "disposition": "confirm",
        "provenance": "fixed",
    }


def test_the_served_contract_states_the_server_side_gap_rather_than_implying_it_away(
    config, tmp_path: Path
) -> None:
    """v1 adds no confirm parameter for supersession or entity creation.

    A contract that named deletion's gate and stopped would leave an agent to
    infer that every hard-to-reverse mutation has one behind it. It does not,
    and the served text has to say so.
    """
    root = tmp_path / "served-vault"
    (root / "Knowledge Base").mkdir(parents=True)

    clause = commands.op_bootstrap(root, profile="compact")["engagement"]["envelope"][
        "confirm_required"
    ].lower()

    assert "supersession" in clause
    assert "entity creation" in clause
    assert "no server-side" in clause
    assert "future work" in clause
    # Command-free, exactly like the epistemic commitments: this clause matters
    # most on the reduced surfaces where `_filter_bootstrap_payload` deletes any
    # string naming an unavailable command.
    assert "(" not in clause and "_memory" not in clause


# -------------------------------------------------------- tier 2: deletion's gate


def test_deletion_still_requires_its_explicit_confirm_parameter(config, vault: Path) -> None:
    _as_permissive_as_the_ranges_allow()
    target = vault / "Knowledge Base" / "Notes" / "envelope-scratch.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Scratch\n\nDisposable.\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        commands.op_delete(vault, path="Knowledge Base/Notes/envelope-scratch.md", confirm=False)

    assert "UNCONFIRMED" in str(error.value)
    assert target.is_file(), "the refusal must leave the file where it was"


# ------------------------------------------------- tier 3: adoption is preview-first


def test_the_adoption_apply_surface_still_defaults_preview_first(
    config, tmp_path: Path
) -> None:
    _as_permissive_as_the_ranges_allow()
    vault = _legacy_vault(tmp_path)
    run_id = commands.op_adoption_studio(vault, action="start")["run_id"]
    commands.op_adoption_studio(vault, action="select", run_id=run_id, include=["Old Notes"])

    # Nothing was previewed, so nothing may be committed.
    with pytest.raises(ValueError) as unplanned:
        commands.op_adoption_studio(vault, action="apply", run_id=run_id)
    assert "INVALID_PHASE" in str(unplanned.value)

    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    assert planned["plan"]["plan_id"]

    # A preview exists, but this apply does not echo it.
    with pytest.raises(ValueError) as unechoed:
        commands.op_adoption_studio(vault, action="apply", run_id=run_id)
    assert "PLAN_STALE" in str(unechoed.value)

    assert not list((vault / "Knowledge Base" / "Sources").glob("**/quarterly*"))


# --------------------------------------------------- the mechanism-removal proof


def test_the_serving_path_refuses_a_forbidden_override_even_if_storage_lets_one_through(
    config, tmp_path: Path, monkeypatch
) -> None:
    """Defence in depth, and it is not decoration.

    Three separate places would have to agree for a `restructure_execution`
    override to reach a client: the write refusal, the stored-override reader,
    and the serving path. Two of them are pinned elsewhere; this pins the third
    by patching `stored_overrides` to answer as though an override had been
    accepted and stored — precisely the state the write refusal exists to make
    unreachable. A ceiling that held only because storage happened to filter
    correctly would be an accident rather than product law.
    """
    root = tmp_path / "served-vault"
    (root / "Knowledge Base").mkdir(parents=True)

    monkeypatch.setattr(
        envelope,
        "stored_overrides",
        lambda: ({"restructure_execution": "silent"}, []),
    )

    served = commands.op_bootstrap(root, profile="compact")["engagement"]["envelope"]

    assert served["classes"]["restructure_execution"]["disposition"] == "confirm", (
        "the scratch mutant proves nothing: the served marker did not depend on "
        "the code that produces it"
    )


# ------------------------------------------- the founder gate is the sole error (2.2)


@pytest.mark.parametrize(
    "disposition",
    ["silent", "advisory", "off", "always-allow", "confirm", "", "from now on"],
)
def test_every_restructure_execution_request_refuses_by_naming_the_founder_gate(
    config, disposition: str
) -> None:
    with pytest.raises(ValueError) as error:
        envelope.set_disposition("restructure_execution", disposition)

    message = str(error.value)
    assert "STANDING_DELEGATION_REFUSED" in message
    assert "founder" in message
    assert "ratification" in message
    assert "v1" in message


@pytest.mark.parametrize("disposition", ["silent", "always-allow", ""])
def test_the_generic_range_refusal_never_fires_for_restructure_execution(
    config, disposition: str
) -> None:
    """Two refusals in one module is how a founder gate becomes a typo message."""
    with pytest.raises(ValueError) as error:
        envelope.set_disposition("restructure_execution", disposition)

    message = str(error.value)
    assert "ENVELOPE_DISPOSITION_OUT_OF_RANGE" not in message
    assert "ENVELOPE_CLASS_FIXED" not in message
    assert "UNKNOWN_ACTION_CLASS" not in message


def test_a_refused_standing_delegation_leaves_the_envelope_untouched(
    config, tmp_path: Path
) -> None:
    prominence.write_prominence("balanced")
    envelope.set_disposition("proactive_capture", "advisory")
    before = envelope.resolved()

    with pytest.raises(ValueError):
        envelope.set_disposition("restructure_execution", "silent")

    assert envelope.resolved() == before
    assert "restructure_execution" not in (config.read_text("utf-8"))
