"""A persisted `entity_recurrence` sweep answers exactly as a full sweep.

The store is read through, so the only claims worth testing are the two a user
depends on: the findings never differ from a full sweep of current files, for the
owner and for a restricted audience, and an unchanged page is not read again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from time import thread_time

import pytest

from exomem import entity_evidence, find, markdown_relations
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal

KB = "Knowledge Base"
NOTES = f"{KB}/Notes"
HIDDEN = f"{NOTES}/Withheld"
RESTRICTED = RequestPrincipal(audience_id="external", surface="mcp", resolved=True)
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
LATENCY_CORPUS_SIZE = 2_000
WARM_SWEEP_CPU_CEILING_SECONDS = 3.0


def _page(title: str, body: str, *, sources: tuple[str, ...] = (), kind: str = "insight") -> str:
    source_lines = "".join(f'  - "[[{source}]]"\n' for source in sources)
    return (
        f"---\ntype: {kind}\ntitle: {title}\nstatus: active\n"
        + (f"sources:\n{source_lines}" if sources else "")
        + f"---\n# {title}\n\n{body}\n"
    )


def _entity(title: str, aliases: tuple[str, ...] = ()) -> str:
    return (
        f"---\ntype: entity\ntitle: {title}\nentity_type: organization\nstatus: active\n"
        f"aliases: {json.dumps(list(aliases))}\n---\n# {title}\n"
    )


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture_files() -> dict[str, str]:
    relation_kind = sorted(markdown_relations.RELATION_TYPES)[0]
    return {
        # An Entity with an alias: the registry the frames and hydration resolve against.
        f"{KB}/Entities/Organizations/amber-guild.md": _entity("Amber Guild", ("Amber Co",)),
        # One identity three origins reach for, typed three ways, plus anchor frames.
        f"{NOTES}/cobalt-1.md": _page(
            "Cobalt 1",
            "Cobalt Works is an organization.\n\nWe use Cobalt Works. Cobalt Works builds Amber Guild.",
            sources=("Source A",),
        ),
        f"{NOTES}/cobalt-2.md": _page("Cobalt 2", "organization: Cobalt Works.", sources=("Source B",)),
        f"{NOTES}/cobalt-3.md": _page("Cobalt 3", "Membership: Cobalt Works.", sources=("Source C",)),
        # Resolved by the registry: one context links the Entity, one relates to it, one does not.
        f"{NOTES}/amber-1.md": _page(
            "Amber 1", "Membership: [[Amber Guild|Amber Guild]].", sources=("Source D",)
        ),
        f"{NOTES}/amber-2.md": _page(
            "Amber 2",
            f"Amber Guild is an organization.\n\n## Relations\n\n- {relation_kind} [[Amber Guild]]",
            sources=("Source E",),
        ),
        f"{NOTES}/amber-3.md": _page("Amber 3", "organization: Amber Guild.", sources=("Source F",)),
        # Qualifies for the restricted audience only: the hidden page joins two Sources.
        f"{NOTES}/violet-1.md": _page(
            "Violet 1", "Violet Circle is an organization.", sources=("Source V1",)
        ),
        f"{NOTES}/violet-2.md": _page("Violet 2", "organization: Violet Circle.", sources=("Source V2",)),
        f"{NOTES}/violet-3.md": _page("Violet 3", "Membership: Violet Circle.", sources=("Source V3",)),
        f"{HIDDEN}/violet-hidden.md": _page(
            "Violet Hidden",
            "Violet Circle is an organization.",
            sources=("Source V1", "Source V2"),
        ),
        # The wikilink lane: two pages link a name nothing defines.
        f"{NOTES}/quill-1.md": _page("Quill 1", "See [[Quill Press]]."),
        f"{NOTES}/quill-2.md": _page("Quill 2", "Also [[Quill Press]]."),
        f"{NOTES}/index.md": _page("Index", "- [[Quill Press]]\n- [[Marlow Hall]]"),
        f"{NOTES}/fenced.md": _page(
            "Fenced", "```\nCobalt Works is an organization.\n[[Marlow Hall]]\n```"
        ),
        f"{NOTES}/carrier.md": _page(
            "Carrier",
            "<!-- exomem-origin: [[Marlow Hall]] Cobalt Works is an organization. -->\nPlain text.",
        ),
        # Excluded from every read surface by `_access.yaml`: read live, never stored.
        f"{KB}/Private/secret.md": _page("Secret", "Cobalt Works is an organization."),
        # Frames only once `collective` becomes a cue noun.
        f"{NOTES}/teal-1.md": _page("Teal 1", "Teal Union is a collective.", sources=("Source T1",)),
        f"{NOTES}/teal-2.md": _page("Teal 2", "collective: Teal Union.", sources=("Source T2",)),
        f"{NOTES}/teal-3.md": _page("Teal 3", "Membership: Teal Union.", sources=("Source T3",)),
    }


def _build(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for rel, text in _fixture_files().items():
        _write(vault, rel, text)
    _write(vault, f"{KB}/_access.yaml", "excluded:\n  - Private\n")
    _write(
        vault,
        f"{KB}/_Governance/scopes/withheld.yaml",
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["Notes/Withheld/**"]\n',
    )
    _write(
        vault,
        f"{KB}/_Governance/rules/withheld.yaml",
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
    )
    return vault


def _reset_caches() -> None:
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find.clear_cache()


def _sweep(vault: Path, principal: RequestPrincipal | None, *, stored: bool) -> tuple[str, dict]:
    """The findings of one explicit entity_recurrence audit, and its refresh counters."""
    _reset_caches()
    run = entity_evidence.run_sweep(vault, principal=principal, stored=stored)
    return run.findings, run.spans.get("entity_evidence.refresh", {})


def _both_audiences_match_a_full_sweep(vault: Path) -> dict:
    """Assert store == full for the owner and a restricted audience; return the owner's counters."""
    counters: dict = {}
    for principal in (None, RESTRICTED):
        full, _ = _sweep(vault, principal, stored=False)
        stored, fields = _sweep(vault, principal, stored=True)
        assert stored == full, f"store path diverged for {principal or 'the owner'}"
        if principal is None:
            counters = fields
    return counters


