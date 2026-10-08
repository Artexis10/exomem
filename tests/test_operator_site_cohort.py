"""Operator/site and supplier-chain relationships through the public writers.

`close-memory-loop` task 5.4, driven by the frozen synthetic fixture 1.5
(`benchmarks/epistemic/memory_loop/supplier_chain.py`). Each episode starts
from its frozen pre-capture world; the capture below is SCRIPTED through the
product's public commands (a forced-call writer check, reported separately
from ordinary-agent initiation) and is scored by the fixture's own
evaluator, never by assertions written after the fact.

What this adds on top of the fixture's own plumbing checks is the
relationship contract: registered core relations where they are truthful
(`operates`, `supplies`), a governed relation-type/v1 extension for a missing
meaning, a same-name site that commits only on an explicit distinct decision,
lot attribution that stays in Records with unknown origin left silent,
succession that keeps the site and its history, and abstention on a bare
shared name.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from epistemic.memory_loop import supplier_chain as sc
from epistemic.memory_loop.contract import read_state

from exomem import commands, entity_candidates, relation_registry, writer_lease
from exomem.governance.principal import library_scope
from exomem.vault import content_hash, read_log_entries

pytestmark = pytest.mark.timeout(600)

WHY = "scripted cohort capture"


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Any]]:
    built: dict[str, tuple[Path, Any]] = {}
    for item in sc.EPISODES:
        root = tmp_path_factory.mktemp(f"cohort-{item.episode_id}") / "vault"
        built[item.episode_id] = (root, sc.build_pre_capture(root, item.episode_id))
    return built


def _world(worlds, episode_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, world = worlds[episode_id]
    root = tmp_path / "vault"
    shutil.copytree(source, root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    return root, world, read_state(root)


def _invoke(root: Path, tool: str, **arguments: Any) -> Any:
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == tool)
    return writer_lease.invoke_command(command, root, **arguments)


def _create(root: Path, entity_type: str, name: str, summary: str, **extra: Any) -> Any:
    return _invoke(
        root,
        "connect_memory",
        operation="create-entity",
        entity_type=entity_type,
        name=name,
        summary=summary,
        **extra,
    )


def _relate(root: Path, path: str, relation: str, target: str, why: str = WHY) -> None:
    """Author one typed edge on an existing page through edit_memory.

    A `## Relations` bullet takes no qualifier, so the edge's provenance is
    the governed write that authored it: its `why` lands in the page's log.
    """
    target = target.removesuffix(".md")
    text = (root / path).read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1].rstrip()
    if "\n## Relations\n" in f"\n{body}\n":
        new_body = f"{body}\n- {relation} [[{target}]]\n"
    else:
        new_body = f"{body}\n\n## Relations\n\n- {relation} [[{target}]]\n"
    _invoke(
        root,
        "edit_memory",
        path=path,
        why=why,
        operation={"kind": "replace_body", "new_body": new_body, "expected_hash": content_hash(text)},
    )


def _registered(root: Path, relation: str) -> bool:
    """A core relation, or an active extension, in the vault's own registry."""
    registry = relation_registry.load_registry(root)
    return relation in registry.core or (
        relation in registry.extensions and getattr(registry.extensions[relation], "status", "active") == "active"
    )


def _provenance(root: Path, path: str, why: str) -> bool:
    return any(why in entry.get("summary", "") for entry in read_log_entries(root, path))


def _append(root: Path, collection: str, item: dict[str, str], key: str) -> None:
    snapshot = commands.op_record_memory(root, action="inspect", collection=collection)["snapshot"]
    _invoke(
        root,
        "record_memory",
        action="append",
        collection=collection,
        item=item,
        item_key=key,
        expected_container_hash=snapshot,
        why=WHY,
    )


def _accepted(episode_id: str, root: Path, world, before) -> None:
    check = sc.check_capture(episode_id, world, before, read_state(root))
    assert check.accepted, [result for result in check.results if result.outcome == "fail"]


def _edges(root: Path, path: str) -> list[tuple[str, str]]:
    """The page's ``## Relations`` edges as the product derives them, each
    target resolved to the page it names."""
    return [
        (edge.relation, edge.target)
        for edge in read_state(root).edges
        if edge.source == path and edge.origin == "markdown_relation"
    ]


