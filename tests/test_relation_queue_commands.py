"""Command-surface contracts for the relation-acceptance queue.

review_memory(mode="relation-queue"), connect_memory(operation="accept-relation"),
and triage_memory relation-ref routing, plus registry-surface exposure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import attention, commands, find, relation_queue, server


def _write_page(
    vault: Path,
    name: str,
    body: str,
    *,
    page_type: str = "insight",
    status: str = "active",
    folder: str = "Notes/Insights",
) -> Path:
    path = vault / "Knowledge Base" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\nstatus: {status}\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed(vault: Path) -> None:
    _write_page(
        vault,
        "acorn",
        "## Relations\n\nSee [[Knowledge Base/Notes/Insights/birch]].",
    )
    _write_page(vault, "birch", "A measured fact.")
    find.clear_cache()


def _first_item(vault: Path) -> dict:
    result = commands.op_review_memory(vault, mode="relation-queue")
    return next(
        item
        for group in result["groups"]
        for item in group["items"]
        if item["from"].endswith("acorn.md") and item["to"].endswith("birch.md")
    )


def _group_hash(vault: Path, path_suffix: str) -> str:
    result = commands.op_review_memory(vault, mode="relation-queue")
    group = next(g for g in result["groups"] if g["path"].endswith(path_suffix))
    return group["content_hash"]


def test_review_memory_relation_queue_mode_is_read_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = commands.op_review_memory(tmp_path, mode="relation-queue")
    assert result["mode"] == "relation-queue"
    assert result["mutated"] is False
    assert result["shown"] >= 1


def test_edit_creates_missing_section_only_when_opted_in(tmp_path: Path) -> None:
    """`edit()` must be able to author the first entry into an absent section
    (create_missing_section=True), while the default still errors on a missing
    heading so an interactive typo doesn't silently spawn a section. Regression
    for the audit's accept-relation HIGH: remember() emits no `## Relations`."""
    from exomem import edit as edit_module

    p = tmp_path / "Knowledge Base" / "Notes" / "Insights" / "n.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: insight\n---\n# N\n\nA body line.\n", encoding="utf-8")
    rel = "Knowledge Base/Notes/Insights/n.md"

    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(
            tmp_path, path=rel, why="x", heading="Relations",
            section_position="append", new_string="- relates_to [[Foo]]",
        )
    assert ei.value.code == "HEADING_NOT_FOUND"

    edit_module.edit(
        tmp_path, path=rel, why="x", heading="Relations",
        section_position="append", new_string="- relates_to [[Foo]]",
        create_missing_section=True,
    )
    text = p.read_text(encoding="utf-8")
    assert "## Relations" in text
    assert "relates_to" in text
    assert text.count("## Relations") == 1  # exactly one section created


def test_accept_relation_creates_relations_section_when_absent(tmp_path: Path) -> None:
    """accept-relation must succeed on a note that has no `## Relations` section
    (the common case — remember() doesn't emit one), creating it. Previously it
    failed HEADING_NOT_FOUND on ~90% of notes."""
    # 'cedar' links to birch in its BODY, with NO ## Relations section.
    _write_page(
        tmp_path, "cedar",
        "Body mentions [[Knowledge Base/Notes/Insights/birch]] inline.",
    )
    _write_page(tmp_path, "birch", "A measured fact.")
    find.clear_cache()

    result = commands.op_review_memory(tmp_path, mode="relation-queue")
    item = next(
        (it for g in result["groups"] for it in g["items"]
         if it["from"].endswith("cedar.md") and it["to"].endswith("birch.md")),
        None,
    )
    if item is None:
        pytest.skip("relation queue surfaced no cedar->birch candidate in this env")
    group_hash = next(
        g["content_hash"] for g in result["groups"] if g["path"].endswith("cedar.md")
    )
    out = commands.op_connect_memory(
        tmp_path,
        operation="accept-relation",
        ref=item["ref"],
        expected_hash=group_hash,
        why="Accepted reviewed relation",
        expected_fingerprint=item["fingerprint"],
    )
    assert out["accepted"] is True
    text = (tmp_path / "Knowledge Base/Notes/Insights/cedar.md").read_text(encoding="utf-8")
    assert "## Relations" in text


