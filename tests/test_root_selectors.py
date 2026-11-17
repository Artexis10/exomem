"""A `root` vault reads the path selectors that a `nested` vault authored, so the same pages stay withheld.

Governance scopes, `_access.yaml` tiers, instance bindings and folder topic carriers are authored against
the wrapper folder and the `Notes/` parent. A `root` vault has neither, and no file is rewritten, so each
reader must read the authored value through the served layout. A reader that does not matches nothing in
`root`, and an empty membership releases the page.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from exomem import access, commands, layout, recall_policy, topic_carriers
from exomem import find as find_module
from exomem.collection_store import history
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope
from exomem.vocabulary import instances, registry_specs
from tests.conftest import FIXTURE_VAULT, bind_root_vault, initialize_vault_state_offline

KB = "Knowledge Base"
NOTES = "Notes"
TOKEN = "zephyrquill"
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
RESTRICTED = "Insights/pump-cause.md"
OPEN = "Patterns/valve-check.md"
EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(title: str, kind: str = "insight") -> str:
    return f"---\ntitle: {title}\ntype: {kind}\n---\n\nThe {TOKEN} gasket needs a check.\n"


@pytest.fixture(autouse=True)
def _clear_caches():
    for clear in (policy._CACHE.clear, membership.clear_memo, egress.clear_decision_memo, find_module.clear_cache):
        clear()
    yield
    for clear in (policy._CACHE.clear, membership.clear_memo, egress.clear_decision_memo, find_module.clear_cache):
        clear()


@pytest.fixture
def root_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bound `root` vault with one page in the `Insights` area and one in the `Patterns` area."""
    dest = tmp_path / "rootvault"
    shutil.copytree(FIXTURE_VAULT / KB, dest, ignore=shutil.ignore_patterns(NOTES))
    shutil.move(dest / "_Schema", dest / ".exomem" / "schema")
    _write(dest, RESTRICTED, _page("Pump cause"))
    _write(dest, OPEN, _page("Valve check", "pattern"))
    bind_root_vault(dest, monkeypatch)
    initialize_vault_state_offline(dest, source="root selectors fixture")
    return dest


def _withhold(vault: Path, *, paths: str = "[]", refs: str = "[]", exclude_paths: str = "[]") -> None:
    """A scope with these authored selectors and a rule that releases nothing of it to the `external` audience."""
    governance = vault / "_Governance"
    _write(
        vault,
        "_Governance/scopes/insights.yaml",
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Insights\npaths: {paths}\nrefs: {refs}\n"
        f"exclude:\n  paths: {exclude_paths}\n",
    )
    _write(
        vault,
        "_Governance/rules/insights-external.yaml",
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 0\n',
    )
    assert governance.is_dir()


def _seen_by_the_external_caller(vault: Path) -> dict[str, object]:
    """What a restricted caller learns of the two pages through the read entry points."""
    with request_scope(EXTERNAL):
        searched = {item["id"] for item in commands.op_search(vault, query=TOKEN, limit=50)["results"]}
        found = {hit["path"] for hit in commands.op_find(vault, query=TOKEN, limit=10)}
        listed = set()
        for folder in ("Insights", "Patterns"):
            try:
                listed.update(entry["path"] for entry in commands.op_list_directory(vault, folder)["entries"])
            except ValueError as refused:  # a folder that holds only withheld pages reads as absent
                assert str(refused).startswith("NOT_FOUND")
        try:
            commands.op_get(vault, path=RESTRICTED)
        except ValueError as refused:
            read = str(refused).split(":")[0]
        else:
            read = "RELEASED"
    return {"search": searched, "find": found, "list": listed, "get": read}


# --------------------------------------------------------------------------- #
# Governance scopes: the disclosure that a `nested` vault's selectors gave
# --------------------------------------------------------------------------- #

_SELECTORS = {
    "wrapper and Notes parent": f'["{KB}/Notes/Insights/**"]',
    "Notes parent": '["Notes/Insights/**"]',
    "bare area": '["Insights/**"]',
}


@pytest.mark.parametrize("paths", _SELECTORS.values(), ids=_SELECTORS.keys())
def test_a_path_scope_withholds_its_area_from_a_restricted_caller_however_it_is_authored(
    root_vault: Path, paths: str
) -> None:
    """The glob matched nothing in `root`, so the empty membership released the page to every search and read."""
    _withhold(root_vault, paths=paths)

    seen = _seen_by_the_external_caller(root_vault)

    assert seen["search"] == {OPEN}
    assert seen["find"] == {OPEN}
    assert seen["list"] == {OPEN}
    assert seen["get"] == "NOT_FOUND"


def test_a_path_ref_withholds_the_page_it_names_from_a_restricted_caller(root_vault: Path) -> None:
    _withhold(root_vault, refs=f'["{KB}/Notes/Insights/pump-cause.md"]')

    seen = _seen_by_the_external_caller(root_vault)

    assert seen["search"] == {OPEN}
    assert seen["get"] == "NOT_FOUND"


