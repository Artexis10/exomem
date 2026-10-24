"""Global inventory recomposes canonical reflection under each request's authority."""

import datetime as dt
from dataclasses import replace

from test_collection_claims import _write_observation
from test_collection_store_service import OWNER, call
from test_collection_store_service import service as service
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, OTHER, manifest_path, manifest_text
from test_governance_egress import _external, write_rule, write_scope

from exomem import commands, due_state, writer_lease
from exomem.collection_store import runtime
from exomem.governance.principal import request_scope


def test_inventory_rechecks_withheld_canonical_reflectors(service):
    """An unchanged projection must not expose a reflector that this request cannot read."""
    root = service.root
    identity = "77777777-7777-4777-8777-777777777777"
    path = manifest_path().replace("Work", "Reflections")
    text = summary_text(manifest_text().replace(CID, identity)
        .replace("title: Work", "title: Reflections")
        .replace("lifecycle: active\n", "lifecycle: active\nclaims:\n  terms: [account, subscriptions]\n")
        .replace("    count: {type: integer}\n", "    sources: {type: array, items: {type: link}}\n"))
    call(root, "record_memory", action="create", manifest_path=path,
         manifest_text=text, why="observe reflection")
    _observation, reference = _write_observation(root)
    call(root, "record_memory", action="append", collection=identity, item_key=OTHER,
         item={"title": "Reflected", "sources": [reference]}, why="reflect observation")
    with request_scope(OWNER):
        runtime._SERVERS[root].call(lambda: due_state.reconcile(
            root, now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)))
    projection = due_state.state_path(root).read_bytes()

    before = call(root, "record_memory", action="inspect", collection=identity)
    assert before["coverage"]["unreflected_refs"] == []
    reflected = call(root, "record_memory", action="inspect")
    assert next(row for row in reflected["collections"]
                if row["collection_id"] == identity)["unreflected"] == 0

    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{identity}/{OTHER}]\n")
    write_rule(root, ceiling=0)
    command = next(command for command in commands.product_commands_for("mcp")
                   if command.name == "record_memory")
    with request_scope(_external()):
        withheld = writer_lease.invoke_command(command, root, action="inspect")
    hidden = next(row for row in withheld["collections"] if row["collection_id"] == identity)
    assert (hidden["committed"], hidden["unreflected"]) == (0, 1)
    write_rule(root, ceiling=6)
    with request_scope(replace(_external(), authorization_session_id="later-session")):
        released = writer_lease.invoke_command(command, root, action="inspect")
    visible = next(row for row in released["collections"] if row["collection_id"] == identity)
    assert (visible["committed"], visible["unreflected"]) == (1, 0)
    assert due_state.state_path(root).read_bytes() == projection
