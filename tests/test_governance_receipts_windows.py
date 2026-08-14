"""Native Windows safety regression coverage for governance receipts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from exomem import mutation_lock
from exomem.governance import receipts


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows receipt contract")


def _month(instance_dir: Path) -> Path:
    month = instance_dir / "2026-08.jsonl"
    month.write_bytes(b'{"schema":"receipt/v1"}\n')
    return month


def _junction(path: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    assert path.is_dir()


def _outside_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Record content and identity so a refused receipt cannot touch the target."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.stat()
        if path.is_dir():
            snapshot[relative] = ("directory", info.st_dev, info.st_ino, info.st_mtime_ns)
        else:
            snapshot[relative] = ("file", path.read_bytes(), info.st_dev, info.st_ino, info.st_mtime_ns)
    return snapshot


def _outside_target(tmp_path: Path, component: str) -> Path:
    outside = tmp_path / f"outside-{component}"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"must not be read or changed")
    return outside


def test_windows_native_receipt_append_critical_chain_and_exact_retry_in_fresh_vault(
    vault: Path,
) -> None:
    """Native handles support the complete ordinary-and-critical receipt protocol."""
    ordinary = receipts.append_event(
        vault,
        event_type="disclosure",
        payload={"outcomes": [{"ref": "Notes/native", "size": 1}]},
    )
    intent_kwargs = {
        "operation": "delete",
        "prior": "a" * 64,
        "target": "b" * 64,
        "affected_ids": ["native-note"],
    }
    intent = receipts.begin_event(vault, **intent_kwargs)
    assert receipts.begin_event(vault, **intent_kwargs)["hash"] == intent["hash"]
    terminal = receipts.commit_event(vault, intent["event_id"], outcome="deleted")
    assert receipts.commit_event(vault, intent["event_id"], outcome="deleted")["hash"] == terminal[
        "hash"
    ]

    verification = receipts.verify_chain(vault)
    assert verification["valid"] is True
    instance = verification["instances"][ordinary["instance_id"]]
    assert instance["tail_seq"] == 3
    head = receipts._read_sidecar_head(vault, ordinary["instance_id"])
    assert head is not None
    assert (head[0], head[2]) == (terminal["seq"], terminal["seq"])


def test_windows_critical_directory_flush_failure_keeps_sidecar_heads_until_exact_retry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file-ahead critical suffix cannot promote either sidecar head prematurely."""
    ordinary = receipts.append_event(
        vault,
        event_type="disclosure",
        payload={"outcomes": [{"ref": "Notes/flush", "size": 1}]},
    )
    instance_id = ordinary["instance_id"]
    before_head = receipts._read_sidecar_head(vault, instance_id)
    assert before_head is not None
    intent_kwargs = {
        "operation": "delete",
        "prior": "a" * 64,
        "target": "b" * 64,
        "affected_ids": ["flush-note"],
    }
    original_flush = mutation_lock._windows_flush_directory_handle

    def refuse_directory_flush(_handle: int) -> None:
        raise OSError("injected shared directory flush refusal")

    monkeypatch.setattr(
        mutation_lock, "_windows_flush_directory_handle", refuse_directory_flush
    )
    with pytest.raises(receipts.ReceiptError, match="durable directory"):
        receipts.begin_event(vault, **intent_kwargs)

    assert receipts._read_sidecar_head(vault, instance_id) == before_head
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / instance_id
    records, issues = receipts._chain_state(instance_dir)
    assert issues == []
    assert len(records) == 2
    assert records[-1]["phase"] == "intent"

    monkeypatch.setattr(
        mutation_lock, "_windows_flush_directory_handle", original_flush
    )
    retry = receipts.begin_event(vault, **intent_kwargs)

    assert retry["hash"] == records[-1]["hash"]
    records_after, issues_after = receipts._chain_state(instance_dir)
    assert issues_after == []
    assert len(records_after) == 2
    recovered_head = receipts._read_sidecar_head(vault, instance_id)
    assert recovered_head is not None
    assert (recovered_head[0], recovered_head[1]) == (retry["seq"], retry["hash"])
    assert (recovered_head[2], recovered_head[3]) == (retry["seq"], retry["hash"])
    terminal = receipts.commit_event(vault, retry["event_id"], outcome="deleted")
    assert receipts.verify_chain(vault)["valid"] is True
    after_head = receipts._read_sidecar_head(vault, instance_id)
    assert after_head is not None
    assert (after_head[0], after_head[2]) == (terminal["seq"], terminal["seq"])


