"""Contract tests for the local compaction-continuation checkpoint hook."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import exomem
from exomem._hooks import exomem_continuation_checkpoint as checkpoint

HOOKS = Path(exomem.__file__).parent / "_hooks"
CHECKPOINT_SCRIPT = HOOKS / "exomem_continuation_checkpoint.py"

PINNED_ADAPTER_FIXTURES = {
    "claude": {
        "version": "2.1.207",
        "source": "https://code.claude.com/docs/en/hooks",
        "events": ("PreCompact", "SessionEnd", "SessionStart"),
    },
    "codex": {
        "version": "0.144.3",
        "source": "https://learn.chatgpt.com/docs/hooks.md",
        "source_fetched": "2026-07-13",
        "events": ("PreCompact", "SessionStart"),
    },
}


def test_bundled_checkpoint_module_exists() -> None:
    assert CHECKPOINT_SCRIPT.is_file()


def test_adapter_fixtures_pin_versions_sources_and_contract_matrix() -> None:
    assert checkpoint.EVENT_CONTRACT_VERSION == 1
    assert checkpoint.OUTPUT_CONTRACT_VERSION == 1
    assert checkpoint.ADAPTER_PROVENANCE == PINNED_ADAPTER_FIXTURES
    assert {client: tuple(events) for client, events in checkpoint._CLIENT_EVENTS.items()} == {
        client: fixture["events"] for client, fixture in PINNED_ADAPTER_FIXTURES.items()
    }


def _event(
    *,
    client: str = "claude",
    event: str = "PreCompact",
    session_id: str = "session-1",
    trigger: str | None = "manual",
    source: str | None = None,
    cwd: Path | None = None,
    transcript: Path | None = None,
) -> dict:
    row = {
        "client": client,
        "event": event,
        "session_id": session_id,
        "turn_id": "turn-1",
        "trigger": trigger,
        "source": source,
        "cwd": str(cwd) if cwd else None,
        "transcript_path": str(transcript) if transcript else None,
        "model": "pinned-test-model",
    }
    return row


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("camel", [False, True])
def test_normalizes_pinned_precompact_envelopes(client: str, camel: bool) -> None:
    payload = {
        ("hookEventName" if camel else "hook_event_name"): "PreCompact",
        ("sessionId" if camel else "session_id"): "s-1",
        ("turnId" if camel else "turn_id"): "t-1",
        ("transcriptPath" if camel else "transcript_path"): "/tmp/t.jsonl",
        "cwd": "/tmp/project",
        "trigger": "auto",
        "model": "fixture-model",
        "custom_instructions": "BEARER super-secret",
        "prompt": "tool payload must be ignored",
    }

    normalized = checkpoint.normalize_event(client, payload)

    assert normalized == {
        "contract_version": 1,
        "client": client,
        "event": "PreCompact",
        "session_id": "s-1",
        "turn_id": "t-1",
        "trigger": "auto",
        "source": None,
        "cwd": "/tmp/project",
        "transcript_path": "/tmp/t.jsonl",
        "model": "fixture-model",
        "normalization": {"degradation": [], "truncation": {}},
    }
    assert "secret" not in json.dumps(normalized).lower()


def test_lifecycle_matrix_is_closed_and_alias_conflicts_are_rejected() -> None:
    assert (
        checkpoint.normalize_event("claude", {"hook_event_name": "SessionEnd", "session_id": "s"})[
            "event"
        ]
        == "SessionEnd"
    )
    assert (
        checkpoint.normalize_event("codex", {"hook_event_name": "SessionEnd", "session_id": "s"})
        is None
    )
    assert (
        checkpoint.normalize_event(
            "claude", {"hook_event_name": "SessionStart", "session_id": "s", "source": "compact"}
        )["source"]
        == "compact"
    )
    assert (
        checkpoint.normalize_event(
            "codex", {"hook_event_name": "SessionStart", "session_id": "s", "source": "resume"}
        )["source"]
        == "resume"
    )
    assert (
        checkpoint.normalize_event("codex", {"hook_event_name": "Stop", "session_id": "s"}) is None
    )
    assert (
        checkpoint.normalize_event(
            "claude",
            {
                "hook_event_name": "PreCompact",
                "session_id": "a",
                "sessionId": "b",
                "trigger": "manual",
            },
        )
        is None
    )
    assert (
        checkpoint.normalize_event(
            "claude", {"hook_event_name": "PreCompact", "session_id": "s", "trigger": "other"}
        )
        is None
    )


def test_client_home_resolution_and_collision_resistant_session_paths(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    assert checkpoint.resolve_home("claude", {"EXOMEM_HOOK_HOME": str(shared)}) == shared
    assert checkpoint.resolve_home("codex", {"EXOMEM_HOOK_HOME": str(shared)}) == shared
    assert (
        checkpoint.resolve_home("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path / "c")})
        == tmp_path / "c"
    )
    assert checkpoint.resolve_home("codex", {"CODEX_HOME": str(tmp_path / "x")}) == tmp_path / "x"

    first = checkpoint.session_state_dir(shared, "claude", "unsafe/a")
    second = checkpoint.session_state_dir(shared, "claude", "unsafe:a")
    other_client = checkpoint.session_state_dir(shared, "codex", "unsafe/a")
    assert first != second
    assert first != other_client
    assert first.parent.name == "claude"
    assert other_client.parent.name == "codex"
    assert "/" not in first.name and "\\" not in first.name


def test_transcript_is_hashed_without_parsing_or_persisting_secret_text(tmp_path: Path) -> None:
    home = tmp_path / "home"
    transcript = tmp_path / "malformed.jsonl"
    secret = b"BEARER-very-secret tool_result system developer compact summary"
    transcript.write_bytes(b"\xff\xfe" + secret + b"\n" + b"x" * 70_000)

    built = checkpoint.build_checkpoint(
        _event(cwd=tmp_path / "not-a-repo", transcript=transcript),
        home,
        observed_at_ns=123_000_000_000,
    )
    encoded = checkpoint.encode_checkpoint(built)

    assert len(encoded) <= checkpoint.MAX_CHECKPOINT_BYTES
    assert secret not in encoded
    assert b"tool_result" not in encoded
    binding = built["structural"]["transcript"]
    assert binding["available"] is True
    assert binding["slice_length"] <= checkpoint.TRANSCRIPT_SLICE_BYTES
    assert len(binding["slice_sha256"]) == 64
    assert "content" not in binding
    assert "non_git" in built["structural"]["degradation"]


def test_structural_digest_controls_identity_and_observation_time_does_not() -> None:
    structural = {
        "schema_version": 1,
        "client": "codex",
        "session_id": "same",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "auto",
        "source": None,
        "model": None,
        "state_root_binding": "a" * 64,
        "workspace": {"head": "1" * 40, "dirty_paths": ["a.py"]},
        "transcript": {"available": False},
        "artifacts": [],
        "degradation": ["transcript_unavailable"],
        "truncation": {},
    }
    first = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)
    repeat = checkpoint.finalize_checkpoint(structural, observed_at_ns=200)
    changed = json.loads(json.dumps(structural))
    changed["workspace"]["head"] = "2" * 40
    second = checkpoint.finalize_checkpoint(changed, observed_at_ns=200)

    assert first["checkpoint_id"] == repeat["checkpoint_id"]
    assert first["structural_digest"] == repeat["structural_digest"]
    assert second["checkpoint_id"] != first["checkpoint_id"]
    assert second["structural_digest"] != first["structural_digest"]
    assert first["event_order"][:2] == [-1, -1]


@pytest.mark.parametrize(
    "case",
    [
        "inner_schema",
        "inner_schema_bool",
        "client_event",
        "client_type",
        "trigger_type",
        "session_control",
        "model_type",
        "workspace_path",
        "workspace_path_type",
        "artifact_path",
        "artifact_hash",
        "artifact_lines",
        "transcript_hash",
        "unknown_degradation",
        "degradation_type",
        "branch_bound",
        "workspace_absolute_name",
        "workspace_nested_name",
        "workspace_control_name",
        "codex_session_start",
        "claude_session_start",
    ],
)
def test_decoder_rejects_hostile_self_consistent_structural_contract(case: str) -> None:
    structural = {
        "schema_version": checkpoint.SCHEMA_VERSION,
        "client": "codex",
        "session_id": "schema-session",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "manual",
        "source": None,
        "model": None,
        "state_root_binding": "a" * 64,
        "workspace": {"available": False},
        "transcript": {"available": False},
        "artifacts": [],
        "degradation": ["non_git", "transcript_unavailable"],
        "truncation": {},
    }
    if case == "inner_schema":
        structural["schema_version"] = 999
    elif case == "inner_schema_bool":
        structural["schema_version"] = True
    elif case == "client_event":
        structural["event"] = "SessionEnd"
    elif case == "client_type":
        structural["client"] = ["codex"]
    elif case == "trigger_type":
        structural["trigger"] = ["manual"]
    elif case == "session_control":
        structural["session_id"] = "session\nSECRET"
    elif case == "model_type":
        structural["model"] = ["not", "a", "string"]
    elif case == "workspace_path":
        structural["workspace"] = {
            "available": True,
            "root": "repo",
            "root_sha256": "b" * 64,
            "branch": "main",
            "detached": False,
            "head": "c" * 40,
            "dirty_paths": ["/etc/passwd"],
        }
    elif case == "workspace_path_type":
        structural["workspace"] = {
            "available": True,
            "root": "repo",
            "root_sha256": "b" * 64,
            "branch": "main",
            "detached": False,
            "head": "c" * 40,
            "dirty_paths": [["not", "text"]],
        }
    elif case.startswith("artifact_"):
        artifact = {
            "path": ".task/TASK.md",
            "size": 10,
            "mtime_ns": 20,
            "sha256": "d" * 64,
            "completed_count": 0,
            "incomplete_count": 1,
            "incomplete_lines": [1],
        }
        if case == "artifact_path":
            artifact["path"] = "notes/tasks.md"
        elif case == "artifact_hash":
            artifact["sha256"] = "SECRET/path"
        else:
            artifact["incomplete_lines"] = [0, 2]
        structural["artifacts"] = [artifact]
    elif case == "transcript_hash":
        structural["transcript"] = {
            "available": True,
            "path": {"kind": "relative", "value": "transcript.jsonl"},
            "observed_size": 10,
            "observed_mtime_ns": 20,
            "slice_offset": 0,
            "slice_length": 10,
            "slice_sha256": "not-a-hash",
        }
    elif case == "unknown_degradation":
        structural["degradation"] = ["SECRET /tmp/path"]
    elif case == "degradation_type":
        structural["degradation"] = [["not", "text"]]
    elif case.startswith("workspace_"):
        workspace_name = {
            "workspace_absolute_name": "/tmp/SECRET",
            "workspace_nested_name": "nested/SECRET",
            "workspace_control_name": "repo\nSECRET",
        }[case]
        structural["workspace"] = {
            "available": False,
            "cwd_name": workspace_name,
            "cwd_sha256": "b" * 64,
        }
    elif case.endswith("session_start"):
        structural.update(
            {
                "client": "claude" if case.startswith("claude") else "codex",
                "event": "SessionStart",
                "trigger": None,
                "source": "resume",
            }
        )
    else:
        structural["workspace"] = {
            "available": True,
            "root": "repo",
            "root_sha256": "b" * 64,
            "branch": "é" * 300,
            "detached": False,
            "head": "c" * 40,
            "dirty_paths": [],
        }
    candidate = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)

    assert checkpoint._decode_checkpoint(checkpoint.encode_checkpoint(candidate)) is None


def test_decoder_rejects_boolean_outer_schema_version(tmp_path: Path) -> None:
    candidate = checkpoint.build_checkpoint(
        _event(client="codex"),
        tmp_path,
        observed_at_ns=100,
    )
    candidate["schema_version"] = True

    assert checkpoint._decode_checkpoint(checkpoint.encode_checkpoint(candidate)) is None


@pytest.mark.parametrize(
    ("client", "event", "trigger"),
    [
        ("codex", "PreCompact", "manual"),
        ("claude", "PreCompact", "auto"),
        ("claude", "SessionEnd", None),
    ],
)
def test_supported_write_generations_round_trip_closed_decoder(
    tmp_path: Path,
    client: str,
    event: str,
    trigger: str | None,
) -> None:
    built = checkpoint.build_checkpoint(
        _event(client=client, event=event, trigger=trigger),
        tmp_path,
        observed_at_ns=100,
    )

    assert checkpoint._decode_checkpoint(checkpoint.encode_checkpoint(built)) == built


def test_artifact_policy_is_closed_bounded_content_free_and_dirty_first(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    active = root / "openspec" / "changes" / "active" / "tasks.md"
    older = root / "openspec" / "changes" / "older" / "tasks.md"
    task = root / ".task" / "TASK.md"
    decoy = root / "notes" / "tasks.md"
    for path in (active, older, task, decoy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- [ ] SECRET TASK WORDS\n- [x] done text\n", encoding="utf-8")
    link = root / ".task" / "RESULT.md"
    try:
        link.symlink_to(decoy)
    except OSError:
        pass
    os.utime(active, ns=(1, 1))
    os.utime(older, ns=(9, 9))

    result, truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths={"openspec/changes/active/tasks.md"},
    )
    serialized = json.dumps(result)

    assert result[0]["path"] == "openspec/changes/active/tasks.md"
    assert "notes/tasks.md" not in {row["path"] for row in result}
    assert ".task/RESULT.md" not in {row["path"] for row in result}
    assert "SECRET" not in serialized and "done text" not in serialized
    assert result[0]["incomplete_lines"] == [1]
    assert result[0]["completed_count"] == 1
    assert result[0]["incomplete_count"] == 1
    assert len(result) <= checkpoint.MAX_ARTIFACTS
    assert isinstance(truncation, dict) and isinstance(degradation, list)


def test_artifact_parent_swap_never_profiles_outside_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("the live rename race is POSIX-specific")
    root = tmp_path / "repo"
    task_dir = root / ".task"
    task_dir.mkdir(parents=True)
    task = task_dir / "TASK.md"
    task.write_text("- [ ] original task\n", encoding="utf-8")
    original_sha = checkpoint._sha256_bytes(task.read_bytes())
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_task = outside / "TASK.md"
    outside_task.write_text("- [ ] outside secret\n", encoding="utf-8")
    outside_sha = checkpoint._sha256_bytes(outside_task.read_bytes())
    moved = root / ".task-original"
    real_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        path_text = os.fspath(path)
        directory_fd = kwargs.get("dir_fd")
        if not swapped and path_text == str(task):
            task_dir.rename(moved)
            task_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        fd = real_open(path, flags, *args, **kwargs)
        if not swapped and path_text == ".task" and directory_fd is not None:
            task_dir.rename(moved)
            task_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return fd

    monkeypatch.setattr(os, "open", racing_open)
    result, _, degradation = checkpoint.collect_artifacts(root, dirty_paths=set())

    assert swapped is True
    hashes = {row["sha256"] for row in result}
    assert outside_sha not in hashes
    assert original_sha not in hashes
    assert not any(row["path"] == ".task/TASK.md" for row in result)
    assert "artifact_raced" in degradation


def test_dirty_openspec_candidate_is_seeded_before_bounded_directory_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    changes = root / "openspec" / "changes"
    for number in range(257):
        path = changes / f"change-{number:03d}" / "tasks.md"
        path.parent.mkdir(parents=True)
        path.write_text("- [x] complete\n", encoding="utf-8")
    dirty = changes / "zzzz-dirty" / "tasks.md"
    dirty.parent.mkdir()
    dirty.write_text("- [x] dirty complete\n", encoding="utf-8")

    result, truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths={"openspec/changes/zzzz-dirty/tasks.md"},
    )

    assert result[0]["path"] == "openspec/changes/zzzz-dirty/tasks.md"
    assert truncation["artifact_candidates"] is True
    assert "artifact_candidates_truncated" in degradation


def test_excess_dirty_artifacts_are_bounded_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    dirty: set[str] = set()
    for number in range(300):
        relative = f"openspec/changes/dirty-{number:03d}/tasks.md"
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("- [ ] active\n", encoding="utf-8")
        dirty.add(relative)
    reads: list[str] = []
    real_read = checkpoint._read_regular_relative

    def counted_read(root_handle, relative: str, limit: int):
        reads.append(relative)
        return real_read(root_handle, relative, limit)

    monkeypatch.setattr(checkpoint, "_read_regular_relative", counted_read)
    result, truncation, degradation = checkpoint.collect_artifacts(root, dirty_paths=dirty)

    assert len(reads) <= checkpoint.MAX_ARTIFACT_CANDIDATE_READS
    assert len(result) == checkpoint.MAX_OPENSPEC_ARTIFACTS
    assert all(row["path"] in dirty for row in result)
    assert truncation["dirty_artifact_candidates"] is True
    assert "dirty_artifact_candidates_truncated" in degradation


def test_non_dirty_fallback_prefers_newest_artifact_before_read_bound(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    changes = root / "openspec" / "changes"
    for number in range(300):
        path = changes / f"a{number:03d}" / "tasks.md"
        path.parent.mkdir(parents=True)
        path.write_text("- [x] complete\n", encoding="utf-8")
        os.utime(path, ns=(number + 1, number + 1))
    newest = changes / "zzz-newest" / "tasks.md"
    newest.parent.mkdir()
    newest.write_text("- [ ] still active\n", encoding="utf-8")
    os.utime(newest, ns=(10_000, 10_000))

    result, truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths=set(),
    )

    assert "openspec/changes/zzz-newest/tasks.md" in {row["path"] for row in result}
    assert truncation["artifact_candidates"] is True
    assert "artifact_candidates_truncated" in degradation


def test_empty_change_directories_do_not_report_false_candidate_truncation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    changes = root / "openspec" / "changes"
    for number in range(300):
        (changes / f"empty-{number:03d}").mkdir(parents=True)
    fixed = root / ".task" / "TASK.md"
    fixed.parent.mkdir()
    fixed.write_text("- [ ] active\n", encoding="utf-8")

    result, truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths=set(),
    )

    assert [row["path"] for row in result] == [".task/TASK.md"]
    assert "artifact_candidates" not in truncation
    assert "artifact_candidates_truncated" not in degradation


def test_large_artifact_tree_bounds_metadata_scan_and_keeps_dirty_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    changes = root / "openspec" / "changes"
    for number in range(700):
        task = changes / f"change-{number:03d}" / "tasks.md"
        task.parent.mkdir(parents=True)
        task.write_text("- [x] complete\n", encoding="utf-8")
        os.utime(task, ns=(number + 1, number + 1))
    dirty = changes / "change-699" / "tasks.md"
    dirty.write_text("- [ ] dirty active\n", encoding="utf-8")
    opened_changes = 0
    scanned_changes = 0
    real_open_child = checkpoint._open_secure_child_directory
    real_scandir = os.scandir
    changes_info = changes.stat()

    def counted_open_child(parent, name: str, **kwargs):
        nonlocal opened_changes
        if parent.path.name == "changes":
            opened_changes += 1
        return real_open_child(parent, name, **kwargs)

    class CountedScandir:
        def __init__(self, iterator, counted: bool) -> None:
            self.iterator = iterator
            self.counted = counted

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *args):
            return self.iterator.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal scanned_changes
            value = next(self.iterator)
            if self.counted:
                scanned_changes += 1
            return value

    def counted_scandir(target):
        counted = False
        if isinstance(target, int):
            info = os.fstat(target)
            counted = (info.st_dev, info.st_ino) == (
                changes_info.st_dev,
                changes_info.st_ino,
            )
        return CountedScandir(real_scandir(target), counted)

    monkeypatch.setattr(checkpoint, "_open_secure_child_directory", counted_open_child)
    monkeypatch.setattr(os, "scandir", counted_scandir)
    started = time.monotonic()

    result, truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths={"openspec/changes/change-699/tasks.md"},
    )

    assert opened_changes <= (
        checkpoint.MAX_ARTIFACT_METADATA_CANDIDATES + checkpoint.MAX_ARTIFACT_CANDIDATE_READS
    )
    assert scanned_changes <= checkpoint.MAX_ARTIFACT_METADATA_CANDIDATES + 1
    assert time.monotonic() - started < 2.0
    assert result[0]["path"] == "openspec/changes/change-699/tasks.md"
    assert truncation["artifact_candidates"] is True
    assert "artifact_candidates_truncated" in degradation


def test_unsafe_artifact_change_directory_is_skipped_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    unsafe = root / "openspec" / "changes" / "bad\nchange" / "tasks.md"
    unsafe.parent.mkdir(parents=True)
    unsafe.write_text("- [ ] active\n", encoding="utf-8")

    result, _truncation, degradation = checkpoint.collect_artifacts(
        root,
        dirty_paths=set(),
    )

    assert result == []
    assert "artifact_unsafe" in degradation


def test_porcelain_z_parser_consumes_rename_and_copy_path_pairs() -> None:
    raw = "R  destination.md\0source.md\0C  copied.md\0original.md\0 M ordinary.md\0"

    assert checkpoint._parse_porcelain_z(raw) == [
        "destination.md",
        "source.md",
        "copied.md",
        "original.md",
        "ordinary.md",
    ]


def test_artifact_checkbox_lines_and_paths_are_utf8_byte_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    task = root / ".task" / "TASK.md"
    task.parent.mkdir(parents=True)
    task.write_text("".join(f"- [ ] secret {i}\n" for i in range(100)), encoding="utf-8")

    result, truncation, _ = checkpoint.collect_artifacts(root, dirty_paths=set())

    assert len(result[0]["incomplete_lines"]) == checkpoint.MAX_INCOMPLETE_LINES
    assert truncation["incomplete_lines"] is True
    assert len(result[0]["path"].encode("utf-8")) <= checkpoint.MAX_PATH_BYTES


def test_renderer_is_bounded_structural_and_advisory() -> None:
    structural = {
        "schema_version": 1,
        "client": "claude",
        "session_id": "s",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "manual",
        "source": None,
        "model": None,
        "state_root_binding": "b" * 64,
        "workspace": {
            "root": "project-" + "é" * 400,
            "branch": "feature/recovery",
            "head": "1" * 40,
            "dirty_paths": [f"src/{i}-{'é' * 120}.py" for i in range(128)],
        },
        "transcript": {"available": False},
        "artifacts": [
            {
                "path": f"openspec/changes/{i}/tasks.md",
                "incomplete_count": 2,
                "incomplete_lines": [3, 7],
            }
            for i in range(16)
        ],
        "degradation": ["transcript_unavailable"],
        "truncation": {"dirty_paths": True},
    }
    candidate = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)

    rendered = checkpoint.render_continuation(candidate, status="rollback")

    assert len(rendered.encode("utf-8")) <= checkpoint.MAX_CONTEXT_BYTES
    assert candidate["checkpoint_id"] in rendered
    assert "rollback" in rendered.lower()
    assert "reconcile" in rendered.lower()
    assert "durable stepping-stone" in rendered
    assert "capture completion" in rendered
    assert "objective" not in rendered.lower()


def test_renderer_preserves_active_artifact_and_flags_before_dirty_path_overflow() -> None:
    structural = {
        "schema_version": 1,
        "client": "codex",
        "session_id": "render-priority",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "auto",
        "source": None,
        "model": None,
        "state_root_binding": "c" * 64,
        "workspace": {
            "root": "repo",
            "branch": "main",
            "head": "1" * 40,
            "dirty_paths": [f"very/long/path/{number:03d}/{'x' * 200}.py" for number in range(128)],
        },
        "transcript": {"available": False},
        "artifacts": [
            {
                "path": "openspec/changes/active/tasks.md",
                "incomplete_count": 2,
                "incomplete_lines": [7, 11],
            }
        ],
        "degradation": ["git_status_unavailable"],
        "truncation": {"dirty_paths": True},
    }
    candidate = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)

    rendered = checkpoint.render_continuation(candidate, status="current")

    assert len(rendered.encode("utf-8")) <= checkpoint.MAX_CONTEXT_BYTES
    assert "artifact: openspec/changes/active/tasks.md" in rendered
    assert "lines=[7, 11]" in rendered
    assert "degraded: git_status_unavailable" in rendered
    assert "truncated: dirty_paths" in rendered
    assert "[continuation fields omitted:" in rendered
    assert "dirty paths=" in rendered
    assert "Reconcile these structural pointers" in rendered


def test_renderer_reserves_artifact_omission_marker_at_max_payload() -> None:
    artifacts = [
        {
            "path": "p" * 435,
            "incomplete_count": 64,
            "incomplete_lines": list(range(1, 65)),
        }
        for number in range(checkpoint.MAX_ARTIFACTS)
    ]
    structural = {
        "schema_version": 1,
        "client": "codex",
        "session_id": "artifact-marker-budget",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "auto",
        "source": None,
        "model": None,
        "state_root_binding": "d" * 64,
        "workspace": {},
        "transcript": {"available": False},
        "artifacts": artifacts,
        "degradation": [],
        "truncation": {},
    }
    candidate = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)

    rendered = checkpoint.render_continuation(candidate, status="current")
    emitted = sum(line.startswith("artifact: ") for line in rendered.splitlines())

    assert 0 < emitted < len(artifacts)
    assert (
        f"[continuation fields omitted: artifact pointers={len(artifacts) - emitted}]" in rendered
    )
    assert len(rendered.encode("utf-8")) <= checkpoint.MAX_CONTEXT_BYTES


def test_renderer_reserves_required_evidence_and_global_omission_footer() -> None:
    artifacts = [
        {
            "path": "p" * 407,
            "incomplete_count": 64,
            "incomplete_lines": list(range(1, 65)),
        }
        for _ in range(checkpoint.MAX_ARTIFACTS)
    ]
    dirty = [f"src/{number:03d}-{'d' * 200}.py" for number in range(128)]
    structural = {
        "schema_version": 1,
        "client": "codex",
        "session_id": "required-render-evidence",
        "turn_id": None,
        "event": "PreCompact",
        "trigger": "auto",
        "source": None,
        "model": None,
        "state_root_binding": "e" * 64,
        "workspace": {
            "available": True,
            "root": "repo",
            "root_sha256": "f" * 64,
            "branch": "main",
            "detached": False,
            "head": "1" * 40,
            "dirty_paths": dirty,
        },
        "transcript": {
            "available": True,
            "path": {"kind": "relative", "value": "transcript.jsonl"},
            "observed_size": 100,
            "observed_mtime_ns": 200,
            "slice_offset": 0,
            "slice_length": 100,
            "slice_sha256": "a" * 64,
        },
        "artifacts": artifacts,
        "degradation": [],
        "truncation": {"dirty_paths": True},
    }
    candidate = checkpoint.finalize_checkpoint(structural, observed_at_ns=100)

    rendered = checkpoint.render_continuation(candidate, status="current")

    assert "workspace: repo branch=main head=" in rendered
    assert "transcript binding: size=100 offset=0 length=100 sha256=" in rendered
    assert "[continuation fields omitted:" in rendered
    assert "artifact pointers=" in rendered
    footer = next(
        line for line in rendered.splitlines() if line.startswith("[continuation fields omitted:")
    )
    omitted_dirty = int(footer.split("dirty paths=", 1)[1].split("]", 1)[0])
    assert 0 < omitted_dirty <= len(dirty)
    assert len(rendered.encode("utf-8")) <= checkpoint.MAX_CONTEXT_BYTES


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_both_adapters_call_the_same_shared_core_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    calls: list[tuple[str, str]] = []
    neutral_outputs: list[dict | None] = []

    def write(event: dict, home: Path, **_: object) -> dict:
        assert event["contract_version"] == checkpoint.EVENT_CONTRACT_VERSION
        calls.append(("write", event["client"]))
        return {"status": "written", "checkpoint_id": "id"}

    candidate = {"checkpoint_id": "id", "structural": {}}

    def select(event: dict, home: Path, **_: object) -> tuple[dict, str]:
        assert event["contract_version"] == checkpoint.EVENT_CONTRACT_VERSION
        calls.append(("select", event["client"]))
        return candidate, "current"

    def render(value: dict, *, status: str) -> str:
        assert value is candidate and status == "current"
        calls.append(("render", client))
        return "bounded context"

    monkeypatch.setattr(checkpoint, "write_checkpoint", write)
    monkeypatch.setattr(checkpoint, "select_checkpoint", select)
    monkeypatch.setattr(checkpoint, "render_continuation", render)
    real_output_adapter = checkpoint._OUTPUT_ADAPTERS[client]

    def output_adapter(value: dict | None) -> dict | None:
        neutral_outputs.append(value)
        return real_output_adapter(value)

    monkeypatch.setitem(checkpoint._OUTPUT_ADAPTERS, client, output_adapter)

    write_result = checkpoint.dispatch_event(
        client,
        {"hook_event_name": "PreCompact", "session_id": "s", "trigger": "manual"},
        environ={"EXOMEM_HOOK_HOME": str(tmp_path)},
    )
    start_result = checkpoint.dispatch_event(
        client,
        {"hook_event_name": "SessionStart", "session_id": "s", "source": "resume"},
        environ={"EXOMEM_HOOK_HOME": str(tmp_path)},
    )

    assert write_result is None
    assert start_result == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "bounded context",
        }
    }
    assert calls == [("write", client), ("select", client), ("render", client)]
    assert neutral_outputs == [
        None,
        {
            "contract_version": checkpoint.OUTPUT_CONTRACT_VERSION,
            "kind": "continuation",
            "event": "SessionStart",
            "additional_context": "bounded context",
        },
    ]


def test_equivalent_client_events_share_one_versioned_normalized_contract() -> None:
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": "same",
        "trigger": "manual",
        "cwd": "/tmp/repo",
    }
    claude = checkpoint.normalize_event("claude", payload)
    codex = checkpoint.normalize_event("codex", payload)

    assert claude is not None and codex is not None
    assert claude["contract_version"] == codex["contract_version"] == 1
    assert {key: value for key, value in claude.items() if key != "client"} == {
        key: value for key, value in codex.items() if key != "client"
    }


def test_shared_core_rejects_unknown_event_contract_before_state_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def future_adapter(_payload: object) -> dict:
        return {
            "contract_version": 999,
            "client": "codex",
            "event": "PreCompact",
            "session_id": "future-contract",
            "turn_id": None,
            "trigger": "manual",
            "source": None,
            "cwd": None,
            "transcript_path": None,
            "model": None,
            "normalization": {"degradation": [], "truncation": {}},
        }

    monkeypatch.setitem(checkpoint._INPUT_ADAPTERS, "codex", future_adapter)

    output = checkpoint.dispatch_event(
        "codex",
        {"hook_event_name": "PreCompact", "session_id": "ignored"},
        environ={"EXOMEM_HOOK_HOME": str(tmp_path)},
    )

    assert output is None
    assert not checkpoint.client_state_root(tmp_path, "codex").exists()


@pytest.mark.parametrize(
    "override",
    [
        {"event": "Stop", "trigger": None},
        {"client": "claude"},
        {"trigger": "automatic"},
        {"trigger": ["manual"]},
        {"source": "resume"},
        {"session_id": ["not", "text"]},
        {"normalization": {"degradation": [["not", "text"]], "truncation": {}}},
        {"contract_version": True},
    ],
)
def test_shared_core_rejects_malformed_v1_normalized_event_before_state_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
) -> None:
    normalized: dict[str, object] = {
        "contract_version": checkpoint.EVENT_CONTRACT_VERSION,
        "client": "codex",
        "event": "PreCompact",
        "session_id": "closed-contract",
        "turn_id": None,
        "trigger": "manual",
        "source": None,
        "cwd": None,
        "transcript_path": None,
        "model": None,
        "normalization": {"degradation": [], "truncation": {}},
    }
    normalized.update(override)
    calls: list[str] = []
    monkeypatch.setitem(checkpoint._INPUT_ADAPTERS, "codex", lambda _payload: normalized)
    monkeypatch.setattr(
        checkpoint,
        "write_checkpoint",
        lambda *_args, **_kwargs: calls.append("write"),
    )
    monkeypatch.setattr(
        checkpoint,
        "select_checkpoint",
        lambda *_args, **_kwargs: calls.append("select"),
    )

    output = checkpoint.dispatch_event(
        "codex",
        {"hook_event_name": "PreCompact", "session_id": "ignored"},
        environ={"EXOMEM_HOOK_HOME": str(tmp_path)},
    )

    assert output is None
    assert calls == []
    assert not checkpoint.client_state_root(tmp_path, "codex").exists()
    assert not checkpoint.client_state_root(tmp_path, "claude").exists()


def test_output_adapter_rejects_boolean_contract_version() -> None:
    assert (
        checkpoint._emit_continuation_output(
            {
                "contract_version": True,
                "kind": "continuation",
                "event": "SessionStart",
                "additional_context": "bounded context",
            }
        )
        is None
    )


def test_subprocess_unknown_event_soft_fails_without_output_or_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "codex"],
        input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "s"}),
        capture_output=True,
        text=True,
        env={**os.environ, "EXOMEM_HOOK_HOME": str(home)},
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not home.exists()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def test_git_status_timeout_is_degraded_not_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_run = subprocess.run

    def timeout_status(command, *args, **kwargs):
        if command[0] == "git" and "status" in command:
            raise subprocess.TimeoutExpired(command, checkpoint.GIT_TIMEOUT_SECONDS)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout_status)
    workspace, _, _, degradation = checkpoint.profile_workspace(str(repo))

    assert workspace["dirty_paths"] == []
    assert "git_status_unavailable" in degradation


@pytest.mark.parametrize(
    ("returncode", "detached", "degraded"),
    [(1, True, False), (128, None, True)],
)
def test_branch_probe_distinguishes_detached_from_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    detached: bool | None,
    degraded: bool,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_run = subprocess.run

    def branch_result(command, *args, **kwargs):
        if command[0] == "git" and "symbolic-ref" in command:
            return subprocess.CompletedProcess(command, returncode, stdout="", stderr="probe")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", branch_result)
    workspace, _, _, degradation = checkpoint.profile_workspace(str(repo))

    assert workspace["branch"] is None
    assert workspace["detached"] is detached
    assert ("git_branch_unavailable" in degradation) is degraded


@pytest.mark.skipif(os.name == "nt", reason="executable sentinel uses a POSIX shell")
def test_workspace_profile_disables_repo_configured_fsmonitor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    marker = tmp_path / "fsmonitor-ran"
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n", encoding="utf-8")
    monitor.chmod(0o700)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(monitor)],
        check=True,
    )

    workspace, _, _, _ = checkpoint.profile_workspace(str(repo))

    assert workspace["available"] is True
    assert not marker.exists()


def test_git_probe_uses_bounded_environment_without_arbitrary_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("EXOMEM_ARBITRARY_SECRET", "must-not-reach-git")
    real_run = subprocess.run
    observed: list[dict[str, str] | None] = []

    def inspect_environment(command, *args, **kwargs):
        if command[0] == "git":
            observed.append(kwargs.get("env"))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", inspect_environment)
    checkpoint.profile_workspace(str(repo))

    assert observed and all(env is not None for env in observed)
    assert all("EXOMEM_ARBITRARY_SECRET" not in env for env in observed if env is not None)
    assert all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in observed if env is not None)


def test_workspace_branch_is_utf8_byte_bounded_with_explicit_flags(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    branch = "/".join(["é" * 100] * 4)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", branch], check=True)

    workspace, _, truncation, degradation = checkpoint.profile_workspace(str(repo))

    assert len(workspace["branch"].encode("utf-8")) <= checkpoint.MAX_PATH_BYTES
    assert "�" not in workspace["branch"]
    assert truncation["branch_bytes"] is True
    assert "git_branch_truncated" in degradation


def test_normalized_identifiers_are_bounded_with_explicit_metadata() -> None:
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": "会" * 300,
        "turn_id": "é" * 400,
        "model": "m" * 700,
        "trigger": "manual",
    }

    event = checkpoint.normalize_event("codex", payload)

    assert event is not None
    assert len(event["session_id"].encode("utf-8")) <= checkpoint.MAX_IDENTIFIER_BYTES
    assert len(event["turn_id"].encode("utf-8")) <= checkpoint.MAX_IDENTIFIER_BYTES
    assert len(event["model"].encode("utf-8")) <= checkpoint.MAX_IDENTIFIER_BYTES
    assert event["session_id"].startswith("sha256:")
    assert event["turn_id"].startswith("sha256:")
    assert event["normalization"]["truncation"] == {
        "model_bytes": True,
        "session_id_bytes": True,
        "turn_id_bytes": True,
    }
    assert set(event["normalization"]["degradation"]) == {
        "model_truncated",
        "session_id_hashed",
        "turn_id_hashed",
    }


@pytest.mark.parametrize("raw_session", ["s\nsecret", "s\ud800secret"])
def test_control_or_surrogate_session_normalization_round_trips_without_leak(
    tmp_path: Path,
    raw_session: str,
) -> None:
    event = checkpoint.normalize_event(
        "codex",
        {
            "hook_event_name": "PreCompact",
            "session_id": raw_session,
            "turn_id": "turn\rsecret",
            "model": "model\x00secret",
            "trigger": "manual",
        },
    )

    assert event is not None
    assert event["session_id"].startswith("sha256:")
    assert event["turn_id"].startswith("sha256:")
    assert event["model"].startswith("sha256:")
    checkpoint.write_checkpoint(event, tmp_path, observed_at_ns=100)
    state = checkpoint.session_state_dir(tmp_path, "codex", event["session_id"])
    current = checkpoint.load_checkpoint(state / "current.json")

    assert current is not None
    raw = (state / "current.json").read_bytes()
    assert b"secret" not in raw


def test_control_workspace_paths_are_omitted_or_hashed_before_round_trip(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo\nunsafe"
    _init_repo(repo)
    (repo / "dirty\nunsafe.txt").write_text("dirty\n", encoding="utf-8")

    built = checkpoint.build_checkpoint(
        _event(client="codex", cwd=repo),
        home,
        observed_at_ns=100,
    )
    raw = checkpoint.encode_checkpoint(built)
    decoded = checkpoint._decode_checkpoint(raw)

    assert decoded == built
    workspace = built["structural"]["workspace"]
    assert "\n" not in workspace["root"]
    assert workspace["dirty_paths"] == []
    assert "workspace_name_hashed" in built["structural"]["degradation"]
    assert "dirty_path_unsafe" in built["structural"]["degradation"]


# Built lazily: os.fsdecode of a non-UTF-8 byte name raises on Windows, and a
# decorator argument is evaluated at import time, so the skipif below cannot
# protect it. Keeping the surrogateescape case POSIX-only keeps this module
# importable on Windows.
_UNSAFE_TRANSCRIPT_NAMES = ["transcript\nunsafe.jsonl"]
if os.name != "nt":
    _UNSAFE_TRANSCRIPT_NAMES.append(os.fsdecode(b"transcript-\xff.jsonl"))


@pytest.mark.skipif(os.name == "nt", reason="surrogateescape path is POSIX-specific")
@pytest.mark.parametrize("name", _UNSAFE_TRANSCRIPT_NAMES)
def test_unsafe_transcript_relative_path_hashes_and_round_trips(
    tmp_path: Path,
    name: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    transcript = home / name
    transcript.write_bytes(b"bounded transcript")

    built = checkpoint.build_checkpoint(
        _event(client="codex", transcript=transcript),
        home,
        observed_at_ns=100,
    )

    assert checkpoint._decode_checkpoint(checkpoint.encode_checkpoint(built)) == built
    assert built["structural"]["transcript"]["path"]["kind"] == "sha256"
    assert "transcript_path_hashed" in built["structural"]["degradation"]


def test_overbound_relative_transcript_path_surfaces_byte_truncation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    transcript = home
    for number in range(8):
        transcript /= f"{number}-{'é' * 40}"
    transcript.mkdir(parents=True)
    transcript /= "transcript.jsonl"
    transcript.write_bytes(b"bounded transcript")

    built = checkpoint.build_checkpoint(
        _event(client="codex", transcript=transcript),
        home,
        observed_at_ns=100,
    )

    assert checkpoint._decode_checkpoint(checkpoint.encode_checkpoint(built)) == built
    assert built["structural"]["transcript"]["path"]["kind"] == "sha256"
    assert built["structural"]["truncation"]["transcript_path_bytes"] is True


def test_write_is_idempotent_rotates_once_and_rejects_stale_writer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    event = _event(client="codex")

    first = checkpoint.write_checkpoint(event, home, observed_at_ns=200)
    repeat = checkpoint.write_checkpoint(event, home, observed_at_ns=300)
    state = checkpoint.session_state_dir(home, "codex", "session-1")

    assert first["status"] == "written"
    assert repeat == {"status": "idempotent", "checkpoint_id": first["checkpoint_id"]}
    assert (state / "current.json").is_file()
    assert not (state / "previous.json").exists()

    newer = checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=400)
    stale = checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    current = checkpoint.load_checkpoint(state / "current.json")

    assert newer["status"] == "written"
    assert stale["status"] == "stale"
    assert current["checkpoint_id"] == newer["checkpoint_id"]
    assert (
        checkpoint.load_checkpoint(state / "previous.json")["checkpoint_id"]
        == first["checkpoint_id"]
    )
    assert stat_mode(state / "current.json") == 0o600
    assert stat_mode(state / ".lock") == 0o600
    assert len(list(state.glob("*.tmp-*"))) == 0


def test_idempotent_redelivery_refreshes_retention_without_rotating_history(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="refresh")
    first_observed = 100
    first = checkpoint.write_checkpoint(event, home, observed_at_ns=first_observed)
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="refresh",
        trigger=None,
        source="resume",
    )
    refresh_observed = first_observed + checkpoint.RETENTION_NS + 2
    assert (
        checkpoint.select_checkpoint(
            start,
            home,
            now_ns=refresh_observed,
        )
        is None
    )

    repeated = checkpoint.write_checkpoint(
        event,
        home,
        observed_at_ns=refresh_observed,
    )
    selected = checkpoint.select_checkpoint(
        start,
        home,
        now_ns=refresh_observed + 1,
    )
    state = checkpoint.session_state_dir(home, "codex", "refresh")
    current = checkpoint.load_checkpoint(state / "current.json")

    assert repeated == {"status": "idempotent", "checkpoint_id": first["checkpoint_id"]}
    assert selected is not None and selected[1] == "current"
    assert current["observed_at_ns"] == refresh_observed
    assert current["event_order"][-2] == refresh_observed
    assert not (state / "previous.json").exists()


@pytest.mark.parametrize("corrupt_current", [False, True])
def test_same_id_retry_recovers_interrupted_rotation_as_one_fresh_generation(
    tmp_path: Path,
    corrupt_current: bool,
) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="same-id-recovery")
    first = checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    state_path = checkpoint.session_state_dir(home, "codex", "same-id-recovery")
    with checkpoint._session_lock(
        home,
        "codex",
        "same-id-recovery",
        create=False,
    ) as state:
        checkpoint._replace_at(state, "current.json", "previous.json")
        if corrupt_current:
            fd = checkpoint._open_secure_file_at(
                state,
                "current.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(fd, b"corrupt")
            finally:
                os.close(fd)

    repeated = checkpoint.write_checkpoint(event, home, observed_at_ns=200)
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="same-id-recovery",
        trigger=None,
        source="resume",
    )
    selected = checkpoint.select_checkpoint(start, home, now_ns=201)
    current = checkpoint.load_checkpoint(state_path / "current.json")

    assert repeated == {"status": "idempotent", "checkpoint_id": first["checkpoint_id"]}
    assert current is not None and current["observed_at_ns"] == 200
    assert selected is not None and selected[1] == "current"
    assert not (state_path / "previous.json").exists()


def test_interrupted_rotation_previous_is_ordering_floor_for_older_different_id(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "rotation-order-floor"
    newer_event = _event(client="codex", session_id=session, trigger="auto")
    newer = checkpoint.write_checkpoint(newer_event, home, observed_at_ns=200)
    state_path = checkpoint.session_state_dir(home, "codex", session)
    with checkpoint._session_lock(home, "codex", session, create=False) as state:
        checkpoint._replace_at(state, "current.json", "previous.json")

    stale = checkpoint.write_checkpoint(
        {**newer_event, "trigger": "manual"},
        home,
        observed_at_ns=100,
    )
    current = checkpoint.load_checkpoint(state_path / "current.json")

    assert stale["status"] == "stale"
    assert current is not None and current["checkpoint_id"] == newer["checkpoint_id"]
    assert not (state_path / "previous.json").exists()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_structural_workspace_change_rotates_with_unchanged_transcript(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _init_repo(repo)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"unchanged transcript")
    event = _event(client="codex", cwd=repo, transcript=transcript)

    first = checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    second = checkpoint.write_checkpoint(event, home, observed_at_ns=200)

    assert second["checkpoint_id"] != first["checkpoint_id"]
    current = checkpoint.load_checkpoint(
        checkpoint.session_state_dir(home, "codex", "session-1") / "current.json"
    )
    assert current["structural"]["workspace"]["dirty_paths"] == ["tracked.txt"]


def test_build_reuses_validated_workspace_root_for_artifact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _init_repo(repo)
    task = repo / ".task" / "TASK.md"
    task.parent.mkdir()
    task.write_text("- [ ] active\n", encoding="utf-8")
    real_git = checkpoint._git
    root_probes = 0

    def flaky_second_probe(cwd: Path, *args: str) -> str | None:
        nonlocal root_probes
        if args == ("rev-parse", "--show-toplevel"):
            root_probes += 1
            if root_probes > 1:
                return None
        return real_git(cwd, *args)

    monkeypatch.setattr(checkpoint, "_git", flaky_second_probe)

    built = checkpoint.build_checkpoint(
        _event(client="codex", cwd=repo),
        home,
        observed_at_ns=100,
    )

    assert root_probes == 1
    assert ".task/TASK.md" in {artifact["path"] for artifact in built["structural"]["artifacts"]}


def test_append_safe_selection_and_explicit_non_detection_outside_saved_slice(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"A" * 80_000)
    write_event = _event(client="claude", transcript=transcript)
    checkpoint.write_checkpoint(write_event, home, observed_at_ns=time.time_ns())
    start = _event(
        client="claude", event="SessionStart", trigger=None, source="compact", transcript=transcript
    )

    selected = checkpoint.select_checkpoint(start, home)
    assert selected is not None and selected[1] == "current"

    with transcript.open("ab") as stream:
        stream.write(b"appended compaction record")
    assert checkpoint.select_checkpoint(start, home) is not None

    with transcript.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"B")
    assert checkpoint.select_checkpoint(start, home) is not None

    current = selected[0]
    offset = current["structural"]["transcript"]["slice_offset"]
    with transcript.open("r+b") as stream:
        stream.seek(offset)
        stream.write(b"C")
    assert checkpoint.select_checkpoint(start, home) is None


def test_truncation_rejected_and_corrupt_current_falls_back_to_valid_previous(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"first" * 20_000)
    manual = _event(client="claude", transcript=transcript)
    checkpoint.write_checkpoint(manual, home, observed_at_ns=time.time_ns() - 2)
    checkpoint.write_checkpoint(
        {**manual, "trigger": "auto"},
        home,
        observed_at_ns=time.time_ns() - 1,
    )
    state = checkpoint.session_state_dir(home, "claude", "session-1")
    (state / "current.json").write_bytes(b"not-json")
    start = _event(
        client="claude",
        event="SessionStart",
        trigger=None,
        source="resume",
        transcript=transcript,
    )

    selected = checkpoint.select_checkpoint(start, home)
    assert selected is not None and selected[1] == "rollback"

    transcript.write_bytes(b"short")
    assert checkpoint.select_checkpoint(start, home) is None


def test_structurally_valid_current_binding_failure_does_not_fall_back(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    transcript = tmp_path / "transcript.jsonl"
    original = b"A" * 80_000
    transcript.write_bytes(original)
    event = _event(client="codex", session_id="no-invalid-fallback", transcript=transcript)
    checkpoint.write_checkpoint(event, home, observed_at_ns=time.time_ns() - 2)
    transcript.write_bytes(b"B" * 80_000)
    checkpoint.write_checkpoint(
        {**event, "trigger": "auto"},
        home,
        observed_at_ns=time.time_ns() - 1,
    )
    transcript.write_bytes(original)
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="no-invalid-fallback",
        trigger=None,
        source="resume",
        transcript=transcript,
    )

    assert checkpoint.select_checkpoint(start, home) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX generation modes")
def test_broad_current_mode_is_corrupt_and_rolls_back_to_restrictive_previous(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="broad-current", trigger="manual")
    previous = checkpoint.write_checkpoint(event, home, observed_at_ns=time.time_ns() - 2)
    checkpoint.write_checkpoint(
        {**event, "trigger": "auto"},
        home,
        observed_at_ns=time.time_ns() - 1,
    )
    state = checkpoint.session_state_dir(home, "codex", "broad-current")
    (state / "current.json").chmod(0o644)
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="broad-current",
        trigger=None,
        source="resume",
    )

    selected = checkpoint.select_checkpoint(start, home)

    assert selected is not None and selected[1] == "rollback"
    assert selected[0]["checkpoint_id"] == previous["checkpoint_id"]


def test_generation_order_inversion_is_rejected_by_live_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    observed = time.time_ns()
    event = _event(client="codex", session_id="inverted", trigger="manual")
    checkpoint.write_checkpoint(event, home, observed_at_ns=observed - 2)
    checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=observed - 1)
    state = checkpoint.session_state_dir(home, "codex", "inverted")
    current_raw = (state / "current.json").read_bytes()
    previous_raw = (state / "previous.json").read_bytes()
    (state / "current.json").write_bytes(previous_raw)
    (state / "previous.json").write_bytes(current_raw)
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="inverted",
        trigger=None,
        source="resume",
    )

    assert checkpoint.select_checkpoint(start, home, now_ns=observed) is None


def test_duplicate_current_and_previous_generation_is_rejected_by_live_selection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    observed = time.time_ns()
    event = _event(client="codex", session_id="duplicate-history", trigger="manual")
    checkpoint.write_checkpoint(event, home, observed_at_ns=observed - 1)
    state = checkpoint.session_state_dir(home, "codex", "duplicate-history")
    shutil.copy2(state / "current.json", state / "previous.json")
    start = _event(
        client="codex",
        event="SessionStart",
        session_id="duplicate-history",
        trigger=None,
        source="resume",
    )

    assert checkpoint.select_checkpoint(start, home, now_ns=observed) is None


def test_selection_rejects_foreign_stale_and_wrong_state_binding(tmp_path: Path) -> None:
    home = tmp_path / "home"
    event = _event(client="codex")
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    start = _event(client="codex", event="SessionStart", trigger=None, source="resume")

    assert (
        checkpoint.select_checkpoint(
            start,
            home,
            now_ns=100 + checkpoint.RETENTION_NS + 1,
        )
        is None
    )
    assert checkpoint.select_checkpoint({**start, "session_id": "other"}, home) is None

    state = checkpoint.session_state_dir(home, "codex", "session-1")
    value = checkpoint.load_checkpoint(state / "current.json")
    value["structural"]["state_root_binding"] = "wrong"
    (state / "current.json").write_bytes(checkpoint.encode_checkpoint(value))
    assert checkpoint.select_checkpoint(start, home, now_ns=101) is None


def test_symlinked_state_and_current_are_rejected_without_touching_target(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    state_root = checkpoint.client_state_root(home, "claude")
    state_root.parent.mkdir(parents=True)
    state_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        checkpoint.write_checkpoint(_event(), home)
    assert list(target.iterdir()) == []

    state_root.unlink()
    checkpoint.write_checkpoint(_event(), home, observed_at_ns=100)
    state = checkpoint.session_state_dir(home, "claude", "session-1")
    current = state / "current.json"
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    current.unlink()
    current.symlink_to(outside)
    with pytest.raises(OSError):
        checkpoint.write_checkpoint({**_event(), "trigger": "auto"}, home, observed_at_ns=200)
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode policy")
def test_insecure_existing_state_root_fails_closed_without_checkpoint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o777)

    with pytest.raises(OSError, match="permissions"):
        checkpoint.write_checkpoint(_event(client="codex"), home, observed_at_ns=100)

    state = checkpoint.session_state_dir(home, "codex", "session-1")
    assert not (state / "current.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode policy")
def test_insecure_existing_session_directory_fails_closed_without_checkpoint(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    state = checkpoint.session_state_dir(home, "codex", "session-1")
    state.mkdir(mode=0o777)
    state.chmod(0o777)

    with pytest.raises(OSError, match="permissions"):
        checkpoint.write_checkpoint(_event(client="codex"), home, observed_at_ns=100)

    assert not (state / "current.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles prevent the test rename")
def test_session_writer_stays_on_retained_root_after_root_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    moved = root.with_name("codex-original")
    real_open = checkpoint._open_posix_directory
    swapped = False

    def swap_after_open(path: Path, *, create: bool, mode: int) -> int:
        nonlocal swapped
        fd = real_open(path, create=create, mode=mode)
        if not swapped and path.absolute() == root.absolute():
            root.rename(moved)
            root.mkdir(mode=0o700)
            swapped = True
        return fd

    monkeypatch.setattr(checkpoint, "_open_posix_directory", swap_after_open)
    checkpoint.write_checkpoint(
        _event(client="codex", session_id="retained-root"),
        home,
        observed_at_ns=100,
    )

    state_name = checkpoint.session_state_dir(home, "codex", "retained-root").name
    assert swapped is True
    assert not (root / state_name).exists()
    assert (moved / state_name / "current.json").is_file()


def test_os_advisory_lock_times_out_and_releases_when_owner_is_killed(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    code = (
        "import sys,time; "
        "from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "x=c.advisory_lock(Path(sys.argv[1]), timeout=1); x.__enter__(); "
        "print('locked', flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError):
            with checkpoint.advisory_lock(lock, timeout=0.05):
                pass
        child.kill()
        child.wait(timeout=5)
        with checkpoint.advisory_lock(lock, timeout=0.5):
            assert lock.read_bytes()
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.parametrize("force_fallback", [False, True])
def test_expired_state_is_tombstoned_and_pruned_with_both_lock_orders(
    tmp_path: Path, force_fallback: bool
) -> None:
    home = tmp_path / "home"
    old = _event(client="codex", session_id="old")
    current = _event(client="codex", session_id="current")
    checkpoint.write_checkpoint(old, home, observed_at_ns=100)
    checkpoint.write_checkpoint(current, home, observed_at_ns=200)

    removed = checkpoint.prune_expired(
        home,
        "codex",
        current_session="current",
        now_ns=100 + checkpoint.RETENTION_NS + 1,
        force_fallback=force_fallback,
    )

    assert removed == 1
    assert not checkpoint.session_state_dir(home, "codex", "old").exists()
    assert checkpoint.session_state_dir(home, "codex", "current").exists()
    assert not list(checkpoint.client_state_root(home, "codex").glob(".tombstone-*"))


def test_prune_refuses_copied_checkpoint_in_arbitrary_user_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    checkpoint.write_checkpoint(
        _event(client="codex", session_id="real-expired"),
        home,
        observed_at_ns=100,
    )
    source = checkpoint.session_state_dir(home, "codex", "real-expired")
    valuable = checkpoint.client_state_root(home, "codex") / "important-user-directory"
    shutil.copytree(source, valuable)
    (valuable / "valuable.txt").write_text("do not delete", encoding="utf-8")

    removed = checkpoint.prune_expired(
        home,
        "codex",
        current_session="active",
        now_ns=100 + checkpoint.RETENTION_NS + 1,
    )

    assert removed == 1
    assert valuable.is_dir()
    assert (valuable / "valuable.txt").read_text(encoding="utf-8") == "do not delete"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits replacing an open directory entry")
def test_tombstone_replacement_is_not_removed_by_stale_retained_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "root"
    tombstone_path = root_path / ".tombstone-session-token"
    tombstone_path.mkdir(parents=True)
    (tombstone_path / "current.json").write_text("discard", encoding="utf-8")
    moved = root_path / ".tombstone-original"
    real_unlink = checkpoint._unlink_at
    swapped = False

    def swap_after_unlink(directory, name: str) -> None:
        nonlocal swapped
        real_unlink(directory, name)
        if not swapped:
            tombstone_path.rename(moved)
            tombstone_path.mkdir()
            swapped = True

    monkeypatch.setattr(checkpoint, "_unlink_at", swap_after_unlink)
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        removed = checkpoint._delete_tombstone_at(root, tombstone_path.name)

    assert swapped is True
    assert removed is False
    assert tombstone_path.is_dir()


def test_tombstone_revalidates_exact_child_allowlist_before_unlink(tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    tombstone_path = root_path / ".tombstone-session-token"
    tombstone_path.mkdir(parents=True)
    current = tombstone_path / "current.json"
    valuable = tombstone_path / "valuable.txt"
    current.write_text("checkpoint", encoding="utf-8")
    valuable.write_text("preserve", encoding="utf-8")

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        removed = checkpoint._delete_tombstone_at(root, tombstone_path.name)

    assert removed is False
    assert current.read_text(encoding="utf-8") == "checkpoint"
    assert valuable.read_text(encoding="utf-8") == "preserve"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits replacing an open directory entry")
def test_tombstone_expected_identity_rejects_replacement_before_open(tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    tombstone_path = root_path / ".tombstone-session-token"
    tombstone_path.mkdir(parents=True)
    (tombstone_path / "current.json").write_text("original", encoding="utf-8")
    moved = root_path / ".tombstone-original"

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._open_secure_child_directory(
            root, tombstone_path.name, create=False
        ) as original:
            identity = checkpoint._directory_identity(original)
        tombstone_path.rename(moved)
        tombstone_path.mkdir()
        replacement = tombstone_path / "current.json"
        replacement.write_text("replacement", encoding="utf-8")
        removed = checkpoint._delete_tombstone_at(root, tombstone_path.name, identity)

    assert removed is False
    assert replacement.read_text(encoding="utf-8") == "replacement"
    assert (moved / "current.json").read_text(encoding="utf-8") == "original"


def test_tombstone_and_temporary_cleanup_never_materialize_unbounded_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "root"
    tombstone_path = root_path / ".tombstone-session-0000000000000000"
    state_path = root_path / "state"
    tombstone_path.mkdir(parents=True)
    state_path.mkdir()
    (tombstone_path / "foreign.txt").write_text("preserve", encoding="utf-8")
    precomputed = [f"current.json.tmp-1-{number:016x}" for number in range(300_000)]
    inspected = 0
    real_list = checkpoint._list_directory

    def huge_listing(directory) -> list[str]:
        nonlocal inspected
        if directory.path in {tombstone_path, state_path}:
            inspected += len(precomputed)
            return precomputed
        return real_list(directory)

    monkeypatch.setattr(checkpoint, "_list_directory", huge_listing)
    started = time.monotonic()
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        assert checkpoint._delete_tombstone_at(root, tombstone_path.name) is False
        with checkpoint._open_secure_child_directory(root, state_path.name, create=False) as state:
            checkpoint._cleanup_temporaries(state)
    elapsed = time.monotonic() - started

    assert inspected <= 32
    assert elapsed < 0.1
    assert (tombstone_path / "foreign.txt").is_file()


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-specific")
def test_fifo_transcript_fails_soft_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "transcript.fifo"
    os.mkfifo(fifo)
    code = (
        "import json,sys; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "print(json.dumps(c.profile_transcript(sys.argv[1],Path(sys.argv[2]))))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code, str(fifo), str(tmp_path / "home")],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    profile, degradation = json.loads(result.stdout)
    assert profile["available"] is False
    assert degradation == ["transcript_unavailable"]


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-specific")
def test_fifo_state_file_rolls_back_without_corrupting_previous(tmp_path: Path) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="fifo-state", trigger="manual")
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=200)
    state = checkpoint.session_state_dir(home, "codex", "fifo-state")
    previous_before = (state / "previous.json").read_bytes()
    (state / "current.json").unlink()
    os.mkfifo(state / "current.json")
    code = (
        "import json,sys; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "e={'client':'codex','event':'SessionStart','session_id':'fifo-state',"
        "'turn_id':None,'trigger':None,'source':'resume','cwd':None,"
        "'transcript_path':None,'model':None}; "
        "r=c.select_checkpoint(e,Path(sys.argv[1]),now_ns=201); "
        "print(json.dumps([r[1],r[0]['checkpoint_id']] if r else None))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code, str(home)],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    assert json.loads(result.stdout)[0] == "rollback"
    assert (state / "previous.json").read_bytes() == previous_before
    assert not (state / "current.json").is_file()


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is POSIX-specific")
def test_fifo_artifact_and_metadata_log_fail_soft_without_blocking(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    task = repo / ".task" / "TASK.md"
    task.parent.mkdir(parents=True)
    os.mkfifo(task)
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    os.mkfifo(root / "events.log")
    code = (
        "import json,sys; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "a=c.collect_artifacts(Path(sys.argv[1]),dirty_paths=set()); "
        "ok=False; "
        "\ntry: c._metadata_log(Path(sys.argv[2]),'codex','PreCompact','empty',1)"
        "\nexcept OSError: ok=True"
        "\nprint(json.dumps([a,ok]))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(home)],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )

    artifacts, log_failed = json.loads(result.stdout)
    assert artifacts[0] == []
    assert log_failed is True


def test_metadata_log_rotates_at_cap_with_complete_valid_jsonl(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)
    log = root / "events.log"
    row = (
        checkpoint._canonical_bytes({"event": "SessionStart", "status": "empty", "duration_ms": 0})
        + b"\n"
    )
    limit = 1024 * 1024
    log.write_bytes(row * (limit // len(row)))
    log.chmod(0o600)

    checkpoint._metadata_log(home, "codex", "SessionStart", "empty", 1)
    raw = log.read_bytes()
    records = [json.loads(line) for line in raw.splitlines()]

    assert len(raw) <= limit
    assert raw.endswith(b"\n")
    assert records[-1]["duration_ms"] == 1
    assert all(checkpoint._valid_metadata_record(record) for record in records)
    assert stat_mode(log) == 0o600


def test_metadata_rotation_replace_failure_preserves_log_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)
    log = root / "events.log"
    row = (
        checkpoint._canonical_bytes({"event": "SessionStart", "status": "empty", "duration_ms": 0})
        + b"\n"
    )
    log.write_bytes(row * (checkpoint.MAX_METADATA_LOG_BYTES // len(row)))
    log.chmod(0o600)
    before = log.read_bytes()
    real_replace = checkpoint._replace_at

    def fail_log_replace(directory, source: str, destination: str) -> None:
        if source.startswith(".events.log.tmp-") and destination == "events.log":
            raise OSError("forced metadata replace failure")
        real_replace(directory, source, destination)

    monkeypatch.setattr(checkpoint, "_replace_at", fail_log_replace)

    with pytest.raises(OSError, match="forced metadata replace failure"):
        checkpoint._metadata_log(home, "codex", "SessionStart", "empty", 1)

    assert log.read_bytes() == before
    assert list(root.glob(".events.log.tmp-*")) == []


def test_metadata_log_rotation_serializes_concurrent_process_writers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)
    log = root / "events.log"
    seed = (
        checkpoint._canonical_bytes({"event": "SessionStart", "status": "empty", "duration_ms": 0})
        + b"\n"
    )
    limit = 1024 * 1024
    log.write_bytes(seed * (limit // len(seed)))
    log.chmod(0o600)
    code = (
        "import sys; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "[(c._metadata_log(Path(sys.argv[1]),'codex','SessionStart','empty',i)) "
        "for i in range(20)]"
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(home)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]

    results = [child.communicate(timeout=20) + (child.returncode,) for child in children]
    raw = log.read_bytes()
    records = [json.loads(line) for line in raw.splitlines()]

    assert all(returncode == 0 for _stdout, _stderr, returncode in results), results
    assert len(raw) <= limit
    assert raw.endswith(b"\n")
    assert all(checkpoint._valid_metadata_record(record) for record in records)
    assert stat_mode(root / ".events.lock") == 0o600


def test_windows_handle_relative_guards_are_present_even_when_not_executable_here() -> None:
    child_source = inspect.getsource(checkpoint._open_secure_child_directory)
    rename_source = inspect.getsource(checkpoint._windows_rename_at)

    assert "_windows_open_path" in child_source
    assert "parent.windows_handle" in child_source
    assert "SetFileInformationByHandle" in rename_source


@pytest.mark.skipif(os.name != "nt", reason="requires live Windows reparse-point semantics")
def test_windows_reparse_child_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "child"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable")
    with checkpoint._open_secure_directory(root, create=False) as parent:
        with pytest.raises(OSError):
            with checkpoint._open_secure_child_directory(parent, "child", create=False):
                pass


def test_hook_subprocess_writes_silently_then_reinjects_bounded_context(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("sensitive conversation body", encoding="utf-8")
    env = {
        **os.environ,
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_VAULT_PATH": str(tmp_path / "must-not-be-used"),
    }
    write_payload = {
        "hookEventName": "PreCompact",
        "sessionId": "subprocess",
        "trigger": "auto",
        "cwd": str(tmp_path / "non-git"),
        "transcriptPath": str(transcript),
        "custom_instructions": "BEARER DO NOT STORE",
    }
    written = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "codex"],
        input=json.dumps(write_payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    resumed = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "codex"],
        input=json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": "subprocess",
                "source": "resume",
                "transcript_path": str(transcript),
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert written.returncode == 0 and written.stdout == ""
    assert resumed.returncode == 0
    envelope = json.loads(resumed.stdout)
    context = envelope["hookSpecificOutput"]["additionalContext"]
    assert len(context.encode("utf-8")) <= checkpoint.MAX_CONTEXT_BYTES
    assert "sensitive conversation body" not in context
    assert "BEARER" not in context
    assert not (tmp_path / "must-not-be-used").exists()


def test_claude_session_end_is_silent_and_disable_preserves_existing_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {**os.environ, "EXOMEM_HOOK_HOME": str(home)}
    base = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "claude"],
        input=json.dumps({"hook_event_name": "SessionEnd", "session_id": "ending"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    state = checkpoint.session_state_dir(home, "claude", "ending") / "current.json"
    before = state.read_bytes()
    disabled = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "claude"],
        input=json.dumps(
            {
                "hook_event_name": "PreCompact",
                "session_id": "ending",
                "trigger": "auto",
            }
        ),
        capture_output=True,
        text=True,
        env={**env, "EXOMEM_CONTINUATION_DISABLE": "1"},
        timeout=5,
    )

    assert base.returncode == disabled.returncode == 0
    assert base.stdout == disabled.stdout == ""
    assert state.read_bytes() == before


def _subprocess_env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_VAULT_PATH": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
    }


@pytest.mark.parametrize(
    ("client", "event", "trigger"),
    [
        ("claude", "PreCompact", "manual"),
        ("claude", "PreCompact", "auto"),
        ("claude", "SessionEnd", None),
        ("codex", "PreCompact", "manual"),
        ("codex", "PreCompact", "auto"),
    ],
)
def test_supported_write_subprocess_contract_is_silent_local_and_bounded(
    tmp_path: Path, client: str, event: str, trigger: str | None
) -> None:
    home = tmp_path / f"{client} home with spaces"
    transcript = tmp_path / "odd\\transcript.jsonl"
    transcript.write_bytes(b"private body\xff" * 8000)
    payload: dict[str, object] = {
        "hookEventName": event,
        "sessionId": "unsafe/session:identifier",
        "cwd": str(tmp_path / "non-git cwd"),
        "transcriptPath": str(transcript),
    }
    if trigger:
        payload["trigger"] = trigger

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", client],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_subprocess_env(home),
        timeout=5,
    )

    assert result.returncode == 0 and result.stdout == ""
    assert time.monotonic() - started < 5
    state = checkpoint.session_state_dir(home, client, "unsafe/session:identifier")
    raw = (state / "current.json").read_bytes()
    assert len(raw) <= checkpoint.MAX_CHECKPOINT_BYTES
    assert b"private body" not in raw
    assert not (tmp_path / "Knowledge Base").exists()
    log = checkpoint.client_state_root(home, client) / "events.log"
    log_text = log.read_text(encoding="utf-8")
    assert str(home) not in log_text and str(transcript) not in log_text
    assert "private body" not in log_text


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("source", ["compact", "resume"])
def test_both_client_start_subprocesses_inject_and_repeat_valid_context(
    tmp_path: Path, client: str, source: str
) -> None:
    home = tmp_path / client
    transcript = tmp_path / f"{client}.jsonl"
    transcript.write_bytes(b"conversation content not for output")
    env = _subprocess_env(home)
    write = {
        "hook_event_name": "PreCompact",
        "session_id": "same-session",
        "trigger": "manual",
        "transcript_path": str(transcript),
    }
    start = {
        "hook_event_name": "SessionStart",
        "session_id": "same-session",
        "source": source,
        "transcript_path": str(transcript),
    }
    subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", client],
        input=json.dumps(write),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    outputs = [
        subprocess.run(
            [sys.executable, str(CHECKPOINT_SCRIPT), "--client", client],
            input=json.dumps(start),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        for _ in range(2)
    ]

    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "conversation content" not in outputs[0]


def test_start_subprocess_is_silent_for_missing_corrupt_oversized_disabled_and_foreign(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = _subprocess_env(home)

    def run(client: str, session: str, *, extra_env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            [sys.executable, str(CHECKPOINT_SCRIPT), "--client", client],
            input=json.dumps(
                {"hook_event_name": "SessionStart", "session_id": session, "source": "resume"}
            ),
            capture_output=True,
            text=True,
            env={**env, **(extra_env or {})},
            timeout=5,
            check=True,
        ).stdout

    assert run("codex", "missing") == ""
    checkpoint.write_checkpoint(_event(client="codex", session_id="valid"), home)
    state = checkpoint.session_state_dir(home, "codex", "valid")
    (state / "current.json").write_bytes(b"{" + b"x" * (checkpoint.MAX_CHECKPOINT_BYTES + 1))
    assert run("codex", "valid") == ""
    assert run("claude", "valid") == ""
    assert run("codex", "valid", extra_env={"EXOMEM_CONTINUATION_DISABLE": "true"}) == ""


def test_missing_session_start_is_read_only_and_creates_no_state_root(tmp_path: Path) -> None:
    home = tmp_path / "never-created"

    result = checkpoint.dispatch_event(
        "codex",
        {"hook_event_name": "SessionStart", "session_id": "missing", "source": "resume"},
        environ={"EXOMEM_HOOK_HOME": str(home)},
    )

    assert result is None
    assert not checkpoint.client_state_root(home, "codex").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows directory handles prevent the test rename")
def test_handle_relative_checkpoint_read_survives_session_path_swap(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original_event = _event(client="codex", session_id="swap-proof", trigger="manual")
    checkpoint.write_checkpoint(original_event, home, observed_at_ns=100)
    state = checkpoint.session_state_dir(home, "codex", "swap-proof")
    original = checkpoint.load_checkpoint(state / "current.json")
    assert original is not None

    with checkpoint._open_secure_directory(state, create=False) as state_handle:
        moved = state.with_name(state.name + "-moved")
        state.rename(moved)
        state.mkdir()
        attacker = checkpoint.build_checkpoint(
            {**original_event, "trigger": "auto"}, home, observed_at_ns=200
        )
        (state / "current.json").write_bytes(checkpoint.encode_checkpoint(attacker))

        loaded = checkpoint.load_checkpoint_at(state_handle, "current.json")

    assert loaded is not None
    assert loaded["checkpoint_id"] == original["checkpoint_id"]
    assert loaded["checkpoint_id"] != attacker["checkpoint_id"]


def test_true_multiprocess_same_id_delivery_creates_no_duplicate_history(tmp_path: Path) -> None:
    home = tmp_path / "home"
    payload = json.dumps({"hook_event_name": "PreCompact", "session_id": "race", "trigger": "auto"})
    processes = [
        subprocess.Popen(
            [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "codex"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subprocess_env(home),
        )
        for _ in range(6)
    ]
    results = [
        process.communicate(payload, timeout=10) + (process.returncode,) for process in processes
    ]

    assert all(stdout == "" and code == 0 for stdout, _stderr, code in results)
    state = checkpoint.session_state_dir(home, "codex", "race")
    assert (state / "current.json").is_file()
    assert not (state / "previous.json").exists()
    assert not list(state.glob("*.tmp-*"))


def test_true_multiprocess_older_observation_cannot_replace_newer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    code = (
        "import json,sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "time.sleep(float(sys.argv[4])); "
        "e={'client':'codex','event':'PreCompact','session_id':'ordered','turn_id':None,"
        "'trigger':sys.argv[2],'source':None,'cwd':None,'transcript_path':None,'model':None}; "
        "print(json.dumps(c.write_checkpoint(e,Path(sys.argv[1]),observed_at_ns=int(sys.argv[3]))))"
    )
    newer = subprocess.Popen(
        [sys.executable, "-c", code, str(home), "auto", "200", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    older = subprocess.Popen(
        [sys.executable, "-c", code, str(home), "manual", "100", "0.2"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert newer.wait(timeout=10) == 0 and older.wait(timeout=10) == 0
    state = checkpoint.session_state_dir(home, "codex", "ordered")
    current = checkpoint.load_checkpoint(state / "current.json")
    assert current["structural"]["trigger"] == "auto"


def test_killed_temporary_writer_is_cleaned_by_next_delivery(tmp_path: Path) -> None:
    home = tmp_path / "home"
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "e={'client':'codex','event':'PreCompact','session_id':'killed','turn_id':None,"
        "'trigger':'auto','source':None,'cwd':None,'transcript_path':None,'model':None}; "
        "h=Path(sys.argv[1]); v=c.build_checkpoint(e,h,observed_at_ns=100); "
        "x=c._session_lock(h,'codex','killed',create=True); s=x.__enter__(); "
        "c._write_temp(s,v); print('temporary',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "temporary"
        child.kill()
        child.wait(timeout=5)
        result = checkpoint.write_checkpoint(
            _event(client="codex", session_id="killed"), home, observed_at_ns=200
        )
        state = checkpoint.session_state_dir(home, "codex", "killed")
        assert result["status"] == "written"
        assert not list(state.glob("*.tmp-*"))
    finally:
        if child.poll() is None:
            child.kill()


def test_true_kill_after_rotation_then_same_id_retry_restores_reinjection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "killed-same-id-rotation"
    event = _event(client="codex", session_id=session)
    first = checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "h=Path(sys.argv[1]); x=c._session_lock(h,'codex',sys.argv[2],create=False); "
        "s=x.__enter__(); c._replace_at(s,'current.json','previous.json'); "
        "print('rotated',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), session],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "rotated"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    state = checkpoint.session_state_dir(home, "codex", session)
    assert not (state / "current.json").exists()
    assert (state / "previous.json").is_file()

    repeated = checkpoint.write_checkpoint(event, home, observed_at_ns=200)
    selected = checkpoint.select_checkpoint(
        _event(
            client="codex",
            event="SessionStart",
            session_id=session,
            trigger=None,
            source="resume",
        ),
        home,
        now_ns=201,
    )

    assert repeated == {"status": "idempotent", "checkpoint_id": first["checkpoint_id"]}
    assert selected is not None and selected[1] == "current"
    assert selected[0]["observed_at_ns"] == 200
    assert not (state / "previous.json").exists()


@pytest.mark.parametrize(
    "stage",
    [
        "directory_creation",
        "lock_acquisition",
        "temp_write_fsync",
        "current_to_previous",
        "temp_to_current",
    ],
)
def test_kill_at_each_storage_stage_preserves_recoverable_state(
    tmp_path: Path,
    stage: str,
) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="kill-stage", trigger="manual")
    if stage in {"current_to_previous", "temp_to_current"}:
        checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "h=Path(sys.argv[1]); stage=sys.argv[2]; "
        "e={'client':'codex','event':'PreCompact','session_id':'kill-stage','turn_id':None,"
        "'trigger':'auto','source':None,'cwd':None,'transcript_path':None,'model':None}; "
        "state=c.session_state_dir(h,'codex','kill-stage'); "
        "x=(c._open_secure_directory(state,create=True) if stage=='directory_creation' "
        "else c._session_lock(h,'codex','kill-stage',create=True)); s=x.__enter__(); "
        "tmp=None if stage in {'directory_creation','lock_acquisition'} "
        "else c._write_temp(s,c.build_checkpoint(e,h,observed_at_ns=200)); "
        "c._replace_at(s,'current.json','previous.json') if stage in "
        "{'current_to_previous','temp_to_current'} else None; "
        "c._replace_at(s,tmp,'current.json') if stage=='temp_to_current' else None; "
        "print(stage,flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), stage],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == stage
        child.kill()
        child.wait(timeout=5)
        state = checkpoint.session_state_dir(home, "codex", "kill-stage")
        if stage == "current_to_previous":
            start = _event(
                client="codex",
                event="SessionStart",
                session_id="kill-stage",
                trigger=None,
                source="resume",
            )
            selected = checkpoint.select_checkpoint(start, home, now_ns=201)
            assert selected is not None and selected[1] == "rollback"
        result = checkpoint.write_checkpoint(
            {**event, "trigger": "auto"},
            home,
            observed_at_ns=300,
        )
        assert result["status"] in {"written", "idempotent"}
        assert checkpoint.load_checkpoint(state / "current.json") is not None
        assert not list(state.glob("*.tmp-*"))
    finally:
        if child.poll() is None:
            child.kill()


def test_interrupted_rotation_recovers_labeled_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="rotation")
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    real_replace = checkpoint._replace_at

    def interrupt(directory, source: str, destination: str) -> None:
        if destination == "current.json" and source.startswith("current.json.tmp-"):
            raise OSError("simulated interruption after rotation")
        real_replace(directory, source, destination)

    monkeypatch.setattr(checkpoint, "_replace_at", interrupt)
    with pytest.raises(OSError, match="simulated"):
        checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=200)

    state = checkpoint.session_state_dir(home, "codex", "rotation")
    assert not (state / "current.json").exists()
    assert checkpoint.load_checkpoint(state / "previous.json") is not None
    start = _event(
        client="codex", event="SessionStart", session_id="rotation", trigger=None, source="resume"
    )
    selected = checkpoint.select_checkpoint(start, home, now_ns=201)
    assert selected is not None and selected[1] == "rollback"


@pytest.mark.parametrize("force_fallback", [False, True])
def test_prune_skips_multiprocess_writer_then_removes_after_release(
    tmp_path: Path, force_fallback: bool
) -> None:
    home = tmp_path / "home"
    old = _event(client="codex", session_id="busy")
    checkpoint.write_checkpoint(old, home, observed_at_ns=100)
    lock_path = checkpoint.session_state_dir(home, "codex", "busy") / ".lock"
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "x=c.advisory_lock(Path(sys.argv[1]),timeout=1); x.__enter__(); "
        "print('locked',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        assert (
            checkpoint.prune_expired(
                home,
                "codex",
                current_session="other",
                now_ns=100 + checkpoint.RETENTION_NS + 1,
                force_fallback=force_fallback,
            )
            == 0
        )
        child.kill()
        child.wait(timeout=5)
        # Pruning is deadline-bounded and cursor-resumed. A loaded runner may
        # exhaust one 50 ms callback after the writer releases, so assert the
        # specified eventual result across bounded subsequent callbacks.
        removals = [
            checkpoint.prune_expired(
                home,
                "codex",
                current_session="other",
                now_ns=100 + checkpoint.RETENTION_NS + 1,
                force_fallback=force_fallback,
            )
            for _ in range(checkpoint.MAX_PRUNE_ENUM_ENTRIES + 2)
        ]
        assert sum(removals) == 1
    finally:
        if child.poll() is None:
            child.kill()


def test_many_busy_prune_candidates_do_not_starve_supported_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)
    for number in range(20):
        busy = root / f"busy-{number:02d}"
        busy.mkdir()
        busy.chmod(0o700)
    real_lock = checkpoint._advisory_lock_at
    first_busy = threading.Event()

    class BusyLock:
        def __enter__(self):
            first_busy.set()
            time.sleep(0.06)
            raise TimeoutError("busy")

        def __exit__(self, *_: object) -> None:
            return None

    def controlled_lock(directory, name: str, *, timeout: float = 0.5):
        if name == ".lock" and directory.path.name.startswith("busy-"):
            return BusyLock()
        return real_lock(directory, name, timeout=timeout)

    monkeypatch.setattr(checkpoint, "_advisory_lock_at", controlled_lock)
    prune = threading.Thread(
        target=checkpoint.prune_expired,
        kwargs={
            "home": home,
            "client": "codex",
            "current_session": "other",
            "now_ns": checkpoint.RETENTION_NS + 1,
        },
    )
    prune.start()
    assert first_busy.wait(timeout=1)
    started = time.monotonic()

    outcome = checkpoint.write_checkpoint(
        _event(client="codex", session_id="writer"),
        home,
        observed_at_ns=100,
    )
    elapsed = time.monotonic() - started
    prune.join(timeout=3)

    assert outcome["status"] == "written"
    assert elapsed < 0.45
    assert not prune.is_alive()
    assert (checkpoint.session_state_dir(home, "codex", "writer") / "current.json").is_file()


def test_prune_respects_total_budget_when_root_lock_is_held(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)
    lock_path = root / ".root.lock"
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "lock=c.advisory_lock(Path(sys.argv[1]),timeout=1); lock.__enter__(); "
        "print('locked',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        started = time.monotonic()

        removed = checkpoint.prune_expired(
            home,
            "codex",
            current_session="current",
            now_ns=checkpoint.RETENTION_NS + 1,
        )
        elapsed = time.monotonic() - started

        assert removed == 0
        assert elapsed < checkpoint.MAX_PRUNE_LOCK_SECONDS + 0.15
    finally:
        child.kill()
        child.wait(timeout=5)


def test_prune_bounds_root_enumeration_before_candidate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    root = checkpoint.client_state_root(home, "codex")
    root.mkdir(parents=True)
    root.chmod(0o700)

    def fail_unbounded_listing(_directory) -> list[str]:
        pytest.fail("pruning must not materialize an unbounded root listing")

    monkeypatch.setattr(checkpoint, "_list_directory", fail_unbounded_listing)
    removed = checkpoint.prune_expired(
        home,
        "codex",
        current_session="current",
        now_ns=checkpoint.RETENTION_NS + 1,
    )

    assert removed == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses Linux directory cookies")
def test_directory_window_cursor_is_bounded_and_eventually_visits_every_entry(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "root"
    root_path.mkdir(mode=0o700)
    expected = {f"entry-{number:04d}" for number in range(257)}
    for name in expected:
        (root_path / name).touch()
    cursor = 0
    seen: set[str] = set()

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        for _ in range(40):
            started = time.monotonic()
            names, cursor, exhausted, inspected = checkpoint._directory_window(
                root,
                cursor=cursor,
                limit=checkpoint.MAX_PRUNE_ENUM_ENTRIES,
                deadline=time.monotonic() + checkpoint.MAX_PRUNE_LOCK_SECONDS,
            )
            elapsed = time.monotonic() - started
            assert inspected <= checkpoint.MAX_PRUNE_ENUM_ENTRIES
            assert elapsed < checkpoint.MAX_PRUNE_LOCK_SECONDS + 0.05
            seen.update(names)
            if exhausted:
                break

    assert seen == expected
    assert exhausted is True and cursor == 0


def test_portable_directory_window_cursor_never_replays_a_growing_prefix(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "portable-root"
    root_path.mkdir(mode=0o700)
    expected = {f"entry-{number:04d}" for number in range(257)}
    for name in expected:
        (root_path / name).touch()
    cursor = 0
    seen: set[str] = set()
    inspected_windows: list[int] = []

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        for _ in range(40):
            started = time.monotonic()
            names, cursor, exhausted, inspected = checkpoint._directory_window(
                root,
                cursor=cursor,
                limit=checkpoint.MAX_PRUNE_ENUM_ENTRIES,
                deadline=time.monotonic() + checkpoint.MAX_PRUNE_LOCK_SECONDS,
                force_portable=True,
            )
            inspected_windows.append(inspected)
            assert inspected <= checkpoint.MAX_PRUNE_ENUM_ENTRIES
            assert time.monotonic() - started < checkpoint.MAX_PRUNE_LOCK_SECONDS + 0.05
            seen.update(names)
            if exhausted:
                break

    assert seen == expected
    assert exhausted is True and cursor == 0
    assert inspected_windows[1] <= checkpoint.MAX_PRUNE_ENUM_ENTRIES


def test_portable_catalog_cursor_is_bounded_persisted_and_fixed_size(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    root_path = checkpoint.client_state_root(home, client)
    root_path.mkdir(parents=True, mode=0o700)
    expected = {
        checkpoint.session_state_dir(home, client, f"catalog-{number:04d}").name
        for number in range(257)
    }
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
            for name in sorted(expected):
                checkpoint._register_prune_catalog_entry(root, root_lock, name)
    catalog = root_path / checkpoint._PRUNE_CATALOG_NAME
    initial_size = catalog.stat().st_size
    seen: set[str] = set()

    for _ in range(40):
        with checkpoint._open_secure_directory(root_path, create=False) as root:
            with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
                cursor = checkpoint._read_prune_sequence(root_lock, offset=17)
                names, next_cursor, exhausted, inspected = checkpoint._prune_catalog_window(
                    root,
                    cursor=cursor,
                    limit=checkpoint.MAX_PRUNE_ENUM_ENTRIES,
                    deadline=time.monotonic() + checkpoint.MAX_PRUNE_LOCK_SECONDS,
                )
                checkpoint._write_prune_sequence(root_lock, next_cursor, offset=17)
        assert inspected <= checkpoint.MAX_PRUNE_ENUM_ENTRIES
        assert catalog.stat().st_size == initial_size == checkpoint._prune_catalog_size()
        seen.update(names)
        if exhausted:
            break

    assert seen == expected
    assert exhausted is True and next_cursor == 0
    assert (root_path / ".root.lock").stat().st_size <= 25


def test_portable_catalog_cursor_progress_survives_fresh_processes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    root_path = checkpoint.client_state_root(home, client)
    root_path.mkdir(parents=True, mode=0o700)
    expected = {
        checkpoint.session_state_dir(home, client, f"process-{number:04d}").name
        for number in range(33)
    }
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
            for name in sorted(expected):
                checkpoint._register_prune_catalog_entry(root, root_lock, name)
    code = "\n".join(
        [
            "import json,sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "with c._open_secure_directory(Path(sys.argv[1]),create=False) as root:",
            "  with c._advisory_lock_at(root,'.root.lock') as lock:",
            "    cursor=c._read_prune_sequence(lock,offset=17)",
            "    names,nxt,done,count=c._prune_catalog_window(",
            "      root,cursor=cursor,limit=c.MAX_PRUNE_ENUM_ENTRIES,",
            "      deadline=time.monotonic()+c.MAX_PRUNE_LOCK_SECONDS,",
            "    )",
            "    c._write_prune_sequence(lock,nxt,offset=17)",
            "print(json.dumps([names,nxt,done,count]))",
        ]
    )
    seen: set[str] = set()

    for _ in range(40):
        result = subprocess.run(
            [sys.executable, "-c", code, str(root_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        names, cursor, exhausted, inspected = json.loads(result.stdout)
        assert inspected <= checkpoint.MAX_PRUNE_ENUM_ENTRIES
        seen.update(names)
        if exhausted:
            break

    assert seen == expected
    assert exhausted is True and cursor == 0


def test_portable_catalog_prune_reaches_owned_state_and_ignores_foreign_copy(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "portable-expired"
    checkpoint.write_checkpoint(
        _event(client=client, session_id=session),
        home,
        observed_at_ns=100,
    )
    root = checkpoint.client_state_root(home, client)
    state = checkpoint.session_state_dir(home, client, session)
    foreign = root / "foreign-unregistered"
    shutil.copytree(state, foreign)

    removed = sum(
        checkpoint.prune_expired(
            home,
            client,
            current_session="other",
            now_ns=100 + checkpoint.RETENTION_NS + 1,
            force_portable_catalog=True,
        )
        for _ in range(40)
    )

    assert removed == 1
    assert not state.exists()
    assert foreign.is_dir()


def test_valid_legacy_current_access_registers_only_the_bound_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "legacy-current"
    event = _event(client=client, session_id=session)
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    root_path = checkpoint.client_state_root(home, client)
    state = checkpoint.session_state_dir(home, client, session)
    foreign = root_path / "foreign-pre-catalog"
    shutil.copytree(state, foreign)
    (root_path / checkpoint._PRUNE_CATALOG_NAME).unlink()

    checkpoint.write_checkpoint(event, home, observed_at_ns=200)

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        assert checkpoint._prune_catalog_contains(root, state.name)
        assert not checkpoint._prune_catalog_contains(root, foreign.name)


def test_corrupt_portable_catalog_fails_closed_without_deleting_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "catalog-corrupt"
    checkpoint.write_checkpoint(
        _event(client=client, session_id=session),
        home,
        observed_at_ns=100,
    )
    root_path = checkpoint.client_state_root(home, client)
    state = checkpoint.session_state_dir(home, client, session)
    catalog = root_path / checkpoint._PRUNE_CATALOG_NAME
    with catalog.open("r+b") as handle:
        handle.seek(checkpoint._PRUNE_CATALOG_HEADER_BYTES - 1)
        byte = handle.read(1)
        handle.seek(checkpoint._PRUNE_CATALOG_HEADER_BYTES - 1)
        handle.write(bytes([byte[0] ^ 0xFF]))

    removals = [
        checkpoint.prune_expired(
            home,
            client,
            current_session="other",
            now_ns=100 + checkpoint.RETENTION_NS + 1,
            force_portable_catalog=True,
        )
        for _ in range(40)
    ]

    assert removals == [0] * 40
    assert state.is_dir()


@pytest.mark.parametrize("stage", ["before_catalog", "after_catalog"])
def test_true_kill_around_portable_catalog_publication_is_recoverable(
    tmp_path: Path,
    stage: str,
) -> None:
    home = tmp_path / "home"
    session = f"catalog-kill-{stage}"
    event = _event(client="codex", session_id=session)
    code = "\n".join(
        [
            "import json,sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "stage=sys.argv[4]",
            "real=c._register_prune_catalog_entry",
            "def stop(root,lock,name):",
            "    target=name.startswith('.pending-') and '.tmp-' in name",
            "    if target and stage=='before_catalog':",
            "        print('before-catalog',flush=True); time.sleep(60)",
            "    real(root,lock,name)",
            "    if target and stage=='after_catalog':",
            "        print('after-catalog',flush=True); time.sleep(60)",
            "c._register_prune_catalog_entry=stop",
            "c.write_checkpoint(json.loads(sys.argv[3]),Path(sys.argv[1]),observed_at_ns=100)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), session, json.dumps(event), stage],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == stage.replace("_", "-")
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    root_path = checkpoint.client_state_root(home, "codex")
    state = checkpoint.session_state_dir(home, "codex", session)
    assert not state.exists()

    for _ in range(40):
        checkpoint.prune_expired(
            home,
            "codex",
            current_session="other",
            now_ns=checkpoint.RETENTION_NS + 1,
            force_portable_catalog=True,
        )

    assert not list(root_path.glob(".pending-*.tmp-*"))


def test_true_kill_before_catalog_publish_resumes_one_bounded_catalog(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "catalog-init-kill"
    event = _event(client="codex", session_id=session)
    code = "\n".join(
        [
            "import json,sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "real=c._replace_at",
            "def stop(root,source,destination):",
            "    if source==c._PRUNE_CATALOG_TEMP and destination==c._PRUNE_CATALOG_NAME:",
            "        print('catalog-ready',flush=True); time.sleep(60)",
            "    return real(root,source,destination)",
            "c._replace_at=stop",
            "c.write_checkpoint(json.loads(sys.argv[2]),Path(sys.argv[1]),observed_at_ns=100)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), json.dumps(event)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "catalog-ready"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    root = checkpoint.client_state_root(home, "codex")
    assert (root / checkpoint._PRUNE_CATALOG_TEMP).is_file()

    checkpoint.write_checkpoint(event, home, observed_at_ns=200)

    assert (root / checkpoint._PRUNE_CATALOG_NAME).stat().st_size == (
        checkpoint._prune_catalog_size()
    )
    assert not (root / checkpoint._PRUNE_CATALOG_TEMP).exists()
    assert not list(root.glob(f"{checkpoint._PRUNE_CATALOG_NAME}.tmp-*"))


def test_catalog_capacity_exhaustion_soft_fails_without_unregistered_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    root_path = checkpoint.client_state_root(home, client)
    root_path.mkdir(parents=True, mode=0o700)
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
            for number in range(checkpoint.MAX_PRUNE_CATALOG_ENTRIES):
                checkpoint._register_prune_catalog_entry(
                    root,
                    root_lock,
                    checkpoint.session_state_dir(home, client, f"full-{number:04d}").name,
                )
    records: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        checkpoint,
        "_metadata_log",
        lambda *args, **kwargs: records.append((args, kwargs)),
    )
    session = "must-not-publish"
    event = checkpoint.normalize_event(
        client,
        {
            "hook_event_name": "PreCompact",
            "session_id": session,
            "trigger": "manual",
        },
    )
    assert event is not None

    assert checkpoint._dispatch_core(event, home, expected_client=client) is None

    state = checkpoint.session_state_dir(home, client, session)
    assert not state.exists()
    assert not list(root_path.glob(".pending-*"))
    assert records and records[-1][0][3] == "error"
    assert records[-1][1]["error_class"] == "OSError"


def test_prune_rotates_bounded_candidates_beyond_sorted_prefix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    now = checkpoint.RETENTION_NS + 1_000
    for number in range(checkpoint.MAX_PRUNE_CANDIDATES):
        checkpoint.write_checkpoint(
            _event(client="codex", session_id=f"a{number:02d}"),
            home,
            observed_at_ns=now,
        )
    expired_session = "zz-expired"
    checkpoint.write_checkpoint(
        _event(client="codex", session_id=expired_session),
        home,
        observed_at_ns=100,
    )

    removals = [
        checkpoint.prune_expired(
            home,
            "codex",
            current_session="current",
            now_ns=now,
        )
        for _ in range(20)
    ]

    assert sum(removals) == 1
    assert not checkpoint.session_state_dir(home, "codex", expired_session).exists()


def test_prune_removes_authorized_expired_interrupted_first_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with checkpoint._session_lock(
        home,
        "codex",
        "interrupted-first-write",
        create=True,
        created_at_ns=100,
    ):
        pass
    state = checkpoint.session_state_dir(home, "codex", "interrupted-first-write")

    assert {item.name for item in state.iterdir()} == {".lock"}
    removed = sum(
        checkpoint.prune_expired(
            home,
            "codex",
            current_session="other",
            now_ns=100 + checkpoint.RETENTION_NS + 1,
        )
        for _ in range(checkpoint.MAX_PRUNE_ENUM_ENTRIES + 2)
    )

    assert removed == 1
    assert not state.exists()


def test_prune_removes_stale_previous_behind_fresh_current(tmp_path: Path) -> None:
    home = tmp_path / "home"
    event = _event(client="codex", session_id="stale-history", trigger="manual")
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=101)
    fresh_observed = checkpoint.RETENTION_NS + 200
    checkpoint.write_checkpoint(
        {**event, "trigger": "manual", "turn_id": "fresh"},
        home,
        observed_at_ns=fresh_observed,
    )
    state = checkpoint.session_state_dir(home, "codex", "stale-history")
    assert checkpoint.load_checkpoint(state / "previous.json")["observed_at_ns"] == 101

    removed = checkpoint.prune_expired(
        home,
        "codex",
        current_session="other",
        now_ns=fresh_observed,
    )

    assert removed == 0
    assert checkpoint.load_checkpoint(state / "current.json")["observed_at_ns"] == fresh_observed
    assert not (state / "previous.json").exists()


def test_prune_cleans_stale_history_for_active_session_without_tombstoning_current(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "only"
    event = _event(client="codex", session_id=session, trigger="manual")
    checkpoint.write_checkpoint(event, home, observed_at_ns=100)
    checkpoint.write_checkpoint({**event, "trigger": "auto"}, home, observed_at_ns=101)
    now = checkpoint.RETENTION_NS + 200
    checkpoint.write_checkpoint(
        {**event, "turn_id": "fresh"},
        home,
        observed_at_ns=now,
    )
    state = checkpoint.session_state_dir(home, "codex", session)
    assert (state / "previous.json").exists()

    removed = checkpoint.prune_expired(
        home,
        "codex",
        current_session=session,
        now_ns=now,
    )

    assert removed == 0
    assert (state / "current.json").exists()
    assert not (state / "previous.json").exists()


def test_prune_cleans_stale_history_after_same_id_freshness_refresh(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "same-id-active"
    manual = _event(client="codex", session_id=session, trigger="manual")
    automatic = {**manual, "trigger": "auto"}
    checkpoint.write_checkpoint(manual, home, observed_at_ns=100)
    checkpoint.write_checkpoint(automatic, home, observed_at_ns=101)
    now = checkpoint.RETENTION_NS + 200
    checkpoint.write_checkpoint(automatic, home, observed_at_ns=now)
    state = checkpoint.session_state_dir(home, "codex", session)
    assert (state / "previous.json").exists()

    checkpoint.prune_expired(
        home,
        "codex",
        current_session=session,
        now_ns=now,
    )

    assert checkpoint.load_checkpoint(state / "current.json")["observed_at_ns"] == now
    assert not (state / "previous.json").exists()


def test_prune_recovers_authorized_crash_tombstone_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "crashed-prune"
    checkpoint.write_checkpoint(_event(client=client, session_id=session), home, observed_at_ns=100)
    root_path = checkpoint.client_state_root(home, client)
    state_name = checkpoint.session_state_dir(home, client, session).name
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
            tombstone = checkpoint._tombstone_expired_candidate(
                root,
                root_lock,
                home,
                client,
                state_name,
                100 + checkpoint.RETENTION_NS + 1,
                force_fallback=False,
            )
    assert tombstone is not None
    tombstone_name, _identity = tombstone
    assert (root_path / tombstone_name).is_dir()

    lookalike = root_path / ".tombstone-not-authorized"
    lookalike.mkdir()
    (lookalike / "valuable.txt").write_text("preserve", encoding="utf-8")
    foreign = root_path / ".tombstone-foreign-session-0123456789abcdef"
    shutil.copytree(root_path / tombstone_name, foreign)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = root_path / ".tombstone-symlink-0000000000000000"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError:
        symlink = None

    removed = checkpoint.prune_expired(
        home,
        client,
        current_session="other",
        now_ns=100 + checkpoint.RETENTION_NS + 2,
    )

    assert removed == 1
    assert not (root_path / tombstone_name).exists()
    assert (lookalike / "valuable.txt").read_text(encoding="utf-8") == "preserve"
    assert foreign.is_dir()
    assert outside.is_dir()
    if symlink is not None:
        assert symlink.is_symlink()


def test_crash_tombstone_recovery_rotates_past_unauthorized_prefix(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "zz-crashed-prune"
    checkpoint.write_checkpoint(
        _event(client=client, session_id=session),
        home,
        observed_at_ns=100,
    )
    root_path = checkpoint.client_state_root(home, client)
    state_name = checkpoint.session_state_dir(home, client, session).name
    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._advisory_lock_at(root, ".root.lock") as root_lock:
            recovered = checkpoint._tombstone_expired_candidate(
                root,
                root_lock,
                home,
                client,
                state_name,
                100 + checkpoint.RETENTION_NS + 1,
                force_fallback=False,
            )
    assert recovered is not None
    authorized_name, _identity = recovered
    lookalikes: list[Path] = []
    for number in range(checkpoint.MAX_PRUNE_CANDIDATES):
        lookalike = root_path / f".tombstone-aa{number:02d}-0000000000000000"
        lookalike.mkdir(mode=0o700)
        (lookalike / "valuable.txt").write_text("preserve", encoding="utf-8")
        lookalikes.append(lookalike)

    removed = sum(
        checkpoint.prune_expired(
            home,
            client,
            current_session="other",
            now_ns=100 + checkpoint.RETENTION_NS + 2,
        )
        for _ in range(20)
    )

    assert removed == 1
    assert not (root_path / authorized_name).exists()
    assert all(path.is_dir() for path in lookalikes)


def test_true_kill_between_session_directory_creation_and_manifest_is_prunable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "abandoned-create"
    code = (
        "import sys,time; from pathlib import Path; "
        "from exomem._hooks import exomem_continuation_checkpoint as c; "
        "h=Path(sys.argv[1]); real=c._ensure_session_manifest; "
        "c._ensure_session_manifest=lambda *args,**kwargs: "
        "(print('created',flush=True),time.sleep(60)); "
        "x=c._session_lock(h,'codex','abandoned-create',create=True,created_at_ns=100); "
        "x.__enter__()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "created"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    state = checkpoint.session_state_dir(home, "codex", session)
    assert state.is_dir()

    removed = sum(
        checkpoint.prune_expired(
            home,
            "codex",
            current_session="other",
            now_ns=100 + 60 * 24 * 60 * 60 * 1_000_000_000,
        )
        for _ in range(20)
    )

    assert removed == 1
    assert not state.exists()


@pytest.mark.parametrize("cleanup", ["retry", "prune"])
def test_true_kill_after_pending_temp_fsync_is_cleaned_boundedly(
    tmp_path: Path,
    cleanup: str,
) -> None:
    home = tmp_path / "home"
    session = "pending-temp-kill"
    code = "\n".join(
        [
            "import sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "h=Path(sys.argv[1])",
            "real=c._replace_at",
            "def stop(d,s,t):",
            "    if s.startswith('.pending-') and '.tmp-' in s and t.startswith('.pending-'):",
            "        print('pending-temp',flush=True)",
            "        time.sleep(60)",
            "    return real(d,s,t)",
            "c._replace_at=stop",
            "x=c._session_lock(h,'codex',sys.argv[2],create=True,created_at_ns=100)",
            "x.__enter__()",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), session],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "pending-temp"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    root = checkpoint.client_state_root(home, "codex")
    assert list(root.glob(".pending-*.tmp-*"))

    if cleanup == "retry":
        checkpoint.write_checkpoint(
            _event(client="codex", session_id=session),
            home,
            observed_at_ns=200,
        )
    else:
        for _ in range(20):
            checkpoint.prune_expired(
                home,
                "codex",
                current_session="other",
                now_ns=time.time_ns() + checkpoint.RETENTION_NS + 1,
            )

    assert not list(root.glob(".pending-*.tmp-*"))


def test_true_kill_after_pending_publish_before_directory_is_pruned(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = "pending-published-kill"
    code = "\n".join(
        [
            "import sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "h=Path(sys.argv[1])",
            "state=c.session_state_dir(h,'codex',sys.argv[2])",
            "real=c._open_secure_child_directory",
            "def stop(d,n,**kw):",
            "    if kw.get('create') and n==state.name:",
            "        print('pending-published',flush=True)",
            "        time.sleep(60)",
            "    return real(d,n,**kw)",
            "c._open_secure_child_directory=stop",
            "x=c._session_lock(h,'codex',sys.argv[2],create=True,created_at_ns=100)",
            "x.__enter__()",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), session],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "pending-published"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
    root = checkpoint.client_state_root(home, "codex")
    state = checkpoint.session_state_dir(home, "codex", session)
    pending = root / checkpoint._pending_session_name(state.name)
    assert pending.is_file() and not state.exists()

    for _ in range(20):
        checkpoint.prune_expired(
            home,
            "codex",
            current_session="other",
            now_ns=100 + checkpoint.RETENTION_NS + 1,
        )

    assert not pending.exists()


@pytest.mark.parametrize("cleanup", ["retry", "active_prune"])
def test_true_kill_after_session_manifest_publish_cleans_redundant_pending(
    tmp_path: Path,
    cleanup: str,
) -> None:
    home = tmp_path / "home"
    session = "manifest-published-pending"
    event = _event(client="codex", session_id=session)
    code = "\n".join(
        [
            "import json,sys,time",
            "from pathlib import Path",
            "from exomem._hooks import exomem_continuation_checkpoint as c",
            "real=c._unlink_at",
            "def stop(d,n):",
            "    if n.startswith('.pending-') and n.endswith('.json'):",
            "        print('manifest-published',flush=True)",
            "        time.sleep(60)",
            "    return real(d,n)",
            "c._unlink_at=stop",
            "c.write_checkpoint(json.loads(sys.argv[3]),Path(sys.argv[1]),observed_at_ns=100)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(home), session, json.dumps(event)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "manifest-published"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()

    root = checkpoint.client_state_root(home, "codex")
    state = checkpoint.session_state_dir(home, "codex", session)
    pending = root / checkpoint._pending_session_name(state.name)
    assert state.is_dir() and pending.is_file()
    with checkpoint._open_secure_directory(state, create=False) as state_handle:
        manifest, status_value = checkpoint.load_session_manifest_at(
            state_handle,
            home,
            "codex",
            state.name,
        )
    assert manifest is not None and status_value == "valid"

    if cleanup == "retry":
        checkpoint.write_checkpoint(event, home, observed_at_ns=200)
    else:
        for _ in range(40):
            checkpoint.prune_expired(
                home,
                "codex",
                current_session=session,
                now_ns=checkpoint.RETENTION_NS + 1_000,
                force_portable_catalog=True,
            )

    assert state.is_dir()
    assert not pending.exists()


def test_boolean_session_and_pending_manifest_versions_are_rejected(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client = "codex"
    session = "bool-manifest"
    state_path = checkpoint.session_state_dir(home, client, session)
    root_path = checkpoint.client_state_root(home, client)
    root_path.mkdir(parents=True)
    root_path.chmod(0o700)
    state_path.mkdir(mode=0o700)
    manifest = checkpoint._session_manifest(home, client, session, 100)
    manifest["schema_version"] = True
    encoded = json.dumps(manifest).encode("utf-8")
    session_manifest = state_path / ".lock"
    session_manifest.write_bytes(b"\0" + encoded)
    session_manifest.chmod(0o600)
    pending_path = root_path / checkpoint._pending_session_name(state_path.name)
    pending_path.write_bytes(encoded)
    pending_path.chmod(0o600)

    with checkpoint._open_secure_directory(root_path, create=False) as root:
        with checkpoint._open_secure_child_directory(root, state_path.name, create=False) as state:
            loaded_session = checkpoint.load_session_manifest_at(
                state,
                home,
                client,
                state_path.name,
            )
        loaded_pending = checkpoint._load_pending_session_at(
            root,
            home,
            client,
            state_path.name,
        )

    assert loaded_session == (None, "corrupt")
    assert loaded_pending == (None, "corrupt")