def _utime_preserving_rewrite(path: Path, text: str) -> None:
    """An out-of-band edit that keeps the size and restores the modification time."""
    before = path.stat()
    assert len(text.encode()) == before.st_size
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _rewrite_cobalt_3(vault: Path) -> None:
    _write(vault, f"{NOTES}/cobalt-3.md", _page("Cobalt 3", "Membership: Cobalt Works, Quill Press.", sources=("Source C",)))


def _delete_quill_2(vault: Path) -> None:
    (vault / f"{NOTES}/quill-2.md").unlink()


def _rename_violet_2(vault: Path) -> None:
    (vault / f"{NOTES}/violet-2.md").rename(vault / f"{NOTES}/violet-2b.md")


def _create_cobalt_entity(vault: Path) -> None:
    _write(vault, f"{KB}/Entities/Organizations/cobalt-works.md", _entity("Cobalt Works"))


def _change_amber_alias(vault: Path) -> None:
    _write(vault, f"{KB}/Entities/Organizations/amber-guild.md", _entity("Amber Guild", ("Amber Syndicate",)))


def _share_a_source(vault: Path) -> None:
    _write(vault, f"{NOTES}/violet-3.md", _page("Violet 3", "Membership: Violet Circle.", sources=("Source V1",)))


def _create_entity_titled_we(vault: Path) -> None:
    _write(vault, f"{KB}/Entities/Organizations/we.md", _entity("We"))


def _same_size_out_of_band_rewrite(vault: Path) -> None:
    path = vault / f"{NOTES}/cobalt-2.md"
    _utime_preserving_rewrite(path, path.read_text(encoding="utf-8").replace("Cobalt", "Cobolt"))


def _add_a_cue_noun(vault: Path) -> None:
    _write(
        vault,
        f"{KB}/_Schema/entity-types.yaml",
        "schema_version: 1\nentity_types:\n  community:\n    folder: Communities\n"
        "    label: Community\n    aliases: []\n    cue_nouns: [collective]\n"
        "    capture_guidance: A stable synthetic community identity.\n"
        "    parent: organization\n    status: active\n",
    )


