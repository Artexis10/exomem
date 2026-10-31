"""Studio proposal/confirmation contracts through the existing REST leaves."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import corpus_aware, epistemic_graph, find, freshness, semantic_contract, server


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "studio-key")
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    return TestClient(server.build_server(require_auth=False).http_app())


def _post(client: TestClient, command: str, body: dict) -> dict:
    request_body = dict(body)
    needs_leaf_result = command in {"edit_memory", "remember", "replace_memory"} or (
        command == "connect_memory"
        and body.get("operation") in {"accept-relation", "create-entity"}
    )
    if needs_leaf_result:
        request_body["response_detail"] = "full"
    response = client.post(
        f"/api/{command}",
        json=request_body,
        headers={"Authorization": "Bearer studio-key"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    data = payload["data"]
    return data.get("diagnostics", data)


def _write(vault: Path, rel: str, content: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_relation_proposal_never_mutates_and_confirmation_uses_audited_edit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_rel = "Knowledge Base/Notes/Insights/studio-relation-target.md"
    related_rel = "Knowledge Base/Notes/Insights/studio-related.md"
    target = _write(
        vault,
        target_rel,
        "---\ntype: insight\nstatus: active\n---\n# Studio relation target\n\n"
        "## Relations\n\n"
        "See [[Knowledge Base/Notes/Insights/studio-related]].\n",
    )
    _write(
        vault,
        related_rel,
        "---\ntype: insight\nstatus: active\n---\n# Studio related\n\nRelated fact.\n",
    )
    find.clear_cache()
    client = _client(vault, monkeypatch)
    before = target.read_bytes()

    proposal = _post(
        client,
        "connect_memory",
        {"operation": "suggest-relations", "path": target_rel, "limit": 10},
    )

    assert proposal["mutated"] is False
    assert proposal["candidates"]
    assert target.read_bytes() == before
    candidate = proposal["candidates"][0]
    current = _post(client, "read_memory", {"path": target_rel})
    relation = candidate["relation_type"]
    destination = str(candidate["to"]).removesuffix(".md")
    _post(
        client,
        "edit_memory",
        {
            "path": target_rel,
            "why": "Accepted reviewed Studio relation",
            "heading": "Relations",
            "section_position": "append",
            "new_string": f"- {relation} [[{destination}]]",
            "expected_hash": current["content_hash"],
        },
    )

    assert f"- {relation} [[{destination}]]" in target.read_text(encoding="utf-8")
    assert "Accepted reviewed Studio relation" in (
        vault / "Knowledge Base/log.md"
    ).read_text(encoding="utf-8")


def test_studio_payloads_carry_the_relations_that_fit_and_settle_once_one_is_written(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "Knowledge Base/Sources/Sessions/2026-05-05-metabolism-curriculum-design.md"
    person = "Knowledge Base/Entities/People/Ada Marsh.md"
    organization = "Knowledge Base/Entities/Organizations/Birch Works.md"
    for page, entity_type, name in (
        (person, "person", "Ada Marsh"),
        (organization, "organization", "Birch Works"),
    ):
        _write(
            vault,
            page,
            f"---\ntype: entity\nentity_type: {entity_type}\nstatus: active\n"
            f"sources:\n  - {source}\n---\n# {name}\n\n## Summary\n\n"
            f"{name} is an invented entity for the Studio check.\n\n## Relations\n\n",
        )
    find.clear_cache()
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    client = _client(vault, monkeypatch)

    # The suggest dialog offers one choice per fitting relation, shown by label and written by key.
    proposal = _post(
        client, "connect_memory", {"operation": "suggest-relations", "path": person, "limit": 10}
    )
    candidate = next(c for c in proposal["candidates"] if c["to"] == organization)
    assert "owns" in [option["key"] for option in candidate["options"]]
    assert all(option["label"] for option in candidate["options"])

    # The queue item offers the same relations and authors no bullet of its own.
    queue = _post(client, "review_memory", {"mode": "relation-queue"})
    group = next(g for g in queue["groups"] if g["path"] == person)
    item = next(i for i in group["items"] if i["to"] == organization)
    assert [option["key"] for option in item["options"]] == [
        option["key"] for option in candidate["options"]
    ]
    assert item["bullet"] is None

    # Accepting it as generic is refused with the code and remediation Studio shows.
    refused = client.post(
        "/api/connect_memory",
        json={
            "operation": "accept-relation",
            "ref": item["ref"],
            "path": group["source_path"],
            "expected_hash": group["source_content_hash"],
            "why": "Accepted reviewed relation from the Studio queue",
            "expected_fingerprint": item["fingerprint"],
        },
        headers={"Authorization": "Bearer studio-key"},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "RELATION_CHOICE_REQUIRED"
    assert refused.json()["error"]["remediation"]

    # The same two pages are also proposed the other way round, in the queue and in a suggestion.
    generic = item["relation_type"]
    back_group = next(g for g in queue["groups"] if g["path"] == organization)
    assert any(i["to"] == person and i["relation_type"] == generic for i in back_group["items"])

    def suggested_generic(page: str, other: str) -> list[dict]:
        found = _post(
            client, "connect_memory", {"operation": "suggest-relations", "path": page, "limit": 10}
        )
        return [c for c in found["candidates"] if c["to"] == other and c["relation_type"] == generic]

    assert suggested_generic(person, organization)
    assert suggested_generic(organization, person)

    # Writing the chosen key through the audited edit lands one bullet.
    current = _post(client, "read_memory", {"path": person})
    destination = organization.removesuffix(".md")
    _post(
        client,
        "edit_memory",
        {
            "path": person,
            "why": "Accepted reviewed Studio relation",
            "heading": "Relations",
            "section_position": "append",
            "new_string": f"- owns [[{destination}]]",
            "expected_hash": current["content_hash"],
        },
    )
    assert (vault / person).read_text(encoding="utf-8").count(f"- owns [[{destination}]]") == 1

    # The specific edge settles the generic proposal between the two pages, whichever way it runs.
    # A proposal of another relation (derived_from) is not settled by it.
    queue = _post(client, "review_memory", {"mode": "relation-queue"})
    listed = {
        (g["path"], i["to"], i["relation_type"]) for g in queue["groups"] for i in g["items"]
    }
    assert (person, organization, generic) not in listed
    assert (organization, person, generic) not in listed
    assert (person, source, "derived_from") in listed
    assert not suggested_generic(person, organization)
    assert not suggested_generic(organization, person)


def test_compilation_proposal_preserves_source_until_confirmed_remember(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_rel = "Knowledge Base/Sources/Articles/studio-source.md"
    source = _write(
        vault,
        source_rel,
        "---\ntype: source\nstatus: unprocessed\ningested_into: []\n---\n"
        "# Studio source\n\nRecorded source material.\n",
    )
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *args, **kwargs: [])
    find.clear_cache()
    client = _client(vault, monkeypatch)
    before = source.read_bytes()

    proposal = _post(client, "compile_source", {"sources": [source_rel]})

    assert proposal["outline_markdown"]
    assert source.read_bytes() == before
    authored = proposal["outline_markdown"].replace(
        "## Claim\n\n",
        "## Claim\n\nRecorded source material supports the compiled conclusion.\n\n",
        1,
    )
    created = _post(
        client,
        "remember",
        {
            "title": "Studio confirmed compilation",
            "note_type": "insight",
            "content": authored,
            "sources": [source_rel],
            "status": "draft",
            "suggestions": False,
        },
    )

    assert (vault / created["path"]).is_file()
    assert "Studio confirmed compilation" in (
        vault / "Knowledge Base/log.md"
    ).read_text(encoding="utf-8")


def test_supersession_confirmation_records_successor_reason_and_pointer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = "Knowledge Base/Notes/Insights/studio-old-conclusion.md"
    old = _write(
        vault,
        old_rel,
        "---\ntype: insight\nstatus: active\n---\n# Studio old conclusion\n\nOld claim.\n",
    )
    find.clear_cache()
    client = _client(vault, monkeypatch)

    result = _post(
        client,
        "replace_memory",
        {
            "old_path": old_rel,
            "title": "Studio successor conclusion",
            "note_type": "insight",
            "content": (
                "# Studio successor conclusion\n\n"
                "## Observations\n\n"
                "- [revised conclusion] The recorded evidence changes the earlier "
                "conclusion. #supersession\n"
            ),
            "reason": "New recorded evidence changed the conclusion",
        },
    )

    old_text = old.read_text(encoding="utf-8")
    assert "status: superseded" in old_text
    assert "superseded_by:" in old_text
    assert (vault / result["new_path"]).is_file()
    log = (vault / "Knowledge Base/log.md").read_text(encoding="utf-8")
    assert "New recorded evidence changed the conclusion" in log


def _seed_relation_queue(vault: Path) -> str:
    acorn_rel = "Knowledge Base/Notes/Insights/studio-queue-acorn.md"
    _write(
        vault,
        acorn_rel,
        "---\ntype: insight\nstatus: active\n---\n# Studio queue acorn\n\n"
        "## Relations\n\n"
        "See [[Knowledge Base/Notes/Insights/studio-queue-birch]].\n",
    )
    _write(
        vault,
        "Knowledge Base/Notes/Insights/studio-queue-birch.md",
        "---\ntype: insight\nstatus: active\n---\n# Studio queue birch\n\nA fact.\n",
    )
    find.clear_cache()
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return acorn_rel


def _acorn_item(client: TestClient, acorn_rel: str) -> tuple[dict, str]:
    queue = _post(client, "review_memory", {"mode": "relation-queue"})
    group = next(g for g in queue["groups"] if g["path"] == acorn_rel)
    assert group["source_path"] == acorn_rel
    assert group["source_content_hash"] == group["content_hash"]
    assert group["items"][0]["source_path"] == acorn_rel
    assert group["items"][0]["source_content_hash"] == group["content_hash"]
    return group["items"][0], group["content_hash"]


def test_relation_queue_panel_render_is_read_only(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acorn_rel = _seed_relation_queue(vault)
    before = (vault / acorn_rel).read_bytes()
    client = _client(vault, monkeypatch)

    queue = _post(client, "review_memory", {"mode": "relation-queue"})

    assert queue["mode"] == "relation-queue"
    assert queue["mutated"] is False
    item, _hash = _acorn_item(client, acorn_rel)
    assert item["to"].endswith("studio-queue-birch.md")
    assert (vault / acorn_rel).read_bytes() == before


def test_relation_queue_accept_round_trip_writes_canonical_bullet(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acorn_rel = _seed_relation_queue(vault)
    client = _client(vault, monkeypatch)
    item, _content_hash = _acorn_item(client, acorn_rel)

    accepted = _post(
        client,
        "connect_memory",
        {
            "operation": "accept-relation",
            "ref": item["ref"],
            "path": item["source_path"],
            "expected_hash": item["source_content_hash"],
            "why": "Accepted reviewed relation from the Studio queue",
            "expected_fingerprint": item["fingerprint"],
        },
    )

    assert accepted["accepted"] is True
    page = (vault / acorn_rel).read_text(encoding="utf-8")
    assert "- links_to [[Knowledge Base/Notes/Insights/studio-queue-birch]]" in page
    assert "Accepted reviewed relation from the Studio queue" in (
        vault / "Knowledge Base/log.md"
    ).read_text(encoding="utf-8")
    # Accepted candidate leaves the queue on re-read.
    find.clear_cache()
    requeued = _post(client, "review_memory", {"mode": "relation-queue"})
    refs = {it["ref"] for g in requeued["groups"] for it in g["items"]}
    assert item["ref"] not in refs


def test_relation_queue_accept_refuses_on_target_drift(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acorn_rel = _seed_relation_queue(vault)
    client = _client(vault, monkeypatch)
    item, _hash = _acorn_item(client, acorn_rel)
    before = (vault / acorn_rel).read_bytes()

    response = client.post(
        "/api/connect_memory",
        json={
            "operation": "accept-relation",
            "ref": item["ref"],
            "path": item["source_path"],
            "expected_hash": "0" * 64,
            "why": "Accepted reviewed relation from the Studio queue",
            "expected_fingerprint": item["fingerprint"],
        },
        headers={"Authorization": "Bearer studio-key"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "STALE_EDIT"
    assert (vault / acorn_rel).read_bytes() == before


def test_relation_queue_triage_round_trip_dismisses_candidate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acorn_rel = _seed_relation_queue(vault)
    client = _client(vault, monkeypatch)
    item, _hash = _acorn_item(client, acorn_rel)

    dismissed = _post(
        client,
        "triage_memory",
        {
            "ref": item["ref"],
            "source_path": item["source_path"],
            "action": "dismiss",
            "why": "Dismissed reviewed relation from the Studio queue",
            "expected_fingerprint": item["fingerprint"],
        },
    )

    assert dismissed["state"] == "dismissed"
    find.clear_cache()
    requeued = _post(client, "review_memory", {"mode": "relation-queue"})
    refs = {it["ref"] for g in requeued["groups"] for it in g["items"]}
    assert item["ref"] not in refs
