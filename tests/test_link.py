"""link tool tests — typed entity creation under Entities/<Type>/."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import entity_candidates
from exomem import link as link_module

TODAY = dt.date(2026, 5, 25)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fm(p: Path) -> dict:
    fm = _read(p).split("\n---\n")[0].removeprefix("---\n")
    return yaml.safe_load(fm)


def _write_extension_registry(vault: Path) -> None:
    path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "place": {
                        "folder": "Places",
                        "label": "Place",
                        "aliases": ["location"],
                        "cue_nouns": ["venue"],
                        "capture_guidance": "A stable place identity.",
                        "parent": "concept",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_link_creates_person_entity(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="person",
        name="Jane Goodall",
        summary="Primatologist; long-running chimpanzee field studies.",
        affiliation="Jane Goodall Institute",
        relationship="referenced",
        today=TODAY,
    )
    assert result.path == "Knowledge Base/Entities/People/Jane Goodall.md"
    written = vault / result.path
    assert written.exists()
    fm = _fm(written)
    assert fm["type"] == "entity"
    assert fm["entity_type"] == "person"
    assert fm["affiliation"] == "Jane Goodall Institute"
    assert fm["relationship"] == "referenced"
    assert "# Jane Goodall" in _read(written)


def test_link_creates_concept_entity_with_domain(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="concept",
        name="Backpressure",
        summary="Reactive-system flow control mechanism.",
        domain="infrastructure",
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "concept"
    assert fm["domain"] == "infrastructure"


def test_link_creates_library_entity_with_metadata(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="library",
        name="pgvector",
        summary="Postgres extension for vector similarity search.",
        language="C",
        repo="https://github.com/pgvector/pgvector",
        license="PostgreSQL",
        used_in=["project-alpha", "project-beta"],
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "library"
    assert fm["language"] == "C"
    assert fm["repo"] == "https://github.com/pgvector/pgvector"
    assert fm["license"] == "PostgreSQL"
    assert fm["used_in"] == ["project-alpha", "project-beta"]


def test_link_creates_decision_entity_with_status(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="decision",
        name="Use Tailscale Funnel Over Cloudflare Tunnel",
        summary="Self-hosted MCP exposure decision.",
        decided="2026-05-18",
        project="project-alpha",
        decision_status="accepted",
        today=TODAY,
    )
    written = vault / result.path
    fm = _fm(written)
    assert fm["entity_type"] == "decision"
    assert fm["decided"] == dt.date(2026, 5, 18)  # YAML parses ISO date
    assert fm["project"] == "project-alpha"
    assert fm["decision_status"] == "accepted"


def test_link_creates_organization_entity_from_registry(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="organization",
        name="Northwind Research",
        summary="A durable research organization used across projects.",
        today=TODAY,
    )

    assert result.path == "Knowledge Base/Entities/Organizations/Northwind Research.md"
    written = vault / result.path
    assert written.exists()
    assert _fm(written)["entity_type"] == "organization"
    assert "# Northwind Research" in _read(written)


def test_link_rejects_invalid_entity_type(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="bogus",
            name="X",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "ENTITY_TYPE_UNKNOWN"
    assert "entity_type" in exc.value.missing


def test_create_entity_accepts_vault_defined_type_and_creates_its_folder_lazily(
    vault: Path,
) -> None:
    _write_extension_registry(vault)
    folder = vault / "Knowledge Base" / "Entities" / "Places"
    assert not folder.exists()

    result = link_module.link(
        vault,
        entity_type="place",
        name="Aster Hall",
        summary="A stable synthetic venue used across notes.",
        today=TODAY,
    )

    assert result.path == "Knowledge Base/Entities/Places/Aster Hall.md"
    assert folder.is_dir()
    assert _fm(vault / result.path)["entity_type"] == "place"


def test_create_entity_rejects_unknown_type_naming_active_ids(vault: Path) -> None:
    _write_extension_registry(vault)

    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="venue",
            name="Unknown Hall",
            summary="This type is not registered.",
            today=TODAY,
        )

    assert exc.value.code == "ENTITY_TYPE_UNKNOWN"
    assert "person" in exc.value.reason
    assert "place" in exc.value.reason


def test_resolve_entity_scopes_to_vault_defined_type(vault: Path) -> None:
    _write_extension_registry(vault)
    place = vault / "Knowledge Base" / "Entities" / "Places" / "aster-hall.md"
    place.parent.mkdir(parents=True)
    place.write_text(
        "---\ntype: entity\ntitle: Aster Hall\naliases: [Aster]\n"
        "entity_type: place\nstatus: active\n---\n# Aster Hall\n",
        encoding="utf-8",
    )
    person = vault / "Knowledge Base" / "Entities" / "People" / "aster-person.md"
    person.write_text(
        "---\ntype: entity\ntitle: Aster Person\naliases: [Aster]\n"
        "entity_type: person\nstatus: active\n---\n# Aster Person\n",
        encoding="utf-8",
    )

    result = entity_candidates.resolve_entity_candidate(
        vault,
        name="Aster",
        entity_type="place",
    )

    assert result["status"] == "match"
    assert [item["entity_type"] for item in result["candidates"]] == ["place"]


def test_link_rejects_missing_name(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="person",
            name="",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "name" in exc.value.missing


def test_link_rejects_missing_summary(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="concept",
            name="X",
            summary="",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "summary" in exc.value.missing


def test_link_rejects_invalid_decision_status(vault: Path) -> None:
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="decision",
            name="X",
            summary="y",
            decision_status="maybe",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_LINK"
    assert "decision_status" in exc.value.missing


def test_link_refuses_when_entity_exists(vault: Path) -> None:
    link_module.link(
        vault,
        entity_type="concept",
        name="Throughput",
        summary="x",
        today=TODAY,
    )
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="concept",
            name="Throughput",
            summary="y",
            today=TODAY,
        )
    assert exc.value.code == "ENTITY_EXISTS"


def test_entity_candidate_resolver_matches_active_page_alias(vault: Path) -> None:
    entity = vault / "Knowledge Base" / "Entities" / "People" / "Grace Hopper.md"
    source = _read(entity).replace("status: active\n", "status: active\naliases: [Amazing Grace]\n")
    entity.write_text(source, encoding="utf-8", newline="\n")

    resolution = entity_candidates.resolve_entity_candidate(vault, name="Amazing Grace")

    assert resolution["status"] == "match"
    assert [candidate["path"] for candidate in resolution["candidates"]] == [
        "Knowledge Base/Entities/People/Grace Hopper.md"
    ]
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="person",
            name="Amazing Grace",
            summary="Would duplicate an existing aliased identity.",
            today=TODAY,
        )
    assert exc.value.code == "ENTITY_EXISTS"
    assert exc.value.candidates == list(resolution["candidates"])


def test_entity_candidate_resolver_returns_bounded_alias_ambiguity(vault: Path) -> None:
    for filename in ("Ada Lovelace.md", "Grace Hopper.md"):
        entity = vault / "Knowledge Base" / "Entities" / "People" / filename
        source = _read(entity).replace("status: active\n", "status: active\naliases: [The Pioneer]\n")
        entity.write_text(source, encoding="utf-8", newline="\n")

    resolution = entity_candidates.resolve_entity_candidate(
        vault, name="The Pioneer", limit=1
    )

    assert resolution["status"] == "ambiguous"
    assert len(resolution["candidates"]) == 1
    assert resolution["omitted_candidate_count"] == 1
    with pytest.raises(link_module.LinkError) as exc:
        link_module.link(
            vault,
            entity_type="person",
            name="The Pioneer",
            summary="Would silently merge ambiguous identities.",
            today=TODAY,
        )
    assert exc.value.code == "ENTITY_AMBIGUOUS"
    assert len(exc.value.candidates) == 2


def test_link_normalizes_tags(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="person",
        name="Tag Test Person",
        summary="x",
        tags=["UPPER", "with space", "with_under", "dupe", "DUPE"],
        today=TODAY,
    )
    fm = _fm(vault / result.path)
    assert fm["tags"] == ["upper", "with-space", "with-under", "dupe"]


def test_link_appends_log_entry(vault: Path) -> None:
    log_file = vault / "Knowledge Base" / "log.md"
    link_module.link(
        vault,
        entity_type="library",
        name="Logged Lib",
        summary="x",
        today=TODAY,
    )
    text = _read(log_file)
    assert "## [2026-05-25] link | Entities/Libraries/Logged Lib" in text


def test_link_connections_normalize_and_render(vault: Path) -> None:
    # Mark a sibling top-level folder read-only so an unresolved reference into
    # it is kept vault-relative (the de-identified replacement for the old
    # hardcoded curated-tree list).
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Reference\n", encoding="utf-8"
    )
    result = link_module.link(
        vault,
        entity_type="concept",
        name="Connected Concept",
        summary="x",
        connections=[
            "Knowledge Base/Notes/Insights/foo",
            "[[Notes/Patterns/bar]]",
            "  Reference/Strategy  ",
        ],
        today=TODAY,
    )
    text = _read(vault / result.path)
    # Each connection rendered as a bullet.
    # KB-relative inputs get promoted to full vault-rooted form. Read-only
    # sibling-tree references (`Reference/...`, marked readonly in _access.yaml)
    # stay vault-relative — they don't live under Knowledge Base/.
    assert "## Relations" in text
    assert "- relates_to [[Knowledge Base/Notes/Insights/foo]]" in text
    assert "- relates_to [[Knowledge Base/Notes/Patterns/bar]]" in text
    assert "- relates_to [[Reference/Strategy]]" in text


def test_link_connections_use_kb_relative_form_for_nested_obsidian_root(
    vault: Path,
) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()

    result = link_module.link(
        vault,
        entity_type="concept",
        name="Nested Root Connection",
        summary="x",
        connections=["Knowledge Base/Notes/Insights/foo"],
        today=TODAY,
    )

    text = _read(vault / result.path)
    assert "- relates_to [[Notes/Insights/foo]]" in text
    assert "[[Knowledge Base/" not in text


# --- shared names: a distinct identity is a decision, not a refusal ---------
#
# `memory-loop` "Contextual name resolution preserves genuine ambiguity": an
# organization and its physical site may share one surface name. Creating the
# second is a non-mutating preparation naming the existing candidates and a
# fingerprint over them; only an explicit `distinct` decision bound to that
# fingerprint commits, and it never touches the other identity's page.


def _write_site_registry(vault: Path) -> None:
    path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "site": {
                        "folder": "Sites",
                        "label": "Site",
                        "aliases": [],
                        "capture_guidance": "A stable physical site identity.",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _vault_bytes(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted((vault / "Knowledge Base").rglob("*"))
        if path.is_file()
    }


def test_shared_name_distinct_identity_needs_decision_not_refusal(vault: Path) -> None:
    from exomem import commands

    _write_site_registry(vault)
    organization = link_module.link(
        vault,
        entity_type="organization",
        name="Kestrel Farm",
        summary="A synthetic farm business that operates a site of the same name.",
        today=TODAY,
    )
    organization_path = vault / organization.path
    organization_bytes = organization_path.read_bytes()
    before = _vault_bytes(vault)

    prepared = link_module.link(
        vault,
        entity_type="site",
        name="Kestrel Farm",
        summary="The physical farm site the business operates.",
        today=TODAY,
    )

    assert isinstance(prepared, link_module.IdentityPreparation)
    preparation = prepared.as_dict()
    assert preparation["mutated"] is False
    assert preparation["identity_decision"] == "required"
    evidence = preparation["identity_preparation"]
    assert [item["path"] for item in evidence["candidates"]] == [organization.path]
    assert evidence["candidates"][0]["entity_type"] == "organization"
    assert evidence["omitted_candidate_count"] == 0
    fingerprint = evidence["candidate_fingerprint"]
    assert len(fingerprint) == 64 and set(fingerprint) <= set("0123456789abcdef")
    assert _vault_bytes(vault) == before

    # The public command returns the same preparation rather than an error.
    via_command = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="site",
        name="Kestrel Farm",
        summary="The physical farm site the business operates.",
    )
    assert via_command["mutated"] is False
    assert via_command["identity_preparation"]["candidate_fingerprint"] == fingerprint
    assert _vault_bytes(vault) == before

    created = link_module.link(
        vault,
        entity_type="site",
        name="Kestrel Farm",
        summary="The physical farm site the business operates.",
        identity_decision={"outcome": "distinct", "candidate_fingerprint": fingerprint},
        today=TODAY,
    )

    assert isinstance(created, link_module.LinkResult)
    assert created.path == "Knowledge Base/Entities/Sites/Kestrel Farm.md"
    site = _fm(vault / created.path)
    assert site["entity_type"] == "site"
    assert "aliases" not in site
    decision = created.as_dict()["identity_decision"]
    assert decision["outcome"] == "distinct"
    assert decision["candidate_fingerprint"] == fingerprint
    assert decision["distinct_from"] == [organization.ref]
    # The other identity keeps its page, name and aliases untouched.
    assert organization_path.read_bytes() == organization_bytes
    resolved = entity_candidates.resolve_entity_candidate(vault, name="Kestrel Farm")
    assert resolved["status"] == "ambiguous"
    assert sorted(item["entity_type"] for item in resolved["candidates"]) == [
        "organization",
        "site",
    ]


def test_same_type_duplicate_is_refused_until_a_distinct_decision(vault: Path) -> None:
    existing = entity_candidates.resolve_entity_candidate(vault, name="Ada Lovelace")
    assert existing["status"] == "match"

    with pytest.raises(link_module.LinkError) as refused:
        link_module.link(
            vault,
            entity_type="person",
            name="Ada Lovelace",
            summary="A second, unrelated person who shares the name.",
            today=TODAY,
        )

    assert refused.value.code == "ENTITY_EXISTS"
    fingerprint = refused.value.candidate_fingerprint
    assert fingerprint is not None and len(fingerprint) == 64
    assert refused.value.as_dict()["candidate_fingerprint"] == fingerprint

    created = link_module.link(
        vault,
        entity_type="person",
        name="Ada Lovelace",
        slug="ada-lovelace-engineer",
        summary="A second, unrelated person who shares the name.",
        identity_decision={"outcome": "distinct", "candidate_fingerprint": fingerprint},
        today=TODAY,
    )

    assert created.path == "Knowledge Base/Entities/People/ada-lovelace-engineer.md"
    assert entity_candidates.resolve_entity_candidate(vault, name="Ada Lovelace")[
        "status"
    ] == "ambiguous"


def test_distinct_decision_refuses_stale_candidate_fingerprint(vault: Path) -> None:
    _write_site_registry(vault)
    link_module.link(
        vault,
        entity_type="organization",
        name="Kestrel Farm",
        summary="A synthetic farm business.",
        today=TODAY,
    )
    prepared = link_module.link(
        vault,
        entity_type="site",
        name="Kestrel Farm",
        summary="The physical farm site.",
        today=TODAY,
    )
    stale = prepared.as_dict()["identity_preparation"]["candidate_fingerprint"]

    # Another identity starts answering to the same name after preparation.
    person = vault / "Knowledge Base" / "Entities" / "People" / "Grace Hopper.md"
    person.write_text(
        _read(person).replace("status: active\n", "status: active\naliases: [Kestrel Farm]\n"),
        encoding="utf-8",
        newline="\n",
    )
    before = _vault_bytes(vault)

    with pytest.raises(link_module.LinkError) as refused:
        link_module.link(
            vault,
            entity_type="site",
            name="Kestrel Farm",
            summary="The physical farm site.",
            identity_decision={"outcome": "distinct", "candidate_fingerprint": stale},
            today=TODAY,
        )

    assert refused.value.code == "STALE_IDENTITY_DECISION"
    assert refused.value.candidate_fingerprint not in (None, stale)
    assert len(refused.value.candidates or []) == 2
    assert _vault_bytes(vault) == before

    for malformed in (
        {"outcome": "merge", "candidate_fingerprint": stale},
        {"outcome": "distinct", "candidate_fingerprint": "not-a-fingerprint"},
        {"outcome": "distinct"},
    ):
        with pytest.raises(link_module.LinkError) as invalid:
            link_module.link(
                vault,
                entity_type="site",
                name="Kestrel Farm",
                summary="The physical farm site.",
                identity_decision=malformed,
                today=TODAY,
            )
        assert invalid.value.code == "INVALID_IDENTITY_DECISION"
    assert _vault_bytes(vault) == before


def test_adoption_refuses_a_shared_name_instead_of_recording_no_page(vault: Path) -> None:
    """An adoption carries no distinct decision, so a name other types carry
    refuses the apply rather than reporting success with no page."""
    from exomem import adoption_proposals

    _write_site_registry(vault)
    link_module.link(
        vault,
        entity_type="organization",
        name="Kestrel Farm",
        summary="A synthetic farm business.",
        today=TODAY,
    )
    before = _vault_bytes(vault)

    with pytest.raises(ValueError, match="IDENTITY_DECISION_REQUIRED"):
        adoption_proposals._route_apply(
            vault,
            "entity",
            {"entity_type": "site", "name": "Kestrel Farm", "summary": "The farm site."},
            why="adopt the site",
            expected_hash=None,
        )
    assert _vault_bytes(vault) == before


def test_link_records_aliases_in_frontmatter(vault: Path) -> None:
    """Capture-time aliases: another spelling of the name, in any script, lands
    in the owner's `aliases` field of the new page, in the order given."""
    result = link_module.link(
        vault,
        entity_type="organization",
        name="Corvane Motors",
        summary="A small carmaker.",
        aliases=["コルヴェイン", " Corvane ", "コルヴェイン", "Corvane Motors"],
        today=TODAY,
    )
    fm = _fm(vault / result.path)
    # Stripped, deduplicated, and never the name itself.
    assert fm["aliases"] == ["コルヴェイン", "Corvane"]
    resolved = entity_candidates.resolve_entity_candidate(vault, name="コルヴェイン")
    assert resolved["status"] == "match"