def test_windows_month_open_never_uses_crt_to_open_retained_directory(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid Windows instance must reach its child without ``os.open(dir)``."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("a" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    original_open = receipts.os.open

    def reject_crt_path(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) in {instance_dir, month}:
            pytest.fail("Windows receipt open attempted CRT access")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_crt_path)
    with receipts._open_month_fd(instance_dir, month.name) as fd:
        assert os.read(fd, 1) == b"{"


def test_windows_month_open_refuses_ordinary_instance_replacement_after_enumeration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enumerated instance identity must remain bound to the retained handle."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("f" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    replacement = instance_dir.with_name(instance_dir.name + "-replacement")
    replacement.mkdir()
    (replacement / month.name).write_bytes(b'{"schema":"receipt/v1"}\n')
    swapped = False

    def replace_after_enumeration(path: Path, name: str) -> None:
        nonlocal swapped
        assert path == instance_dir
        assert name == month.name
        instance_dir.rename(instance_dir.with_name(instance_dir.name + "-original"))
        replacement.rename(instance_dir)
        swapped = True

    monkeypatch.setattr(receipts, "_after_month_enumeration", replace_after_enumeration)

    with pytest.raises(receipts.ReceiptError, match="instance path"):
        with receipts._open_month_fd(instance_dir, month.name):
            pass

    assert swapped is True


def test_windows_month_open_normalizes_a_direct_child_open_refusal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native child-open failures must stay content-free ``ReceiptError`` results."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("b" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    detail = r"native child denial C:\\private\\receipt.jsonl"
    original_open = receipts._open_secure_file_at

    def deny_child(directory, name, flags, mode=0o600):  # noqa: ANN001
        if name == month.name:
            raise PermissionError(detail)
        return original_open(directory, name, flags, mode)

    monkeypatch.setattr(receipts, "_open_secure_file_at", deny_child)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as exc_info:
        with receipts._open_month_fd(instance_dir, month.name):
            pass
    assert detail not in str(exc_info.value)


def test_windows_month_open_normalizes_child_identity_and_reparse_refusals(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native child identity and reparse failures are content-free receipt refusals."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("d" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    detail = r"reparse target C:\\private\\outside"

    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: False)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as identity_error:
        with receipts._open_month_fd(instance_dir, month.name):
            pass

    original_open = mutation_lock._windows_open_path

    def refuse_reparse(path: Path, **kwargs: object) -> int:
        if not kwargs["directory"]:
            raise OSError(detail)
        return original_open(path, **kwargs)

    monkeypatch.setattr(mutation_lock, "_windows_open_path", refuse_reparse)
    with pytest.raises(receipts.ReceiptError, match="evidence path") as reparse_error:
        with receipts._open_month_fd(instance_dir, month.name):
            pass
    assert detail not in str(identity_error.value)
    assert detail not in str(reparse_error.value)


