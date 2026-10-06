"""Every public door a restricted reader can reach keeps an unreleased origin payload off the wire.

One carrier's assessment reason and input reference must never reach an audience
whose bound input is withheld, whichever door it reads through and wherever a hand
edit moved the carrier: as the writer placed it, indented into prose code,
indented into a semantic unit's own body, or into one of the unit's fields. The
other tests cover an entity's lede and another page's inbound link. Each door is
called through the same dispatcher every surface shares. A door that refuses the
caller is safe; the test also proves the page itself stays readable, so a door
cannot pass by hiding it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_episode_recovery import _write_source_rule
from test_origin_bindings import _write

from exomem import (
    commands,
    epistemic_graph,
    graph_sync,
    note,
    provenance,
    working_set_index,
    writer_lease,
)
from exomem import find as find_module
from exomem.governance.principal import RequestPrincipal, request_scope

READER = RequestPrincipal(audience_id="client-a", surface="mcp")
REASON = "PRIVATEREASON"
WORD = "zebrafact"


def _doors(path: str, unit_ref: str) -> list[tuple[str, str, dict]]:
    folder = path.rsplit("/", 1)[0]
    doors = [
        ("read_memory", "read_memory", {"path": path}),
        ("read_memory raw", "read_memory", {"path": path, "include_raw": True}),
        ("read_memory frontmatter", "read_memory", {"path": path, "frontmatter_only": True}),
        ("read_memory unit", "read_memory", {"path": path, "unit_ref": unit_ref}),
        ("read_memory links", "read_memory", {"path": path, "links": True}),
        ("read_memory history", "read_memory", {"path": path, "include_history": True}),
        ("ask_memory pack", "ask_memory", {"query": WORD, "mode": "keyword", "deep": True}),
        ("activate_context", "activate_context", {"turn": f"{WORD} Parent page"}),
        ("browse_memory overview", "browse_memory", {"mode": "overview"}),
        ("browse_memory folder", "browse_memory", {"path": folder}),
        ("connect_memory context", "connect_memory", {"operation": "context", "path": path}),
        ("connect_memory graph", "connect_memory", {"operation": "graph-context", "query": WORD}),
        ("connect_memory links", "connect_memory", {"operation": "suggest-links", "path": path}),
        ("connect_memory relations", "connect_memory", {"operation": "suggest-relations", "path": path}),
        ("connect_memory inbound", "connect_memory", {"operation": "inbound-links", "path": path}),
        ("schema_memory census", "schema_memory", {"operation": "census", "subject": "relations"}),
        ("schema_memory categories", "schema_memory", {"operation": "validate", "subject": "categories"}),
    ]
    doors += [
        (f"ask_memory {level}", "ask_memory", {"query": WORD, "mode": "keyword", "result_level": level})
        for level in ("auto", "page", "unit")
    ]
    doors += [
        (f"review_memory {mode}", "review_memory", {"mode": mode})
        for mode in (
            "attention", "activation", "audit", "stale", "upkeep", "dispositions",
            "contradiction", "relation-debt", "relation-queue", "unprocessed-sources",
            "vocabulary", "plan-progress",
        )
    ]
    doors += [
        ("review_memory evolution", "review_memory", {"mode": "evolution", "path": path}),
        ("review_memory provenance", "review_memory", {"mode": "provenance"}),
        ("review_memory provenance key", "review_memory", {"mode": "provenance", "key": "exomem"}),
        ("review_memory provenance tag", "review_memory", {"mode": "provenance", "tag": "exomem-origin"}),
        ("review_memory provenance path", "review_memory", {"mode": "provenance", "path": path}),
    ]
    return doors


def door_results(vault: Path, path: str, unit_ref: str, secrets: tuple[str, ...]) -> dict[str, str]:
    """Each door's outcome for the restricted reader: `served`, `leaked` or `refused`."""
    byname = {command.name: command for command in commands.PRODUCT_COMMANDS}
    out: dict[str, str] = {}
    with request_scope(READER):
        for label, name, kwargs in _doors(path, unit_ref):
            try:
                wire = json.dumps(writer_lease.invoke_command(byname[name], vault, **kwargs), default=str)
            except Exception as error:  # noqa: BLE001 - a refusal discloses nothing
                out[label] = f"refused: {str(error)[:80]}"
                continue
            out[label] = "leaked" if any(secret in wire for secret in secrets) else "served"
    return out


def _carrier(vault: Path, reason: str = f"{REASON}, see [[{REASON}]]") -> tuple[str, str]:
    """A carrier whose reason holds the secret both as prose and as a link target."""
    binding = _write(vault, "Original evidence.\n")
    block = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [
                {"inputs": ["original"], "basis": "agent_assessment", "by": "agent", "reason": reason}
            ],
            "bindings": [],
        }
    )
    return block, binding["reference"]


