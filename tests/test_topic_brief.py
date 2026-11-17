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
from test_activation_related_pages import AXIS, FINGERPRINT, _toward_turn
from test_connector_boundary import configured_boundary as configured_boundary
from test_governance_egress import _external, _reset_caches, write_rule
from test_graph_deferred_queue import _seed_live_freshness
from test_private_vocabulary_instances import private_instances as private_instances
from test_working_set_withheld_twins import _withhold

from exomem import (
    commands,
    embeddings,
    epistemic_graph,
    index_sync,
    lexstore,
    readiness,
    recall_space,
    topic_brief,
)
from exomem import find as find_module
from exomem.embedding_index import EmbeddingIndex
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


def _survey_world(vault: Path) -> list[str]:
    """A lagoon note linking one survey note that links eight spokes: the spokes are the lagoon's second hop.

    The newest spokes are the furthest from the lagoon in meaning, so similarity and recency keep different ones.
    """
    _note(vault, "lagoon", "Surveyed in [[survey]].\n\n## Claim\n\nThe lagoon silts up each spring.\n")
    _note(vault, "survey", " ".join(f"[[spoke-{n}]]" for n in range(8)) + "\n\n## Claim\n\nThe survey covers eight sites.\n")
    for n in range(8):
        _note(vault, f"spoke-{n}", f"## Claim\n\nSite {n} was measured.\n", updated=f"2026-09-0{n + 1}")
    _index(vault)
    return [f"{NOTES}/spoke-{n}.md" for n in range(8)]


def _plant_vectors(vault: Path, monkeypatch: pytest.MonkeyPatch, vectors: dict[str, Any]) -> None:
    """One resident recall chunk per page: the sidecar and the matrix are real, the encoder is out of the loop."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(recall_space, "resident_fingerprint", lambda model: FINGERPRINT)
    recall = embeddings.get_embedding_index(vault)
    for rel, vector in vectors.items():
        recall.upsert_file(rel, [f"chunk of {rel}"], vector[None, :], 1.0)
    recall.all_vectors()


def test_a_second_hop_keeps_the_notes_nearest_the_seed_and_counts_the_rest(tmp_path, monkeypatch) -> None:
    """A broad member links many notes; only the second-hop notes nearest the seed join, whatever their age."""
    vault = _vault(tmp_path, monkeypatch)
    spokes = _survey_world(vault)
    _plant_vectors(vault, monkeypatch, {
        f"{NOTES}/lagoon.md": AXIS, f"{NOTES}/survey.md": _toward_turn(0.5),
        **{spoke: _toward_turn(0.9 - 0.1 * n) for n, spoke in enumerate(spokes)},
    })

    brief = _read(vault, f"{NOTES}/lagoon.md")

    notes = [item["path"] for item in brief["items"] if item["kind"] == "note"]
    assert notes == [f"{NOTES}/lagoon.md", f"{NOTES}/survey.md", *spokes[:topic_brief.SECOND_HOP_CAP]]  # nearest first
    assert brief["ranked_by"] == "similarity"
    (cut,) = brief["missing"]
    assert (cut["role"], cut["reason"], cut["count"]) == ("compiled_note", "cap", 8 - topic_brief.SECOND_HOP_CAP)


def test_a_note_embedded_after_the_last_read_takes_its_place_on_the_next_read(tmp_path, monkeypatch) -> None:
    """A write is embedded after the graph has it; the owner's kept walk must not keep the order from before."""
    vault = _vault(tmp_path, monkeypatch)
    spokes = _survey_world(vault)
    _plant_vectors(vault, monkeypatch, {
        f"{NOTES}/lagoon.md": AXIS, **{spoke: _toward_turn(0.9 - 0.1 * n) for n, spoke in enumerate(spokes[:-1])},
    })
    assert spokes[-1] not in {item["path"] for item in _read(vault, f"{NOTES}/lagoon.md")["items"]}

    _plant_vectors(vault, monkeypatch, {spokes[-1]: _toward_turn(0.95)})

    notes = [item["path"] for item in _read(vault, f"{NOTES}/lagoon.md")["items"] if item["kind"] == "note"]
    assert notes[2] == spokes[-1]  # now the nearest second-hop note


