from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import record_governance
from exomem.governance import egress


def test_query_loads_release_policy_once_for_a_thousand_authorization_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-row authorizer must reuse the query boundary's policy snapshot."""
    policy = object()
    loads = 0

    def load(root: Path) -> object:
        nonlocal loads
        assert root == tmp_path
        loads += 1
        return policy

    manifest = SimpleNamespace(
        semantic_profile="records",
        storage=SimpleNamespace(source="Knowledge Base/Records/Items"),
        schema=SimpleNamespace(fields={}),
        record_presentation=None,
    )

    def resolve(*_args: object, **kwargs: object) -> object:
        assert kwargs["policy"] is policy
        return manifest

    def release(
        root: Path, _relative: str, *, policy: object | None = None, **_kwargs: object
    ) -> str:
        assert root == tmp_path
        if policy is None:
            policy = load(root)
        assert policy is expected_policy
        return egress.LEVEL_FULL

    def query(*_args: object, **kwargs: object) -> SimpleNamespace:
        authorize = kwargs["authorize_path"]
        for number in range(1000):
            assert authorize(f"Knowledge Base/Records/Items/{number}.md")
        return SimpleNamespace(rows=())

    expected_policy = policy
    monkeypatch.setattr(egress.policy_module, "load", load)
    monkeypatch.setattr(record_governance, "_resolve_released_collection", resolve)
    monkeypatch.setattr(egress, "release_level_for_path_only", release)
    monkeypatch.setattr(record_governance.record_formats, "query_collection", query)

    record_governance.query_collection(tmp_path, "Knowledge Base/Records/_collection.md")

    assert loads == 1
