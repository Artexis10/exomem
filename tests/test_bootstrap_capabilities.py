"""Active-surface bootstrap conformance across public adapters."""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands
from exomem import server as server_module
from exomem.__main__ import _simple_cli_action_names, main
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface

PROFILES = ("compact", "full", "diagnostics")
TIER2_PRODUCT_TOOLS = {"manage_memory_file", "query_dataset", "read_media"}
KNOWN_CALLABLE_NAMES = (
    set(commands.PRODUCT_PUBLIC_NAMES)
    | {command.name for command in commands.COMMANDS}
    | set(commands.PRODUCT_ROUTE_HELPERS)
    | set(commands.simple_action_names())
)
_STRUCTURED_TOOL_LISTS = {
    "advanced",
    "advanced_tools",
    "available_product_tools",
    "common_tools",
    "exported_aliases",
    "first_run_safe",
    "hand_registered_tools",
    "primary",
    "primary_tools",
}


def _call_mcp(mcp, profile: str) -> dict:
    result = asyncio.run(
        mcp.call_tool("bootstrap", {"profile": profile}, run_middleware=False)
    )
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for content in getattr(result, "content", ()):
        text = getattr(content, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("bootstrap returned no structured payload")


def _extract_advertised_tool_refs(
    payload: object,
    *,
    product_names: set[str],
    known_names: set[str] | None = None,
) -> set[str]:
    """Recursively find structured and call-shaped advertised tool references.

    Legacy aliases such as ``get`` and ``find`` are ordinary prose, so they count
    only in structured tool positions, exact list entries, or function-call form.
    Product names containing underscores are specific enough to match in prose.
    """

    known = known_names or product_names
    refs: set[str] = set()

    def text_refs(value: str) -> None:
        for name in known:
            if ("_" in name or name in product_names) and re.search(
                rf"(?<!\w){re.escape(name)}(?!\w)", value
            ):
                refs.add(name)
            elif re.search(rf"(?<!\w){re.escape(name)}\s*\(", value):
                refs.add(name)

    def walk(value: object, *, key: str | None = None) -> None:
        if isinstance(value, dict):
            if key in {"product_commands", "tool_catalog"}:
                routes = value.get("routes")
                if isinstance(routes, dict):
                    refs.update(set(routes) & known)
                    for route_names in routes.values():
                        if isinstance(route_names, (list, tuple)):
                            refs.update(
                                item
                                for item in route_names
                                if isinstance(item, str) and item in known
                            )
            for child_key, child in value.items():
                if child_key == "tool" and isinstance(child, str) and child in known:
                    refs.add(child)
                    continue
                if child_key == "route" and isinstance(child, str) and child in known:
                    refs.add(child)
                    continue
                if child_key in _STRUCTURED_TOOL_LISTS and isinstance(
                    child, (list, tuple)
                ):
                    refs.update(
                        item for item in child if isinstance(item, str) and item in known
                    )
                walk(child, key=str(child_key))
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key=key)
            return
        if isinstance(value, str):
            text_refs(value)

    walk(payload)
    return refs


def _assert_conforms(
    payload: dict,
    *,
    expected_product_tuple: tuple[str, ...],
    exported_names: set[str],
    expected_surface: str,
    expected_profile: str,
    tier2_enabled: bool,
    aliases: set[str] | None = None,
) -> None:
    active = payload["active_capabilities"]
    assert active["surface"] == expected_surface
    assert active["profile"] == expected_profile
    assert active["tier2_policy"] == ("enabled" if tier2_enabled else "disabled")
    assert active["available_product_tools"] == sorted(expected_product_tuple)
    assert re.fullmatch(r"[0-9a-f]{64}", active["active_capability_sha256"])
    assert set(active["exported_aliases"]) == (aliases or set())

    refs = _extract_advertised_tool_refs(
        payload,
        product_names=set(commands.PRODUCT_PUBLIC_NAMES),
        known_names=KNOWN_CALLABLE_NAMES,
    )
    assert refs <= exported_names, sorted(refs - exported_names)

    canonical = payload["server"]["canonical_mcp_tool_surface"]
    assert canonical["scope"] == "packaged-full-mcp-discovery"
    assert canonical["sha256"] == payload["server"][
        "published_mcp_tool_surface_sha256"
    ]
    assert payload["server"]["published_mcp_tool_surface_scope"] == (
        "packaged-full-mcp-discovery"
    )


