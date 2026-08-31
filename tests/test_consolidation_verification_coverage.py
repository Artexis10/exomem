from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import commands
from exomem.governance import consolidation_verification_coverage, egress


def _command_registry() -> dict[str, commands.Command]:
    return {command.name: command for command in commands.COMMANDS}


def _product_registry() -> dict[str, commands.Command]:
    return {command.name: command for command in commands.PRODUCT_COMMANDS}


def test_real_content_registries_generate_complete_closed_world_inventory() -> None:
    canonical = _command_registry()
    products = _product_registry()
    inventory = consolidation_verification_coverage.build_coverage_inventory(
        canonical, product_registry=products
    )
    by_id = {branch.branch_id: branch for branch in inventory}

    expected_commands = {
        f"command:{name}"
        for name, command in canonical.items()
        if name in egress.content_returning_commands(canonical) or not command.read_only
    }
    expected_products = {
        f"product:{name}"
        for name, command in products.items()
        if name in egress.content_returning_commands(products) or not command.read_only
    }
    assert {branch_id for branch_id in by_id if branch_id.startswith("command:")} == (
        expected_commands
    )
    assert {branch_id for branch_id in by_id if branch_id.startswith("product:")} == (
        expected_products
    )
    assert by_id["command:note"].seal_disposition == "mutation"
    assert by_id["command:note"].probe_disposition == "seal-only"
    assert by_id["product:remember"].receipt_outcome == "mutation"
    assert by_id["product:compile_source"].projector_adapter == "structure"
    assert by_id["product:query_dataset"].receipt_outcome == "structure"
    assert by_id["product:read_media"].tombstone_gate == "frames"
    assert by_id["command:list_trash"].tombstone_gate == "structure"
    assert "selector:record_memory.action=query" in by_id
    assert "route:server_transfer:_download" in by_id
    assert all(
        branch.probe_disposition
        and branch.seal_disposition
        and branch.projector_adapter
        and branch.receipt_outcome
        and branch.tombstone_gate
        for branch in inventory
    )


def test_new_content_command_without_release_capabilities_fails_closed() -> None:
    registry = _command_registry()
    registry["future_content"] = SimpleNamespace(read_only=True)

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable,
        match="CONSOLIDATION_VERIFICATION_COVERAGE_UNAVAILABLE",
    ):
        consolidation_verification_coverage.build_coverage_inventory(registry)


def test_new_selector_without_tombstone_disposition_fails_closed() -> None:
    selectors = egress.selector_capability_registry()
    selectors[("future_content", "mode")] = {"raw": {"outcome": "binary", "tombstone": ""}}

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable
    ):
        consolidation_verification_coverage.build_coverage_inventory(
            _command_registry(),
            selector_capabilities=selectors,
        )


def test_new_selector_with_invented_adapter_fails_closed() -> None:
    selectors = egress.selector_capability_registry()
    selectors[("future_content", "mode")] = {
        "raw": {"outcome": "invented-adapter", "tombstone": "invented-gate"}
    }

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable
    ):
        consolidation_verification_coverage.build_coverage_inventory(
            _command_registry(),
            selector_capabilities=selectors,
        )


def test_new_non_command_route_without_probe_disposition_fails_closed() -> None:
    routes = egress.non_command_route_capability_registry()
    routes["future-download"] = {
        "projector": "binary",
        "receipt": "binary",
        "tombstone": "binary",
        "seal": "transfer",
    }

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable
    ):
        consolidation_verification_coverage.build_coverage_inventory(
            _command_registry(),
            route_capabilities=routes,
        )


def test_unknown_route_disposition_fails_closed() -> None:
    routes = egress.non_command_route_capability_registry()
    routes["future-resource"] = {
        "projector": "artifact-reference",
        "receipt": "artifact-reference",
        "tombstone": "artifact-reference",
        "seal": "read",
        "probe": "owner-bypass",
    }

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable
    ):
        consolidation_verification_coverage.build_coverage_inventory(
            _command_registry(),
            route_capabilities=routes,
        )


def test_unknown_route_adapter_fails_closed() -> None:
    routes = egress.non_command_route_capability_registry()
    routes["future-resource"] = {
        "projector": "invented-projector",
        "receipt": "invented-outcome",
        "tombstone": "invented-gate",
        "seal": "read",
        "probe": "positive-negative",
    }

    with pytest.raises(
        consolidation_verification_coverage.ConsolidationVerificationCoverageUnavailable
    ):
        consolidation_verification_coverage.build_coverage_inventory(
            _command_registry(),
            route_capabilities=routes,
        )


def test_complete_explicit_route_joins_inventory() -> None:
    routes = egress.non_command_route_capability_registry()
    routes["future-resource"] = {
        "projector": "artifact-reference",
        "receipt": "artifact-reference",
        "tombstone": "artifact-reference",
        "seal": "read",
        "probe": "positive-negative",
    }
    inventory = consolidation_verification_coverage.build_coverage_inventory(
        _command_registry(),
        route_capabilities=routes,
    )

    assert "route:future-resource" in {branch.branch_id for branch in inventory}


def _registered_surface_route_ids() -> frozenset[str]:
    source = Path(__file__).resolve().parent.parent / "src" / "exomem"
    registrations: set[str] = set()
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = (
                decorator
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"custom_route", "prompt", "resource"}
            )
            for _decorator in decorators:
                route_id = f"{path.stem}:{node.name}"
                assert route_id not in registrations, (
                    f"surface route id is ambiguous; split the handler: {route_id}"
                )
                registrations.add(route_id)
    return frozenset(registrations)


def test_every_registered_surface_route_has_an_explicit_disposition() -> None:
    registrations = _registered_surface_route_ids()
    dispositions = egress.surface_route_disposition_registry()

    egress.assert_surface_route_registrations_covered(registrations)
    assert registrations == frozenset(dispositions)
    assert dispositions["server_hosted:_export"] == "operator-control"
    assert dispositions["server_hosted:_export_download"] == "operator-control"
    assert not {
        "server_hosted:_export",
        "server_hosted:_export_download",
    } & set(egress.non_command_route_capability_registry())


def test_new_real_surface_registration_fails_the_release_gate() -> None:
    registrations = _registered_surface_route_ids() | {"future_server:new_content_route"}

    with pytest.raises(RuntimeError, match="RELEASE_COVERAGE_MISSING"):
        egress.assert_surface_route_registrations_covered(registrations)


def test_command_registry_runs_the_coverage_gate_at_import() -> None:
    assert "build_coverage_inventory" in inspect.getsource(commands)
