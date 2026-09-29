"""Dynamic entity types behave as one identity on MCP, CLI and REST.

`close-memory-loop` task 5.2 and `memory-loop` "Dynamic identities and
relations use normal public surfaces": a synthetic equipment family (a vault
root with a subtype) and a physical-site type are registered, created, resolved
exactly and by parent family, and traversed through the three public doors.
Every door reports the same canonical ref and path, graph publication is
reported as it is rather than as current, and nothing routes through a generic
file writer or an organization type.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient

from exomem import epistemic_graph, server
from exomem.__main__ import main as cli_main
from exomem.governance.principal import owner_principal, request_scope

REGISTRY = {
    "schema_version": 1,
    "entity_types": {
        "equipment": {
            "folder": "Equipment",
            "label": "Equipment",
            "aliases": [],
            "capture_guidance": "A durable piece of physical equipment.",
        },
        "milking-machine": {
            "folder": "Milking Machines",
            "label": "Milking Machine",
            "aliases": [],
            "capture_guidance": "A milking machine installed at a site.",
            "parent": "equipment",
        },
        "site": {
            "folder": "Sites",
            "label": "Site",
            "aliases": [],
            "capture_guidance": "A stable physical site identity.",
        },
    },
    "facets": {"site": {"region": {"cardinality": "single", "value": "text"}}},
}

SITE = "Kestrel Upper Field"
PUMP = "Parlour Vacuum Pump"
PARLOUR = "Rotary Parlour Unit"


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    return TestClient(server.build_server(require_auth=False).http_app())


class Doors:
    def __init__(self, client: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
        self.client = client
        self.capsys = capsys

    def mcp(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        mcp = server.build_server(require_auth=False)
        with request_scope(owner_principal(surface="mcp")):
            called = asyncio.run(mcp.call_tool(tool, args, run_middleware=False))
        if isinstance(called.structured_content, dict):
            content = called.structured_content
            return content.get("result", content) if set(content) == {"result"} else content
        return json.loads(called.content[0].text)

    def rest(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            f"/api/{tool}", json=args, headers={"Authorization": "Bearer sekret"}
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def cli(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        argv = [tool]
        for key, value in args.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, dict):
                argv += [flag, json.dumps(value)]
            elif isinstance(value, list):
                argv += [flag, ",".join(value)]
            else:
                argv += [flag, str(value)]
        argv.append("--json")
        self.capsys.readouterr()
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        out = self.capsys.readouterr().out
        assert code == 0, out
        return json.loads(out)["data"]

    def all(self, tool: str, args: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            "mcp": self.mcp(tool, args),
            "cli": self.cli(tool, args),
            "rest": self.rest(tool, args),
        }


@pytest.fixture
def doors(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Doors:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return Doors(_rest_client(monkeypatch), capsys)


def _create(summary: str, **args: Any) -> dict[str, Any]:
    return {"operation": "create-entity", "summary": summary, **args}


def _identity(candidate: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        candidate["ref"],
        candidate["path"],
        candidate["entity_type"],
        candidate["entity_family"],
    )


def test_dynamic_types_register_create_resolve_and_traverse_on_three_doors(
    vault: Path, doors: Doors
) -> None:
    registered = doors.mcp(
        "schema_memory",
        {
            "subject": "entity-types",
            "operation": "save-entity-types",
            "proposal": REGISTRY,
            "why": "Synthetic equipment family and physical sites for the loop cohort.",
        },
    )
    assert registered["state"] == "committed", registered
    assert (vault / "Knowledge Base/_Schema/entity-types.yaml").is_file()

    # Every door serves the same registry, with vault-rooted families and no
    # organization classification forced onto a site or a machine.
    for door, payload in doors.all("bootstrap", {"profile": "compact", "section": "entities"}).items():
        types = {item["id"]: item for item in payload["entity_registry"]["types"]}
        assert types["site"]["family"] == "site", door
        assert types["site"]["facets"] == {"region": "single text"}, door
        assert types["equipment"]["family"] == "equipment", door
        assert types["milking-machine"]["family"] == "equipment", door
        assert types["milking-machine"]["folder"] == "Milking Machines", door

    # One creation per door, each through the typed entity writer.
    created = {
        "mcp": doors.mcp(
            "connect_memory",
            _create(
                "The physical field the parlour stands on.",
                entity_type="site",
                name=SITE,
                facets={"region": "Upland"},
            ),
        ),
        "rest": doors.rest(
            "connect_memory",
            _create(
                "The vacuum pump that serves the parlour.",
                entity_type="equipment",
                name=PUMP,
                connections=[f"Knowledge Base/Entities/Sites/{SITE}"],
            ),
        ),
        "cli": doors.cli(
            "connect_memory",
            _create(
                "A rotary milking parlour installed on the field.",
                entity_type="milking-machine",
                name=PARLOUR,
                connections=[f"Knowledge Base/Entities/Sites/{SITE}"],
            ),
        ),
    }
    expected_paths = {
        "mcp": f"Knowledge Base/Entities/Sites/{SITE}.md",
        "rest": f"Knowledge Base/Entities/Equipment/{PUMP}.md",
        "cli": f"Knowledge Base/Entities/Milking Machines/{PARLOUR}.md",
    }
    envelope = {"ok", "state", "status", "terminal", "mutated", "path", "request_id", "receipt_id"}
    for door, receipt in created.items():
        assert receipt["state"] == "committed", (door, receipt)
        assert receipt["mutated"] is True, door
        assert receipt["path"] == expected_paths[door], door
        page = yaml.safe_load((vault / receipt["path"]).read_text(encoding="utf-8").split("---")[1])
        assert page["type"] == "entity"
        assert page["entity_type"] in {"site", "equipment", "milking-machine"}
        # The same receipt contract on every door, graph publication included.
        assert envelope <= set(receipt), (door, sorted(receipt))
        # Publication is reported as it stands: durable, graph still pending.
        assert receipt["graph_sync"] in {"pending", "completed"}, (door, receipt)
        if receipt["graph_sync"] == "pending":
            assert receipt["graph_sync_code"] == "GRAPH_SYNC_REBUILD_IN_PROGRESS", door
    site_page = yaml.safe_load(
        (vault / expected_paths["mcp"]).read_text(encoding="utf-8").split("---")[1]
    )
    assert site_page["region"] == "Upland"

    # Exact resolution: every door names the same canonical identity.
    for name, entity_type in ((SITE, "site"), (PUMP, "equipment"), (PARLOUR, "milking-machine")):
        answers = doors.all(
            "connect_memory",
            {"operation": "resolve-entity", "name": name, "entity_type": entity_type},
        )
        identities = {door: _identity(answer["candidates"][0]) for door, answer in answers.items()}
        assert all(answer["status"] == "match" for answer in answers.values()), answers
        assert len(set(identities.values())) == 1, identities
        assert identities["mcp"][2] == entity_type

    # Parent-family resolution: the subtype answers to its vault-rooted family,
    # and never to an unrelated core family.
    by_family = doors.all(
        "connect_memory",
        {"operation": "resolve-entity", "name": PARLOUR, "entity_family": "equipment"},
    )
    assert {answer["status"] for answer in by_family.values()} == {"match"}
    assert {
        (answer["candidates"][0]["entity_type"], answer["candidates"][0]["entity_family"])
        for answer in by_family.values()
    } == {("milking-machine", "equipment")}
    elsewhere = doors.all(
        "connect_memory",
        {"operation": "resolve-entity", "name": PARLOUR, "entity_family": "organization"},
    )
    assert {answer["status"] for answer in elsewhere.values()} == {"no_match"}

    # Traversal after publication: the same neighbourhood on every door, and
    # the family filter keeps only the equipment family around the site.
    site_path = expected_paths["mcp"]
    before_publication = doors.mcp(
        "connect_memory", {"operation": "graph-context", "path": site_path, "depth": 1}
    )
    # A graph read is either current (every created identity is a node) or
    # says it is not, with a reason; never an available graph missing them.
    graph = before_publication["graph"]
    if graph["available"]:
        published = {node["path"] for node in graph["nodes"]} | {
            seed["path"] for seed in graph["seeds"]
        }
        assert set(expected_paths.values()) <= published, graph
    else:
        assert graph["reason"], graph
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    traversed = doors.all(
        "connect_memory",
        {"operation": "graph-context", "path": site_path, "depth": 1},
    )
    neighbourhoods = {}
    for door, context in traversed.items():
        graph = context["graph"]
        assert graph["available"] is True, (door, graph)
        neighbourhoods[door] = {
            node["path"]: node["metadata"].get("entity_type")
            for node in graph["nodes"]
            if node.get("path")
        }
    assert len({json.dumps(value, sort_keys=True) for value in neighbourhoods.values()}) == 1
    assert neighbourhoods["mcp"][expected_paths["rest"]] == "equipment"
    assert neighbourhoods["mcp"][expected_paths["cli"]] == "milking-machine"

    family_only = doors.all(
        "connect_memory",
        {
            "operation": "graph-context",
            "path": site_path,
            "depth": 1,
            "entity_family": "equipment",
        },
    )
    for door, context in family_only.items():
        families = {
            node["metadata"].get("entity_family")
            for node in context["graph"]["nodes"]
            if node.get("path") and node["path"] != site_path
        }
        assert families == {"equipment"}, (door, families)
