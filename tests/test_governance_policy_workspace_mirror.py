from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from exomem import reserved_paths, writer_lease
from exomem.governance import policy

POLICY_A = b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n"
POLICY_B = b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAW\npaths:\n  - Sources/**\n"
POLICY_C = b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAX\npaths:\n  - Evidence/**\n"


@pytest.fixture(autouse=True)
def _lease_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    state = tmp_path / "lease-state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _workspace(vault: Path) -> Path:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True)
    return root


def _mirror(
    vault: Path,
    *,
    reviewed: policy.AuthoringSnapshot,
    target_documents: tuple[tuple[str, bytes], ...],
    barrier: Callable[[str, str], None] | None = None,
) -> str:
    with reserved_paths._owner_authority_scope("govern_memory"):
        return policy.mirror_authoring_workspace(
            vault,
            reviewed=reviewed,
            target_documents=target_documents,
            barrier=barrier,
        )


def test_exact_workspace_mirror_replaces_adds_and_removes_under_one_guard(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    root = _workspace(vault)
    (root / "scopes" / "a.yaml").write_bytes(POLICY_A)
    (root / "scopes" / "remove.yaml").write_bytes(POLICY_B)
    reviewed = policy.observe_authoring_snapshot(vault)
    assert reviewed is not None
    target = (
        ("scopes/a.yaml", POLICY_B),
        ("scopes/added.yaml", POLICY_C),
    )

    assert (
        _mirror(
            vault,
            reviewed=reviewed,
            target_documents=target,
        )
        == "complete"
    )

    final = policy.observe_authoring_snapshot(vault)
    assert final is not None
    assert final.documents == target
    assert not (root / "scopes" / "remove.yaml").exists()


def test_exact_workspace_mirror_refuses_unreviewed_drift_without_writes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    root = _workspace(vault)
    path = root / "scopes" / "a.yaml"
    path.write_bytes(POLICY_A)
    reviewed = policy.observe_authoring_snapshot(vault)
    assert reviewed is not None
    path.write_bytes(POLICY_C)

    assert (
        _mirror(
            vault,
            reviewed=reviewed,
            target_documents=(("scopes/a.yaml", POLICY_B),),
        )
        == "diverged"
    )
    assert path.read_bytes() == POLICY_C


def test_exact_workspace_mirror_reports_each_effect_to_the_caller(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    root = _workspace(vault)
    (root / "scopes" / "a.yaml").write_bytes(POLICY_A)
    reviewed = policy.observe_authoring_snapshot(vault)
    assert reviewed is not None
    observed: list[tuple[str, str]] = []

    result = _mirror(
        vault,
        reviewed=reviewed,
        target_documents=(("scopes/a.yaml", POLICY_B),),
        barrier=lambda phase, relative: observed.append((phase, relative)),
    )

    assert result == "complete"
    assert observed == [
        ("before_write", "scopes/a.yaml"),
        ("after_write", "scopes/a.yaml"),
    ]


def test_exact_workspace_mirror_requires_owner_authority_before_writing(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    root = _workspace(vault)
    path = root / "scopes" / "a.yaml"
    path.write_bytes(POLICY_A)
    reviewed = policy.observe_authoring_snapshot(vault)
    assert reviewed is not None

    with pytest.raises(RuntimeError, match="lacks governance owner authority"):
        policy.mirror_authoring_workspace(
            vault,
            reviewed=reviewed,
            target_documents=(("scopes/a.yaml", POLICY_B),),
        )

    assert path.read_bytes() == POLICY_A
