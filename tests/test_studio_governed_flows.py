"""Studio proposal/confirmation contracts through the existing REST leaves."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import corpus_aware, find, server


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
    relation = candidate.get("relation_type") or "relates_to"
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
    created = _post(
        client,
        "remember",
        {
            "title": "Studio confirmed compilation",
            "note_type": "insight",
            "content": proposal["outline_markdown"],
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
            "content": "# Studio successor conclusion\n\nRevised measured claim.\n",
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
    return acorn_rel


def _acorn_item(client: TestClient, acorn_rel: str) -> tuple[dict, str]:
    queue = _post(client, "review_memory", {"mode": "relation-queue"})
    group = next(g for g in queue["groups"] if g["path"] == acorn_rel)
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
    item, content_hash = _acorn_item(client, acorn_rel)

    accepted = _post(
        client,
        "connect_memory",
        {
            "operation": "accept-relation",
            "ref": item["ref"],
            "expected_hash": content_hash,
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
        {"ref": item["ref"], "action": "dismiss", "expected_fingerprint": item["fingerprint"]},
    )

    assert dismissed["state"] == "dismissed"
    find.clear_cache()
    requeued = _post(client, "review_memory", {"mode": "relation-queue"})
    refs = {it["ref"] for g in requeued["groups"] for it in g["items"]}
    assert item["ref"] not in refs