def test_an_exclude_path_keeps_its_page_out_of_the_scope_however_it_is_authored(root_vault: Path) -> None:
    """An exclusion that matches nothing leaves the page in the scope, which withholds a page the owner opened."""
    _withhold(root_vault, paths='["Insights/**", "Patterns/**"]', exclude_paths=f'["{KB}/Notes/Patterns/**"]')

    seen = _seen_by_the_external_caller(root_vault)

    assert seen["search"] == {OPEN}
    assert seen["get"] == "NOT_FOUND"


def test_a_selector_names_the_same_pages_in_every_spelling(root_vault: Path) -> None:
    """The translated read is the proof of the plan's disclosure diff: one member set for all three spellings."""
    members = []
    for paths in _SELECTORS.values():
        _withhold(root_vault, paths=paths)
        for clear in (policy._CACHE.clear, membership.clear_memo, egress.clear_decision_memo):
            clear()
        loaded = policy.load(root_vault)
        scope_ids = {
            page: membership.evaluate_subject(root_vault, membership.MarkdownSubject(path=page), loaded, None)
            for page in (RESTRICTED, OPEN)
        }
        members.append(scope_ids)

    assert members[0] == members[1] == members[2]
    assert members[0][RESTRICTED] == {SCOPE_ID} and members[0][OPEN] == frozenset()


def _add_the_playbooks_area(vault: Path) -> None:
    """The owner's registry save that gives `Playbooks` its own note type, which makes it a registered area."""
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="note-types", operation="inspect")
        commands.op_schema_memory(
            vault,
            subject="note-types",
            operation="save",
            proposal={"upsert": {"playbook": {
                "label": "Playbook", "description": "A playbook.",
                "attributes": {"role": "compiled", "folder": "Playbooks"},
            }}},
            expected_hash=inspected["content_hash"],
            why="the owner keeps playbooks in their own area",
        )


def _what_stored_state_follows(vault: Path) -> dict[str, str | None]:
    """Every identity that an index, a recall checkpoint or a history release is kept against."""
    return {
        "access": access.policy_fingerprint(vault),
        "recall": recall_policy.recall_policy_identity(vault)[1],
        "publication": access.publication_policy_snapshot(vault).fingerprint,
        "history": history.release_inputs(vault),
    }


def test_a_registry_save_that_adds_an_area_withholds_its_page_from_a_restricted_caller_at_once(
    root_vault: Path,
) -> None:
    """A `Notes/<Area>/` selector reads as the area's folder only for a registered area, so a save moves its members."""
    _write(root_vault, "Playbooks/restart.md", _page("Restart order"))
    _withhold(root_vault, paths=f'["{KB}/Notes/Playbooks/**"]')

    def seen() -> dict[str, object]:
        with request_scope(EXTERNAL):
            searched = "Playbooks/restart.md" in {
                item["id"] for item in commands.op_search(root_vault, query=TOKEN, limit=50)["results"]
            }
            found = "Playbooks/restart.md" in {
                hit["path"] for hit in commands.op_find(root_vault, query=TOKEN, limit=10)
            }
            try:
                commands.op_get(root_vault, path="Playbooks/restart.md")
            except ValueError as refused:
                read = str(refused).split(":")[0]
            else:
                read = "RELEASED"
        return {"search": searched, "find": found, "get": read}

    # No registered area: the selector keeps its written form and selects nothing, so the page is released.
    assert seen() == {"search": True, "find": True, "get": "RELEASED"}

    _add_the_playbooks_area(root_vault)

    assert seen() == {"search": False, "find": False, "get": "NOT_FOUND"}


def test_a_registry_save_that_adds_an_area_moves_every_identity_that_stored_state_follows(root_vault: Path) -> None:
    """Stored recall checkpoints and history releases follow these identities, and no page or policy byte changed."""
    _write(root_vault, "Playbooks/restart.md", _page("Restart order"))
    _write(root_vault, "_access.yaml", "excluded:\n  - Notes/Playbooks\n")
    _withhold(root_vault, paths=f'["{KB}/Notes/Playbooks/**"]')
    before = _what_stored_state_follows(root_vault)
    assert access.access_tier(root_vault, "Playbooks/restart.md") != access.TIER_EXCLUDED

    _add_the_playbooks_area(root_vault)
    after = _what_stored_state_follows(root_vault)

    assert access.access_tier(root_vault, "Playbooks/restart.md") == access.TIER_EXCLUDED
    assert all(before[name] != after[name] for name in before), (before, after)
    # A recall checkpoint is published only while these two agree.
    assert before["access"] == before["recall"] == before["publication"]
    assert after["access"] == after["recall"] == after["publication"]


def test_a_registry_save_that_adds_an_area_leaves_the_identities_of_a_nested_vault_as_they_were(
    tmp_path: Path,
) -> None:
    """The area set belongs to `root`: a nested vault keeps its fingerprints, so none of its stored state rebuilds."""
    vault = tmp_path / "nested"
    shutil.copytree(FIXTURE_VAULT, vault)
    initialize_vault_state_offline(vault, source="nested identity fixture")
    raw = b"excluded:\n  - Notes/Playbooks\n"
    (vault / KB / "_access.yaml").write_bytes(raw)
    _withhold(vault / KB, paths=f'["{KB}/Notes/Playbooks/**"]')
    before = _what_stored_state_follows(vault)

    _add_the_playbooks_area(vault)
    after = _what_stored_state_follows(vault)

    assert before == after
    assert before["access"] == hashlib.sha256(raw).hexdigest()


