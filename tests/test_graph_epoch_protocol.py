"""Red-first protocol tests for graph checkpoint publication primitives."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from exomem import epistemic_graph, graph_sync, vault, writer_lease
from exomem import hosted_portability as portability


def _checkpoint_payload(**changes: object) -> str:
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=(("Knowledge Base/Notes/example.md", "a" * 64),),
        created_paths=("Knowledge Base/Notes/example.md",),
    )
    payload = checkpoint.as_dict()
    payload.update(changes)
    unsigned = {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
    payload["checkpoint_sha256"] = hashlib.sha256(
        graph_sync._DOMAIN
        + json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _valid_protocol_artifact(artifact: str) -> str:
    if artifact == "floor":
        return graph_sync.GraphSyncGenerationFloor.create(1).render()
    if artifact == "checkpoint":
        return graph_sync.GraphSyncCheckpoint.create(
            generation=1,
            mutation_id="0123456789abcdef01234567",
            paths=(),
            created_paths=(),
            scope="full",
        ).render()
    return graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="0123456789abcdef01234567",
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=b"s" * 32,
    ).render()


@pytest.mark.parametrize(
    "changes",
    [
        {"paths": [["Knowledge Base/Notes/../example.md", "a" * 64]]},
        {"paths": [["Knowledge Base//Notes/example.md", "a" * 64]]},
        {"paths": [["Knowledge Base/Notes\\example.md", "a" * 64]]},
        {"paths": [["/Knowledge Base/Notes/example.md", "a" * 64]]},
        {"paths": [["C:/Example/example.md", "a" * 64]]},
        {"paths": [["C:relative.md", "a" * 64]]},
        {"paths": [["//server/share/example.md", "a" * 64]]},
        {"paths": [["//?/C:/Example/example.md", "a" * 64]]},
        {"paths": [["\\\\?\\" + "C:" + "\\Example\\example.md", "a" * 64]]},
        {"paths": [["Knowledge Base/Notes/CON.md", "a" * 64]]},
        {"paths": [["Knowledge Base/Notes/example. ", "a" * 64]]},
        {"paths": [["Knowledge Base/Notes/example.md", "A" * 64]]},
        {
            "paths": [
                ["Knowledge Base/Notes/example.md", "a" * 64],
                ["Knowledge Base/Notes/example.md", "a" * 64],
            ]
        },
        {"created_paths": ["Knowledge Base/Notes/missing.md"]},
        {"scope": "full", "paths": [["Knowledge Base/Notes/example.md", "a" * 64]]},
    ],
)
def test_checkpoint_parser_rejects_noncanonical_path_entries_before_admission(
    changes: dict[str, object],
) -> None:
    assert graph_sync.GraphSyncCheckpoint.parse(_checkpoint_payload(**changes)) is None


@pytest.mark.parametrize(
    "path",
    [
        "C:/Example/example.md",
        "C:relative.md",
        "//server/share/example.md",
        "//?/C:/Example/example.md",
        "\\\\?\\" + "C:" + "\\Example\\example.md",
        "Knowledge Base/Notes/CON.md",
        "Knowledge Base/Notes/CON .md",
        "Knowledge Base/Notes/CONIN$.md",
        "Knowledge Base/Notes/CONOUT$.md",
        "Knowledge Base/Notes/COM¹.md",
        "Knowledge Base/Notes/LPT².md",
        "Knowledge Base/Notes/example. ",
    ],
)
def test_checkpoint_parser_rejects_windows_qualified_or_reserved_paths(path: str) -> None:
    assert graph_sync.GraphSyncCheckpoint.parse(
        _checkpoint_payload(paths=[[path, "a" * 64]], created_paths=[path])
    ) is None


def test_checkpoint_parser_limits_path_entries_not_rendered_bytes() -> None:
    paths = tuple(
        (f"Knowledge Base/Notes/{index:04d}-" + "x" * 80 + ".md", "a" * 64)
        for index in range(1_000)
    )
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=paths,
        created_paths=(),
    )

    assert len(checkpoint.render().encode("utf-8")) > 1_000
    assert graph_sync.GraphSyncCheckpoint.parse(checkpoint.render()) == checkpoint

    too_many = _checkpoint_payload(
        paths=[[f"Knowledge Base/Notes/{index}.md", "a" * 64] for index in range(1_001)],
        created_paths=[],
    )
    assert graph_sync.GraphSyncCheckpoint.parse(too_many) is None


@pytest.mark.parametrize("field", ["paths", "created_paths"])
def test_full_scope_parser_caps_raw_entries_before_item_validation(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(_checkpoint_payload(scope="full", paths=[], created_paths=[]))
    payload[field] = (
        [["not-a-path", "a" * 64]] * 1_001
        if field == "paths"
        else ["not-a-path"] * 1_001
    )
    unsigned = {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
    payload["checkpoint_sha256"] = hashlib.sha256(
        graph_sync._DOMAIN + graph_sync._canonical_json(unsigned)
    ).hexdigest()
    monkeypatch.setattr(
        graph_sync,
        "_is_canonical_relative_path",
        lambda _value: pytest.fail("oversized full-scope entries were inspected"),
    )
    assert graph_sync.GraphSyncCheckpoint.parse(graph_sync._canonical_json(payload)) is None

    empty = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    assert graph_sync.GraphSyncCheckpoint.parse(empty.render()) == empty


def test_checkpoint_accepts_1000_exact_1024_byte_paths_in_file_and_sqlite_meta(
    tmp_path: Path,
) -> None:
    paths = tuple(
        (
            f"Knowledge Base/Notes/{index:04d}-"
            + "x" * (1_024 - len(f"Knowledge Base/Notes/{index:04d}-.md"))
            + ".md",
            "a" * 64,
        )
        for index in range(1_000)
    )
    assert {len(path.encode("utf-8")) for path, _digest in paths} == {1_024}
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=paths,
        created_paths=tuple(path for path, _digest in paths),
    )
    assert graph_sync.GraphSyncCheckpoint.parse(checkpoint.render()) == checkpoint

    graph_sync.floor_path(tmp_path).parent.mkdir(parents=True)
    graph_sync.floor_path(tmp_path).write_text(
        graph_sync.GraphSyncGenerationFloor.create(4).render(), encoding="utf-8"
    )
    graph_sync.checkpoint_path(tmp_path).write_text(checkpoint.render(), encoding="utf-8")
    with sqlite3.connect(graph_sync.floor_path(tmp_path).with_name(".graph.sqlite")) as conn:
        conn.execute("CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("graph_sync_generation", "4"),
                ("graph_sync_digest", checkpoint.checkpoint_sha256),
                ("graph_sync_checkpoint", checkpoint.render()),
            ),
        )
    assert graph_sync.acknowledgement_state(tmp_path) == (
        "valid",
        graph_sync.GraphBuildOutcome.covering(checkpoint),
    )


def test_checkpoint_create_rejects_over_limit_created_paths_before_normalization() -> None:
    created_paths = tuple(f"Knowledge Base/Notes/{index:04d}.md" for index in range(1_001))
    with pytest.raises(ValueError, match="created paths cannot exceed 1000 entries"):
        graph_sync.GraphSyncCheckpoint.create(
            generation=4,
            mutation_id="0123456789abcdef01234567",
            paths=(),
            created_paths=created_paths,
        )


@pytest.mark.parametrize("artifact", ["checkpoint", "floor"])
def test_graph_protocol_rejects_boolean_versions(artifact: str) -> None:
    if artifact == "checkpoint":
        assert graph_sync.GraphSyncCheckpoint.parse(_checkpoint_payload(version=True)) is None
    else:
        floor = graph_sync.GraphSyncGenerationFloor.create(4).as_dict()
        floor["version"] = True
        assert graph_sync.GraphSyncGenerationFloor.parse(json.dumps(floor)) is None


def test_checkpoint_nfc_is_accepted_and_nfd_is_rejected() -> None:
    nfc_path = "Knowledge Base/Notes/caf\N{LATIN SMALL LETTER E WITH ACUTE}.md"
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=((nfc_path, "a" * 64),),
        created_paths=(nfc_path,),
    )
    assert graph_sync.GraphSyncCheckpoint.parse(checkpoint.render()) == checkpoint
    nfd_path = "Knowledge Base/Notes/cafe\N{COMBINING ACUTE ACCENT}.md"
    assert graph_sync.GraphSyncCheckpoint.parse(
        _checkpoint_payload(paths=[[nfd_path, "a" * 64]], created_paths=[nfd_path])
    ) is None


def test_receipt_rejects_malformed_hmac_and_all_signed_tampering() -> None:
    secret = b"s" * 32
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        checkpoint_generation=4,
        checkpoint_sha256="c" * 64,
        commit_secret=secret,
    )
    malformed = receipt.as_dict()
    malformed["receipt_hmac_sha256"] = "not-a-digest"
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(malformed)) is None

    def assert_tampered(**changes: object) -> None:
        payload = receipt.as_dict()
        payload.update(changes)
        if "terminal_projection" in changes:
            payload["terminal_projection_sha256"] = hashlib.sha256(
                graph_sync._RECEIPT_TERMINAL_DOMAIN
                + json.dumps(
                    payload["terminal_projection"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        parsed = graph_sync.GraphCommitReceipt.parse(graph_sync._canonical_json(payload))
        assert parsed is not None
        assert parsed.verify(
            secret,
            idempotency_key_digest="a" * 64,
            command_digest="b" * 64,
            attempt_id="0123456789abcdef01234567",
            commit_token="fedcba987654321001234567",
        ) is False

    assert_tampered(receipt_hmac_sha256="0" * 64)
    assert_tampered(idempotency_key_digest="d" * 64)
    assert_tampered(command_digest="e" * 64)
    assert_tampered(attempt_id="111111111111111111111111")
    assert_tampered(commit_token="222222222222222222222222")
    invalid_projection = receipt.as_dict()
    invalid_projection["terminal_projection"] = {"status": "failed", "mutated": True}
    invalid_projection["terminal_projection_sha256"] = hashlib.sha256(
        graph_sync._RECEIPT_TERMINAL_DOMAIN
        + graph_sync._canonical_json(invalid_projection["terminal_projection"])
    ).hexdigest()
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(invalid_projection)) is None
    assert_tampered(checkpoint_generation=5, checkpoint_sha256="f" * 64)

    assert receipt.verify(
        secret,
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="111111111111111111111111",
        commit_token="fedcba987654321001234567",
    ) is False


def test_v2_receipt_requires_an_authenticated_outer_canonical_disposition() -> None:
    secret = b"s" * 32
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=secret,
    )
    assert receipt.canonical_disposition == "success"
    assert receipt.verify(
        secret,
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
    )

    committed_failure = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
        canonical_disposition="committed_failure",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=secret,
    )
    assert graph_sync.GraphCommitReceipt.parse(committed_failure.render()) == committed_failure
    assert committed_failure.verify(
        secret,
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
    )

    missing = receipt.as_dict()
    missing.pop("canonical_disposition")
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(missing)) is None
    invalid = receipt.as_dict()
    invalid["canonical_disposition"] = "unknown"
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(invalid)) is None

    tampered = receipt.as_dict()
    tampered["canonical_disposition"] = "committed_failure"
    parsed = graph_sync.GraphCommitReceipt.parse(graph_sync._canonical_json(tampered))
    assert parsed is not None
    assert parsed.verify(
        secret,
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
    ) is False


def test_v2_receipt_requires_exact_canonical_utf8_json_before_authentication() -> None:
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=b"s" * 32,
    )
    rendered = receipt.render()
    assert graph_sync.GraphCommitReceipt.parse(rendered) == receipt

    payload = receipt.as_dict()
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(payload, indent=2)) is None
    assert graph_sync.GraphCommitReceipt.parse(
        json.dumps(dict(reversed(tuple(payload.items()))), separators=(",", ":"))
    ) is None
    assert graph_sync.GraphCommitReceipt.parse(
        rendered.replace('"status":"committed"', '"status":"committ\\u0065d"')
    ) is None
    assert graph_sync.GraphCommitReceipt.parse(rendered + "\n") is None

    legacy = receipt.as_dict()
    legacy.pop("canonical_disposition")
    legacy.pop("receipt_hmac_sha256")
    legacy["version"] = 1
    legacy["commit_point"] = True
    assert graph_sync.GraphCommitReceipt.parse(json.dumps(legacy, indent=2)) is not None


def test_v2_receipt_rejects_noncanonical_terminal_projection_scalars_before_auth() -> None:
    secret = b"s" * 32
    projection = {
        "_terminal": "exomem.mutation-terminal",
        "version": 1,
        "ok": True,
        "state": "committed",
        "status": "committed",
        "committed": True,
        "mutated": True,
        "terminal": True,
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "receipt_id": "0123456789abcdef",
        "operation_id": "123e4567-e89b-42d3-a456-426614174001",
        "warnings_count": 0,
        "result_sha256": "a" * 64,
    }
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba987654321001234567",
        canonical_disposition="success",
        terminal_projection=projection,
        commit_secret=secret,
    )
    assert graph_sync.GraphCommitReceipt.parse(receipt.render()) == receipt

    invalid_fields = (
        ("_terminal", "wrong-marker"),
        ("version", True),
        ("ok", "yes"),
        ("state", "unknown"),
        ("status", "failed"),
        ("committed", 1),
        ("mutated", None),
        ("terminal", 0),
        ("request_id", "not-a-uuid"),
        ("receipt_id", "not-a-receipt-tag"),
        ("operation_id", ""),
        ("warnings_count", -7),
        ("warnings_count", True),
        ("result_sha256", "not-a-digest"),
    )
    for field, value in invalid_fields:
        malformed = dict(projection)
        malformed[field] = value
        payload = receipt.as_dict()
        payload["terminal_projection"] = malformed
        payload["terminal_projection_sha256"] = hashlib.sha256(
            graph_sync._RECEIPT_TERMINAL_DOMAIN
            + graph_sync._canonical_json(malformed)
        ).hexdigest()
        assert graph_sync.GraphCommitReceipt.parse(graph_sync._canonical_json(payload)) is None
        with pytest.raises(ValueError, match="terminal projection"):
            graph_sync.GraphCommitReceipt.create(
                idempotency_key_digest="a" * 64,
                command_digest="b" * 64,
                attempt_id="0123456789abcdef01234567",
                commit_token="fedcba987654321001234567",
                canonical_disposition="success",
                terminal_projection=malformed,
                commit_secret=secret,
            )

    with pytest.raises(ValueError, match="content-free"):
        graph_sync.GraphCommitReceipt.create(
            idempotency_key_digest="a" * 64,
            command_digest="b" * 64,
            attempt_id="0123456789abcdef01234567",
            commit_token="fedcba987654321001234567",
            canonical_disposition="success",
            terminal_projection={"committed_failure_code": "forbidden"},
            commit_secret=secret,
        )
    assert receipt.verify(
        secret,
        idempotency_key_digest="a" * 64,
        command_digest="b" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="222222222222222222222222",
    ) is False


def test_bounded_artifact_readers_reject_sparse_oversized_inputs(
    tmp_path: Path,
) -> None:
    receipt_token = "0123456789abcdef01234567"
    paths_and_limits = (
        (graph_sync.graph_commit_receipt_path(tmp_path, receipt_token), graph_sync._RECEIPT_READ_LIMIT),
        (graph_sync.floor_path(tmp_path), graph_sync._FLOOR_READ_LIMIT),
        (graph_sync.checkpoint_path(tmp_path), graph_sync._CHECKPOINT_READ_LIMIT),
    )
    for path, limit in paths_and_limits:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(limit + 1)

    assert graph_sync.read_graph_commit_receipt(tmp_path, receipt_token) is None
    assert graph_sync.floor_state(tmp_path) == ("malformed", None)
    assert graph_sync.checkpoint_state(tmp_path) == ("malformed", None)
    for path, _limit in paths_and_limits[1:]:
        with pytest.raises(OSError, match="could not be safely read"):
            graph_sync._prior_artifact_bytes(tmp_path, path)


def test_bounded_artifact_reader_rejects_regular_oversized_input_without_large_allocation(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "regular-artifact.json"
    artifact.write_bytes(b"x" * 129)
    with pytest.raises(OSError, match="could not be safely read"):
        graph_sync._read_bounded_bytes(artifact, limit=128)


@pytest.mark.parametrize("artifact", ["floor", "checkpoint", "receipt"])
def test_protocol_artifact_reader_rejects_symlink_outside_vault(
    tmp_path: Path, artifact: str
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(_valid_protocol_artifact(artifact), encoding="utf-8")
    token = "0123456789abcdef01234567"
    path = {
        "floor": graph_sync.floor_path(tmp_path),
        "checkpoint": graph_sync.checkpoint_path(tmp_path),
        "receipt": graph_sync.graph_commit_receipt_path(tmp_path, token),
    }[artifact]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside)

    if artifact == "floor":
        assert graph_sync.floor_state(tmp_path) == ("malformed", None)
    elif artifact == "checkpoint":
        assert graph_sync.checkpoint_state(tmp_path) == ("malformed", None)
    else:
        assert graph_sync.read_graph_commit_receipt(tmp_path, token) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO contract is POSIX-only")
@pytest.mark.parametrize("artifact", ["floor", "checkpoint", "receipt"])
def test_protocol_artifact_reader_rejects_fifo_without_blocking(
    tmp_path: Path, artifact: str
) -> None:
    token = "0123456789abcdef01234567"
    path = {
        "floor": graph_sync.floor_path(tmp_path),
        "checkpoint": graph_sync.checkpoint_path(tmp_path),
        "receipt": graph_sync.graph_commit_receipt_path(tmp_path, token),
    }[artifact]
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)

    if artifact == "floor":
        assert graph_sync.floor_state(tmp_path) == ("malformed", None)
    elif artifact == "checkpoint":
        assert graph_sync.checkpoint_state(tmp_path) == ("malformed", None)
    else:
        assert graph_sync.read_graph_commit_receipt(tmp_path, token) is None


@pytest.mark.parametrize("artifact", ["floor", "checkpoint", "receipt"])
def test_protocol_artifact_reader_rejects_replace_during_guarded_read(
    tmp_path: Path, artifact: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import held_fs

    token = "0123456789abcdef01234567"
    path = {
        "floor": graph_sync.floor_path(tmp_path),
        "checkpoint": graph_sync.checkpoint_path(tmp_path),
        "receipt": graph_sync.graph_commit_receipt_path(tmp_path, token),
    }[artifact]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_valid_protocol_artifact(artifact), encoding="utf-8")
    replacement = path.with_suffix(".replacement")
    replacement.write_text(_valid_protocol_artifact(artifact), encoding="utf-8")
    from exomem import state_paths

    # Warm the state-dir anchor's capability probe: its probe READS a probe
    # file, and a patched read that replaces on first call would otherwise
    # spend the replacement on the probe instead of on the guarded read.
    warmed = held_fs.acquire(state_paths.vault_state_dir(tmp_path))
    assert warmed.ok
    warmed.require().close()
    acquired = held_fs.acquire(tmp_path)
    assert acquired.ok
    filesystem = acquired.require()
    filesystem_type = type(filesystem)
    filesystem.close()
    original = filesystem_type.read
    replaced = False

    def replace_after_snapshot(self, file):  # noqa: ANN001
        nonlocal replaced
        result = original(self, file)
        if not replaced:
            replacement.replace(path)
            replaced = True
        return result

    monkeypatch.setattr(filesystem_type, "read", replace_after_snapshot)
    if artifact == "floor":
        assert graph_sync.floor_state(tmp_path) == ("malformed", None)
    elif artifact == "checkpoint":
        assert graph_sync.checkpoint_state(tmp_path) == ("malformed", None)
    else:
        assert graph_sync.read_graph_commit_receipt(tmp_path, token) is None


def test_fresh_vault_missing_graph_artifacts_remain_absent_and_admit_first_write(
    tmp_path: Path,
) -> None:
    assert graph_sync.floor_state(tmp_path) == ("absent", None)
    assert graph_sync.checkpoint_state(tmp_path) == ("absent", None)
    assert graph_sync.status(tmp_path) == {"state": "current", "generation": 0}

    note = tmp_path / "Knowledge Base/Notes/first.md"
    vault.batch_atomic_write(
        [vault.PlannedWrite(note, "# First\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )
    assert graph_sync.checkpoint_state(tmp_path)[0] == "valid"


def test_deeply_nested_protocol_json_fails_closed_through_file_backed_status(
    tmp_path: Path,
) -> None:
    nested = ("[" * 2_000 + "0" + "]" * 2_000).encode("ascii")
    graph_sync.floor_path(tmp_path).parent.mkdir(parents=True)
    graph_sync.floor_path(tmp_path).write_bytes(nested)
    graph_sync.checkpoint_path(tmp_path).write_bytes(nested)
    receipt_path = graph_sync.graph_commit_receipt_path(
        tmp_path, "0123456789abcdef01234567"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(nested)

    assert graph_sync.GraphSyncGenerationFloor.parse(nested) is None
    assert graph_sync.GraphSyncCheckpoint.parse(nested) is None
    assert graph_sync.GraphCommitReceipt.parse(nested) is None
    assert graph_sync.floor_state(tmp_path) == ("malformed", None)
    assert graph_sync.checkpoint_state(tmp_path) == ("malformed", None)
    assert graph_sync.read_graph_commit_receipt(tmp_path, "0123456789abcdef01234567") is None
    assert graph_sync.status(tmp_path)["state"] == "unavailable"


@pytest.mark.parametrize(
    "parser",
    (
        graph_sync.GraphSyncCheckpoint.parse,
        graph_sync.GraphSyncGenerationFloor.parse,
        graph_sync.GraphCommitReceipt.parse,
    ),
)
def test_protocol_parsers_fail_closed_on_json_recursion(
    parser: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        graph_sync.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RecursionError("deep JSON")),
    )
    assert parser(b"{}") is None  # type: ignore[operator]


def test_oversized_sqlite_checkpoint_meta_fails_closed_without_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = graph_sync.floor_path(tmp_path).with_name(".graph.sqlite")
    sidecar.parent.mkdir(parents=True)
    with sqlite3.connect(sidecar) as conn:
        conn.execute("CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("graph_sync_generation", "1"),
                ("graph_sync_digest", "a" * 64),
                ("graph_sync_checkpoint", "x" * 129),
            ),
        )
    monkeypatch.setattr(graph_sync, "_CHECKPOINT_READ_LIMIT", 128)
    monkeypatch.setattr(
        graph_sync.GraphSyncCheckpoint,
        "parse",
        classmethod(lambda _cls, _raw: pytest.fail("oversized meta reached checkpoint parser")),
    )
    assert graph_sync.acknowledgement_state(tmp_path) == ("malformed", None)


def test_checkpoint_parser_rejects_duplicate_closed_json_fields() -> None:
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=4,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    raw = checkpoint.render().replace('"generation":4,', '"generation":4,"generation":4,')

    assert graph_sync.GraphSyncCheckpoint.parse(raw) is None


def test_acknowledgement_requires_the_full_checkpoint_meta_proof(tmp_path: Path) -> None:
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    graph_sync.floor_path(tmp_path).parent.mkdir(parents=True)
    graph_sync.floor_path(tmp_path).write_text(
        graph_sync.GraphSyncGenerationFloor.create(1).render(), encoding="utf-8"
    )
    graph_sync.checkpoint_path(tmp_path).write_text(checkpoint.render(), encoding="utf-8")
    sidecar = graph_sync.floor_path(tmp_path).with_name(".graph.sqlite")
    with sqlite3.connect(sidecar) as conn:
        conn.execute("CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("graph_sync_generation", "1"),
                ("graph_sync_digest", checkpoint.checkpoint_sha256),
            ),
        )

    assert graph_sync.acknowledgement_state(tmp_path)[0] == "malformed"
    assert graph_sync.status(tmp_path)["state"] == "unavailable"

    with sqlite3.connect(sidecar) as conn:
        conn.execute(
            "INSERT INTO graph_meta(key, value) VALUES (?, ?)",
            ("graph_sync_checkpoint", checkpoint.render()),
        )
    assert graph_sync.acknowledgement_state(tmp_path) == (
        "valid",
        graph_sync.GraphBuildOutcome.covering(checkpoint),
    )
    assert graph_sync.status(tmp_path)["state"] == "current"


@pytest.mark.parametrize(
    "read_acknowledgement",
    [
        graph_sync.acknowledgement_state,
        graph_sync._malformed_acknowledgement_generation,
    ],
    ids=["acknowledgement_state", "malformed_generation_hint"],
)
def test_acknowledgement_readers_close_the_sidecar_they_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_acknowledgement: object,
) -> None:
    """A read handle that outlives its reader refuses the next publication.

    `with sqlite3.connect(...)` commits the open transaction and leaves the
    connection itself open -- the context manager is a transaction scope, not a
    close. Every acknowledgement read therefore added one more live handle to
    `.graph.sqlite`, and both readers sit on the graph read path through
    `classify_epoch` / `status` / `_open_read_snapshot`. That is an unbounded
    handle leak in a long-lived server, and on Windows it is the sharing
    violation that makes the publication `os.replace` fail with WinError 32 --
    the failure class the reader-cycling publication hold exists to avoid.
    """
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="0123456789abcdef01234567",
        paths=(),
        created_paths=(),
        scope="full",
    )
    sidecar = graph_sync.floor_path(tmp_path).with_name(".graph.sqlite")
    sidecar.parent.mkdir(parents=True)
    setup = sqlite3.connect(sidecar)
    with setup:
        setup.execute("CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        setup.executemany(
            "INSERT INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("graph_sync_generation", "1"),
                ("graph_sync_digest", checkpoint.checkpoint_sha256),
                ("graph_sync_checkpoint", checkpoint.render()),
            ),
        )
    setup.close()

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(conn)
        return conn

    monkeypatch.setattr(epistemic_graph, "_sqlite_connect_owned", recording_connect)
    read_acknowledgement(tmp_path)  # type: ignore[operator]

    assert opened, "the acknowledgement reader never opened the sidecar"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_status_and_public_graph_availability_agree_on_missing_checkpoint_meta(
    tmp_path: Path,
) -> None:
    from exomem.epistemic_graph import EpistemicGraphIndex

    note = tmp_path / "Knowledge Base/Notes/proof.md"
    vault.batch_atomic_write(
        [vault.PlannedWrite(note, "# Proof\n")], vault_root=tmp_path, post_commit_fanout=False
    )
    index = EpistemicGraphIndex(tmp_path)
    index.rebuild_all()
    assert graph_sync.status(tmp_path)["state"] == "current"
    assert index.available() is True

    with index._connect() as conn:
        conn.execute("DELETE FROM graph_meta WHERE key = 'graph_sync_checkpoint'")

    assert graph_sync.status(tmp_path)["state"] == "unavailable"
    assert index.available() is False


def test_graph_commit_receipt_is_closed_content_free_and_portable(tmp_path: Path) -> None:
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="1" * 64,
        command_digest="2" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba9876543210fedcba98",
        canonical_disposition="success",
        terminal_projection={
            "status": "committed",
            "mutated": True,
        },
        checkpoint_generation=7,
        checkpoint_sha256="3" * 64,
        commit_secret=b"s" * 32,
    )

    assert "secret-command-argument" not in receipt.render()
    assert "secret-payload-value" not in receipt.render()
    assert graph_sync.GraphCommitReceipt.parse(receipt.render()) == receipt
    assert receipt.receipt_hmac_sha256 is not None

    stored = graph_sync.write_graph_commit_receipt(tmp_path, receipt)
    assert stored == graph_sync.graph_commit_receipt_path(tmp_path, receipt.commit_token)
    assert graph_sync.read_graph_commit_receipt(tmp_path, receipt.commit_token) == receipt
    assert graph_sync.is_graph_input_path(
        "Knowledge Base/.graph-commit-receipts/fedcba9876543210fedcba98.json"
    ) is False
    assert (
        portability.classify_artifact(
            "Knowledge Base/.graph-commit-receipts/fedcba9876543210fedcba98.json"
        ).artifact_class
        is portability.ArtifactClass.PORTABLE_DERIVED
    )


def test_v2_graph_commit_receipt_authenticates_every_closed_field() -> None:
    """A copied or edited vault receipt is advisory without its local secret."""
    secret = b"s" * 32
    receipt = graph_sync.GraphCommitReceipt.create(
        idempotency_key_digest="1" * 64,
        command_digest="2" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba9876543210fedcba98",
        canonical_disposition="success",
        terminal_projection={"status": "committed", "mutated": True},
        commit_secret=secret,
    )

    assert receipt.version == 2
    assert receipt.verify(
        secret,
        idempotency_key_digest="1" * 64,
        command_digest="2" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba9876543210fedcba98",
    )
    tampered = receipt.as_dict()
    tampered["terminal_projection"] = {"status": "committed", "mutated": False}
    tampered["terminal_projection_sha256"] = __import__("hashlib").sha256(
        graph_sync._RECEIPT_TERMINAL_DOMAIN
        + graph_sync._canonical_json(tampered["terminal_projection"])
    ).hexdigest()
    parsed = graph_sync.GraphCommitReceipt.parse(graph_sync._canonical_json(tampered))
    assert parsed is not None
    assert not parsed.verify(
        secret,
        idempotency_key_digest="1" * 64,
        command_digest="2" * 64,
        attempt_id="0123456789abcdef01234567",
        commit_token="fedcba9876543210fedcba98",
    )


def test_graph_receipt_rejects_non_nfc_checkpoint_paths() -> None:
    with pytest.raises(ValueError, match="paths must be canonical"):
        graph_sync.GraphSyncCheckpoint.create(
            generation=1,
            mutation_id="0123456789abcdef01234567",
            paths=(("Knowledge Base/Notes/cafe\u0301.md", "1" * 64),),
            created_paths=("Knowledge Base/Notes/cafe\u0301.md",),
        )


def test_receipt_leaf_projection_never_copies_collection_paths() -> None:
    from exomem.mutation_terminal import receipt_leaf_projection

    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "create",
        "collection_id": "c",
        "item_key": "i",
        "before_item_hash": None,
        "after_item_hash": "1" * 64,
        "before_container_hash": "2" * 64,
        "after_container_hash": "3" * 64,
        "affected_paths": ["Knowledge Base/private-secret.md"],
        "payload_hash": "4" * 64,
        "outcome": "committed",
        "audit_correlation": "a",
    }

    assert receipt_leaf_projection(receipt) == {}


def test_namespaced_receipt_key_digest_does_not_cross_replay_namespaces() -> None:
    first = graph_sync.namespaced_idempotency_key_digest("mcp:alice", "client-key")
    second = graph_sync.namespaced_idempotency_key_digest("mcp:bob", "client-key")

    assert first != second
    assert len(first) == 64
    assert "client-key" not in first


def test_floor_checkpoint_and_receipt_writes_do_not_mark_active_mutation_committed(
    tmp_path: Path,
) -> None:
    trace = writer_lease._ACTIVE_MUTATION_TRACE.set(("request", "command", "receipt"))
    committed = writer_lease._ACTIVE_MUTATION_COMMITTED.set(False)
    try:
        floor = graph_sync.GraphSyncGenerationFloor.create(1)
        checkpoint = graph_sync.GraphSyncCheckpoint.create(
            generation=1,
            mutation_id="0123456789abcdef01234567",
            paths=(),
            created_paths=(),
            scope="full",
        )
        receipt = graph_sync.GraphCommitReceipt.create(
            idempotency_key_digest="1" * 64,
            command_digest="2" * 64,
            attempt_id="0123456789abcdef01234567",
            commit_token="fedcba9876543210fedcba98",
            canonical_disposition="success",
            terminal_projection={"status": "committed", "mutated": True},
            commit_secret=b"s" * 32,
        )

        graph_sync._write_floor(tmp_path, floor)
        graph_sync._write_checkpoint(tmp_path, checkpoint)
        graph_sync.write_graph_commit_receipt(tmp_path, receipt)

        assert writer_lease._ACTIVE_MUTATION_COMMITTED.get() is False

        vault.batch_atomic_write(
            [vault.PlannedWrite(tmp_path / "Knowledge Base/Notes/committed.md", "# committed\n")],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )
        assert writer_lease._ACTIVE_MUTATION_COMMITTED.get() is True
    finally:
        writer_lease._ACTIVE_MUTATION_COMMITTED.reset(committed)
        writer_lease._ACTIVE_MUTATION_TRACE.reset(trace)


def test_active_mutation_claim_context_exposes_only_opaque_protocol_identity() -> None:
    assert writer_lease.active_mutation_claim_token() is None
    assert writer_lease.active_mutation_command_digest() is None

    with writer_lease.active_mutation_claim_context(
        claim_token="0123456789abcdef01234567",
        command_digest="a" * 64,
    ):
        assert writer_lease.active_mutation_claim_token() == "0123456789abcdef01234567"
        assert writer_lease.active_mutation_command_digest() == "a" * 64

    assert writer_lease.active_mutation_claim_token() is None
    assert writer_lease.active_mutation_command_digest() is None