def test_a_kept_walk_follows_the_matrix_that_ranked_it(tmp_path, monkeypatch) -> None:
    """The owner's kept walk is keyed by the matrix that ranked it: a lag on one page, then on another, then none."""
    vault = _vault(tmp_path, monkeypatch)
    spokes = _survey_world(vault)
    newest = spokes[-1]
    _plant_vectors(vault, monkeypatch, {
        f"{NOTES}/lagoon.md": AXIS, newest: _toward_turn(0.95),
        **{spoke: _toward_turn(0.9 - 0.1 * n) for n, spoke in enumerate(spokes[:-1])},
    })
    current = EmbeddingIndex.require_resident_matrix

    def lagging(*stale: str) -> None:
        """A matrix that lags the sidecar on these pages' rows, as a service serves one until it catches up."""
        monkeypatch.setattr(EmbeddingIndex, "require_resident_matrix", lambda self: current(self)._replace(stale_paths=frozenset(stale)))

    def second_hop() -> tuple[str, list[str]]:
        brief = _read(vault, f"{NOTES}/lagoon.md")
        return brief["ranked_by"], [item["path"] for item in brief["items"] if item["kind"] == "note"][2:]

    lagging(newest)
    assert second_hop() == ("similarity", spokes[:5])  # the newest spoke's rows changed since this matrix
    lagging(spokes[0])
    assert second_hop() == ("similarity", [newest, *spokes[1:5]])

    def cold(self: EmbeddingIndex) -> None:
        raise recall_space.CorpusMatrixCold("loading")

    monkeypatch.setattr(EmbeddingIndex, "require_resident_matrix", cold)
    assert second_hop() == ("recency", spokes[3:][::-1])


def test_on_a_profile_with_no_resident_matrix_a_warm_read_reads_no_edge(tmp_path, monkeypatch) -> None:
    """A profile that keeps no recall matrix ranks by recency on every read, and still keeps the owner's walk."""
    vault = _vault(tmp_path, monkeypatch)
    spokes = _survey_world(vault)
    _plant_vectors(vault, monkeypatch, {f"{NOTES}/lagoon.md": AXIS, **{spoke: _toward_turn(0.5) for spoke in spokes}})
    monkeypatch.setattr(EmbeddingIndex, "resident_matrix", lambda self: None)
    monkeypatch.setattr(EmbeddingIndex, "require_resident_matrix", lambda self: None)
    edge_reads: list[int] = []
    real = epistemic_graph.GraphView.neighbor_edges
    monkeypatch.setattr(
        epistemic_graph.GraphView, "neighbor_edges", lambda *args, **kwargs: edge_reads.append(1) or real(*args, **kwargs)
    )

    cold = _read(vault, f"{NOTES}/lagoon.md")
    walked = len(edge_reads)
    warm = _read(vault, f"{NOTES}/lagoon.md")

    assert walked > 0 and len(edge_reads) == walked  # the warm read read no edge
    assert cold["ranked_by"] == warm["ranked_by"] == "recency"


@pytest.mark.parametrize("vectors", [True, False], ids=["vectors", "no-vectors"])
def test_a_seed_whose_only_neighbour_is_withheld_reads_as_one_with_none(tmp_path, monkeypatch, vectors) -> None:
    """Whether nearness ranks a restricted caller's walk is decided without the pages it may not see."""

    def world(name: str, *, with_withheld: bool) -> str:
        vault = _vault(tmp_path, monkeypatch, name)
        _note(vault, "hub", "Berths: [[classified]].\n\n## Claim\n- id: hub-claim\n\nThe quay has eight berths.\n")
        if with_withheld:
            _note(vault, "classified", "## Claim\n- id: secret-claim\n\nThe quay safe holds the spare key.\n")
        _withhold(vault, f"{NOTES}/classified.md", ceiling=0)
        _index(vault)
        if vectors:
            planted = {f"{NOTES}/hub.md": AXIS}
            if with_withheld:
                planted[f"{NOTES}/classified.md"] = _toward_turn(0.9)
            _plant_vectors(vault, monkeypatch, planted)
        _reset_caches()
        return json.dumps(_read(vault, f"{NOTES}/hub.md", _external()), sort_keys=True)

    served = world("with", with_withheld=True)
    assert served == world("without", with_withheld=False)
    assert json.loads(served)["ranked_by"] == ("similarity" if vectors else "recency")


