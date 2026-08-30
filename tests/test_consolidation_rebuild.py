from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem.governance import consolidation_rebuild

CANONICAL = "Knowledge Base/Notes/canonical.md"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot(vault_root: Path) -> str:
    return _sha((vault_root / CANONICAL).read_bytes())


def _terminal(
    component: str,
    context: consolidation_rebuild.DerivativeRebuildContext,
) -> consolidation_rebuild.DerivativeRebuildTerminal:
    return consolidation_rebuild.DerivativeRebuildTerminal(
        schema=consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
        component=component,
        canonical_census_digest=context.canonical_census_digest,
        artifact_fingerprint=_sha(f"rebuilt:{component}".encode()),
    )


def test_rebuild_runs_the_closed_component_set_after_every_batch_is_final(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "destination"
    canonical = vault / CANONICAL
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"destination canonical bytes")
    expected = _snapshot(vault)
    calls: list[tuple[str, Path, str]] = []

    def rebuild(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        calls.append((component, context.vault_root, context.canonical_census_digest))
        return _terminal(component, context)

    result = consolidation_rebuild.rebuild_destination_derivatives(
        vault_root=vault,
        expected_canonical_census_digest=expected,
        expected_batch_count=3,
        committed_batch_ordinals=(0, 1, 2),
        snapshot_census=_snapshot,
        rebuild_component=rebuild,
    )

    assert tuple(component for component, _root, _digest in calls) == (
        "media",
        "lexical",
        "embedding",
        "semantic-unit",
        "graph",
        "freshness",
        "identity",
        "review",
    )
    assert all(root == vault and digest == expected for _component, root, digest in calls)
    assert result.canonical_census_digest == expected
    assert result.completed_components == tuple(component for component, *_ in calls)
    assert canonical.read_bytes() == b"destination canonical bytes"


@pytest.mark.parametrize(
    "committed",
    ((), (0,), (0, 2), (1, 0), (0, 1, 2, 3)),
)
def test_rebuild_refuses_before_the_exact_content_partition_is_final(
    tmp_path: Path,
    committed: tuple[int, ...],
) -> None:
    vault = tmp_path / "destination"
    canonical = vault / CANONICAL
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"destination canonical bytes")
    calls: list[str] = []

    with pytest.raises(
        consolidation_rebuild.DerivativeRebuildUnavailable,
        match="consolidation derivative rebuild is unavailable",
    ):
        consolidation_rebuild.rebuild_destination_derivatives(
            vault_root=vault,
            expected_canonical_census_digest=_snapshot(vault),
            expected_batch_count=3,
            committed_batch_ordinals=committed,
            snapshot_census=_snapshot,
            rebuild_component=lambda component, _context: calls.append(component),
        )

    assert calls == []


def test_rebuild_stops_on_the_first_canonical_byte_change(tmp_path: Path) -> None:
    vault = tmp_path / "destination"
    canonical = vault / CANONICAL
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"destination canonical bytes")
    expected = _snapshot(vault)
    calls: list[str] = []

    def rebuild(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        calls.append(component)
        if component == "semantic-unit":
            canonical.write_bytes(b"mutated by a broken rebuild")
        return _terminal(component, context)

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        consolidation_rebuild.rebuild_destination_derivatives(
            vault_root=vault,
            expected_canonical_census_digest=expected,
            expected_batch_count=1,
            committed_batch_ordinals=(0,),
            snapshot_census=_snapshot,
            rebuild_component=rebuild,
        )

    assert calls == ["media", "lexical", "embedding", "semantic-unit"]


@pytest.mark.parametrize(
    "changed_field",
    ("schema", "component", "canonical_census_digest", "artifact_fingerprint"),
)
def test_rebuild_rejects_a_malformed_or_unbound_component_terminal(
    tmp_path: Path,
    changed_field: str,
) -> None:
    vault = tmp_path / "destination"
    canonical = vault / CANONICAL
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"destination canonical bytes")
    expected = _snapshot(vault)

    def rebuild(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        values = {
            "schema": consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
            "component": component,
            "canonical_census_digest": context.canonical_census_digest,
            "artifact_fingerprint": _sha(component.encode()),
        }
        values[changed_field] = {
            "schema": "wrong/v1",
            "component": "wrong",
            "canonical_census_digest": "f" * 64,
            "artifact_fingerprint": "not-a-digest",
        }[changed_field]
        return consolidation_rebuild.DerivativeRebuildTerminal(**values)

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        consolidation_rebuild.rebuild_destination_derivatives(
            vault_root=vault,
            expected_canonical_census_digest=expected,
            expected_batch_count=1,
            committed_batch_ordinals=(0,),
            snapshot_census=_snapshot,
            rebuild_component=rebuild,
        )


def test_rebuild_has_no_source_database_installation_input(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_database = source / "Knowledge Base/.lexical.sqlite"
    source_database.parent.mkdir(parents=True)
    source_database.write_bytes(b"source database sentinel")
    canonical = destination / CANONICAL
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"destination canonical bytes")
    expected = _snapshot(destination)

    def rebuild(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        assert not hasattr(context, "source_root")
        assert not hasattr(context, "source_database")
        derived = destination / "Knowledge Base" / f".{component}.derived"
        derived.write_bytes(canonical.read_bytes() + component.encode())
        return consolidation_rebuild.DerivativeRebuildTerminal(
            schema=consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
            component=component,
            canonical_census_digest=context.canonical_census_digest,
            artifact_fingerprint=_sha(derived.read_bytes()),
        )

    consolidation_rebuild.rebuild_destination_derivatives(
        vault_root=destination,
        expected_canonical_census_digest=expected,
        expected_batch_count=1,
        committed_batch_ordinals=(0,),
        snapshot_census=_snapshot,
        rebuild_component=rebuild,
    )

    assert source_database.read_bytes() == b"source database sentinel"
    assert not (destination / "Knowledge Base/.lexical.sqlite").exists()