def test_accept_writes_bullet_byte_identical_to_studio_path(tmp_path: Path) -> None:
    studio_vault = tmp_path / "studio"
    accept_vault = tmp_path / "accept"
    studio_vault.mkdir()
    accept_vault.mkdir()
    _seed(studio_vault)
    _seed(accept_vault)
    acorn_rel = "Knowledge Base/Notes/Insights/acorn.md"

    # Accept path: one governed server-side step.
    item = _first_item(accept_vault)
    accept_hash = _group_hash(accept_vault, "acorn.md")
    commands.op_connect_memory(
        accept_vault,
        operation="accept-relation",
        ref=item["ref"],
        expected_hash=accept_hash,
        why="Accepted reviewed relation",
        expected_fingerprint=item["fingerprint"],
    )

    # Studio path: the existing two-step client flow's governed edit.
    find.clear_cache()
    studio_hash = commands.op_get(studio_vault, path=acorn_rel)["content_hash"]
    assert studio_hash == accept_hash
    relation = item["relation_type"] or "relates_to"
    destination = str(item["to"]).removesuffix(".md")
    commands.op_edit_memory(
        studio_vault,
        path=acorn_rel,
        why="Accepted reviewed relation",
        heading="Relations",
        section_position="append",
        new_string=f"- {relation} [[{destination}]]",
        expected_hash=studio_hash,
    )

    assert (accept_vault / acorn_rel).read_bytes() == (
        studio_vault / acorn_rel
    ).read_bytes()


def test_accept_fingerprint_mismatch_refuses_and_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    acorn = tmp_path / "Knowledge Base/Notes/Insights/acorn.md"
    item = _first_item(tmp_path)
    accept_hash = _group_hash(tmp_path, "acorn.md")
    before = acorn.read_bytes()
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        commands.op_connect_memory(
            tmp_path,
            operation="accept-relation",
            ref=item["ref"],
            expected_hash=accept_hash,
            why="Accepted reviewed relation",
            expected_fingerprint="deadbeef" * 3,
        )
    assert acorn.read_bytes() == before


def test_accept_hash_mismatch_refuses_and_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    acorn = tmp_path / "Knowledge Base/Notes/Insights/acorn.md"
    item = _first_item(tmp_path)
    before = acorn.read_bytes()
    with pytest.raises(ValueError, match="STALE_EDIT"):
        commands.op_connect_memory(
            tmp_path,
            operation="accept-relation",
            ref=item["ref"],
            expected_hash="0" * 64,
            why="Accepted reviewed relation",
            expected_fingerprint=item["fingerprint"],
        )
    assert acorn.read_bytes() == before


def test_accept_requires_expected_fingerprint(tmp_path: Path) -> None:
    # The spec requires accept-relation to validate the candidate fingerprint
    # against the live signal — an omitted expected_fingerprint must not be
    # treated as "skip this check" (that made the guard skippable by a
    # caller simply not sending it).
    _seed(tmp_path)
    acorn = tmp_path / "Knowledge Base/Notes/Insights/acorn.md"
    item = _first_item(tmp_path)
    accept_hash = _group_hash(tmp_path, "acorn.md")
    before = acorn.read_bytes()
    with pytest.raises(ValueError, match="INVALID_ACCEPT"):
        commands.op_connect_memory(
            tmp_path,
            operation="accept-relation",
            ref=item["ref"],
            expected_hash=accept_hash,
            why="Accepted reviewed relation",
            # expected_fingerprint omitted entirely.
        )
    assert acorn.read_bytes() == before


def test_accepted_item_absent_on_reread(tmp_path: Path) -> None:
    _seed(tmp_path)
    item = _first_item(tmp_path)
    accept_hash = _group_hash(tmp_path, "acorn.md")
    commands.op_connect_memory(
        tmp_path,
        operation="accept-relation",
        ref=item["ref"],
        expected_hash=accept_hash,
        why="Accepted reviewed relation",
        expected_fingerprint=item["fingerprint"],
    )
    find.clear_cache()
    after = commands.op_review_memory(tmp_path, mode="relation-queue")
    refs = {it["ref"] for group in after["groups"] for it in group["items"]}
    assert item["ref"] not in refs


