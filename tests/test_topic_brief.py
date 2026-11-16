"""A topic brief, read through `read_memory` by an owner and by a restricted caller.

The workflow under test is an agent asking what a subject's notes conclude, cite and leave open, then
reading the brief again later. The checks name what only a real vault catches: a withheld page that moves
a byte a restricted caller receives, a brief that reads pages again when nothing changed, a closing relation
that sits on another page, and a watermark that one principal moves for another.

Invented names throughout.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import initialize_vault_state_offline
from test_activate_context_surface import _surface_server
from test_governance_egress import _external, _reset_caches
from test_working_set_withheld_twins import _withhold

from exomem import commands, epistemic_graph, lexstore, topic_brief
from exomem import find as find_module
from exomem.governance import egress, principal

NOTES = "Knowledge Base/Notes/Insights"
SOURCES = "Knowledge Base/Sources/Articles"
OWNER = principal.owner_principal(surface="library")
PARTNER = principal.RequestPrincipal(audience_id="partner", surface="library")


def _id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"brief/{name}"))


def _ref(name: str) -> str:
    return f"exomem://memory/{_id(name)}"


def _put(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _note(vault: Path, name: str, body: str = "", *, updated: str = "2026-09-01", head: str = "") -> str:
    rel = f"{NOTES}/{name}.md"
    _put(
        vault, rel,
        f"---\ntype: insight\nstatus: active\nupdated: {updated}\nexomem_id: {_id(name)}\n{head}---\n\n# {name}\n\n{body}",
    )
    return rel


def _source(vault: Path, name: str, *, kind: str = "article", captured: str = "2026-08-01") -> str:
    rel = f"{SOURCES}/{name}.md"
    _put(
        vault, rel,
        f"---\ntype: source\nsource_type: {kind}\ncaptured: {captured}\nexomem_id: {_id(name)}\n---\n\n# {name}\n\nCaptured text.\n",
    )
    return rel


def _cites(*names: str) -> str:
    return "sources:\n" + "".join(f'  - "[[{SOURCES}/{name}]]"\n' for name in names)


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "vault") -> Path:
    vault = tmp_path / name
    vault.mkdir()
    initialize_vault_state_offline(vault, source="topic brief fixture")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _reindex(vault: Path) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()


def _index(vault: Path) -> None:
    _reindex(vault)
    lexstore.ensure_fresh(vault)
    topic_brief._CACHE.clear()


def _read(vault: Path, subject: str, caller: Any = None, **options: Any) -> dict[str, Any]:
    """One brief read through the command path and the terminal egress filter, as `caller` (default: the owner)."""
    options.setdefault("brief", True)
    with principal.request_scope(caller or OWNER), principal.invocation_scope():
        result = commands.op_read_memory(vault, path=subject, **options)
        return egress.postfilter("read_memory", result, vault, request_kwargs={"path": subject, **options})


def _refs(brief: dict[str, Any], section: str) -> list[str]:
    return [item["ref"] for item in brief["items"] if item["section"] == section]


def _hub_world(vault: Path) -> None:
    """A hub that cites a Source, links one sister and has two open threads, one of them answered."""
    _source(vault, "tide-table")
    _note(
        vault, "hub",
        "See [the berth docs](https://example.test/berths) and [[sister-one]].\n\n"
        "```\n[a link in code](https://example.test/in-code)\n```\n\n"
        "## Claim\n- id: old-claim\n\nBerths close at dusk.\n\n"
        "## Claim\n- id: new-claim\n- relations: supersedes: [[#old-claim]]\n\nBerths close at nightfall.\n\n"
        "## Open threads\n- id: thread-one\n\nWho keeps the key?\n\n"
        "## Open threads\n- id: thread-two\n\nWhen does the key change hands?\n\n"
        "## Summary\n\n- [fact] The outer quay has eight berths.\n",
        head="summary: Everything about the quay.\n" + _cites("tide-table"),
    )
    _note(
        vault, "sister-one",
        "Same docs: [berth docs](https://example.test/berths).\n\n"
        f"## Finding\n- id: key-answer\n- relations: answers: [[{NOTES}/hub#thread-one]]\n\nThe harbour master keeps the key.\n",
        updated="2026-09-02",
    )
    _index(vault)


def test_a_hub_brief_serves_conclusions_sources_and_open_questions(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    assert brief["ref"] == f"exomem://brief/{_id('hub')}" and brief["state"] == "ready"
    assert brief["seed"]["ref"] == _ref("hub")
    conclusions = [item for item in brief["items"] if item["section"] == "conclusions"]
    assert [(item["kind"], item["text"]) for item in conclusions] == [
        ("note", "Everything about the quay."),
        ("claim", "Berths close at nightfall."),
        ("observation", "The outer quay has eight berths."),
        ("note", ""),
        ("finding", "The harbour master keeps the key."),
    ]
    (hub_header,) = [item for item in conclusions if item["ref"] == _ref("hub")]
    assert hub_header["text"] == "Everything about the quay."
    assert hub_header["omitted"] == {"superseded": 1, "closed": 1, "expired": 0}
    assert _refs(brief, "open_questions") == [f"{_ref('hub')}#thread-two"]
    (source,) = [item for item in brief["items"] if item["kind"] == "source"]
    assert (source["ref"], source["title"], source["source_kind"], source["captured"]) == (
        _ref("tide-table"), "tide-table", "article", "2026-08-01",
    )
    links = [item["url"] for item in brief["items"] if item["kind"] == "link"]
    assert links == ["https://example.test/berths"]
    assert all({"ref", "path", "updated"} <= item.keys() for item in brief["items"])
    # The ref the response names reads the same brief, whichever door it comes through.
    again = _read(vault, brief["ref"], brief=False)
    assert {k: v for k, v in again.items() if k != "changed_since"} == {k: v for k, v in brief.items() if k != "changed_since"}


def test_a_page_that_only_points_at_the_seed_is_still_a_member(tmp_path, monkeypatch) -> None:
    """The one edge between the pages runs from the sister to the seed, so a walk of outgoing links alone loses it."""
    vault = _vault(tmp_path, monkeypatch)
    _note(vault, "hub", "## Open threads\n- id: thread-one\n\nWho keeps the key?\n")
    _note(
        vault, "sister",
        f"## Finding\n- id: key-answer\n- relations: answers: [[{NOTES}/hub#thread-one]]\n\nThe harbour master keeps the key.\n",
    )
    _index(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    assert f"{_ref('sister')}#key-answer" in _refs(brief, "conclusions")
    assert _refs(brief, "open_questions") == []  # the sister's answer closes the thread


def test_a_withheld_member_changes_no_byte_a_restricted_caller_receives(tmp_path, monkeypatch) -> None:
    """The restricted caller reads the same brief whether or not a page it may not read exists, and after it changes."""
    from test_governance_egress import SCOPE_ID  # noqa: F401 - the scope the shared helper writes

    def world(name: str, *, with_withheld: bool) -> Path:
        vault = _vault(tmp_path, monkeypatch, name)
        _source(vault, "tide-table")
        _note(
            vault, "hub", "Berths: [[sister]] and [[classified]].\n\n## Claim\n- id: hub-claim\n\nThe quay has eight berths.\n",
            head=_cites("tide-table"),
        )
        _note(vault, "sister", "## Claim\n- id: sister-claim\n\nThe quay lights are on a timer.\n")
        if with_withheld:
            _note(
                vault, "classified", "## Claim\n- id: secret-claim\n\nThe quay safe holds the spare key.\n",
                head=_cites("tide-table"),
            )
        _withhold(vault, f"{NOTES}/classified.md", ceiling=0)
        _index(vault)
        _reset_caches()
        return vault

    def both(vault: Path) -> list[str]:
        return [
            json.dumps(_read(vault, subject, _external()), sort_keys=True)
            for subject in (f"{NOTES}/hub.md", "quay berths")
        ]

    with_page, without_page = world("with", with_withheld=True), world("without", with_withheld=False)
    first = both(with_page)
    assert first == both(without_page)
    served = json.loads(first[0])
    assert "secret-claim" not in json.dumps(served) and served["missing"] == [] and served["changed_since"] == {"state": "first_read"}

    _note(with_page, "classified", "## Claim\n- id: secret-claim\n\nThe quay safe holds a new key.\n", head=_cites("tide-table"))
    _index(with_page)
    _index(without_page)
    _reset_caches()
    assert both(with_page) == both(without_page)
    assert json.loads(both(with_page)[0])["changed_since"]["state"] == "unchanged"


def test_a_member_withheld_after_the_compile_makes_the_terminal_guard_serve_nothing(tmp_path, monkeypatch) -> None:
    """A policy change lands between the compile and the response: the guard decides again and serves no item."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"

    with principal.request_scope(_external()), principal.invocation_scope():
        compiled = commands.op_read_memory(vault, path=hub, brief=True)
        assert compiled["items"]  # the control: this caller reads the brief
        _withhold(vault, f"{NOTES}/sister-one.md", ceiling=0)
        _reset_caches()
        guarded = egress.postfilter("read_memory", compiled, vault, request_kwargs={"path": hub, "brief": True})

    assert guarded["state"] == "unavailable" and guarded["items"] == [] and guarded["continuation"] is None


