from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import capabilities, commands, schema, server, source_closure
from exomem.__main__ import main as cli_main
from exomem.governance import principal as principal_module

MISSING = "Knowledge Base/Sources/Articles/uncaptured-original"
BODY = {
    "content": "## Claim\n\nA draft derived conclusion.\n",
    "title": "Surface source closure",
    "note_type": "insight",
    "status": "draft",
    "sources": [MISSING],
}


def _assert_error(error: dict) -> None:
    assert error == {
        "code": "UNRESOLVED_SOURCE_CITATION",
        "message": "one or more explicit sources do not resolve to captured material",
        "remediation": source_closure.UNRESOLVED_REMEDIATION,
        "unresolved_sources": [MISSING],
        "unresolved_source_count": 1,
        "unresolved_sources_truncated": False,
    }


def _remember_command() -> commands.Command:
    return next(
        command
        for command in commands.product_commands_for("mcp", expose_tier2=True)
        if command.name == "remember"
    )


def _mcp_error(vault: Path) -> dict:
    command = _remember_command()
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="product",
        tier2_enabled=True,
        product_commands=(command.name,),
    )
    injected = (vault, schema.load_source_schema(vault)) if command.needs_schema else (vault,)
    wrapped = commands.bind_vault(
        command.leaf,
        *injected,
        name=command.name,
        description=command.doc,
        command=command,
        surface_descriptor=descriptor,
    )
    with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
        result = wrapped(**BODY)
    assert result["success"] is False
    assert result["mutated"] is False
    return result["error"]


def _rest_client(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "source-closure-test")
    return TestClient(server.build_server(require_auth=False).http_app())


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    try:
        code = cli_main(argv)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    output = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(output)


def test_mcp_rest_and_cli_json_share_source_closure_envelope(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mcp = _mcp_error(vault)

    client = _rest_client(vault, monkeypatch)
    response = client.post(
        "/api/remember",
        json=BODY,
        headers={"Authorization": "Bearer source-closure-test"},
    )
    assert response.status_code == 400, response.text
    rest = response.json()["error"]

    code, cli_payload = _run_cli(
        [
            "remember",
            "--content",
            BODY["content"],
            "--title",
            BODY["title"],
            "--field",
            "note_type=insight",
            "--field",
            "status=draft",
            "--field",
            f"sources={MISSING}",
            "--json",
        ],
        capsys,
    )
    assert code == 1
    cli = cli_payload["error"]

    _assert_error(mcp)
    assert rest == mcp
    assert cli == mcp


def test_openapi_documents_bounded_source_closure_fields(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_doc = _rest_client(vault, monkeypatch).get("/api/openapi.json").json()
    error = schema_doc["components"]["schemas"]["Error"]

    assert error["additionalProperties"] is False
    assert error["properties"]["unresolved_sources"]["maxItems"] == 8
    assert error["properties"]["unresolved_source_count"]["type"] == "integer"
    assert error["properties"]["unresolved_sources_truncated"]["type"] == "boolean"


def test_human_cli_renders_source_closure_code_message_and_remediation(
    vault: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        code = cli_main(
            [
                "remember",
                "--content",
                BODY["content"],
                "--title",
                BODY["title"],
                "--field",
                "note_type=insight",
                "--field",
                "status=draft",
                "--field",
                f"sources={MISSING}",
            ]
        )
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    rendered = captured.out + captured.err

    assert code == 1
    assert "UNRESOLVED_SOURCE_CITATION" in rendered
    assert "do not resolve to captured material" in rendered
    assert "Capture the original material" in rendered