def _configure_server(
    monkeypatch: pytest.MonkeyPatch,
    vault: Path,
    *,
    tier2_enabled: bool,
    legacy: bool = False,
) -> None:
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    if tier2_enabled:
        monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    if legacy:
        monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    else:
        monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)


@pytest.mark.parametrize("tier2_enabled", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_all_mcp_bootstrap_profiles_match_live_tools(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier2_enabled: bool,
    legacy: bool,
) -> None:
    _configure_server(
        monkeypatch, vault, tier2_enabled=tier2_enabled, legacy=legacy
    )
    mcp = server_module.build_server(require_auth=False)
    live_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    product_tuple = tuple(
        command.name
        for command in commands.product_commands_for(
            "mcp", expose_tier2=tier2_enabled
        )
    )
    aliases = live_names - set(product_tuple)

    for profile in PROFILES:
        payload = _call_mcp(mcp, profile)
        _assert_conforms(
            payload,
            expected_product_tuple=product_tuple,
            exported_names=live_names,
            expected_surface="mcp",
            expected_profile="product-with-legacy-aliases" if legacy else "product",
            tier2_enabled=tier2_enabled,
            aliases=aliases,
        )
        refs = _extract_advertised_tool_refs(
            payload,
            product_names=set(commands.PRODUCT_PUBLIC_NAMES),
            known_names=KNOWN_CALLABLE_NAMES,
        )
        if not legacy:
            assert {"note", "find", "get", "transfer_token"}.isdisjoint(refs)
        if not tier2_enabled:
            assert TIER2_PRODUCT_TOOLS.isdisjoint(
                _extract_advertised_tool_refs(
                    payload,
                    product_names=set(commands.PRODUCT_PUBLIC_NAMES),
                    known_names=KNOWN_CALLABLE_NAMES,
                )
            )


@pytest.mark.parametrize("tier2_enabled", [True, False])
def test_all_rest_bootstrap_profiles_match_openapi_operations(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier2_enabled: bool,
) -> None:
    _configure_server(monkeypatch, vault, tier2_enabled=tier2_enabled)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    mcp = server_module.build_server(require_auth=False)
    client = TestClient(mcp.http_app())
    auth = {"Authorization": "Bearer sekret"}
    openapi = client.get("/api/openapi.json", headers=auth).json()
    exported = {
        operation["post"]["operationId"]
        for operation in openapi["paths"].values()
    }
    product_tuple = tuple(
        command.name
        for command in commands.product_commands_for(
            "rest", expose_tier2=tier2_enabled
        )
    )

    for profile in PROFILES:
        response = client.post(
            "/api/bootstrap", json={"profile": profile}, headers=auth
        )
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        _assert_conforms(
            payload,
            expected_product_tuple=product_tuple,
            exported_names=exported,
            expected_surface="rest",
            expected_profile="openapi",
            tier2_enabled=tier2_enabled,
        )
        assert "read_media" not in _extract_advertised_tool_refs(
            payload,
            product_names=set(commands.PRODUCT_PUBLIC_NAMES),
            known_names=KNOWN_CALLABLE_NAMES,
        )


@pytest.mark.parametrize("tier2_enabled", [True, False])
def test_all_cli_bootstrap_profiles_match_parser_registry(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tier2_enabled: bool,
) -> None:
    _configure_server(monkeypatch, vault, tier2_enabled=tier2_enabled)
    product_tuple = tuple(
        command.name
        for command in commands.product_commands_for(
            "cli", expose_tier2=tier2_enabled
        )
    )
    aliases = set(_simple_cli_action_names())
    assert aliases == set(commands.simple_action_names())
    assert "remember" in aliases & set(product_tuple)
    exported = set(product_tuple) | aliases

    for profile in PROFILES:
        assert main(["bootstrap", "--profile", profile, "--json"]) == 0
        output = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(output)["data"]
        _assert_conforms(
            payload,
            expected_product_tuple=product_tuple,
            exported_names=exported,
            expected_surface="cli",
            expected_profile="product",
            tier2_enabled=tier2_enabled,
            aliases=aliases,
        )
        assert "read_media" not in _extract_advertised_tool_refs(
            payload,
            product_names=set(commands.PRODUCT_PUBLIC_NAMES),
            known_names=KNOWN_CALLABLE_NAMES,
        )


def test_narrow_surface_filters_every_profile_without_deleting_useful_routes(
    vault: Path,
) -> None:
    descriptor = ActiveSurfaceDescriptor(
        surface="test",
        profile="narrow",
        tier2_enabled=False,
        product_commands=("bootstrap", "ask_memory", "remember"),
    )

    with active_surface(descriptor):
        payloads = [commands.op_bootstrap(vault, profile=profile) for profile in PROFILES]

    for payload in payloads:
        refs = _extract_advertised_tool_refs(
            payload,
            product_names=set(commands.PRODUCT_PUBLIC_NAMES),
            known_names=KNOWN_CALLABLE_NAMES,
        )
        assert refs <= descriptor.callable_commands
        assert {"review_memory", "read_media"}.isdisjoint(refs)
        assert payload["simple_actions"]["ask"]["route"]["tool"] == "ask_memory"
        assert payload["simple_actions"]["remember"]["route"]["tool"] == "remember"


def test_direct_python_bootstrap_defaults_to_canonical_full_mcp(vault: Path) -> None:
    payload = commands.op_bootstrap(vault)
    expected = tuple(
        command.name for command in commands.product_commands_for("mcp", expose_tier2=True)
    )
    _assert_conforms(
        payload,
        expected_product_tuple=expected,
        exported_names=set(expected),
        expected_surface="mcp",
        expected_profile="canonical-full-product",
        tier2_enabled=True,
    )


def test_bootstrap_guidance_uses_product_writer_not_legacy_note(vault: Path) -> None:
    for profile in PROFILES:
        serialized = json.dumps(commands.op_bootstrap(vault, profile=profile))
        assert "note()" not in serialized
        assert "remember()" in serialized


def test_active_surface_context_is_nested_and_concurrent_safe(vault: Path) -> None:
    outer = ActiveSurfaceDescriptor(
        surface="test", profile="outer", tier2_enabled=False,
        product_commands=("bootstrap", "ask_memory"),
        exported_aliases=("ask",),
    )
    inner = ActiveSurfaceDescriptor(
        surface="test", profile="inner", tier2_enabled=False,
        product_commands=("bootstrap", "remember"),
        exported_aliases=("capture",),
    )

    with active_surface(outer):
        before = commands.op_bootstrap(vault)["active_capabilities"]
        with active_surface(inner):
            nested = commands.op_bootstrap(vault)["active_capabilities"]
        after = commands.op_bootstrap(vault)["active_capabilities"]

    assert before["profile"] == after["profile"] == "outer"
    assert nested["profile"] == "inner"

    def render(
        descriptor: ActiveSurfaceDescriptor,
    ) -> tuple[str, list[str], list[str]]:
        with active_surface(descriptor):
            active = commands.op_bootstrap(vault)["active_capabilities"]
            return (
                active["profile"],
                active["available_product_tools"],
                active["exported_aliases"],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(render, (outer, inner)))

    assert results == [
        ("outer", ["ask_memory", "bootstrap"], ["ask"]),
        ("inner", ["bootstrap", "remember"], ["capture"]),
    ]
