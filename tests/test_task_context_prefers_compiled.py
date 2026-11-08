"""Task leads send the agent to the current compiled note before the raw episode it absorbed."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _write
from test_governance_egress import _external, _reset_governance_caches
from test_working_set_index import _seed_structure
from test_working_set_withheld_twins import _withhold

from exomem import find, lexstore

KB = "Knowledge Base"
# The recorder names a recap file this way; the catalogue lists only files of that shape as episodes.
EPISODE = f"{KB}/Sources/Episodes/2026-10-01-kestrel-cabin-eq-presets-ep0123456789ab-20261001t051410265784-ab7d4d5a.md"
PROFILE = f"{KB}/Notes/Research/Vehicles/kestrel-vehicle-profile.md"
TWIN = f"{KB}/Notes/Restricted/kestrel-vehicle-profile.md"


def _seed_episode_and_profile(vault: Path, *, profile_link: str) -> None:
    _seed_structure(vault)
    # The episode is short and dense in the turn's words, so plain text ranking puts it first.
    _write(vault, EPISODE, (
        "---\ntype: source\nsource_type: episode\ncaptured: 2026-10-01T05:14:10Z\n"
        "episode: ep-0123456789ab0123456789ab01234567\nclient: chatgpt\n"
        f"ingested_into: [\"[[{profile_link}]]\"]\n---\n\n"
        "# Kestrel cabin EQ presets\n\nBuilt a first Kestrel EQ preset: Kestrel EQ bass cut, Kestrel EQ draft.\n"
    ))
    # The compiled profile holds the tested result among other vehicle facts.
    _write(vault, PROFILE, (
        "---\ntype: research-note\nstatus: active\nupdated: 2026-10-20\n"
        "sources: [\"[[Sources/Episodes/2026-10-01-kestrel-cabin-eq-presets-ep0123456789ab-20261001t051410265784-ab7d4d5a]]\"]\n---\n\n"
        "# Kestrel vehicle profile\n\n## Tyres\n\nWinter tyres are fitted in November and checked for tread depth.\n\n"
        "## Service\n\nThe engine takes a synthetic oil and an annual service at the dealer.\n\n"
        "## Audio\n\nThe tested and preferred EQ is the 31-band curve; it replaced the first 10-band preset.\n"
    ))


def _leads(vault: Path, **caller) -> list[str]:
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    # A running service keeps the shared link resolver resident; writers and searches load it.
    find.shared_resolver(vault)
    packet = _activate(vault, turn="EQ for Kestrel", **caller)
    return [item["source_ref"] for item in packet["task_context"]["candidates"]]


def test_task_leads_put_the_compiled_note_before_the_episode_it_absorbed(vault: Path) -> None:
    """The incident: recall led with an early episode and the agent answered from its first draft."""
    _seed_episode_and_profile(vault, profile_link="Notes/Research/Vehicles/kestrel-vehicle-profile")

    leads = _leads(vault)

    assert PROFILE in leads and EPISODE in leads, leads
    assert leads.index(PROFILE) < leads.index(EPISODE), leads


def test_a_withheld_namesake_does_not_change_a_restricted_callers_order(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare link that a withheld page makes ambiguous must not reveal that page through the order."""
    # A restricted caller gets task leads only from its released corpus.
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    _seed_episode_and_profile(vault, profile_link="kestrel-vehicle-profile")
    _withhold(vault, TWIN, ceiling=0)
    without_twin = _leads(vault, caller=_external())
    _write(vault, TWIN, "---\ntype: note\nstatus: active\n---\n\n# Kestrel vehicle profile\n\nPrivate.\n")
    _reset_governance_caches()

    with_twin = _leads(vault, caller=_external())

    assert EPISODE in with_twin and PROFILE in with_twin, with_twin
    assert with_twin == without_twin
