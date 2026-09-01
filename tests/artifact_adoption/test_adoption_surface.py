from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from starlette.testclient import TestClient


def _adoption(selected: str = "selected-file") -> dict[str, str]:
    return {
        "key": "synthetic-output:final",
        "trigger": "selected",
        "selected_file_id": selected,
    }


def _delivery_manifest(*, link_type: str = "link") -> str:
    return f"""---
type: collection
exomem_id: 77777777-7777-4777-8777-777777777777
title: Delivered artifacts
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [occurred_on, remote_reference]
  fields:
    occurred_on:
      type: date
      required: true
    artifact:
      type: {link_type}
      required: true
    remote_reference:
      type: string
      required: true
    remote_verified:
      type: boolean
      required: true
    platform_reference:
      type: string
---
"""


def _setup_delivery_collection(vault: Path, *, link_type: str = "link") -> str:
    relative = "Knowledge Base/Records/Deliveries/_collection.md"
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_delivery_manifest(link_type=link_type), encoding="utf-8")
    (path.parent / "Items").mkdir(exist_ok=True)
    return relative


def _stage_evidence_receipt(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    from exomem import client_artifacts, media_processing

    data = b"selected exact bytes"

    def stage(file, _budget, **_kwargs):
        staged = tmp_path / "selected.png"
        staged.write_bytes(data)
        return client_artifacts.StagedArtifact(
            file_id=str(file["file_id"]),
            path=staged,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type="image/png",
            filename="selected.png",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="project",
        category="deliverables",
        files=[
            {
                "download_url": "https://files.example/selected",
                "file_id": "selected-file",
                "file_name": "selected.png",
                "mime_type": "image/png",
            }
        ],
        adoption=_adoption(),
    )
    assert result["files"][0]["outcome"] == "stored"
    return result["files"][0]["adoption"]


def _stage_source_receipt(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    from exomem import client_artifacts, media_processing

    data = b"selected source bytes"

    def stage(file, _budget, **_kwargs):
        staged = tmp_path / "selected-source.png"
        staged.write_bytes(data)
        return client_artifacts.StagedArtifact(
            file_id=str(file["file_id"]),
            path=staged,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type="image/png",
            filename="selected-source.png",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    result = client_artifacts.capture_source_artifacts(
        vault,
        source_schema=source_schema,
        title="Synthetic reasoning input",
        files=[
            {
                "download_url": "https://files.example/selected-source",
                "file_id": "selected-file",
                "file_name": "selected-source.png",
                "mime_type": "image/png",
            }
        ],
        adoption=_adoption(),
    )
    assert result["files"][0]["outcome"] == "stored"
    return result["files"][0]["adoption"]


def _delivery(
    evidence_page: str,
    *,
    proof: dict[str, str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_page": evidence_page,
        "link_field": "artifact",
        "reported_remote_ref": "remote:123",
        "reported_remote_field": "remote_reference",
        "verified_remote_field": "remote_verified",
    }
    if proof is not None:
        value["platform_reference_field"] = "platform_reference"
        value["platform_proof"] = proof
    return value


def _delivery_item(evidence_page: str, *, verified: bool, reference: str | None = None) -> dict:
    item = {
        "occurred_on": "2026-09-01",
        "artifact": evidence_page,
        "remote_reference": "remote:123",
        "remote_verified": verified,
    }
    if reference is not None:
        item["platform_reference"] = reference
    return item


def test_public_source_and_evidence_commands_forward_the_closed_adoption_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, commands

    source_calls: list[dict[str, object]] = []
    evidence_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        client_artifacts,
        "capture_source_artifacts",
        lambda _root, **kwargs: source_calls.append(kwargs) or {"files": [], "summary": {}},
    )
    monkeypatch.setattr(
        client_artifacts,
        "preserve_artifacts",
        lambda _root, **kwargs: evidence_calls.append(kwargs) or {"files": [], "summary": {}},
    )
    envelope = _adoption()
    handle = {
        "download_url": "https://files.example/final",
        "file_id": "selected-file",
        "file_name": "final.png",
        "mime_type": "image/png",
    }

    commands.op_capture_source(
        tmp_path,
        object(),
        title="Research input",
        files=[handle],
        adoption=envelope,
    )
    commands.op_preserve_artifacts(
        tmp_path,
        scope="project",
        category="deliverables",
        files=[handle],
        adoption=envelope,
    )

    assert source_calls[0]["adoption"] == envelope
    assert evidence_calls[0]["adoption"] == envelope
    for name in ("capture_source", "preserve_artifacts"):
        command = next(item for item in commands.PRODUCT_COMMANDS if item.name == name)
        parameter = next(item for item in command.params if item.name == "adoption")
        assert parameter.required is False
        assert parameter.type == "nullable_dict"
        assert parameter.schema is not None
        [object_shape, null_shape] = parameter.schema["anyOf"]
        assert object_shape["additionalProperties"] is False
        assert set(object_shape["required"]) == {"key", "trigger", "selected_file_id"}
        assert null_shape == {"type": "null"}


def test_compact_projection_preserves_approved_bounded_adoption_outcomes() -> None:
    from exomem.mutation_terminal import _artifact_receipt_projection

    receipt = {
        "version": 1,
        "committed": True,
        "key_digest": "b" * 64,
        "trigger": "selected",
        "selected_file_id": "selected-file",
        "lane": "evidence",
        "destination": "Knowledge Base/Evidence/project/deliverables",
        "stored_path": "Knowledge Base/Evidence/project/deliverables/final.png",
        "page_path": "Knowledge Base/Evidence/project/deliverables/final.png.md",
        "hash_algorithm": "sha256",
        "hash": "a" * 64,
        "size": 3,
        "content_type": "image/png",
        "media_id": "sha256:" + "a" * 64,
    }
    result = {
        "files": [
            {"file_id": "draft", "outcome": "unselected"},
            {
                "file_id": "selected-file",
                "outcome": "replayed",
                "stored_path": receipt["stored_path"],
                "path": receipt["stored_path"],
                "page": receipt["page_path"],
                "size": 3,
                "hash": "a" * 64,
                "hash_algorithm": "sha256",
                "media_id": "sha256:" + "a" * 64,
                "content_type": "image/png",
                "warnings": [],
                "adoption": receipt,
            },
            {
                "file_id": "expired",
                "outcome": "failed",
                "code": "ADOPTION_REPLAY_UNVERIFIABLE",
                "reason": "previous adoption bytes could not be reverified",
            },
        ],
        "summary": {"stored": 0, "replayed": 1, "failed": 1, "unselected": 1},
    }

    assert _artifact_receipt_projection(result) == result


def test_compact_projection_preserves_logical_counts_when_malformed_rows_are_bounded() -> None:
    """Detail repair must not rewrite the core's logical overflow summary."""
    from exomem.mutation_terminal import _artifact_receipt_projection

    result = {
        "files": [
            {
                "file_id": f"invalid-{index}",
                "outcome": "failed",
                "code": "INVALID_ADOPTION",
                "reason": "adoption trigger is invalid",
            }
            for index in range(8)
        ],
        "summary": {"stored": 0, "failed": 100_003, "omitted": 99_995},
    }

    projected = _artifact_receipt_projection(result)

    assert len(projected["files"]) == 8
    assert projected["summary"] == {
        "stored": 0,
        "failed": 100_003,
        "omitted": 99_995,
    }


def test_public_adoption_and_delivery_schemas_are_closed() -> None:
    from exomem import commands

    source = inspect.signature(commands.op_capture_source).parameters["adoption"].annotation
    evidence = inspect.signature(commands.op_preserve_artifacts).parameters["adoption"].annotation
    delivery = inspect.signature(commands.op_record_memory).parameters["delivery"].annotation

    assert source == evidence
    assert source is not inspect.Parameter.empty
    assert delivery is not inspect.Parameter.empty


@pytest.mark.parametrize(
    ("length", "accepted"),
    ((64, True), (65, False), (128, False), (129, False)),
)
def test_public_adoption_trigger_schema_matches_runtime_bound(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    length: int,
    accepted: bool,
) -> None:
    from exomem import client_artifacts, commands, media_processing

    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "preserve_artifacts")
    schema = next(item for item in command.params if item.name == "adoption").schema
    assert schema is not None
    object_schema = schema["anyOf"][0]
    adoption = {
        "key": f"synthetic-boundary-{length}",
        "trigger": "x" * length,
        "selected_file_id": "selected-file",
    }
    schema_accepts = not list(Draft202012Validator(object_schema).iter_errors(adoption))

    staged = tmp_path / f"selected-{length}.png"
    staged.write_bytes(b"exact selected bytes")
    fetches = 0

    def stage(file, _budget, **_kwargs):
        nonlocal fetches
        fetches += 1
        return client_artifacts.StagedArtifact(
            file_id=str(file["file_id"]),
            path=staged,
            size=staged.stat().st_size,
            sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
            content_type="image/png",
            filename=staged.name,
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    monkeypatch.setattr(media_processing, "classify_media", lambda _path: None)
    result = client_artifacts.preserve_artifacts(
        vault,
        scope="synthetic-case",
        category="outputs",
        files=[
            {
                "download_url": "https://files.example/selected",
                "file_id": "selected-file",
                "file_name": staged.name,
                "mime_type": "image/png",
            }
        ],
        adoption=adoption,
    )
    runtime_accepts = result["files"][0]["outcome"] == "stored"

    assert schema_accepts is accepted
    assert runtime_accepts is accepted
    assert fetches == int(accepted)


def test_registry_mcp_rest_openapi_and_cli_share_closed_adoption_contract(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from exomem import commands, server
    from exomem.__main__ import main
    from exomem.governance import principal

    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-key")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    mcp = server.build_server(require_auth=False)

    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "preserve_artifacts")
    registry_schema = next(item for item in command.params if item.name == "adoption").schema
    assert registry_schema is not None
    tools = {item.name: item for item in asyncio.run(mcp.list_tools())}
    mcp_schema = tools["preserve_artifacts"].to_mcp_tool().model_dump(mode="json")[
        "inputSchema"
    ]["properties"]["adoption"]
    rest = TestClient(mcp.http_app())
    openapi_schema = rest.get("/api/openapi.json").json()["paths"][
        "/api/preserve_artifacts"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][
        "adoption"
    ]
    for projected in (mcp_schema, openapi_schema):
        assert projected["anyOf"] == registry_schema["anyOf"]
        assert projected["anyOf"][0]["additionalProperties"] is False

    arguments = {
        "scope": "synthetic-case",
        "category": "outputs",
        "files": [
            {
                "download_url": "https://files.example/final",
                "file_id": "selected-file",
            }
        ],
        "adoption": {**_adoption(), "unknown": True},
    }
    with principal.request_scope(principal.owner_principal(surface="mcp")):
        mcp_result = asyncio.run(
            mcp.call_tool("preserve_artifacts", arguments, run_middleware=False)
        )
    mcp_payload = (
        mcp_result.structured_content
        if isinstance(mcp_result.structured_content, dict)
        else json.loads(mcp_result.content[0].text)
    )
    rest_response = rest.post(
        "/api/preserve_artifacts",
        headers={"Authorization": "Bearer synthetic-key"},
        json=arguments,
    )
    assert rest_response.status_code == 200
    rest_payload = rest_response.json()["data"]

    exit_code = main(
        [
            "preserve_artifacts",
            "--scope",
            str(arguments["scope"]),
            "--category",
            str(arguments["category"]),
            "--files",
            json.dumps(arguments["files"]),
            "--adoption",
            json.dumps(arguments["adoption"]),
            "--json",
        ]
    )
    assert exit_code == 0
    cli_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]

    for payload in (mcp_payload, rest_payload, cli_payload):
        assert payload["files"] == [
            {
                "file_id": "selected-file",
                "outcome": "failed",
                "code": "INVALID_ADOPTION",
                "reason": "adoption must contain exactly key, trigger, and selected_file_id",
            }
        ]
        assert payload["summary"] == {"stored": 0, "failed": 1}


def test_delivery_registry_mcp_and_openapi_close_nested_platform_proof(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands, server

    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-key")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    mcp = server.build_server(require_auth=False)
    document = TestClient(mcp.http_app()).get("/api/openapi.json").json()
    openapi_delivery = document["paths"]["/api/record_memory"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]["delivery"]["anyOf"][0]
    tools = {item.name: item for item in asyncio.run(mcp.list_tools())}
    mcp_delivery = tools["record_memory"].to_mcp_tool().model_dump(mode="json")[
        "inputSchema"
    ]["properties"]["delivery"]["anyOf"][0]
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "record_memory")
    registry_delivery = next(item for item in command.params if item.name == "delivery").schema
    assert registry_delivery is not None
    registry_delivery = registry_delivery["anyOf"][0]

    for delivery in (registry_delivery, mcp_delivery, openapi_delivery):
        assert delivery["additionalProperties"] is False
        proof = delivery["properties"]["platform_proof"]
        assert proof["additionalProperties"] is False
        assert set(proof["required"]) == {"algorithm", "digest", "reference"}
        assert proof["properties"]["algorithm"]["const"] == "sha256"


def test_record_memory_delivery_argument_is_append_only(tmp_path: Path) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            tmp_path,
            action="inspect",
            delivery={
                "evidence_page": "Knowledge Base/Evidence/project/final.png.md",
                "link_field": "artifact",
                "reported_remote_ref": "remote:123",
                "reported_remote_field": "remote_reference",
                "verified_remote_field": "remote_verified",
            },
        )

    assert raised.value.code == "INVALID_RECORD_ARGUMENTS"
    assert "delivery" in raised.value.message


def test_upload_capability_is_an_explicit_non_committing_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import commands

    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "synthetic-secret")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://memory.example")

    result = commands.op_transfer_artifact(tmp_path, operation="upload", lane="evidence")

    assert result["handoff_status"] == "handoff_prepared"
    assert result["committed"] is False
    for forbidden in ("saved", "stored", "adopted", "delivered"):
        assert forbidden not in result


