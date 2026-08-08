"""The signer must produce records the real verifier accepts.

A signer that agrees only with its own idea of the format is worthless -- the
whole point is that ``hosted_plugins._load_signed_directory_evidence`` accepts
the output. These tests therefore round-trip through the actual verifier rather
than re-implementing its rules.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT = "a" * 64
KEY_ID = "operator-key-1"
SECRET = "an-operator-promotion-secret-value"

PAYLOADS = {
    "production-evidence": {
        "surfaces": {"mcp": "ok"},
        "compatibility_sha256": "b" * 64,
        "command_surface_sha256": "c" * 64,
        "schema_contract_sha256": "d" * 64,
        "full_tool_contract_sha256": "e" * 64,
        "origin_rejection": True,
        "response_minimization": True,
        "sampled_output_sale_free": True,
    },
    "prerequisite-evidence": {"channels": {"claude-connector": {}}},
    "public-admission-evidence": {"admission": {"ordinary_acquisition": True}},
    "reviewer-access-evidence": {"channels": {"claude-connector": {}}},
}


def signer() -> object:
    spec = importlib.util.spec_from_file_location(
        "sign_directory_evidence", REPO_ROOT / "scripts" / "sign-directory-evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_record(root: Path, name: str, record: dict) -> None:
    target = root / "plugins" / "hosted" / "directory" / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_signed_record_is_accepted_by_the_real_verifier(tmp_path: Path, name: str) -> None:
    module = signer()
    record = module.build(
        name=name,
        payload=PAYLOADS[name],
        deployment_sha256=DEPLOYMENT,
        key_id=KEY_ID,
        secret=SECRET,
        ttl=timedelta(hours=1),
    )
    write_record(tmp_path, name, record)

    evidence_type = module.EVIDENCE_TYPES[name][0]
    loaded = hosted_plugins._load_signed_directory_evidence(
        tmp_path,
        name,
        evidence_type,
        trusted_key_id=KEY_ID,
        trusted_secret=SECRET,
        deployment_sha256=DEPLOYMENT,
    )
    assert loaded["evidence_type"] == evidence_type
    assert loaded["deployment_sha256"] == DEPLOYMENT


def test_tampering_with_a_signed_payload_is_rejected(tmp_path: Path) -> None:
    module = signer()
    record = module.build(
        name="public-admission-evidence",
        payload={"admission": {"ordinary_acquisition": True}},
        deployment_sha256=DEPLOYMENT,
        key_id=KEY_ID,
        secret=SECRET,
        ttl=timedelta(hours=1),
    )
    # Flip the asserted fact while keeping the signature: the signature must
    # cover the payload, not just the envelope.
    record["admission"] = {"ordinary_acquisition": False}
    write_record(tmp_path, "public-admission-evidence", record)

    with pytest.raises(ValueError, match="invalid operator signature"):
        hosted_plugins._load_signed_directory_evidence(
            tmp_path,
            "public-admission-evidence",
            "directory-public-admission",
            trusted_key_id=KEY_ID,
            trusted_secret=SECRET,
            deployment_sha256=DEPLOYMENT,
        )


def test_a_record_signed_for_another_deployment_is_rejected(tmp_path: Path) -> None:
    module = signer()
    record = module.build(
        name="public-admission-evidence",
        payload={"admission": {"ordinary_acquisition": True}},
        deployment_sha256="f" * 64,
        key_id=KEY_ID,
        secret=SECRET,
        ttl=timedelta(hours=1),
    )
    write_record(tmp_path, "public-admission-evidence", record)

    # Evidence proves something about one deployment. Reusing it for the next
    # one would let a stale probe vouch for code that never ran.
    with pytest.raises(ValueError, match="binds a different deployment"):
        hosted_plugins._load_signed_directory_evidence(
            tmp_path,
            "public-admission-evidence",
            "directory-public-admission",
            trusted_key_id=KEY_ID,
            trusted_secret=SECRET,
            deployment_sha256=DEPLOYMENT,
        )


def test_payload_fields_must_match_the_contract() -> None:
    module = signer()
    with pytest.raises(SystemExit, match="missing"):
        module.build(
            name="production-evidence",
            payload={"surfaces": {}},
            deployment_sha256=DEPLOYMENT,
            key_id=KEY_ID,
            secret=SECRET,
            ttl=timedelta(hours=1),
        )


def test_ttl_beyond_the_contract_ceiling_is_refused() -> None:
    module = signer()
    # The verifier caps evidence at 24h; signing a longer one would produce a
    # record that is rejected later, at submission time, for no obvious reason.
    with pytest.raises(SystemExit, match="at most 24"):
        module.build(
            name="public-admission-evidence",
            payload={"admission": {"ordinary_acquisition": True}},
            deployment_sha256=DEPLOYMENT,
            key_id=KEY_ID,
            secret=SECRET,
            ttl=timedelta(hours=25),
        )
