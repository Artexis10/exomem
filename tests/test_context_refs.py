"""Stable Exomem context refs."""

from __future__ import annotations

from exomem import context_refs


def test_vault_and_source_refs_are_url_safe() -> None:
    assert (
        context_refs.vault_ref("Warranty Case/laptop-receipt.md")
        == "exomem://vault/Warranty%20Case/laptop-receipt.md"
    )
    assert (
        context_refs.source_ref("Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt.md")
        == "exomem://source/Knowledge%20Base/Sources/Imported/2026-07-07-laptop-receipt"
    )


def test_proposal_ref_is_stable_for_source_set() -> None:
    sources = ["Knowledge Base/Sources/Imported/a", "Knowledge Base/Sources/Imported/b"]
    assert context_refs.proposal_ref(sources) == context_refs.proposal_ref(list(sources))
    assert context_refs.proposal_ref(sources).startswith("exomem://proposal/")
    assert context_refs.proposal_ref(sources) != context_refs.proposal_ref(list(reversed(sources)))
