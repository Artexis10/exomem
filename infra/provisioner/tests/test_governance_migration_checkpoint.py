from __future__ import annotations

from dataclasses import replace

import pytest

from exomem_provisioner.driver import DriverTerminal, EffectContext


def _module():
    from exomem_provisioner import governance_migration_checkpoint

    return governance_migration_checkpoint


def _context():
    return EffectContext("operation-1", "provider-operation-1", "tenant-1", "cell-1", 7)


def _binding(context=None, **changes):
    return _module().migration_binding(
        context or _context(),
        **{
            "pvc_uid": "pvc-1",
            "runtime_image": "ghcr.io/example/runtime@sha256:" + "a" * 64,
            **changes,
        },
    )


@pytest.mark.parametrize(
    "phase", ["inspect", "prepare", "enroll", "commit", "complete", "confirmed"]
)
def test_checkpoint_round_trips_inside_existing_durable_bound(phase):
    module = _module()
    checkpoint = module.MigrationCheckpoint(
        phase=phase,
        vault_fingerprint="a" * 64,
        binding=_binding(),
        source_store_digest=None if phase == "inspect" else "b" * 64,
        plan_digest="c" * 64 if phase not in {"inspect", "prepare"} else None,
    )
    encoded = checkpoint.encode()
    assert len(encoded) <= 256
    assert module.MigrationCheckpoint.decode(encoded) == checkpoint
    if phase not in {"inspect", "prepare"}:
        assert len(encoded) == 244
    assert "tenant-1" not in encoded and "pvc-1" not in encoded


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", "operation-2"),
        ("provider_operation_id", "provider-operation-2"),
        ("tenant_id", "tenant-2"),
        ("cell_id", "cell-2"),
        ("fence_generation", 8),
    ],
)
def test_checkpoint_binds_exact_current_operation_and_fence(field, value):
    assert _binding(replace(_context(), **{field: value})) != _binding()


def test_checkpoint_binds_pvc_and_pinned_image_but_not_retry_checkpoint():
    assert _binding(pvc_uid="pvc-2") != _binding()
    assert _binding(runtime_image="ghcr.io/example/runtime@sha256:" + "b" * 64) != _binding()
    assert _binding(replace(_context(), checkpoint="retry-progress")) == _binding()


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "unknown"},
        {"phase": "prepare"},
        {"phase": "enroll", "source_store_digest": "b" * 64},
        {"phase": "commit", "plan_digest": "c" * 64},
        {"source_store_digest": "b" * 64},
        {"plan_digest": "c" * 64},
        {"vault_fingerprint": "A" * 64},
        {"vault_fingerprint": "a" * 64 + "\n"},
        {"binding": "x" * 42},
        {"binding": "!" * 43},
        {"binding": "_" * 43},
    ],
)
def test_checkpoint_rejects_noncanonical_or_phase_inconsistent_fields(change):
    with pytest.raises(DriverTerminal, match="^PROVISIONER_CHECKPOINT_INVALID$"):
        _module().MigrationCheckpoint(
            **{
                "phase": "inspect",
                "vault_fingerprint": "a" * 64,
                "binding": _binding(),
                **change,
            }
        )


@pytest.mark.parametrize("raw", [None, {}, "", "gm2:i:a:b:-:-", "gm1:i:a:b:-:-", "x" * 257])
def test_checkpoint_decoder_refuses_foreign_or_malformed_progress(raw):
    with pytest.raises(DriverTerminal, match="^PROVISIONER_CHECKPOINT_INVALID$"):
        _module().MigrationCheckpoint.decode(raw)


def test_checkpoint_decoder_rejects_extra_fields_and_whitespace():
    module = _module()
    raw = module.MigrationCheckpoint("inspect", "a" * 64, _binding()).encode()
    for malformed in (raw + ":extra", raw + "\n", " " + raw, raw.replace(":i:", ":inspect:")):
        with pytest.raises(DriverTerminal, match="^PROVISIONER_CHECKPOINT_INVALID$"):
            module.MigrationCheckpoint.decode(malformed)
