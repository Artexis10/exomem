from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "infra/scripts/repair_hosted_legacy_permissions.py"


def _module():
    assert _SCRIPT.is_file(), "permission repair helper is not implemented"
    spec = importlib.util.spec_from_file_location("repair_hosted_legacy_permissions", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _marker(cell_id: str, vault_id: str, kind: str) -> bytes:
    return (
        json.dumps(
            {
                "binding_version": 2,
                "cell_id": cell_id,
                "log_root": "/var/lib/exomem/logs",
                "root_kind": kind,
                "runtime_gid": os.getgid(),
                "runtime_uid": os.getuid(),
                "state_root": "/var/lib/exomem/state",
                "vault_id": vault_id,
                "vault_root": "/var/lib/exomem/vault",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _volume(tmp_path: Path) -> tuple[Path, str, str]:
    cell_id = "cell-alpha"
    vault_id = "vault-alpha"
    volume = tmp_path / "volume"
    volume.mkdir(mode=0o775, parents=True)
    for kind in ("vault", "state", "log"):
        root = volume / ("logs" if kind == "log" else kind)
        root.mkdir(mode=0o770)
        (root / ".exomem-hosted-cell.json").write_bytes(_marker(cell_id, vault_id, kind))
        nested = root / "nested"
        nested.mkdir(mode=0o770)
        payload = nested / "payload.bin"
        payload.write_bytes((kind + "-payload").encode())
        payload.chmod(0o660)
        root.chmod(0o2770)
    return volume, cell_id, vault_id


def _repair(module, volume: Path, cell_id: str, vault_id: str):
    return module.repair_volume_permissions(
        volume_root=volume,
        cell_id=cell_id,
        vault_id=vault_id,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
        max_entries=100,
        max_bytes=1024 * 1024,
    )


def test_repair_converges_only_bound_subtrees_and_preserves_every_byte(tmp_path: Path) -> None:
    module = _module()
    volume, cell_id, vault_id = _volume(tmp_path)
    volume_before = volume.stat()

    receipt = _repair(module, volume, cell_id, vault_id)

    assert receipt["status"] == "repaired"
    assert receipt["content_sha256_before"] == receipt["content_sha256_after"]
    assert receipt["root_count"] == 3
    assert receipt["entry_count"] == 12
    assert set(receipt) == {
        "status",
        "content_sha256_before",
        "content_sha256_after",
        "root_count",
        "entry_count",
        "total_bytes",
    }
    assert (volume.stat().st_uid, volume.stat().st_gid, volume.stat().st_mode & 0o7777) == (
        volume_before.st_uid,
        volume_before.st_gid,
        volume_before.st_mode & 0o7777,
    )
    for root_name in ("vault", "state", "logs"):
        root = volume / root_name
        assert root.stat().st_mode & 0o7777 == 0o700
        assert (root / "nested").stat().st_mode & 0o7777 == 0o700
        assert (root / "nested/payload.bin").stat().st_mode & 0o7777 == 0o600
        assert (root / ".exomem-hosted-cell.json").stat().st_mode & 0o7777 == 0o600


def test_repair_refuses_a_foreign_marker_before_any_metadata_change(tmp_path: Path) -> None:
    module = _module()
    volume, cell_id, vault_id = _volume(tmp_path)
    first = volume / "vault"
    (volume / "state/.exomem-hosted-cell.json").write_bytes(
        _marker("another-cell", vault_id, "state")
    )

    with pytest.raises(module.RepairRefusal, match="binding marker is invalid"):
        _repair(module, volume, cell_id, vault_id)

    assert first.stat().st_mode & 0o7777 == 0o2770


def test_repair_refuses_symlinks_and_hardlinks_before_any_metadata_change(
    tmp_path: Path,
) -> None:
    module = _module()
    for unsafe in ("symlink", "hardlink"):
        volume, cell_id, vault_id = _volume(tmp_path / unsafe)
        first = volume / "vault"
        target = volume / "logs/nested/payload.bin"
        if unsafe == "symlink":
            (volume / "logs/nested/unsafe").symlink_to(target)
        else:
            os.link(target, volume / "logs/nested/unsafe")

        with pytest.raises(module.RepairRefusal, match="unsafe filesystem entry"):
            _repair(module, volume, cell_id, vault_id)

        assert first.stat().st_mode & 0o7777 == 0o2770


def test_repair_is_idempotent(tmp_path: Path) -> None:
    module = _module()
    volume, cell_id, vault_id = _volume(tmp_path)

    first = _repair(module, volume, cell_id, vault_id)
    second = _repair(module, volume, cell_id, vault_id)

    assert second == first