def _placed(vault: Path, placement: str) -> tuple[object, str]:
    """A client-a page whose carrier a hand edit then moved, its input withheld."""
    block, reference = _carrier(vault)
    _write_source_rule(vault, ceiling=6)
    with request_scope(READER):
        created = note.note(
            vault,
            content=block + f"\n\n## Claim\n- id: claim\n\nPublic claim {WORD} retained.\n",
            note_type="insight",
            title="Parent page",
            status="draft",
        )
    page = vault / created.path
    text = page.read_text(encoding="utf-8")
    start = text.index("<!-- exomem-origin")
    end = text.index("-->", start) + 3
    if placement == "indented-into-prose":
        text = text[:start] + "Para.\n\n    " + text[start:]
    elif placement == "indented-into-a-unit":
        text = (text[:start] + text[end:]).rstrip("\n") + "\n\n    " + text[start:end] + "\n"
    elif placement == "into-a-unit-field":
        moved = text[:start] + text[end:]
        text = moved.replace("- id: claim\n", f"- id: claim\n- note: {text[start:end]}\n", 1)
    page.write_text(text, encoding="utf-8")
    _write_source_rule(vault, ceiling=0)
    find_module.clear_cache()
    graph_sync.drain_active_rebuilds()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return created, reference


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "placement", ["as-written", "indented-into-prose", "indented-into-a-unit", "into-a-unit-field"]
)
def test_no_restricted_door_serves_an_unreleased_origin_payload(vault: Path, placement: str) -> None:
    created, reference = _placed(vault, placement)

    results = door_results(vault, created.path, created.ref + "#claim", (REASON, reference))

    leaked = sorted(label for label, outcome in results.items() if outcome == "leaked")
    assert not leaked, f"leaked through: {leaked}"
    with request_scope(READER):
        read = commands.op_get(vault, path=created.path)
        found = commands.op_find(vault, query=REASON, mode="keyword")
    assert "Public claim" in read["body"]
    assert created.path not in json.dumps(found, default=str)


def test_activation_never_quotes_an_unreleased_origin_carrier_as_an_entity_lede(vault: Path) -> None:
    block, reference = _carrier(vault)
    _write_source_rule(vault, ceiling=0)
    entity = vault / "Knowledge Base" / "Entities" / "People" / "Quorin Vale.md"
    entity.write_text(
        "---\ntype: entity\nentity_type: person\nstatus: active\n"
        "created: 2026-10-01\nupdated: 2026-10-01\n---\n\n"
        f"# Quorin Vale\n\n{block}\n\nQuorin Vale is a public cartographer.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    working_set_index.WorkingSetIndex(vault).rebuild()
    activate = next(command for command in commands.PRODUCT_COMMANDS if command.name == "activate_context")

    with request_scope(READER):
        packet = writer_lease.invoke_command(activate, vault, turn="Who is Quorin Vale?")

    wire = json.dumps(packet, default=str)
    assert REASON not in wire and reference not in wire
    identity = [unit["text"] for unit in packet["units"] if unit["role"] == "identity"]
    assert identity == ["Quorin Vale is a public cartographer."]


def test_a_carrier_is_never_an_inbound_link_a_restricted_reader_is_shown(vault: Path) -> None:
    block, reference = _carrier(vault, reason=f"{REASON} [[Linked page]]")
    _write_source_rule(vault, ceiling=6)
    with request_scope(READER):
        linked = note.note(
            vault, content="Linked claim.\n", note_type="insight", title="Linked page", status="draft"
        )
        parent = note.note(
            vault,
            content=block + "\n\nPublic claim, see [[Linked page]].\n",
            note_type="insight",
            title="Parent page",
            status="draft",
        )
    _write_source_rule(vault, ceiling=0)
    find_module.clear_cache()
    byname = {command.name: command for command in commands.PRODUCT_COMMANDS}

    with request_scope(READER):
        read = writer_lease.invoke_command(byname["read_memory"], vault, path=linked.path, links=True)
        inbound = writer_lease.invoke_command(
            byname["connect_memory"], vault, operation="inbound-links", path=linked.path
        )

    wire = json.dumps([read, inbound], default=str)
    assert REASON not in wire and reference not in wire
    assert [link["path"] for link in read["links"]["inbound"]] == [parent.path]


def test_a_carrier_never_makes_a_name_recur_for_a_restricted_reader(vault: Path) -> None:
    block, reference = _carrier(vault)
    _write_source_rule(vault, ceiling=0)
    for slug in ("first-page", "second-page"):
        page = vault / "Knowledge Base" / "Notes" / "Insights" / f"{slug}.md"
        page.write_text(
            f"---\ntitle: {slug}\ntype: insight\nstatus: active\n"
            "created: 2026-09-01\nupdated: 2026-09-01\n---\n\n"
            f"{block}\n\nPublic claim about [[Ghost Name]].\n",
            encoding="utf-8",
        )
    find_module.clear_cache()
    review = next(command for command in commands.PRODUCT_COMMANDS if command.name == "review_memory")

    with request_scope(READER):
        attention = writer_lease.invoke_command(
            review, vault, mode="attention", categories=["entity_recurrence"]
        )

    wire = json.dumps(attention, default=str)
    assert REASON not in wire and reference not in wire
    assert "Ghost Name" in wire