def test_operator_and_same_name_site_are_distinct_identities(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "org-and-site", tmp_path, monkeypatch)
    organization = world.key_to_path["org_merrow"]
    summary = "Orchard of about forty acres on the north slope of Tarrow valley."

    # The site shares the company's exact name: the writer prepares a decision
    # rather than refusing, and writes nothing until the agent decides.
    prepared = _create(root, "site", "Merrow Farm", summary)
    assert prepared["mutated"] is False
    evidence = prepared["identity_preparation"]
    assert [item["path"] for item in evidence["candidates"]] == [organization]
    assert read_state(root) == before

    created = _create(
        root,
        "site",
        "Merrow Farm",
        summary,
        identity_decision={
            "outcome": "distinct",
            "candidate_fingerprint": evidence["candidate_fingerprint"],
        },
    )
    site = created["path"]
    assert site == "Knowledge Base/Entities/Sites/Merrow Farm.md"
    # The operating role is a registered core relation, not ownership.
    _relate(root, organization, "operates", site)

    _accepted("org-and-site", root, world, before)
    assert _registered(root, "operates")
    assert ("operates", site) in _edges(root, organization)
    # The organization's own title and aliases never moved to the site.
    after_org = read_state(root).pages[organization]
    assert (after_org.title, after_org.entity_type, after_org.aliases) == (
        "Merrow Farm",
        "organization",
        (),
    )
    assert entity_candidates.resolve_entity_candidate(root, name="Merrow Farm")["status"] == (
        "ambiguous"
    )


def test_one_organization_carries_several_roles(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "multi-role", tmp_path, monkeypatch)
    organization = world.key_to_path["org_merrow"]
    text = (root / organization).read_text(encoding="utf-8")
    _invoke(
        root,
        "edit_memory",
        path=organization,
        why=WHY,
        operation={
            "kind": "replace_string",
            "old_string": "Apple business based in Tarrow valley.",
            "new_string": (
                "Apple business based in Tarrow valley. Grows most of what it sells and "
                "sells it at the Saturday market stall."
            ),
            "expected_hash": content_hash(text),
        },
    )
    _relate(root, organization, "operates", world.key_to_path["site_merrow"])

    _accepted("multi-role", root, world, before)
    assert _registered(root, "operates")
    assert ("operates", world.key_to_path["site_merrow"]) in _edges(root, organization)
    # No identity per role, and the chalkboard name made no brand.
    assert len([page for page in read_state(root).entities() if "Merrow" in page.title]) == 2


def test_bare_shared_name_leads_to_abstention(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "shared-name", tmp_path, monkeypatch)

    resolved = _invoke(root, "connect_memory", operation="resolve-entity", name="Merrow Farm")

    # Both identities answer; nothing in the turn separates them, so the agent
    # writes the hearsay to neither and creates nothing to hold it.
    assert resolved["status"] == "ambiguous"
    assert sorted(item["path"] for item in resolved["candidates"]) == sorted(
        [world.key_to_path["org_merrow"], world.key_to_path["site_merrow"]]
    )
    _accepted("shared-name", root, world, before)
    assert read_state(root) == before


def test_mixed_producer_lots_keep_their_own_origin(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "mixed-purchase", tmp_path, monkeypatch)
    purchases = sc.PURCHASES.manifest_path
    rows = (
        ("0921-A", {"producer": "Merrow Farm", "origin": "label"}),
        (
            "0921-B",
            {
                "producer": "Pellow Orchards",
                "origin": "reported",
                "origin_source": "stall worker at Merrow Farm",
            },
        ),
        ("0921-C", {"origin": "unknown"}),
    )
    for index, (lot, fields) in enumerate(rows, start=1):
        _append(
            root,
            purchases,
            {"lot": lot, "bought_on": "2026-09-21", "seller": "Merrow Farm", **fields},
            f"20000000-0000-4000-8000-00000000009{index}",
        )
    # The general sourcing relationship the seller reported is a registered
    # core edge; which lot it covers stays in Records.
    reported = "Merrow Farm stall reported lot 0921-B came in from Pellow Orchards (2026-09-21)"
    _relate(
        root,
        world.key_to_path["org_pellow"],
        "supplies",
        world.key_to_path["org_merrow"],
        why=reported,
    )

    _accepted("mixed-purchase", root, world, before)
    assert _registered(root, "supplies")
    assert _provenance(root, world.key_to_path["org_pellow"], reported)
    after = read_state(root)
    unknown = [
        record for record in after.records if record.fields.get("lot") == "0921-C"
    ]
    assert len(unknown) == 1 and not unknown[0].fields.get("producer")
    # No producer edge is inferred for the seller or for the unknown lot, and
    # no shortcut edge presents the reported source as the seller's production.
    merrow_edges = _edges(root, world.key_to_path["org_merrow"])
    assert not [edge for edge in merrow_edges if edge[0] == "produces"]
    assert _edges(root, world.key_to_path["org_pellow"]) == [
        ("supplies", world.key_to_path["org_merrow"])
    ]


