"""The anchors an import or export Job checks its grant and presigned URLs against (contain-cloud-controller-compromise D2/D4).

The cell reads them by name (src/exomem/cloud_grant.py); only the Jobs that move a cell's data receive them.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from cellctl import main
from cellctl.capacity import GIB
from cellctl.exports import ExportRow, ExportSize
from cellctl.imports import ImportRow
from cellctl.manifests import (
    CellManifestSpec,
    render_export_job,
    render_import_job,
    render_statefulset,
)

from .test_artifact_transport import CELL_ID, _digest
from .test_manifests import _spec
from .test_reconcile import _cluster_config

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MIB = 1024 * 1024
# Shared with the cell's own names (src/exomem/cloud_grant.py).
ENDPOINT_ENV = "EXOMEM_CLOUD_OBJECT_STORE_ENDPOINT"
BUCKET_ENV = "EXOMEM_CLOUD_OBJECT_STORE_BUCKET"
GRANT_KEY_ENV = "EXOMEM_CLOUD_GRANT_PUBLIC_KEY"


def _public_pem(private_key=None) -> str:
    key = private_key or ed25519.Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def _import_job(**anchors) -> dict:
    spec = _spec(**anchors)
    item = ImportRow(str(uuid.uuid4()), spec.cell_id, "adopt", 23, 1, "uploaded", NOW,
                     ciphertext_sha256="a" * 64, ciphertext_bytes=256)
    return render_import_job(spec, item, "import", deadline_seconds=120, url="https://storage.example/ciphertext")


def _export_job(*, verify: bool, **anchors) -> dict:
    spec = CellManifestSpec(cell_id=CELL_ID, image="registry.example/cell@sha256:" + "a" * 64, replicas=0,
                            read_only=False, storage_gib=20, hold_kind="export", hold_started_at=NOW.isoformat(),
                            **anchors)
    item = ExportRow(str(uuid.uuid4()), CELL_ID, "requested", NOW,
                     **({"archive_version_id": "v1", "archive_bytes": 5, "archive_sha256": "b" * 64} if verify else {}))
    size = ExportSize(max_bytes=20 * GIB, part_bytes=64 * MIB, parts=327)
    return render_export_job(spec, item, size, part_urls=["https://storage.example/part"], deadline_seconds=1800)


_JOBS = [
    pytest.param(lambda **anchors: _import_job(**anchors), id="import"),
    pytest.param(lambda **anchors: _export_job(verify=False, **anchors), id="export"),
    pytest.param(lambda **anchors: _export_job(verify=True, **anchors), id="export-verification"),
]


def _env(job: dict) -> dict:
    return {entry["name"]: entry.get("value") for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}


@pytest.mark.parametrize("render", _JOBS)
def test_a_job_that_moves_data_receives_the_configured_anchors(render) -> None:
    grant_key = _public_pem()
    env = _env(render(object_store_endpoint="https://s3.example", object_store_bucket="cells-bucket",
                      grant_public_key=grant_key))
    assert (env[ENDPOINT_ENV], env[BUCKET_ENV], env[GRANT_KEY_ENV]) == ("https://s3.example", "cells-bucket", grant_key)


@pytest.mark.parametrize("render", _JOBS)
def test_an_anchor_that_is_not_configured_is_not_rendered(render) -> None:
    """Defect: an empty value reads as configured and the cell then compares a URL against nothing."""
    assert {ENDPOINT_ENV, BUCKET_ENV, GRANT_KEY_ENV}.isdisjoint(_env(render()))
    env = _env(render(object_store_endpoint="https://s3.example", object_store_bucket="cells-bucket"))
    assert GRANT_KEY_ENV not in env and env[BUCKET_ENV] == "cells-bucket"


def test_the_serving_pod_and_its_init_container_receive_no_anchor() -> None:
    spec = _spec(object_store_endpoint="https://s3.example", object_store_bucket="cells-bucket",
                 grant_public_key=_public_pem())
    pod = render_statefulset(spec)["spec"]["template"]["spec"]
    names = {entry["name"] for container in [*pod["containers"], *pod["initContainers"]]
             for entry in container["env"]}
    assert {ENDPOINT_ENV, BUCKET_ENV, GRANT_KEY_ENV}.isdisjoint(names)


def test_a_grant_key_moves_no_cells_digest_so_no_cell_restarts() -> None:
    """Defect: rolling out the grant key restarts every serving cell for a value its pod never reads."""
    keyed = dataclasses.replace(_cluster_config(), grant_public_key=_public_pem())
    assert _digest(CELL_ID, keyed) == _digest(CELL_ID, _cluster_config())


def test_the_grant_key_loads_from_the_environment_and_is_empty_by_default(monkeypatch) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    assert main.build_cluster_config().grant_public_key == ""
    grant_key = _public_pem()
    monkeypatch.setenv("CELLCTL_GRANT_PUBLIC_KEY", grant_key)
    assert main.build_cluster_config().grant_public_key == grant_key


def test_a_grant_key_the_cell_would_refuse_is_refused_here_without_echoing_it() -> None:
    private = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    wrong_algorithm = _public_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for bad in ("not a key", private, wrong_algorithm):
        with pytest.raises(ValueError, match="grant public key must be an Ed25519") as raised:
            dataclasses.replace(_cluster_config(), grant_public_key=bad)
        assert bad not in str(raised.value)
        with pytest.raises(ValueError, match="grant public key must be an Ed25519"):
            _spec(grant_public_key=bad)