def test_windows_month_open_normalizes_yielded_io_failure_without_path_detail(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller I/O failure cannot leak a native filesystem diagnostic."""
    instance_dir = vault / "Knowledge Base" / "_Governance" / "events" / ("e" * 32)
    instance_dir.mkdir(parents=True)
    month = _month(instance_dir)
    detail = r"I/O error C:\\private\\receipt.jsonl"
    with pytest.raises(receipts.ReceiptError, match="evidence path") as exc_info:
        with receipts._open_month_fd(instance_dir, month.name):
            raise OSError(detail)
    assert detail not in str(exc_info.value)


def test_windows_directory_flush_never_uses_crt_directory_descriptor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critical directory durability must use a writable raw Windows handle."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    original_open = receipts.os.open

    def reject_crt_directory(path, *args, **kwargs):  # noqa: ANN001
        if Path(path) == directory:
            pytest.fail("Windows receipt fsync attempted CRT directory access")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_crt_directory)
    receipts._fsync_directory(directory)


def test_windows_raw_directory_flush_failure_is_content_free(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-handle flush failure remains a content-free durable-directory refusal."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    detail = r"FlushFileBuffers C:\\private\\receipt-directory"
    handles: list[int] = []

    def fail_flush(handle: int) -> None:
        handles.append(handle)
        raise OSError(detail)

    monkeypatch.setattr(mutation_lock, "_windows_flush_directory_handle", fail_flush)
    with pytest.raises(receipts.ReceiptError, match="durable directory") as exc_info:
        receipts._fsync_directory(directory)

    assert handles
    assert detail not in str(exc_info.value)


def test_windows_raw_directory_handle_closes_once_after_success_and_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt layer owns each native leaf handle exactly once."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    closed: list[int] = []

    class Retained:
        windows_handle = 401

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(receipts, "_open_secure_directory", lambda *_args, **_kwargs: Retained())
    monkeypatch.setattr(receipts, "_windows_open_path", lambda *_args, **_kwargs: 409)
    monkeypatch.setattr(receipts, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(receipts, "_windows_handle_identity", lambda _handle: (1, 2, 3))
    monkeypatch.setattr(receipts, "_windows_close_handle", closed.append)

    with receipts._open_windows_receipt_directory(directory) as handle:
        assert handle == 409
    assert closed == [409]

    closed.clear()
    with pytest.raises(OSError, match="caller failure"):
        with receipts._open_windows_receipt_directory(directory):
            raise OSError("caller failure")
    assert closed == [409]


def test_windows_directory_flush_uses_only_a_write_capable_exact_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flush route retains ancestors read-only and flushes only the leaf handle."""
    directory = vault / "Knowledge Base" / "_Governance" / "events"
    directory.mkdir(parents=True)
    retained: list[Path] = []
    opened: list[tuple[Path, dict[str, object]]] = []
    flushed: list[int] = []

    class Retained:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.windows_handle = 401

        def __enter__(self):
            retained.append(self.path)
            return self

        def __exit__(self, *_args) -> None:
            return None

    def open_path(path: Path, **kwargs: object) -> int:
        opened.append((path, kwargs))
        return 409

    monkeypatch.setattr(mutation_lock, "_open_secure_directory", lambda path, **_kwargs: Retained(path))
    monkeypatch.setattr(mutation_lock, "_windows_open_path", open_path)
    monkeypatch.setattr(mutation_lock, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(mutation_lock, "_windows_handle_identity", lambda handle: (1, 2, handle))
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", lambda _handle: None)
    monkeypatch.setattr(mutation_lock, "_windows_flush_directory_handle", flushed.append)

    receipts._fsync_directory(directory)

    assert retained == [directory.parent]
    assert [path for path, _kwargs in opened] == [directory]
    assert opened[0][1]["directory"] is True
    assert opened[0][1]["access"] == 0x40000000  # GENERIC_WRITE only on the exact leaf
    assert not opened[0][1]["share"] & 0x4  # FILE_SHARE_DELETE
    assert flushed == [409]


@pytest.mark.parametrize("component", ["_Governance", "events", "instance"])
def test_windows_receipt_junction_component_is_refused_without_touching_target(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """A real receipt-path junction never grants access to its target."""
    instance_id = "c" * 32
    events = vault / "Knowledge Base" / "_Governance" / "events"
    governance = events.parent
    outside = _outside_target(tmp_path, component)
    before = _outside_snapshot(outside)
    if component == "_Governance":
        _junction(governance, outside)
    elif component == "events":
        governance.mkdir()
        _junction(events, outside)
    else:
        events.mkdir(parents=True)
        _junction(events / instance_id, outside)
    monkeypatch.setattr(receipts, "_instance_id", lambda _conn: instance_id)

    original_open = receipts.os.open

    def reject_outside_open(path, *args, **kwargs):  # noqa: ANN001
        candidate = Path(path)
        if candidate.resolve(strict=False).is_relative_to(outside.resolve()):
            pytest.fail("receipt opened the reparse target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_outside_open)
    with pytest.raises(receipts.ReceiptError) as exc_info:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert str(outside) not in str(exc_info.value)
    assert _outside_snapshot(outside) == before


@pytest.mark.parametrize("component", ["_Governance", "events", "instance"])
def test_windows_first_instance_creation_refuses_reparse_swap_without_touching_target(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    """A creation-time junction race must not create an outside receipt tree."""
    knowledge_base = vault / "Knowledge Base"
    governance = knowledge_base / "_Governance"
    events = governance / "events"
    outside = _outside_target(tmp_path, component)
    before = _outside_snapshot(outside)
    original_instance_dir = receipts._instance_dir
    swapped = False

    def swap_after_validation(vault_root: Path, instance_id: str) -> Path:
        nonlocal swapped
        candidate = original_instance_dir(vault_root, instance_id)
        if not swapped:
            if component == "_Governance":
                _junction(governance, outside)
            elif component == "events":
                governance.mkdir(exist_ok=True)
                _junction(events, outside)
            else:
                governance.mkdir(exist_ok=True)
                events.mkdir(exist_ok=True)
                _junction(candidate, outside)
            swapped = True
        return candidate

    monkeypatch.setattr(receipts, "_instance_dir", swap_after_validation)

    original_open = receipts.os.open

    def reject_outside_open(path, *args, **kwargs):  # noqa: ANN001
        candidate = Path(path)
        if candidate.resolve(strict=False).is_relative_to(outside.resolve()):
            pytest.fail("receipt opened the swapped reparse target")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", reject_outside_open)
    with pytest.raises(receipts.ReceiptError) as exc_info:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert swapped is True
    assert str(outside) not in str(exc_info.value)
    assert _outside_snapshot(outside) == before