def test_record_delivery_requires_existing_collection_before_any_write(tmp_path: Path) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            tmp_path,
            action="append",
            collection="Knowledge Base/Records/Deliveries/_collection.md",
            item={
                "occurred_on": "2026-09-01",
                "artifact": "Knowledge Base/Evidence/project/final.png.md",
                "remote_reference": "remote:123",
                "remote_verified": False,
            },
            why="record reported delivery",
            delivery={
                "evidence_page": "Knowledge Base/Evidence/project/final.png.md",
                "link_field": "artifact",
                "reported_remote_ref": "remote:123",
                "reported_remote_field": "remote_reference",
                "verified_remote_field": "remote_verified",
            },
        )

    assert raised.value.code == "COLLECTION_NOT_FOUND"
    assert not (tmp_path / "Knowledge Base/Records/Deliveries").exists()


def test_existing_compatible_records_collection_accepts_reported_delivery_after_receipt(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_memory

    collection = _setup_delivery_collection(vault)
    receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
    evidence_page = str(receipt["page_path"])

    result = record_memory.record_memory(
        vault,
        action="append",
        collection=collection,
        item=_delivery_item(evidence_page, verified=False),
        why="record reported delivery",
        delivery=_delivery(evidence_page),
    )

    assert result["outcome"] == "committed"
    [written] = (vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")
    text = written.read_text(encoding="utf-8")
    assert f"artifact: {evidence_page}" in text
    assert 'remote_reference: "remote:123"' in text
    assert "remote_verified: false" in text


def test_matching_platform_proof_is_required_for_verified_remote_identity(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_memory

    collection = _setup_delivery_collection(vault)
    receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
    evidence_page = str(receipt["page_path"])
    proof = {
        "algorithm": "sha256",
        "digest": str(receipt["hash"]),
        "reference": "platform-receipt:123",
    }

    result = record_memory.record_memory(
        vault,
        action="append",
        collection=collection,
        item=_delivery_item(evidence_page, verified=True, reference=proof["reference"]),
        why="record proved delivery",
        delivery=_delivery(evidence_page, proof=proof),
    )

    assert result["outcome"] == "committed"


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing-receipt", "DELIVERY_RECEIPT_REQUIRED"),
        ("incompatible-link", "DELIVERY_SCHEMA_INCOMPATIBLE"),
        ("verified-without-proof", "DELIVERY_ITEM_MISMATCH"),
        ("mismatched-proof", "DELIVERY_PROOF_MISMATCH"),
    ],
)
def test_invalid_delivery_writes_no_record(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_code: str,
) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    collection = _setup_delivery_collection(
        vault, link_type="string" if case == "incompatible-link" else "link"
    )
    if case == "missing-receipt":
        evidence_page = "Knowledge Base/Evidence/project/deliverables/missing.png.md"
        receipt = {"hash": "a" * 64}
    else:
        receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
        evidence_page = str(receipt["page_path"])
    proof = None
    verified = case == "verified-without-proof"
    reference = None
    if case == "mismatched-proof":
        proof = {
            "algorithm": "sha256",
            "digest": "f" * 64,
            "reference": "platform-receipt:wrong",
        }
        verified = True
        reference = proof["reference"]

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            vault,
            action="append",
            collection=collection,
            item=_delivery_item(evidence_page, verified=verified, reference=reference),
            why="refuse unproved delivery",
            delivery=_delivery(evidence_page, proof=proof),
        )

    assert raised.value.code == expected_code
    assert list((vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")) == []


def test_delivery_envelope_rejects_unknown_fields_before_record_write(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    collection = _setup_delivery_collection(vault)
    receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
    evidence_page = str(receipt["page_path"])
    delivery = _delivery(evidence_page)
    delivery["claim_saved"] = True

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            vault,
            action="append",
            collection=collection,
            item=_delivery_item(evidence_page, verified=False),
            why="refuse unknown delivery field",
            delivery=delivery,
        )

    assert raised.value.code == "INVALID_DELIVERY"
    assert raised.value.details["allowed_fields"]
    assert list((vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")) == []


def test_source_adoption_receipt_cannot_authorize_evidence_delivery(
    vault: Path,
    source_schema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    collection = _setup_delivery_collection(vault)
    receipt = _stage_source_receipt(vault, source_schema, tmp_path, monkeypatch)
    source_page = str(receipt["page_path"])

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            vault,
            action="append",
            collection=collection,
            item=_delivery_item(source_page, verified=False),
            why="refuse source-only delivery",
            delivery=_delivery(source_page),
        )

    assert raised.value.code == "DELIVERY_EVIDENCE_REQUIRED"
    assert list((vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")) == []


def test_missing_mapped_link_field_is_machine_readable_and_writes_nothing(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_memory
    from exomem.cli_ops import OpError

    collection = _setup_delivery_collection(vault)
    receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
    evidence_page = str(receipt["page_path"])
    delivery = _delivery(evidence_page)
    delivery["link_field"] = "undeclared_artifact"

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            vault,
            action="append",
            collection=collection,
            item=_delivery_item(evidence_page, verified=False),
            why="refuse missing link field",
            delivery=delivery,
        )

    assert raised.value.code == "DELIVERY_SCHEMA_INCOMPATIBLE"
    assert raised.value.details == {
        "field": "undeclared_artifact",
        "expected_type": "link",
        "actual_type": None,
    }
    assert list((vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")) == []


def test_delivery_rechecks_receipt_at_record_commit_boundary(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import record_governance, record_memory
    from exomem.cli_ops import OpError

    collection = _setup_delivery_collection(vault)
    receipt = _stage_evidence_receipt(vault, tmp_path, monkeypatch)
    evidence_page = str(receipt["page_path"])
    receipt_path = vault / evidence_page
    original_manifest = (vault / collection).read_bytes()
    original_authorize = record_governance.precommit_authorize_mutation
    raced = False

    def race_receipt(*args, **kwargs):
        nonlocal raced
        result = original_authorize(*args, **kwargs)
        if not raced:
            raced = True
            receipt_path.write_text(
                receipt_path.read_text(encoding="utf-8") + "\n<!-- concurrent change -->\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(record_governance, "precommit_authorize_mutation", race_receipt)

    with pytest.raises(OpError) as raised:
        record_memory.record_memory(
            vault,
            action="append",
            collection=collection,
            item=_delivery_item(evidence_page, verified=False),
            why="refuse raced delivery receipt",
            delivery=_delivery(evidence_page),
        )

    assert raised.value.code == "STALE_RECORD"
    assert raced is True
    assert (vault / collection).read_bytes() == original_manifest
    assert list((vault / "Knowledge Base/Records/Deliveries/Items").glob("*.md")) == []
