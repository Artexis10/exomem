"""A batch committed under a vault creation lock fans out after the lock is released.

Measured 2026-09-28 on the personal service: every `edit_memory`,
`observe_memory` and most `remember` commits ran their post-commit index
fan-out inside `vault_creation_lock("semantic-creation")`. The lexical upsert
needs the `lexical-catalog-publication` namespace, which a thread holding
another creation lock may not take (`VAULT_LOCK_NESTED`), so it deferred on
every write: the batch was judged incomplete, a durable full-index receipt was
minted, and retrieval admission was revoked until a background retry landed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, deferred_index, lexstore, semantic_index
from exomem import vault as vault_module

PAGE = "Knowledge Base/Notes/fanout-probe.md"


def _page(root: Path, body: str = "first body") -> Path:
    path = root / PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: note\n---\n\n# Fanout probe\n\n{body}\n", encoding="utf-8")
    return path


def _held() -> set[str]:
    return set(getattr(vault_module._HELD_LOCKS, "keys", set()))


def test_fanout_waits_for_the_creation_lock_to_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(tmp_path)
    seen: list[set[str]] = []
    monkeypatch.setattr(
        vault_module,
        "post_commit_batch_fanout",
        lambda *_a, **_k: seen.append(_held()) or True,
    )
    with vault_module.vault_creation_lock(tmp_path, "semantic-creation"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(path=page, content="# Fanout probe\n\nsecond\n")],
            vault_root=tmp_path,
        )
        assert seen == []
    assert seen == [set()]


def test_fanout_outside_any_creation_lock_still_runs_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(tmp_path)
    seen: list[set[str]] = []
    monkeypatch.setattr(
        vault_module,
        "post_commit_batch_fanout",
        lambda *_a, **_k: seen.append(_held()) or True,
    )
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=page, content="# Fanout probe\n\nsecond\n")],
        vault_root=tmp_path,
    )
    assert seen == [set()]


def test_a_committed_batch_fans_out_even_when_the_locked_body_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(tmp_path)
    seen: list[set[str]] = []
    monkeypatch.setattr(
        vault_module,
        "post_commit_batch_fanout",
        lambda *_a, **_k: seen.append(_held()) or True,
    )
    with pytest.raises(RuntimeError, match="after commit"):
        with vault_module.vault_creation_lock(tmp_path, "semantic-creation"):
            vault_module.batch_atomic_write(
                [vault_module.PlannedWrite(path=page, content="# Fanout probe\n\nsecond\n")],
                vault_root=tmp_path,
            )
            raise RuntimeError("after commit")
    assert seen == [set()]


def test_the_deferred_fanout_sees_the_writers_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`remember` hands its semantic parent state to the fan-out through a
    context variable it resets before the lock is released."""
    page = _page(tmp_path)
    seen: list[object] = []
    monkeypatch.setattr(
        vault_module,
        "post_commit_batch_fanout",
        lambda *_a, **_k: seen.append(semantic_index._ACTIVE_PARENT_STATES.get()) or True,
    )
    marker = {PAGE: object()}
    with vault_module.vault_creation_lock(tmp_path, "semantic-creation"):
        token = semantic_index._ACTIVE_PARENT_STATES.set(marker)
        try:
            vault_module.batch_atomic_write(
                [vault_module.PlannedWrite(path=page, content="# Fanout probe\n\nsecond\n")],
                vault_root=tmp_path,
            )
        finally:
            semantic_index._ACTIVE_PARENT_STATES.reset(token)
    assert seen == [marker]


INSIGHT = "Knowledge Base/Notes/Insights/fanout-observe.md"


def test_an_existing_page_write_upserts_lexical_rows_inline_and_mints_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    insight = tmp_path / INSIGHT
    insight.parent.mkdir(parents=True, exist_ok=True)
    insight.write_text(
        "---\ntitle: Fanout observe\ntype: insight\nstatus: active\n"
        "exomem_id: 00000000-0000-4000-8000-000000000181\nupdated: 2026-07-15\n---\n\n"
        "# Fanout observe\n\nExisting prose.\n",
        encoding="utf-8",
    )
    if not lexstore.maintained_content_index_enabled():
        pytest.skip("no maintained lexical index on this SQLite build")
    lexstore.ensure_fresh(tmp_path)
    scheduled: list[object] = []
    real_schedule = lexstore._schedule_runtime_catalog_repair

    def spy(root, **kwargs):
        scheduled.append(kwargs.get("deferred_paths"))
        return real_schedule(root, **kwargs)

    monkeypatch.setattr(lexstore, "_schedule_runtime_catalog_repair", spy)
    result = commands.op_observe_memory(
        tmp_path,
        path=INSIGHT,
        operation="add",
        category="rule",
        content="Lexical rows land with the write",
    )
    components = {
        item["component"]: item["outcome"] for item in result["semantic"]["index"]["components"]
    }
    assert components["lexstore"] == "completed", result["semantic"]["index"]
    assert scheduled == []
    assert deferred_index.snapshot_full(tmp_path) == []
