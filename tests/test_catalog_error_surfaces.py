"""Task 4.3 (restore-indexed-category-recall) — command-surface parity for
typed private catalog outcomes.

Pins the design.md Decision 4 / spec *Incomplete Exact Recall Is Observable*
requirement that REST, MCP, and CLI project the identical shared `OpError`
envelope for a safe exact-category plan the maintained semantic catalog
cannot yet answer completely (`RETRIEVAL_INDEX_WARMING`):

* REST maps the code to HTTP 503 and preserves bounded `retry_after_ms`;
* the OpenAPI `Error`/`ErrorEnvelope` schema documents and accepts the fixed
  warming fields (`code`, `message`, `remediation`, `status`, `complete`,
  `retry_after_ms`) with no extra keys;
* MCP (the `bind_vault` tool wrapper) and CLI (`--json`) project the exact
  same envelope `cli_ops` already builds for REST — no surface invents its
  own shape or leaks raw query/category/path/exception detail.

RED until `cli_ops.http_status_for` maps `RETRIEVAL_INDEX_WARMING` to 503 and
the REST OpenAPI `Error` schema accepts the `complete` field the typed
outcome carries (`find.RetrievalIndexWarming` itself already exists).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from exomem import capabilities, cli_ops, commands, freshness, lexstore, schema, server
from exomem import find as find_module
from exomem.__main__ import main as cli_main

_ALLOWED_WARMING_ERROR_KEYS = frozenset(
    {"code", "message", "remediation", "ok", "error_code", "status", "complete", "retry_after_ms"}
)
_FORBIDDEN_SUBSTRINGS = (
    "warming-surface",  # the note's rel-path stem
    "Knowledge Base",
    ".md",
    "config",  # the requested category value
    "sqlite3",
    "OperationalError",
    "Traceback",
    "auto",  # configured-but-unresolved backend name must never leak
)


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()


def _write_note(root: Path, rel_path: str, body: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"catalog-error-surface:{rel_path}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {path.stem}\n"
        f"exomem_id: {page_id}\n"
        "status: active\n"
        "updated: 2026-07-22\n"
        "---\n\n"
        f"# {path.stem}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_live_freshness(root: Path, paths: list[Path]) -> None:
    vault_entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    kb_entries = [
        entry for entry in vault_entries if Path(entry[0]).is_relative_to(root / "Knowledge Base")
    ]
    freshness.seed(root, "kb", kb_entries)
    freshness.seed(root, "vault", vault_entries)


@pytest.fixture
def warming_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A vault whose one category candidate cannot be served from a complete
    catalog: inline foreground repair is disabled and the background repair
    scheduler is stubbed, so a safe exact-category plan raises the typed
    ``RetrievalIndexWarming`` outcome instead of a false empty or a scan.
    """
    target = _write_note(
        vault,
        "Knowledge Base/Notes/warming-surface.md",
        "- [config] not yet cataloged ^warming-surface",
    )
    _seed_live_freshness(vault, [target])
    monkeypatch.setattr(find_module, "_FOREGROUND_LEXICAL_REPAIR_PAGE_CAP", 0)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *_a, **_k: None)
    assert not lexstore.lexical_path(vault).exists()
    return vault


_ASK_MEMORY_BODY = {
    "query": "",
    "categories": ["config"],
    "scope": "kb-only",
    "mode": "keyword",
    "result_level": "unit",
    "limit": 20,
}


def _assert_warming_error(error: dict) -> None:
    assert error["code"] == "RETRIEVAL_INDEX_WARMING"
    assert error["status"] in {"warming", "temporarily_unavailable"}
    assert error["complete"] is False
    retry_after = error["retry_after_ms"]
    assert isinstance(retry_after, int) and 0 < retry_after <= 60_000
    assert error["message"]
    assert error["remediation"]


def _assert_no_leak(error: dict) -> None:
    assert set(error) <= _ALLOWED_WARMING_ERROR_KEYS, set(error) - _ALLOWED_WARMING_ERROR_KEYS
    blob = json.dumps(error, ensure_ascii=False)
    for sentinel in _FORBIDDEN_SUBSTRINGS:
        assert sentinel not in blob, f"leaked sentinel {sentinel!r} in {blob!r}"


# --------------------------------------------------------------------------- #
# cli_ops: the shared HTTP-status mapping REST relies on.
# --------------------------------------------------------------------------- #


def test_http_status_for_retrieval_index_warming_is_503() -> None:
    assert cli_ops.http_status_for("RETRIEVAL_INDEX_WARMING") == 503


def test_http_status_for_retrieval_index_warming_is_not_client_fault() -> None:
    # A guard against regressing to the default 400: warming is a server-side,
    # retryable state, never a caller mistake.
    assert cli_ops.http_status_for("RETRIEVAL_INDEX_WARMING") != 400


# --------------------------------------------------------------------------- #
# REST: POST /api/ask_memory and the OpenAPI self-description.
# --------------------------------------------------------------------------- #


def _rest_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY",
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


