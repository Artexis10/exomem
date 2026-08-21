"""The Tier-2 file leaves are self-guarding on every routing.

The non-semantic branches (non-``.md`` create/append, directory create)
acquire the vault mutation boundary in the leaf itself, so a narrowed command
surface can never write governed log/index state — or race concurrent
creates — without the boundary held. Regression for the review finding that
these three sub-paths escaped the boundary entirely under the narrow
predicate, silently clobbering concurrent writes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import append_to_file as append_module
from exomem import create_directory as create_directory_module
from exomem import create_file as create_file_module
from exomem import mutation_lock
from exomem.append_to_file import append_to_file
from exomem.cli_ops import OpError
from exomem.create_directory import create_directory
from exomem.create_file import CreateFileError, create_file


def _record_boundary_state(monkeypatch, module, attr="batch_atomic_write"):
    observed: list[str] = []
    real = getattr(module, attr)

    def spy(*args, **kwargs):
        observed.append(mutation_lock.active_mutation_snapshot()["state"])
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, spy)
    return observed


def test_non_markdown_create_writes_under_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _record_boundary_state(monkeypatch, create_file_module)
    create_file(vault, path="Knowledge Base/data.json", content="{}\n")
    assert observed == ["held"]


def test_non_markdown_append_writes_under_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_file(vault, path="Knowledge Base/notes.txt", content="one\n")
    observed = _record_boundary_state(monkeypatch, append_module)
    append_to_file(vault, path="Knowledge Base/notes.txt", content="two\n")
    assert observed == ["held"]


def test_directory_create_runs_under_the_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _record_boundary_state(
        monkeypatch, create_directory_module, attr="write_log_entry"
    )
    create_directory(vault, path="Knowledge Base/New Area")
    assert observed == ["held"]


def test_concurrent_non_markdown_creates_do_not_clobber(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent creates of the same non-``.md`` path: exactly one wins,
    and the loser gets an honest refusal instead of silently clobbering the
    winner's bytes (the reproduced review defect). A saturated outer mutation
    boundary may return retryable MUTATION_BUSY before the leaf re-check."""
    real_write = create_file_module.batch_atomic_write
    a_in_boundary = threading.Event()
    a_release = threading.Event()
    gated_once = threading.Event()

    def gated_write(*args, **kwargs):
        if not gated_once.is_set():
            gated_once.set()
            a_in_boundary.set()
            a_release.wait(2.0)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(create_file_module, "batch_atomic_write", gated_write)

    results: dict[str, str] = {}

    def writer(name: str) -> None:
        try:
            create_file(
                vault,
                path="Knowledge Base/race.json",
                content=f'{{"writer": "{name}"}}\n',
            )
            results[name] = "ok"
        except (CreateFileError, OpError) as error:
            results[name] = error.code

    thread_a = threading.Thread(target=writer, args=("A",))
    thread_a.start()
    assert a_in_boundary.wait(5.0), "writer A never reached its commit"
    thread_b = threading.Thread(target=writer, args=("B",))
    thread_b.start()
    # Give B a beat to pass its pre-boundary existence check and park on the
    # boundary A holds; then release A. (If B arrives later, its ordinary
    # pre-check refuses instead — the same honest FILE_EXISTS outcome.)
    time.sleep(0.1)
    a_release.set()
    thread_a.join(10.0)
    thread_b.join(10.0)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert results["A"] == "ok"
    assert results["B"] in {"FILE_EXISTS", "MUTATION_BUSY"}
    if results["B"] == "MUTATION_BUSY":
        with pytest.raises(CreateFileError) as retry:
            create_file(
                vault,
                path="Knowledge Base/race.json",
                content='{"writer": "B"}\n',
            )
        assert retry.value.code == "FILE_EXISTS"
    content = (vault / "Knowledge Base" / "race.json").read_text(encoding="utf-8")
    assert content == '{"writer": "A"}\n'
