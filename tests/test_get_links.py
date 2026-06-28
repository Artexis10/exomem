"""get(links=True) inbound/outbound link summary — via the REST /api/get path
(which calls the same `_link_summary` the MCP `get` tool uses) and the helper directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kb_mcp import find as find_module
from kb_mcp import server

TARGET_REL = "Knowledge Base/Notes/Insights/link-target.md"
SOURCE_REL = "Knowledge Base/Notes/Insights/link-source.md"

TARGET = """\
---
type: insight
status: active
created: 2026-01-01
updated: 2026-01-01
---

# Link Target

## Claim

The target of an inbound link.
"""

SOURCE = """\
---
type: insight
status: active
created: 2026-01-01
updated: 2026-01-01
---

# Link Source

## Claim

This builds on [[Knowledge Base/Notes/Insights/link-target]].
"""


def _seed(vault: Path) -> None:
    (vault / TARGET_REL).write_text(TARGET, encoding="utf-8")
    (vault / SOURCE_REL).write_text(SOURCE, encoding="utf-8")
    find_module.clear_cache()


def _client(vault, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("KB_MCP_REST_API_KEY", "KB_MCP_UPLOAD_TOKEN"):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_outbound_links_populated(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/get",
        json={"path": SOURCE_REL, "links": True},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    links = r.json()["links"]
    assert "Knowledge Base/Notes/Insights/link-target" in links["outbound"]


def test_inbound_links_populated(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/get",
        json={"path": TARGET_REL, "links": True},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    inbound = r.json()["links"]["inbound"]
    assert any("link-source" in m["path"] for m in inbound), inbound


def test_links_absent_when_flag_off(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    client = _client(vault, monkeypatch, KB_MCP_REST_API_KEY="sekret")
    r = client.post(
        "/api/get",
        json={"path": SOURCE_REL},  # links defaults False
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    assert "links" not in r.json()


def test_link_summary_helper_direct(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    body = (vault / SOURCE_REL).read_text(encoding="utf-8").split("---\n", 2)[-1]
    out = server._link_summary(vault, SOURCE_REL, body)
    assert out["outbound"] == ["Knowledge Base/Notes/Insights/link-target"]
    # Source has no inbound links of its own in this seed.
    assert out["inbound"] == []
