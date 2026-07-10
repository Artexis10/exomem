from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import attention, commands, review_state

IDENTITY = "11111111-1111-4111-8111-111111111111"


def _write_isolated(vault: Path, text: str = "First version.") -> Path:
    path = vault / "Knowledge Base/Notes/Insights/isolated.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: insight
status: active
created: 2026-07-10
updated: 2026-07-10
exomem_id: {IDENTITY}
---
## Finding

{text}
""",
        encoding="utf-8",
    )
    return path


def test_attention_adds_stable_review_and_target_refs_without_state_write(
    tmp_path: Path,
) -> None:
    _write_isolated(tmp_path)

    first = attention.attention(tmp_path, categories=["relation_debt"])
    second = attention.attention(tmp_path, categories=["relation_debt"])

    item = first.items[0]
    assert item.ref.startswith("exomem://review/")
    assert item.target_ref == f"exomem://memory/{IDENTITY}"
    assert item.state == "open"
    assert (item.ref, item.fingerprint) == (
        second.items[0].ref,
        second.items[0].fingerprint,
    )
    assert not review_state.state_path(tmp_path).exists()


def test_dismiss_filters_matching_fingerprint_and_changed_content_resurfaces(
    tmp_path: Path,
) -> None:
    _write_isolated(tmp_path)
    original = attention.attention(tmp_path, categories=["relation_debt"]).items[0]
    store = review_state.ReviewStateStore(tmp_path)
    store.apply(
        original.item_id,
        original.fingerprint,
        action="dismiss",
        why="reviewed and intentionally standalone",
        now=dt.datetime(2026, 7, 10, tzinfo=dt.UTC),
    )

    assert attention.attention(tmp_path, categories=["relation_debt"]).items == []
    hidden = attention.attention(
        tmp_path, categories=["relation_debt"], state="dismissed"
    ).items[0]
    assert hidden.ref == original.ref
    assert hidden.state == "dismissed"

    _write_isolated(tmp_path, "Materially changed second version.")
    resurfaced = attention.attention(tmp_path, categories=["relation_debt"]).items[0]
    assert resurfaced.ref == original.ref
    assert resurfaced.fingerprint != original.fingerprint
    assert resurfaced.state == "open"


def test_snooze_expiry_and_reopen(tmp_path: Path) -> None:
    _write_isolated(tmp_path)
    item = attention.attention(
        tmp_path, categories=["relation_debt"], today=dt.date(2026, 7, 10)
    ).items[0]
    store = review_state.ReviewStateStore(tmp_path)
    store.apply(item.item_id, item.fingerprint, action="snooze", until="2026-07-12")

    state, _ = store.effective_state(
        item.item_id, item.fingerprint, today=dt.date(2026, 7, 12)
    )
    assert state == "snoozed"
    state, _ = store.effective_state(
        item.item_id, item.fingerprint, today=dt.date(2026, 7, 13)
    )
    assert state == "open"

    reopened = store.apply(item.item_id, item.fingerprint, action="reopen")
    assert reopened["state"] == "open"
    assert store.decision(item.item_id, item.fingerprint) is None


def test_malformed_state_is_explicit_and_not_overwritten(tmp_path: Path) -> None:
    path = review_state.state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="REVIEW_STATE_INVALID"):
        review_state.ReviewStateStore(tmp_path).load()

    assert path.read_text(encoding="utf-8") == "{not-json"


def test_review_state_json_is_versioned_and_fingerprint_keyed(tmp_path: Path) -> None:
    store = review_state.ReviewStateStore(tmp_path)
    store.apply("a" * 24, "b" * 24, action="dismiss")

    payload = json.loads(review_state.state_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["version"] == review_state.SCHEMA_VERSION
    assert payload["records"][f"{'a' * 24}:{'b' * 24}"]["action"] == "dismiss"


def test_review_reference_validation() -> None:
    ref = review_state.review_ref("a" * 24)
    assert review_state.parse_review_ref(ref) == "a" * 24
    with pytest.raises(ValueError, match="INVALID_REVIEW_REFERENCE"):
        review_state.review_ref("a")
    with pytest.raises(ValueError, match="INVALID_REVIEW_REFERENCE"):
        review_state.parse_review_ref("exomem://review/not-hex")


def test_triage_operation_dismisses_and_reopens_current_item(tmp_path: Path) -> None:
    _write_isolated(tmp_path)
    item = attention.attention(tmp_path, categories=["relation_debt"]).items[0]

    dismissed = commands.op_triage_memory(
        tmp_path,
        ref=item.ref,
        action="dismiss",
        why="reviewed",
    )

    assert dismissed["state"] == "dismissed"
    assert dismissed["target_ref"] == item.target_ref
    assert attention.attention(tmp_path, categories=["relation_debt"]).items == []

    reopened = commands.op_triage_memory(tmp_path, ref=item.ref, action="reopen")
    assert reopened["state"] == "open"
    visible = attention.attention(tmp_path, categories=["relation_debt"]).items
    assert visible[0].ref == item.ref


def test_review_and_triage_permissions_are_separate() -> None:
    registry = {command.name: command for command in commands.PRODUCT_COMMANDS}

    assert registry["review_memory"].read_only is True
    assert registry["triage_memory"].read_only is False