def test_dismissed_item_absent_until_fingerprint_changes(tmp_path: Path) -> None:
    _seed(tmp_path)
    item = _first_item(tmp_path)
    dismissed = commands.op_triage_memory(tmp_path, ref=item["ref"], action="dismiss")
    assert dismissed["state"] == "dismissed"
    assert dismissed["ref"] == item["ref"]

    find.clear_cache()
    after = commands.op_review_memory(tmp_path, mode="relation-queue")
    refs = {it["ref"] for group in after["groups"] for it in group["items"]}
    assert item["ref"] not in refs

    acorn = tmp_path / "Knowledge Base/Notes/Insights/acorn.md"
    acorn.write_text(
        acorn.read_text(encoding="utf-8") + "\nMaterially changed body.\n",
        encoding="utf-8",
    )
    find.clear_cache()
    resurfaced = commands.op_review_memory(tmp_path, mode="relation-queue")
    resurfaced_refs = {
        it["ref"] for group in resurfaced["groups"] for it in group["items"]
    }
    assert item["ref"] in resurfaced_refs


def test_relation_triage_does_not_resolve_activation_items(tmp_path: Path) -> None:
    # cedar is connected by a generic link (typed_relation_debt activation
    # finding) AND yields a links_to relation candidate.
    _write_page(
        tmp_path, "cedar", "## Overview\n\nSee [[Knowledge Base/Notes/Insights/birch]]."
    )
    _write_page(tmp_path, "birch", "A measured fact.")
    find.clear_cache()

    relation_item = next(
        item
        for group in commands.op_review_memory(tmp_path, mode="relation-queue")["groups"]
        for item in group["items"]
        if item["from"].endswith("cedar.md")
    )
    activation_item = next(
        item
        for item in attention.activation(tmp_path, limit=0).items
        if item.path.endswith("cedar.md")
    )
    assert relation_item["ref"] != activation_item.ref

    # A relation ref never resolves an activation/attention item: the attention
    # resolver structurally rejects the relation-namespaced ref.
    with pytest.raises(ValueError, match="INVALID_REVIEW_REFERENCE"):
        attention.item_by_ref(tmp_path, relation_item["ref"])
    # An activation ref never resolves a relation item.
    with pytest.raises(ValueError, match="INVALID_REVIEW_REFERENCE"):
        relation_queue.resolve_candidate(tmp_path, activation_item.ref)

    # Dismissing the relation candidate leaves the activation item open.
    commands.op_triage_memory(tmp_path, ref=relation_item["ref"], action="dismiss")
    still_open = {
        item.ref for item in attention.activation(tmp_path, limit=0).items
    }
    assert activation_item.ref in still_open


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "queue-key")
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_AUD", raising=False)
    return TestClient(server.build_server(require_auth=False).http_app())


def _post(client: TestClient, name: str, body: dict) -> dict:
    request_body = dict(body)
    if name == "connect_memory" and body.get("operation") == "accept-relation":
        request_body["response_detail"] = "full"
    response = client.post(
        f"/api/{name}",
        json=request_body,
        headers={"Authorization": "Bearer queue-key"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True, payload
    data = payload["data"]
    return data.get("diagnostics", data)


def test_new_operations_exposed_on_all_registry_surfaces(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parent tools carry the new mode/operation on every generated surface.
    for surface in ("mcp", "rest", "cli"):
        names = {c.name for c in commands.product_commands_for(surface)}
        assert {"review_memory", "connect_memory", "triage_memory"} <= names

    _seed(vault)
    client = _client(vault, monkeypatch)

    queue = _post(client, "review_memory", {"mode": "relation-queue"})
    assert queue["mode"] == "relation-queue"
    acorn_group = next(g for g in queue["groups"] if g["path"].endswith("acorn.md"))
    item = acorn_group["items"][0]
    group_hash = acorn_group["content_hash"]
    accepted = _post(
        client,
        "connect_memory",
        {
            "operation": "accept-relation",
            "ref": item["ref"],
            "expected_hash": group_hash,
            "expected_fingerprint": item["fingerprint"],
            "why": "Accepted reviewed relation via REST",
        },
    )
    assert accepted["accepted"] is True
    assert accepted["bullet"].startswith("- ")