def test_operator_succession_keeps_the_site_and_its_history(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "operator-succession", tmp_path, monkeypatch)
    site = world.key_to_path["site_merrow"]
    # The handover retires the predecessor's operates edge: after it, only
    # the Records history presents Merrow Farm as the site's operator.
    predecessor = world.key_to_path["org_merrow"]
    text = (root / predecessor).read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    kept = "\n".join(line for line in body.splitlines() if "vault.operates" not in line)
    _invoke(
        root,
        "edit_memory",
        path=predecessor,
        why="the orchard changed operator",
        operation={
            "kind": "replace_body",
            "new_body": kept.replace("## Relations\n", "").rstrip() + "\n",
            "expected_hash": content_hash(text),
        },
    )
    callow = _create(
        root, "organization", "Callow Cider Company", "Cider maker running the north-slope orchard."
    )["path"]
    _append(
        root,
        sc.OPERATORS.manifest_path,
        {
            "site": "Merrow Farm Orchard",
            "operator": "Callow Cider Company",
            "since": "2026-09-01",
            "source": "Merrow Farm stall",
        },
        "20000000-0000-4000-8000-000000000022",
    )
    stated = "Merrow Farm stall: Callow Cider Company runs the orchard since 2026-09-01"
    _relate(root, callow, "operates", site, why=stated)

    _accepted("operator-succession", root, world, before)
    assert _registered(root, "operates")
    assert _provenance(root, callow, stated)
    # Running the orchard is not owning it; the site kept its identity and the
    # earlier assignment stays as history in Records.
    assert _edges(root, callow) == [("operates", site)]
    history = sorted(
        record.fields["operator"]
        for record in read_state(root).records
        if record.collection == sc.OPERATORS.manifest_path
    )
    assert history == ["Callow Cider Company", "Merrow Farm"]


def test_a_missing_meaning_goes_through_the_governed_relation_extension(
    worlds, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, world, before = _world(worlds, "brand", tmp_path, monkeypatch)
    brand = _create(root, "brand", "Tarrow Crown", "The valley growers' quality mark.")["path"]

    # No core relation says "sells under": propose one with semantics, a core
    # parent and a direction. A proposal is not an active definition.
    proposed = _invoke(
        root,
        "schema_memory",
        subject="relations",
        operation="propose-relation",
        proposal={
            "requested_label": "sells_under",
            "parent": "relates_to",
            "description": "Sells its produce under a mark or label it does not own.",
            "direction": "directed",
        },
    )
    assert proposed["valid"] is True, proposed
    key = next(iter(proposed["delta"]["upsert"]))
    assert key == "vault.sells_under"
    assert relation_registry.load_registry(root).definition(key) is None

    with library_scope():
        saved = _invoke(
            root,
            "schema_memory",
            subject="relations",
            operation="save-relations",
            proposal=proposed["delta"],
            expected_hash=proposed["content_hash"],
            why="Selling under a growers' mark is a durable, recurring distinction.",
        )
    assert saved["state"] == "committed", saved
    assert relation_registry.load_registry(root).definition(key) is not None

    _relate(root, world.key_to_path["org_merrow"], key, brand)
    _relate(root, world.key_to_path["org_pellow"], key, brand)

    _accepted("brand", root, world, before)


def test_the_core_registry_ships_the_cohort_relations() -> None:
    core = relation_registry.core_registry().core
    for key in ("located_at", "operates", "supplies", "produces", "member_of"):
        definition = core[key]
        assert definition.direction == "directed", key
        assert definition.family not in {"relation", "link", "mention"}, key
