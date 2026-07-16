from dataclasses import dataclass

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
