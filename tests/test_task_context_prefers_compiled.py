"""Task leads send the agent to the current compiled note before the raw episode it absorbed."""

from __future__ import annotations

from pathlib import Path

from test_activate_context_maps import _activate
from test_context_catalogue import _built, _write
from test_working_set_index import _seed_structure

from exomem import find, lexstore

KB = "Knowledge Base"
# The recorder names a recap file this way; the catalogue lists only files of that shape as episodes.
EPISODE = f"{KB}/Sources/Episodes/2026-10-01-kestrel-cabin-eq-presets-ep0123456789ab-20261001t051410265784-ab7d4d5a.md"
PROFILE = f"{KB}/Notes/Research/Vehicles/kestrel-vehicle-profile.md"


def test_task_leads_put_the_compiled_note_before_the_episode_it_absorbed(vault: Path) -> None:
    """The incident: recall led with an early episode and the agent answered from its first draft."""
    _seed_structure(vault)
    # The episode is short and dense in the turn's words, so plain text ranking puts it first.
    _write(vault, EPISODE, (
        "---\ntype: source\nsource_type: episode\ncaptured: 2026-10-01T05:14:10Z\n"
        "episode: ep-0123456789ab0123456789ab01234567\nclient: chatgpt\n"
        "ingested_into: [\"[[Notes/Research/Vehicles/kestrel-vehicle-profile]]\"]\n---\n\n"
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
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    # A running service keeps the shared link resolver resident; writers and searches load it.
    find.shared_resolver(vault)

    packet = _activate(vault, turn="EQ for Kestrel")

    leads = [item["source_ref"] for item in packet["task_context"]["candidates"]]
    assert PROFILE in leads and EPISODE in leads, leads
    assert leads.index(PROFILE) < leads.index(EPISODE), leads