def test_link_without_aliases_writes_no_alias_field(vault: Path) -> None:
    result = link_module.link(
        vault,
        entity_type="organization",
        name="Tessary Works",
        summary="A tool shop.",
        today=TODAY,
    )
    assert "aliases" not in _fm(vault / result.path)


@pytest.mark.parametrize(
    "aliases",
    [[""], ["   "], ["x" * 65], ["line\nbreak"], [f"Name {n}" for n in range(9)]],
    ids=["empty", "blank", "too-long", "newline", "too-many"],
)
def test_link_refuses_an_ungoverned_alias(vault: Path, aliases: list[str]) -> None:
    with pytest.raises(link_module.LinkError) as info:
        link_module.link(
            vault,
            entity_type="organization",
            name="Corvane Motors",
            summary="A small carmaker.",
            aliases=aliases,
            today=TODAY,
        )
    assert info.value.code == "INVALID_LINK"
    assert info.value.missing == ["aliases"]
    assert not (vault / "Knowledge Base" / "Entities" / "Organizations" / "Corvane Motors.md").exists()


def test_link_refuses_an_alias_another_entity_already_answers_to(vault: Path) -> None:
    link_module.link(
        vault,
        entity_type="organization",
        name="Corvane Motors",
        summary="A small carmaker.",
        aliases=["コルヴェイン"],
        today=TODAY,
    )
    with pytest.raises(link_module.LinkError) as info:
        link_module.link(
            vault,
            entity_type="organization",
            name="Corvane Holdings",
            summary="Its parent.",
            aliases=["コルヴェイン"],
            today=TODAY,
        )
    assert info.value.code == "ENTITY_EXISTS"
    assert info.value.missing == ["aliases"]