def test_a_nested_vault_still_reads_the_selector_as_written(tmp_path: Path) -> None:
    """The translation belongs to `root`: `nested` keeps matching the spelling that its wrapper gives."""
    vault = tmp_path / "nested"
    shutil.copytree(FIXTURE_VAULT, vault)
    initialize_vault_state_offline(vault, source="nested selectors fixture")
    patterns = ("Notes/Patterns/**",)

    assert layout.read_selectors(patterns) is patterns
    assert membership.path_selector_matches(patterns, f"{KB}/Notes/Patterns/kill-switch-for-risky-releases.md")
    assert not membership.path_selector_matches(("Patterns/**",), f"{KB}/Notes/Patterns/kill-switch-for-risky-releases.md")


# --------------------------------------------------------------------------- #
# `_access.yaml` tiers
# --------------------------------------------------------------------------- #

_TIER_SPELLINGS = {
    "wrapper and Notes parent": f"{KB}/Notes/Insights",
    "Notes parent": "Notes/Insights",
    "bare area": "Insights",
}


@pytest.mark.parametrize("prefix", _TIER_SPELLINGS.values(), ids=_TIER_SPELLINGS.keys())
def test_an_excluded_tier_keeps_its_area_unreadable_however_it_is_authored(root_vault: Path, prefix: str) -> None:
    """The tier matched nothing in `root`, so an excluded tree was indexed and searchable."""
    _write(root_vault, "_access.yaml", f"excluded:\n  - {prefix}\n")

    assert access.access_tier(root_vault, RESTRICTED) == access.TIER_EXCLUDED
    assert access.access_tier(root_vault, OPEN) != access.TIER_EXCLUDED
    with library_scope():
        found = {item["id"] for item in commands.op_search(root_vault, query=TOKEN, limit=50)["results"]}
    assert found == {OPEN}


@pytest.mark.parametrize("prefix", _TIER_SPELLINGS.values(), ids=_TIER_SPELLINGS.keys())
def test_a_readonly_tier_refuses_a_write_into_its_area_however_it_is_authored(root_vault: Path, prefix: str) -> None:
    _write(root_vault, "_access.yaml", f"readonly:\n  - {prefix}\n")

    before = (root_vault / RESTRICTED).read_bytes()
    with library_scope(), pytest.raises(ValueError, match="WRITE_REFUSED"):
        commands.op_append_to_file(root_vault, path=RESTRICTED, content="tail\n")

    assert (root_vault / RESTRICTED).read_bytes() == before


# --------------------------------------------------------------------------- #
# Instance bindings and folder topic carriers
# --------------------------------------------------------------------------- #

PRIVATE = "01ARZ3NDEKTSV4RRFFQ69G5FC0"


def _binding_document() -> dict:
    return {
        "public": {"namespace": f"{KB}/_Schema/public", "history": "public"},
        "private": {PRIVATE: {"namespace": f"{KB}/_Schema/private", "history": "private"}},
        "destinations": {f"{KB}/Notes/Insights": PRIVATE},
        "selections": {},
    }


def test_an_instance_binding_selects_the_overlay_that_sits_in_the_root_vault(root_vault: Path) -> None:
    """The overlay path kept its wrapper, so a bound private registry was read from a folder that `root` lacks."""
    spec = registry_specs()["note-types"]

    chosen = instances.select(root_vault, spec, PRIVATE, configured=_binding_document)

    assert chosen.overlay(root_vault) == root_vault / "_Schema" / "private" / "note-types.yaml"


def test_an_instance_destination_selects_the_private_instance_for_a_root_area(root_vault: Path) -> None:
    """The destination folder kept its wrapper and `Notes/` parent, so a page in its area fell to the public instance."""
    chosen = instances.page_scope(
        root_vault, "Insights/new-page.md", {}, prospective=True, configured=_binding_document
    )

    assert chosen == PRIVATE


def test_a_folder_carrier_reaches_the_entries_of_its_root_area(root_vault: Path) -> None:
    """A carrier authored `Notes/<Area>/x` never matched the `<Area>/x` folder where a root vault keeps those entries."""
    _write(
        root_vault,
        "_Schema/topic-carriers.yaml",
        "schema_version: 1\nentries:\n  pumps:\n    description: Pump notes.\n"
        "    attributes: {kind: folder, match: Notes/Insights/Pumps, topics: [], origin: {}, rejected: []}\n",
    )

    carrier = topic_carriers.load(root_vault).of_kind(topic_carriers.KIND_FOLDER)[0]

    assert carrier.folder_parts == ("insights", "pumps")
    assert topic_carriers.identity_of(carrier.kind, carrier.match) == topic_carriers.identity_of(
        topic_carriers.KIND_FOLDER, "Insights/Pumps"
    )
