from dataclasses import dataclass

import pytest
from pydantic import RootModel

from scripts import e2e_product_loop


class _Hit(RootModel[dict[str, object]]):
    pass


@dataclass
class _SchemaHit:
    path: str


def test_unwrap_result_normalizes_nested_typed_mcp_values() -> None:
    value = {
        "hits": [
            _Hit({"path": "Knowledge Base/example.md"}),
            _SchemaHit(path="Knowledge Base/generated.md"),
        ]
    }

    assert e2e_product_loop._unwrap_result(value) == {
        "hits": [
            {"path": "Knowledge Base/example.md"},
            {"path": "Knowledge Base/generated.md"},
        ]
    }


def test_mutation_diagnostics_requires_and_unwraps_committed_full_envelope() -> None:
    diagnostics = {"saved": {"content_hash": "after"}}

    assert e2e_product_loop._mutation_diagnostics(
        {
            "ok": True,
            "status": "committed",
            "mutated": True,
            "request_id": "request-1",
            "diagnostics": diagnostics,
        },
        operation="schema_memory",
    ) == diagnostics


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"graph_status": "current"}, {"graph_status": "current"}),
        (
            {
                "ok": True,
                "status": "committed",
                "mutated": True,
                "diagnostics": {"graph_status": "refreshed"},
            },
            {"graph_status": "refreshed"},
        ),
    ],
)
def test_maintenance_diagnostics_accepts_raw_noop_or_committed_full_result(
    result: dict[str, object],
    expected: dict[str, object],
) -> None:
    assert e2e_product_loop._maintenance_diagnostics(
        result,
        operation="maintain_memory",
    ) == expected


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "status": "retryable", "mutated": False, "diagnostics": {}},
        {"ok": True, "status": "committed", "mutated": True},
    ],
)
def test_mutation_diagnostics_rejects_noncommitted_or_nonfull_results(
    result: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="remember mutation"):
        e2e_product_loop._mutation_diagnostics(result, operation="remember")
