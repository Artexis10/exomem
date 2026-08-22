"""Governed structured reads have no representation below full release."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, query_data, server, server_transfer, upload_tokens
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal, request_scope

EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
DATASET = "Knowledge Base/Notes/Governed/private.csv"
VIDEO = "Knowledge Base/Notes/Governed/private.mp4"


@pytest.fixture(autouse=True)
def _clear_governance_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _clear_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _govern(
    vault: Path,
    *,
    ceiling: int,
    audience: str = "external",
    selector: str = 'paths: ["Notes/Governed/**"]',
) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "structured.yaml").write_text(
        "governance_version: 1\n"
        f"id: {SCOPE_ID}\n"
        "name: Governed structured content\n"
        f"{selector}\n",
        encoding="utf-8",
    )
    (root / "rules" / "structured.yaml").write_text(
        "governance_version: 1\n"
        f"id: {RULE_ID}\n"
        f'scope_ids: ["{SCOPE_ID}"]\n'
        f"audience: {audience}\n"
        f"ceiling: {ceiling}\n"
        "options:\n"
        "  abstract: Approved companion abstract.\n",
        encoding="utf-8",
    )
    _clear_caches()


def _write(vault: Path, relative: str, data: bytes) -> Path:
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


@pytest.mark.parametrize("ceiling", range(6))
@pytest.mark.parametrize("aggregate", (None, "count", "profile"))
def test_dataset_rows_reductions_and_profile_are_missing_before_parse(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: int,
    aggregate: str | None,
) -> None:
    target = _write(vault, DATASET, b"name,value\nhidden-sentinel,42\n")
    _govern(vault, ceiling=ceiling)

    def forbidden_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("withheld dataset bytes reached the parser")

    monkeypatch.setattr(query_data, "load_generic_rows", forbidden_parse)
    with request_scope(EXTERNAL), pytest.raises(ValueError) as withheld:
        commands.op_query_dataset(vault, path=DATASET, aggregate=aggregate)

    target.unlink()
    _clear_caches()
    with request_scope(EXTERNAL), pytest.raises(ValueError) as missing:
        commands.op_query_dataset(vault, path=DATASET, aggregate=aggregate)

    assert str(withheld.value) == str(missing.value)
    assert "hidden-sentinel" not in str(withheld.value)


def test_unresolved_semantic_dataset_is_missing_before_parse(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(vault, DATASET, b"name,value\nhidden-sentinel,42\n")
    _govern(vault, ceiling=egress.LEVEL_FULL, selector='types: ["source"]')

    monkeypatch.setattr(
        query_data,
        "load_generic_rows",
        lambda *_args, **_kwargs: pytest.fail(
            "unresolved dataset bytes reached the parser"
        ),
    )
    with request_scope(EXTERNAL), pytest.raises(ValueError, match="^NOT_FOUND:"):
        commands.op_query_dataset(vault, path=DATASET)


@pytest.mark.parametrize("ceiling", range(6))
def test_video_frames_are_missing_before_decoder_import(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: int
) -> None:
    target = _write(vault, VIDEO, b"\x00\x00\x00\x18ftypmp42hidden-video")
    _govern(vault, ceiling=ceiling)
    monkeypatch.setattr(
        commands,
        "_video_frames_module",
        lambda: pytest.fail("withheld video reached the decoder"),
    )

    with request_scope(EXTERNAL), pytest.raises(ValueError) as withheld:
        commands.op_get_video_frames(vault, path=VIDEO)
    target.unlink()
    _clear_caches()
    with request_scope(EXTERNAL), pytest.raises(ValueError) as missing:
        commands.op_get_video_frames(vault, path=VIDEO)

    assert str(withheld.value) == str(missing.value)


def test_local_download_authorizes_before_reading_response_bytes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = "Knowledge Base/Notes/Governed/private.bin"
    _write(vault, binary, b"hidden-binary-sentinel")
    _govern(vault, ceiling=egress.LEVEL_EXCERPT, audience="owner")
    monkeypatch.setattr(server, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sek")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint("sek", scope="download")
    original_read = server_transfer.reserved_paths.read_generic_bytes

    def guarded_read(root: Path, relative: str):
        if relative == binary:
            raise AssertionError("response bytes were read before authorization")
        return original_read(root, relative)

    monkeypatch.setattr(
        server_transfer.reserved_paths, "read_generic_bytes", guarded_read
    )
    response = client.get(
        "/download",
        params={"path": binary},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert b"hidden-binary-sentinel" not in response.content


def test_bound_dataset_companion_is_projected_without_substituting_raw_rows(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"name,value\nhidden-sentinel,42\n"
    _write(vault, DATASET, raw)
    card_relative = "Knowledge Base/Notes/Governed/private-dataset.md"
    card = vault / card_relative
    card.write_text(
        "---\n"
        "type: dataset\n"
        "title: Governed dataset companion\n"
        "summary: Approved companion summary.\n"
        f"data_file: {DATASET}\n"
        "format: csv\n"
        "governance_companion:\n"
        "  version: 1\n"
        "  state: classified\n"
        "  artifact_class: dataset\n"
        f"  artifact_path: {DATASET}\n"
        f"  artifact_sha256: {hashlib.sha256(raw).hexdigest()}\n"
        f"  artifact_size: {len(raw)}\n"
        "  format: csv\n"
        "  semantics:\n"
        "    projects: []\n"
        "    tags: []\n"
        "    types: [source]\n"
        "    classes: []\n"
        "---\n"
        "The companion is a separately governed Markdown representation.\n",
        encoding="utf-8",
    )
    _govern(vault, ceiling=egress.LEVEL_ABSTRACT)
    monkeypatch.setattr(
        query_data,
        "load_generic_rows",
        lambda *_args, **_kwargs: pytest.fail(
            "the direct route substituted the companion for raw rows"
        ),
    )

    with request_scope(EXTERNAL):
        recalled = commands.op_find(
            vault,
            query="governed dataset companion",
            mode="keyword",
            graph=False,
            limit=5,
        )
        companion = commands.op_get(vault, path=card_relative)
        with pytest.raises(ValueError, match="^NOT_FOUND:"):
            commands.op_query_dataset(vault, path=DATASET)

    assert any(
        hit.get("abstract") == "Approved companion abstract." for hit in recalled
    )
    assert companion["level"] == egress.LEVEL_ABSTRACT
    assert companion["abstract"] == "Approved companion abstract."
    assert "hidden-sentinel" not in str(companion)