def test_a_topic_read_matching_only_withheld_pages_is_not_found(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _note(vault, "classified", "## Claim\n\nThe lighthouse vault holds the spare key.\n")
    _withhold(vault, f"{NOTES}/classified.md", ceiling=0)
    _index(vault)
    _reset_caches()

    with pytest.raises(ValueError, match="NOT_FOUND|not found") as refusal:
        _read(vault, "lighthouse vault", _external())
    with pytest.raises(ValueError) as nowhere:
        _read(vault, "a subject no page mentions", _external())
    assert str(refusal.value) == str(nowhere.value)


def test_a_walk_stops_two_hops_from_the_seed(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    for n in range(4):
        link = f"Next: [[post-{n + 1}]].\n\n" if n < 3 else ""
        _note(vault, f"post-{n}", f"{link}## Claim\n\nPost {n} is lit.\n")
    _index(vault)

    brief = _read(vault, f"{NOTES}/post-0.md")

    assert {item["path"] for item in brief["items"] if item["kind"] == "note"} == {f"{NOTES}/post-{n}.md" for n in range(3)}
    assert "related" not in brief  # a page subject runs no search


def test_a_topic_read_names_the_hits_it_did_not_walk_as_related(tmp_path, monkeypatch) -> None:
    """Two separate pairs of pages match the topic text. The better hit seeds a brief; the other pair is only related."""
    vault = _vault(tmp_path, monkeypatch)
    for pair in ("east", "west"):
        _note(vault, f"{pair}-a", f"Lantern post [[{pair}-b]].\n\n## Claim\n\nThe {pair} lantern post is lit.\n")
        _note(vault, f"{pair}-b", f"## Claim\n\nThe {pair} lantern post is the second one.\n")
    _index(vault)

    brief = _read(vault, "lantern post")

    members = {item["path"] for item in brief["items"] if item["kind"] == "note"}
    related = {item["path"] for item in brief["related"]}
    pair = brief["seed"]["path"].rsplit("/", 1)[-1].split("-")[0]
    other = "west" if pair == "east" else "east"
    assert members == {f"{NOTES}/{pair}-a.md", f"{NOTES}/{pair}-b.md"}
    assert related == {f"{NOTES}/{other}-a.md", f"{NOTES}/{other}-b.md"}  # search hits never become members
    assert all({"title", "ref", "path"} <= item.keys() for item in brief["related"])


def test_a_warm_read_reads_no_page_and_an_edit_makes_a_new_generation(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    reads: list[str] = []
    real = topic_brief._read_page
    monkeypatch.setattr(topic_brief, "_read_page", lambda root, path: reads.append(path) or real(root, path))

    cold = _read(vault, f"{NOTES}/hub.md")
    pages_read = len(reads)
    warm = _read(vault, f"{NOTES}/hub.md")

    assert pages_read >= 2 and len(reads) == pages_read  # the warm read read no page
    assert warm["generation"] == cold["generation"] and warm["items"] == cold["items"]

    _note(vault, "sister-one", "## Finding\n- id: key-answer\n\nThe deputy keeps the key now.\n", updated="2026-09-03")
    _reindex(vault)
    edited = _read(vault, f"{NOTES}/hub.md")

    assert len(reads) > pages_read and edited["generation"] != cold["generation"]
    assert "The deputy keeps the key now." in {item.get("text") for item in edited["items"]}


def test_changed_since_names_what_this_principal_has_not_read(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"

    assert _read(vault, hub)["changed_since"] == {"state": "first_read"}
    assert _read(vault, hub)["changed_since"] == {
        "state": "unchanged", "new": [], "changed": [], "removed": [], "new_units": [],
    }

    _note(
        vault, "hub",
        "See [[sister-one]] and [[sister-two]].\n\n## Claim\n- id: new-claim\n\nBerths close at nightfall.\n\n"
        "## Claim\n- id: fresh-claim\n\nThe outer quay gained a ninth berth.\n\n"
        "## Open threads\n- id: thread-two\n\nWhen does the key change hands?\n",
        updated="2026-10-01", head="summary: Everything about the quay.\n" + _cites("tide-table"),
    )
    _note(vault, "sister-two", "## Finding\n- id: lamp\n\nThe quay lamps are lit at dusk.\n", updated="2026-10-02")
    (vault / f"{NOTES}/sister-one.md").unlink()
    _reindex(vault)

    owner = _read(vault, hub)["changed_since"]

    assert owner["state"] == "changed"
    assert [item["ref"] for item in owner["new"]] == [_ref("sister-two")]
    assert [(item["ref"], item["updated"]) for item in owner["changed"]] == [(_ref("hub"), "2026-10-01")]
    assert owner["removed"] == [_ref("sister-one")]
    assert f"{_ref('hub')}#fresh-claim" in owner["new_units"] and f"{_ref('sister-two')}#lamp" in owner["new_units"]
    # A watermark belongs to its principal: a second principal still has the whole brief to read.
    assert _read(vault, hub, PARTNER)["changed_since"] == {"state": "first_read"}
    assert _read(vault, hub)["changed_since"]["state"] == "unchanged"


def test_a_cut_brief_counts_what_it_left_out(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(topic_brief, "NOTE_CAP", 3)
    links = " ".join(f"[[spoke-{n}]]" for n in range(6))
    _note(vault, "hub", f"Spokes: {links}\n\n## Claim\n\nThe hub has six spokes.\n")
    for n in range(6):
        _note(vault, f"spoke-{n}", f"## Claim\n\nSpoke {n} is a short one.\n", updated=f"2026-09-0{n + 1}")
    _index(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    kept = {item["path"] for item in brief["items"] if item["kind"] == "note"}
    assert len(kept) == 3 and f"{NOTES}/hub.md" in kept
    assert kept >= {f"{NOTES}/spoke-5.md", f"{NOTES}/spoke-4.md"}  # the most recent of the same distance stay
    (cut,) = brief["missing"]
    assert (cut["role"], cut["reason"], cut["count"]) == ("compiled_note", "cap", 4)


def test_a_hub_wider_than_the_inspection_bound_reports_a_lower_bound(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(topic_brief, "NOTE_CAP", 3)
    monkeypatch.setattr(topic_brief, "COUNT_BOUND", 2)
    links = " ".join(f"[[spoke-{n}]]" for n in range(6))
    _note(vault, "hub", f"Spokes: {links}\n\n## Claim\n\nThe hub has six spokes.\n")
    for n in range(6):
        _note(vault, f"spoke-{n}", f"## Claim\n\nSpoke {n} is a short one.\n", updated=f"2026-09-0{n + 1}")
    _index(vault)

    (cut,) = _read(vault, f"{NOTES}/hub.md")["missing"]

    # Two spokes were inspected past the cap and four were not: the count says "at least two", never "two".
    assert (cut["role"], cut["reason"], cut["count"], cut["exact"]) == ("compiled_note", "cap", 2, False)


def test_a_cut_source_list_counts_what_it_left_out(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(topic_brief, "SOURCE_CAP", 2)
    names = [f"article-{n}" for n in range(5)]
    for n, name in enumerate(names):
        _source(vault, name, captured=f"2026-08-0{n + 1}")
    _note(vault, "hub", "## Claim\n\nFive articles cover the quay.\n", head=_cites(*names))
    _index(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    assert len([item for item in brief["items"] if item["kind"] == "source"]) == 2
    (cut,) = brief["missing"]
    assert (cut["role"], cut["reason"], cut["count"]) == ("source", "cap", 3)


def test_brief_reads_and_refusals_are_counted_under_their_own_route_label(tmp_path, monkeypatch) -> None:
    from exomem import metrics

    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    metrics.reset()

    _read(vault, f"{NOTES}/hub.md")
    with pytest.raises(ValueError):
        _read(vault, "a subject no page mentions")

    counters = {
        (counter["name"], tuple(sorted(counter["labels"].items()))): counter["value"]
        for counter in metrics.snapshot()["counters"]
    }
    assert counters[("exomem_tool_calls_total", (("outcome", "success"), ("tool", "read_page:brief")))] == 1
    assert counters[("exomem_tool_calls_total", (("outcome", "failure"), ("tool", "read_page:brief")))] == 1
    assert any(
        name == "exomem_tool_failures_total" and ("tool", "read_page:brief") in labels for name, labels in counters
    )
    metrics.reset()


def test_a_brief_refuses_the_options_it_cannot_honour(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    for option in ({"unit_ref": "x"}, {"frontmatter_only": True}, {"links": True}, {"include_raw": True},
                   {"include_history": True}, {"units": ["a"]}, {"offset": 5}):
        with pytest.raises(ValueError, match="INVALID_VIEW_READ_OPTIONS"):
            _read(vault, f"{NOTES}/hub.md", **option)


def test_a_brief_pages_with_a_signed_continuation_and_refuses_it_once_the_brief_changes(tmp_path, monkeypatch) -> None:
    from test_context_views import _frozen

    _frozen(monkeypatch, page_size=3)
    vault = _vault(tmp_path, monkeypatch)
    links = " ".join(f"[[spoke-{n}]]" for n in range(4))
    _note(vault, "hub", f"Spokes: {links}\n\n## Claim\n\nThe hub has four spokes.\n")
    for n in range(4):
        _note(vault, f"spoke-{n}", f"## Claim\n\nSpoke {n} is a short one.\n")
    _index(vault)
    hub = f"{NOTES}/hub.md"

    first = _read(vault, hub)
    assert len(first["items"]) == 3 and first["continuation"]
    assert _read(vault, hub)["changed_since"]["state"] == "first_read"  # a read stopped half way is read again in full

    pages, token = [first], first["continuation"]
    while token:
        pages.append(_read(vault, first["ref"], continuation=token, brief=False))
        token = pages[-1]["continuation"]
    served = [item["ref"] for page in pages for item in page["items"]]
    assert len(served) == len(set(served)) == 10 and {page["generation"] for page in pages} == {first["generation"]}
    assert _read(vault, hub)["changed_since"]["state"] == "unchanged"  # the last page advanced the watermark

    held = _read(vault, hub)
    _note(vault, "spoke-0", "## Claim\n\nSpoke 0 is a changed one.\n")
    _reindex(vault)
    with pytest.raises(ValueError, match="CONTINUATION_STALE"):
        _read(vault, first["ref"], continuation=held["continuation"], brief=False)


def test_the_mcp_surface_serves_the_brief_the_library_serves(vault: Path, monkeypatch) -> None:
    from fastmcp import Client

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _hub_world(vault)
    mcp = _surface_server(monkeypatch)

    async def read() -> dict[str, Any]:
        async with Client(mcp) as client:
            result = await client.call_tool("read_page", {"page": f"{NOTES}/hub.md", "part": "brief"})
        return result.structured_content

    over_mcp = asyncio.run(read())
    in_process = _read(vault, f"{NOTES}/hub.md")

    drop = ("changed_since",)  # the first read advanced the watermark both calls share
    assert over_mcp["ref"].startswith("exomem://brief/") and over_mcp["items"]
    assert {k: v for k, v in over_mcp.items() if k not in drop} == {k: v for k, v in in_process.items() if k not in drop}
