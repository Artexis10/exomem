"""The served contract classes every promotion (`promote-referents-without-nudges` D2).

Owner decision O2 makes the owner's entity creation, typed edges and registry saves
`proactive_capture`. Before this, the served texts told the agent to confirm them or
left them unclassified.

The payload test reads the structured fields an agent acts on. The phrase test keeps
the retired confirm-first texts out of every served payload and shipped copy, where a
stale copy would still make the agent ask.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, prominence, vocabulary_authority
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope

ROOT = Path(__file__).resolve().parents[1]

OWNER_ON_V1 = {
    "entity_creation": "proactive_capture",
    "typed_edge": "proactive_capture",
    "registry_save": "proactive_capture",
    "queue_acceptance": "link_acceptance",
    "edge_removal": "restructure_execution",
    "promotion_revert": "restructure_execution",
}


def test_an_owner_on_a_v1_vault_reads_every_promotion_class(vault: Path) -> None:
    with library_scope():
        reference = commands.op_bootstrap(vault, profile="compact", section="all")
        core = commands.op_bootstrap(vault, profile="compact")

    assert reference["vocabulary_workflow"]["authority"]["contract_version"] == "v1"
    assert reference["engagement"]["envelope"]["promotion_classes"] == OWNER_ON_V1
    # The always-served core carries the same map, not only the envelope section.
    assert core["engagement"]["envelope"]["promotion_classes"] == OWNER_ON_V1
    lifecycle = reference["entity_registry"]["lifecycle"]["authority"]
    assert lifecycle["relation_outside_curation"] == "proactive_capture"


def test_a_resolved_non_owner_can_only_surface_a_registry_save(vault: Path) -> None:
    """Its save becomes a pending item for the owner, so it cannot promote one."""
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        reference = commands.op_bootstrap(vault, profile="compact", section="all")

    assert reference["engagement"]["envelope"]["promotion_classes"] == {
        **OWNER_ON_V1,
        "registry_save": "structural_suggestions",
    }


def test_a_v2_vault_serves_its_grant_rule_and_no_map(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ActivatedAuthority:
        def __init__(self, _root: Path) -> None:
            pass

        def runtime_status(self) -> vocabulary_authority.AuthorityStatus:
            return vocabulary_authority.AuthorityStatus("v2", 7, 0)

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", ActivatedAuthority)
    with library_scope():
        reference = commands.op_bootstrap(vault, profile="compact", section="all")

    assert "promotion_classes" not in reference["engagement"]["envelope"]
    authority = reference["vocabulary_workflow"]["authority"]
    assert authority["contract_version"] == "v2"
    assert authority["scoped_delegation"] is True
    assert authority["rule"]
    lifecycle = reference["entity_registry"]["lifecycle"]["authority"]
    assert lifecycle["relation_outside_curation"] == "link_acceptance"


#: The confirm-first texts that design D2 item 8 retires, as each was served or
#: shipped. Each told the owner's agent to ask before a promotion, or left one
#: unclassified.
RETIRED_TEXTS = (
    "Decision not permission; v1 writers retain confirmation.",
    '"relation_outside_curation": "link_acceptance"',
    "Missing schema: structural_suggestions/restructure_execution; relations: link_acceptance.",
    "an affiliation relation requires link_acceptance",
    "entity merge and deletion. Deletion has a server-side confirm parameter",
    "On a personal vault, additive entity creation follows proactive_capture",
    "| `link_acceptance` | confirm | accepting a suggested relation |",
    "affiliation relations require `link_acceptance`",
    "an affiliation relation requires `link_acceptance`",
    "an affiliation relation uses `link_acceptance`",
    "affiliation relations use `link_acceptance`",
    "affiliation relation: `link_acceptance`",
)

#: Every shipped carrier of the agent contract: the scaffold in the wheel, the
#: committed plugin packages, and the pasted custom instructions in the guides.
SHIPPED_ROOTS = (
    "src/exomem/_scaffold",
    "plugins/claude-code",
    "plugins/cloud/generated",
    "docs",
    "QUICKSTART.md",
    "README.md",
)


def _shipped_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for root in SHIPPED_ROOTS:
        base = ROOT / root
        for path in [base] if base.is_file() else sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                texts[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return texts


def _served_texts(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    texts: dict[str, str] = {}
    for level in prominence.CANON:
        monkeypatch.setenv("EXOMEM_PROMINENCE", level)
        with library_scope():
            for profile, section in (
                ("compact", None),
                ("compact", "all"),
                ("full", None),
                ("diagnostics", None),
            ):
                payload = commands.op_bootstrap(vault, profile=profile, section=section)
                texts[f"bootstrap {level}/{profile}/{section}"] = json.dumps(
                    payload, ensure_ascii=False
                )
    return texts


def test_no_retired_confirm_first_text_is_served_or_shipped(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    carriers = {**_served_texts(vault, monkeypatch), **_shipped_texts()}

    found = [
        (name, retired)
        for name, text in carriers.items()
        for retired in RETIRED_TEXTS
        if retired in text
    ]
    assert not found, found