def test_rest_ask_memory_warming_is_503_with_bounded_retry(
    warming_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _rest_client(monkeypatch, EXOMEM_REST_API_KEY="sekret")
    r = client.post(
        "/api/ask_memory",
        json=_ASK_MEMORY_BODY,
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 503, r.text
    payload = r.json()
    assert payload["success"] is False
    _assert_warming_error(payload["error"])
    _assert_no_leak(payload["error"])


def test_rest_ask_memory_warming_is_excluded_from_hot_cache(
    warming_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second identical call must still warm — never serve a cached empty
    # from the first (failed) attempt.
    client = _rest_client(monkeypatch, EXOMEM_REST_API_KEY="sekret")
    for _ in range(2):
        r = client.post(
            "/api/ask_memory",
            json=_ASK_MEMORY_BODY,
            headers={"Authorization": "Bearer sekret"},
        )
        assert r.status_code == 503, r.text
        _assert_warming_error(r.json()["error"])


def test_rest_openapi_error_schema_accepts_warming_fields(
    warming_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _rest_client(monkeypatch, EXOMEM_REST_API_KEY="sekret")
    doc = client.get("/api/openapi.json").json()
    error_schema = doc["components"]["schemas"]["Error"]
    assert error_schema.get("additionalProperties") is False
    allowed_props = set(error_schema["properties"])
    assert error_schema["properties"]["retry_after_ms"]["type"] == "integer"
    assert error_schema["properties"]["complete"]["type"] == "boolean"

    ask_memory_responses = doc["paths"]["/api/ask_memory"]["post"]["responses"]
    assert "503" in ask_memory_responses

    warming_response = client.post(
        "/api/ask_memory",
        json=_ASK_MEMORY_BODY,
        headers={"Authorization": "Bearer sekret"},
    )
    error = warming_response.json()["error"]
    assert set(error) <= allowed_props
    for required in error_schema["required"]:
        assert required in error


# --------------------------------------------------------------------------- #
# MCP: the exact `bind_vault` tool wrapper FastMCP registers.
# --------------------------------------------------------------------------- #


def _ask_memory_command() -> commands.Command:
    return next(
        c
        for c in commands.product_commands_for("mcp", expose_tier2=True)
        if c.name == "ask_memory"
    )


def test_mcp_tool_wrapper_projects_same_warming_envelope(warming_vault: Path) -> None:
    cmd = _ask_memory_command()
    surface_descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="product",
        tier2_enabled=True,
        product_commands=(cmd.name,),
    )
    injected = (
        (warming_vault, schema.load_source_schema(warming_vault))
        if cmd.needs_schema
        else (warming_vault,)
    )
    wrapped = commands.bind_vault(
        cmd.leaf,
        *injected,
        name=cmd.name,
        description=cmd.doc,
        command=cmd,
        surface_descriptor=surface_descriptor,
    )

    result = wrapped(**_ASK_MEMORY_BODY)

    assert result["success"] is False
    _assert_warming_error(result["error"])
    _assert_no_leak(result["error"])


def test_mcp_and_rest_project_identical_warming_error(
    warming_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _ask_memory_command()
    surface_descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="product",
        tier2_enabled=True,
        product_commands=(cmd.name,),
    )
    injected = (
        (warming_vault, schema.load_source_schema(warming_vault))
        if cmd.needs_schema
        else (warming_vault,)
    )
    wrapped = commands.bind_vault(
        cmd.leaf,
        *injected,
        name=cmd.name,
        description=cmd.doc,
        command=cmd,
        surface_descriptor=surface_descriptor,
    )
    mcp_error = wrapped(**_ASK_MEMORY_BODY)["error"]

    client = _rest_client(monkeypatch, EXOMEM_REST_API_KEY="sekret")
    rest_error = client.post(
        "/api/ask_memory",
        json=_ASK_MEMORY_BODY,
        headers={"Authorization": "Bearer sekret"},
    ).json()["error"]

    assert mcp_error == rest_error


# --------------------------------------------------------------------------- #
# CLI: `kb ask_memory ... --json`.
# --------------------------------------------------------------------------- #


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    try:
        code = cli_main(argv)
    except SystemExit as e:  # argparse usage errors
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_ask_memory_warming_preserves_shared_envelope(
    warming_vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run_cli(
        [
            "ask_memory",
            "",
            "--mode",
            "keyword",
            "--scope",
            "kb-only",
            "--categories",
            "config",
            "--result-level",
            "unit",
            "--limit",
            "20",
            "--json",
        ],
        capsys,
    )
    assert code == 1
    payload = json.loads(out)
    assert payload["success"] is False
    _assert_warming_error(payload["error"])
    _assert_no_leak(payload["error"])


def test_cli_and_rest_project_identical_warming_error(
    warming_vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _rest_client(monkeypatch, EXOMEM_REST_API_KEY="sekret")
    rest_error = client.post(
        "/api/ask_memory",
        json=_ASK_MEMORY_BODY,
        headers={"Authorization": "Bearer sekret"},
    ).json()["error"]

    _code, out, _err = _run_cli(
        [
            "ask_memory",
            "",
            "--mode",
            "keyword",
            "--scope",
            "kb-only",
            "--categories",
            "config",
            "--result-level",
            "unit",
            "--limit",
            "20",
            "--json",
        ],
        capsys,
    )
    cli_error = json.loads(out)["error"]

    assert cli_error == rest_error


# --------------------------------------------------------------------------- #
# Legacy compatibility: a plain OpError with no `details` is untouched.
# --------------------------------------------------------------------------- #


def test_plain_op_error_http_mapping_and_shape_are_unaffected() -> None:
    # Guards against the 503 mapping (or the new OpenAPI `complete` field)
    # widening to swallow unrelated codes or add unrequested keys.
    error = cli_ops.error_dict(cli_ops.OpError("NOT_FOUND", "no such file"))
    assert error == {"code": "NOT_FOUND", "message": "no such file", "remediation": error["remediation"]}
    assert cli_ops.http_status_for("NOT_FOUND") == 404