def _stored_pages(vault: Path, *, kind: str) -> set[str]:
    """Walk the fixture independently of the store: non-Entities or Entities pages."""
    pages = {
        path.relative_to(vault).as_posix()
        for path in (vault / KB).rglob("*.md")
        if not path.relative_to(vault).as_posix().startswith(f"{KB}/Private/")
        and not path.relative_to(vault).as_posix().startswith(f"{HIDDEN}/")
    }
    entities = {rel for rel in pages if rel.startswith(f"{KB}/Entities/")}
    return entities if kind == "entities" else pages - entities


@pytest.mark.parametrize("trust_ctime", [True, False], ids=["stat-identity", "hash-every-row"])
def test_store_path_matches_a_full_sweep_for_every_audience_across_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_ctime: bool
) -> None:
    monkeypatch.setattr(entity_evidence, "_ctime_is_change_time", lambda: trust_ctime)
    vault = _build(tmp_path)

    # Cold build: every page this audience may see, bar the excluded one, is extracted.
    owner_full, _ = _sweep(vault, None, stored=False)
    restricted_full, _ = _sweep(vault, RESTRICTED, stored=False)
    assert "violet circle" in restricted_full and "violet circle" not in owner_full, (
        "the hidden page must change what the restricted audience sees"
    )
    assert "quill press" in owner_full and "cobalt works" in owner_full
    counters = _both_audiences_match_a_full_sweep(vault)
    expected_cold = len(_stored_pages(vault, kind="rest")) + len(_stored_pages(vault, kind="entities"))
    assert counters["pages_reextracted"] == expected_cold + 1  # the hidden page is the owner's own
    assert counters["pages_volatile"] == 1  # the excluded page is read live, never stored

    steps: list[tuple[str, Callable[[Path], None], int | Callable[[Path], int]]] = [
        ("edit a page", _rewrite_cobalt_3, 1),
        ("delete a page", _delete_quill_2, 0),
        ("rename a page", _rename_violet_2, 1),
        ("create an Entity", _create_cobalt_entity, 1),
        ("change an alias", _change_amber_alias, 1),
        ("change a shared Source", _share_a_source, 1),
        ("create an Entity titled We", _create_entity_titled_we, 1),
        ("rewrite a page out of band, keeping size and mtime", _same_size_out_of_band_rewrite, 1),
        (
            "add a cue noun to the entity-type registry",
            _add_a_cue_noun,
            lambda root: len(_stored_pages(root, kind="rest")) + 1,  # plus the owner's hidden page
        ),
    ]
    for name, mutate, expected in steps:
        mutate(vault)
        counters = _both_audiences_match_a_full_sweep(vault)
        want = expected(vault) if callable(expected) else expected
        assert counters["pages_reextracted"] == want, f"{name}: re-extracted {counters}"
        # A second sweep of the unchanged vault reads no page again.
        _, again = _sweep(vault, None, stored=True)
        assert again["pages_reextracted"] == 0, f"{name}: a repeat sweep re-extracted {again}"


def test_a_warm_sweep_of_a_large_vault_extracts_no_page_and_stays_cheap(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault, f"{KB}/Entities/Organizations/amber-guild.md", _entity("Amber Guild", ("Amber Co",)))
    for index in range(LATENCY_CORPUS_SIZE):
        body = (
            "Amber guild is an organization that maintains the shared tools."
            if index % 2 == 0
            else "Membership: amber guild. We use the survey notes on Tuesday."
        )
        _write(vault, f"{NOTES}/note-{index:04d}.md", _page(f"Note {index}", body, sources=(f"Source {index}",)))

    _sweep(vault, None, stored=True)
    started = thread_time()
    _, fields = _sweep(vault, None, stored=True)
    elapsed = thread_time() - started

    assert fields["pages_reextracted"] == 0
    assert elapsed < WARM_SWEEP_CPU_CEILING_SECONDS