def test_with_no_vector_reading_the_second_hop_keeps_the_newest_and_says_so(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    spokes = _survey_world(vault)

    brief = _read(vault, f"{NOTES}/lagoon.md")

    kept = {item["path"] for item in brief["items"] if item["kind"] == "note"} - {f"{NOTES}/lagoon.md", f"{NOTES}/survey.md"}
    assert kept == set(spokes[-topic_brief.SECOND_HOP_CAP:])
    assert brief["ranked_by"] == "recency"


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


def test_a_warm_read_of_an_unchanged_graph_reads_no_edge(tmp_path, monkeypatch) -> None:
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    edge_reads: list[int] = []
    real = epistemic_graph.GraphView.neighbor_edges
    monkeypatch.setattr(
        epistemic_graph.GraphView, "neighbor_edges", lambda *args, **kwargs: edge_reads.append(1) or real(*args, **kwargs)
    )

    cold = _read(vault, f"{NOTES}/hub.md")
    walked = len(edge_reads)
    warm = _read(vault, f"{NOTES}/hub.md")

    assert walked > 0 and len(edge_reads) == walked  # the warm read read no edge
    assert warm["items"] == cold["items"] and warm["missing"] == cold["missing"]


def test_an_edit_through_the_write_path_moves_the_graph_token_and_the_next_read_shows_it(tmp_path, monkeypatch) -> None:
    """The memo is keyed on the graph's token: a governed write drained incrementally, not rebuilt, must move it."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    _seed_live_freshness(vault)
    hub, sister = f"{NOTES}/hub.md", f"{NOTES}/sister-one.md"
    before = _read(vault, hub)
    assert "The harbour master keeps the key." in {item.get("text") for item in before["items"]}
    token = epistemic_graph.cache_token(vault)

    page = commands.op_read_memory(vault, path=sister)
    operation = {
        "kind": "replace_string", "old_string": "The harbour master keeps the key.",
        "new_string": "The deputy keeps the key.", "expected_hash": page["content_hash"],
    }
    preview = commands.op_edit_memory(vault, path=sister, why="The key changed hands.", operation=operation, validate_only=True)
    commands.op_edit_memory(
        vault, path=sister, why="The key changed hands.",
        operation={**operation, "transition_token": preview["semantic"]["transition_token"]},
    )
    _seed_live_freshness(vault)  # the watcher's part: the registry now holds the edited bytes
    index_sync.drain_deferred_work(vault)

    moved = epistemic_graph.cache_token(vault)
    texts = {item.get("text") for item in _read(vault, hub)["items"]}
    assert moved is not None and token is not None
    assert moved[3] == token[3] and int(moved[2]) > int(token[2])  # one file, written in place: the drain, not a rebuild
    assert "The deputy keeps the key." in texts and "The harbour master keeps the key." not in texts


def test_an_access_policy_that_withholds_a_member_is_not_served_from_the_memo(tmp_path, monkeypatch) -> None:
    """The owner's memo is keyed on the recall policy and the graph snapshot: a withheld page never rides a kept walk."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"
    assert "The harbour master keeps the key." in {item.get("text") for item in _read(vault, hub)["items"]}

    (vault / "Knowledge Base" / "_access.yaml").write_text("excluded:\n  - Notes/Insights/sister-one.md\n", encoding="utf-8")
    # The graph still holds the page and has not republished under the new policy: the read abstains rather than serve it.
    unpublished = _read(vault, hub)
    assert unpublished["state"] == "index_warming" and unpublished["items"] == []

    _reindex(vault)
    republished = _read(vault, hub)
    assert republished["state"] == "ready" and "harbour master" not in json.dumps(republished)
    assert "Berths close at nightfall." in {item.get("text") for item in republished["items"]}


def test_a_restricted_caller_walks_afresh_after_a_policy_withholds_a_member(tmp_path, monkeypatch) -> None:
    """A restricted caller's release decision is not in the graph token, so its walk is never kept."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"
    assert "The harbour master keeps the key." in {item.get("text") for item in _read(vault, hub, _external())["items"]}

    _withhold(vault, f"{NOTES}/sister-one.md", ceiling=0)
    _reset_caches()
    after = _read(vault, hub, _external())

    assert after["state"] == "ready" and "harbour master" not in json.dumps(after)
    assert "Berths close at nightfall." in {item.get("text") for item in after["items"]}


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


def test_a_purpose_that_withholds_a_member_leaves_the_watermark_of_a_read_without_one(tmp_path, monkeypatch) -> None:
    """One audience reads under two views; neither read moves what the other last saw."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"
    _withhold(vault, f"{NOTES}/sister-one.md", ceiling=0)
    write_rule(vault, ceiling=0, extra="purpose: marketing\npurpose_condition: matches\n")
    _reset_caches()
    plain, narrowed = _external(), _external("marketing")
    unchanged = {"state": "unchanged", "new": [], "changed": [], "removed": [], "new_units": []}

    assert "harbour master" in json.dumps(_read(vault, hub, plain))
    without_member = _read(vault, hub, narrowed)
    assert "harbour master" not in json.dumps(without_member)  # the control: the purpose withholds the member
    assert without_member["changed_since"] == {"state": "first_read"}
    assert _read(vault, hub, plain)["changed_since"] == unchanged
    assert _read(vault, hub, narrowed)["changed_since"] == unchanged


def test_two_clients_of_one_owner_with_different_ceilings_keep_separate_watermarks(
    vault: Path, configured_boundary, monkeypatch
) -> None:
    """A client that reads less than another is not told that the other's pages left the brief."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _, authenticate = configured_boundary
    limited, full = authenticate("limited"), authenticate("full")
    _note(vault, "public-hub", "See [[private-sister]].\n\n## Claim\n- id: hub-claim\n\nThe hub is open.\n", head="project: public-project\n")
    _note(vault, "private-sister", "## Claim\n- id: secret-claim\n\nThe sister is private.\n", head="project: private-project\n")
    _index(vault)
    hub = f"{NOTES}/public-hub.md"
    unchanged = {"state": "unchanged", "new": [], "changed": [], "removed": [], "new_units": []}

    assert "The sister is private." in json.dumps(_read(vault, hub, full))  # the control: only the limited client lacks it
    cut = _read(vault, hub, limited)
    assert "The sister is private." not in json.dumps(cut) and cut["changed_since"] == {"state": "first_read"}
    assert _read(vault, hub, full)["changed_since"] == unchanged
    assert _read(vault, hub, limited)["changed_since"] == unchanged


def test_a_brief_the_terminal_guard_withholds_does_not_advance_the_watermark(tmp_path, monkeypatch) -> None:
    """The watermark moves when the brief is served, not when it is compiled."""
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"

    with principal.request_scope(_external()), principal.invocation_scope():
        compiled = commands.op_read_memory(vault, path=hub, brief=True)
        _withhold(vault, f"{NOTES}/sister-one.md", ceiling=0)
        _reset_caches()
        guarded = egress.postfilter("read_memory", compiled, vault, request_kwargs={"path": hub, "brief": True})
    assert guarded["state"] == "unavailable"  # the control: the guard served nothing

    assert _read(vault, hub, _external())["changed_since"] == {"state": "first_read"}


def test_a_watermark_cut_at_its_unit_bound_says_so_instead_of_listing_the_rest_as_new(tmp_path, monkeypatch) -> None:
    from exomem import brief_watermarks

    monkeypatch.setattr(brief_watermarks, "MAX_UNITS", 2)
    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    hub = f"{NOTES}/hub.md"

    first = _read(vault, hub)
    assert len([item for item in first["items"] if item["ref"] != _ref("hub") and "#" in item["ref"]]) > 2  # the control: more units than the row keeps

    assert _read(vault, hub)["changed_since"] == {
        "state": "unchanged", "new": [], "changed": [], "removed": [], "new_units": [], "new_units_exact": False,
    }


def test_a_private_registry_change_with_no_page_edit_ends_the_cached_brief(
    vault: Path, private_instances, monkeypatch
) -> None:
    """A page's own instance can reinterpret its units, so the cache key names the instances' registries too."""
    from exomem.vocabulary import instances, registry_spec

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    full = private_instances("full")
    _note(
        vault, "private-interpretation", "## Container\n\n### Protocol\n- id: local-protocol\n\nPrivate procedure with a stable address.\n",
        head="project: private-project\n",
    )
    _index(vault)
    path = f"{NOTES}/private-interpretation.md"
    with principal.request_scope(full):
        overlay = instances.select(vault, registry_spec("categories"), "01ARZ3NDEKTSV4RRFFQ69G5FAV").overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        before = _read(vault, path, full)
        assert "Private procedure with a stable address." not in json.dumps(before)  # the control: no assertion trait yet

        overlay.write_text(
            "schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n    traits: [assertion]\n"
        )
        after = _read(vault, path, full)

    assert "Private procedure with a stable address." in {item.get("text") for item in after["items"]}


def test_a_unit_dated_to_the_last_day_of_the_calendar_still_serves_its_brief(tmp_path, monkeypatch) -> None:
    """`9999-12-31` has no following day to expire on; the brief serves the unit as current, with no error."""
    vault = _vault(tmp_path, monkeypatch)
    _note(
        vault, "hub",
        "## Claim\n- id: forever\n- until: 9999-12-31\n\nThe quay has a harbour master.\n\n"
        "## Claim\n- id: recheck\n- check_by: 9999-12-31\n\nThe quay lights are on a timer.\n",
    )
    _index(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    assert {"The quay has a harbour master.", "The quay lights are on a timer."} <= {item.get("text") for item in brief["items"]}


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


def test_a_hub_wider_than_a_step_reads_its_share_of_rows_and_says_the_count_is_a_lower_bound(tmp_path, monkeypatch) -> None:
    """A compiled note that links thousands of pages costs a step its share of edge rows, not its degree."""
    vault = _vault(tmp_path, monkeypatch)
    # `raising=False` lets this test run against a build without the bound: there it fails on the assertions below.
    monkeypatch.setattr(topic_brief, "STEP_ROWS", 4, raising=False)
    monkeypatch.setattr(topic_brief, "PAGE_ROWS_FLOOR", 1, raising=False)
    links = " ".join(f"[[spoke-{n}]]" for n in range(12))
    _note(vault, "hub", f"Spokes: {links}\n\n## Claim\n\nThe hub has twelve spokes.\n")
    for n in range(12):
        _note(vault, f"spoke-{n}", f"## Claim\n\nSpoke {n} is a short one.\n", updated=f"2026-09-{n + 1:02d}")
    _index(vault)

    brief = _read(vault, f"{NOTES}/hub.md")

    spokes = [item for item in brief["items"] if item["kind"] == "note" and item["path"] != f"{NOTES}/hub.md"]
    assert 0 < len(spokes) <= topic_brief.STEP_ROWS
    (cut,) = [entry for entry in brief["missing"] if entry["role"] == "compiled_note"]
    assert (cut["reason"], cut["exact"]) == ("cap", False)


def test_a_withheld_spoke_leaves_the_row_bound_where_a_vault_without_it_would(tmp_path, monkeypatch) -> None:
    """Rows that end on a withheld page are decided before they count, so they cannot crowd the visible spokes out."""
    monkeypatch.setattr(topic_brief, "STEP_ROWS", 6, raising=False)
    monkeypatch.setattr(topic_brief, "PAGE_ROWS_FLOOR", 1, raising=False)

    def world(name: str, *, with_withheld: bool) -> Path:
        vault = _vault(tmp_path, monkeypatch, name)
        links = " ".join(f"[[spoke-{n}]] [[classified-{n}]]" for n in range(5))
        _note(vault, "hub", f"Spokes: {links}\n")
        for n in range(5):
            _note(vault, f"spoke-{n}", f"Spoke {n} is a short one.\n", updated=f"2026-09-{n + 1:02d}")
            if with_withheld:
                _note(vault, f"classified-{n}", f"Classified {n} is a hidden one.\n")
        _withhold(vault, *[f"{NOTES}/classified-{n}.md" for n in range(5)], ceiling=0)
        _index(vault)
        _reset_caches()
        return vault

    with_pages, without_pages = world("with", with_withheld=True), world("without", with_withheld=False)

    served = _read(with_pages, f"{NOTES}/hub.md", _external())

    assert served == _read(without_pages, f"{NOTES}/hub.md", _external())
    assert len([item for item in served["items"] if item["kind"] == "note"]) == 6  # the hub and its five visible spokes
    assert served["missing"] == []


def test_a_hub_that_cites_many_sources_still_serves_every_sister_note(tmp_path, monkeypatch) -> None:
    """A step's row bound counts rows by what they can yield, so many cited Sources cannot crowd a linked note out of the walk."""
    monkeypatch.setattr(topic_brief, "STEP_ROWS", 16, raising=False)
    monkeypatch.setattr(topic_brief, "PAGE_ROWS_FLOOR", 16, raising=False)
    sisters = [f"sister-{n}" for n in range(8)]
    articles = [f"article-{n:02d}" for n in range(40)]
    vault = _vault(tmp_path, monkeypatch)
    for name in articles:
        _source(vault, name)
    for name in sisters:
        _note(vault, name, f"## Claim\n\n{name} is a short one.\n")
    _note(vault, "hub", f"Sisters: {' '.join(f'[[{name}]]' for name in sisters)}\n\n## Claim\n\nThe hub has eight sisters.\n", head=_cites(*articles))
    _withhold(vault, _note(vault, "classified", "Classified.\n"), ceiling=0)
    _index(vault)
    _reset_caches()

    for caller in (None, _external()):
        brief = _read(vault, f"{NOTES}/hub.md", caller)

        served = {item["path"] for item in brief["items"] if item["kind"] == "note"}
        assert served == {f"{NOTES}/{name}.md" for name in ["hub", *sisters]}
        (cut,) = brief["missing"]  # the Sources were cut at the bound; the notes were not
        # The rows past the bound were never read, so how many Sources they hold is unknown: the count says so, never 0.
        assert cut == {"role": "source", "reason": "cap", "count": None, "exact": False}


def test_a_long_hub_still_serves_every_sister_note(tmp_path, monkeypatch) -> None:
    """Rows that stay on the hub, such as each unit's own block, cannot yield a member, so they cannot crowd a linked note out."""
    monkeypatch.setattr(topic_brief, "STEP_ROWS", 16, raising=False)
    monkeypatch.setattr(topic_brief, "PAGE_ROWS_FLOOR", 16, raising=False)
    sisters = [f"sister-{n}" for n in range(8)]
    vault = _vault(tmp_path, monkeypatch)
    for name in sisters:
        _note(vault, name, f"## Claim\n\n{name} is a short one.\n")
    claims = "".join(f"## Claim\n\nThe hub's point {n} holds.\n\n" for n in range(20))  # more units than the share
    _note(vault, "hub", f"Sisters: {' '.join(f'[[{name}]]' for name in sisters)}\n\n{claims}")
    _withhold(vault, _note(vault, "classified", "Classified.\n"), ceiling=0)
    _index(vault)
    _reset_caches()

    for caller in (None, _external()):
        brief = _read(vault, f"{NOTES}/hub.md", caller)

        served = {item["path"] for item in brief["items"] if item["kind"] == "note"}
        assert served == {f"{NOTES}/{name}.md" for name in ["hub", *sisters]}


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


def test_brief_reads_and_refusals_are_counted_in_series_of_their_own(tmp_path, monkeypatch) -> None:
    """A brief read is also a `read_page` call, so its series must stay off `exomem_tool_*`, where a sum would count it twice."""
    from exomem import metrics

    vault = _vault(tmp_path, monkeypatch)
    _hub_world(vault)
    metrics.reset()

    _read(vault, f"{NOTES}/hub.md")
    with pytest.raises(ValueError):
        _read(vault, "a subject no page mentions")

    snapshot = metrics.snapshot()
    counters = {
        (counter["name"], tuple(sorted(counter["labels"].items()))): counter["value"] for counter in snapshot["counters"]
    }
    assert counters[(topic_brief.READS, (("outcome", "success"),))] == 1
    assert counters[(topic_brief.READS, (("outcome", "failure"),))] == 1
    assert any(name == topic_brief.FAILURES for name, _labels in counters)
    assert any(histogram["name"] == topic_brief.DURATION for histogram in snapshot["histograms"])
    assert not any(name.startswith("exomem_tool_") for name, _labels in counters)
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
