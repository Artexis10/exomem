"""Review listings and decisions read one held review pass.

A listing that follows a completed pass reads no vault page, and it lists what an audit in the
request would list. Only the refresh worker runs a pass. A caller that something is withheld from
never reads a held pass. A stale answer never offers an item that a write resolved, moved or
deleted, and it marks an item whose page changed. Background passes keep their pace.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from exomem import (
    activation,
    audit,
    commands,
    due_state,
    find,
    review_pass,
    review_state,
    writer_lease,
)
from exomem import note as note_module
from exomem.cli_ops import OpError
from exomem.governance import egress, membership, policy
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from tests.page_opens import pages_opened

NOTES = "Knowledge Base/Notes/Insights"
SOURCES = "Knowledge Base/Sources/Articles"
OWNER = owner_principal(surface="mcp")
CLIENT = RequestPrincipal(audience_id="client-a", surface="mcp")


class _Passes:
    """Each audit and scan the review queues run: `(kind, start, end, thread)`, in monotonic seconds."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, float, float, str]] = []
        self.failures = 0
        self.delay = 0.0
        self.fail = False

    def background(self) -> list[tuple[str, float, float, str]]:
        """The whole review audits that the refresh worker ran."""
        return [run for run in self.runs if run[0] == "audit" and run[3] == "exomem-review-pass"]

    def in_requests(self) -> list[tuple[str, float, float, str]]:
        return [run for run in self.runs if run[3] != "exomem-review-pass"]


@pytest.fixture
def passes(monkeypatch: pytest.MonkeyPatch) -> _Passes:
    record = _Passes()

    def counted(kind: str, real: Callable) -> Callable:
        def run(*args, **kwargs):
            start = time.monotonic()
            if record.fail:
                record.failures += 1
                raise RuntimeError("a check failed")
            time.sleep(record.delay)
            result = real(*args, **kwargs)
            record.runs.append((kind, start, time.monotonic(), threading.current_thread().name))
            return result

        return run

    monkeypatch.setattr(audit, "audit", counted("audit", audit.audit))
    monkeypatch.setattr(activation, "scan", counted("scan", activation.scan))
    return record


@pytest.fixture(autouse=True)
def writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "writer"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    yield
    review_pass.stop()


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _note(vault: Path, slug: str, body: str, *, updated: str = "2026-01-01") -> str:
    rel = f"{NOTES}/{slug}.md"
    (vault / rel).write_text(
        f"---\ntitle: {slug}\ntype: insight\nstatus: active\ncreated: {updated}\n"
        f"updated: {updated}\n---\n\n{body}",
        encoding="utf-8",
    )
    return rel


def _unlinked_notes(vault: Path, count: int) -> list[str]:
    """Notes with no outbound link: each is one relation-debt item."""
    return [_note(vault, f"fact-{index}", f"# Fact {index}\n\nA measured fact {index}.\n")
            for index in range(count)]


def _source(vault: Path, slug: str) -> str:
    rel = f"{SOURCES}/{slug}.md"
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_text(
        f"---\ntype: source\ningested_into: []\ncaptured: 2026-01-01\n---\n\n# {slug}\n\nImported text.\n",
        encoding="utf-8",
    )
    return rel


def _serve(vault: Path, warm_managed_cell) -> None:
    """Seed the change map, the catalogue and the due-state projection, as a live cell has them."""
    find.clear_cache()
    warm_managed_cell(vault)
    due_state.reconcile(vault)


def _list(vault: Path, *, mode: str = "attention", limit: int = 0) -> dict:
    return writer_lease.invoke_command(
        _command("review_memory"), vault, mode=mode, limit=limit, state="all"
    )


def _dismiss(vault: Path, item_id: str) -> dict:
    return writer_lease.invoke_command(
        _command("triage_memory"), vault, item_id=item_id, action="dismiss", why="handled: resolved"
    )


def _edit(vault: Path, rel: str) -> None:
    writer_lease.invoke_command(
        _command("edit_memory"), vault,
        path=rel, why="tidy", operation={"kind": "replace_tags", "tags": ["tidy"]},
    )


def _paths(reply: dict) -> list[str]:
    return [item["path"] for item in reply["items"]]


def _item(reply: dict, rel: str) -> dict:
    return next(item for item in reply["items"] if item["path"] == rel)


def _from_held(reply: dict) -> bool:
    """Whether a listing read a held pass: its reply then names the pass time."""
    return '"as_of"' in json.dumps(reply)


def _wait_for(condition: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _hold(vault: Path) -> None:
    """Start the refresh worker, and wait until the owner's listings read its two held passes."""
    review_pass.start(vault)
    with request_scope(OWNER):
        _wait_for(lambda: _from_held(_list(vault)) and _from_held(_list(vault, mode="activation")))


def _keep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the worker start no further pass, so a stale pass stays the held one."""
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_GAP_FACTOR", "1000000")


def _withhold_from(vault: Path, audience: str) -> None:
    """Withhold every note named `withheld-*` from `audience`."""
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "withheld-notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAC\nname: Withheld notes\n"
        'paths: ["Notes/Insights/withheld-*.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld-notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        f'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAC"]\naudience: {audience}\nceiling: 0\n',
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    membership.clear_memo()
    policy._CACHE.clear()


@pytest.mark.parametrize("caller", [OWNER, CLIENT], ids=["owner", "restricted"])
def test_a_held_listing_equals_a_listing_computed_in_the_request(
    vault: Path, warm_managed_cell, caller: RequestPrincipal
) -> None:
    _unlinked_notes(vault, 3)
    # Two predictions on one page: two items on one path, and each fused with the page's
    # relation debt.
    _note(vault, "forecast", "## Prediction\n\n- id: p1\n- check_by: 2026-01-01\n\nThe queue drains.\n\n"
          "## Prediction\n\n- id: p2\n- check_by: 2026-02-01\n\nThe cache warms.\n")
    # An old page with two inbound links, one from a page the client may not see. For the
    # owner it is linked enough; for the client it is a stale-review candidate.
    old = _note(vault, "old-claim", "# Old claim\n\nA settled fact.\n", updated="2024-01-01")
    for linker in ("citing", "withheld-citing"):
        _note(vault, linker, f"# Citing\n\nSee [[{old.removesuffix('.md')}]].\n")
    _withhold_from(vault, "client-a")
    _serve(vault, warm_managed_cell)
    _hold(vault)
    with request_scope(OWNER):
        dismissed = _dismiss(vault, _item(_list(vault), f"{NOTES}/fact-1.md")["id"])
    with request_scope(caller):
        held = _list(vault)
        # With no worker, nothing reads a held pass: the next listing runs its own audit.
        review_pass.stop()
        fresh = _list(vault)

    def listing(reply: dict) -> dict:
        return {key: value for key, value in reply.items() if key not in ("as_of", "refreshing")}

    # Nothing is withheld from the owner, so its listing reads the held pass. The client's
    # listing runs its own audit, while the owner's pass is held.
    assert _from_held(held) is (caller is OWNER)
    assert not _from_held(fresh)
    assert listing(held) == listing(fresh)
    assert _paths(fresh).count(f"{NOTES}/forecast.md") == 2
    assert _item(fresh, f"{NOTES}/fact-1.md")["item_id"] == dismissed["item_id"]
    assert _item(fresh, f"{NOTES}/fact-1.md")["state"] == "dismissed"
    assert ("stale_review" in _item(fresh, old)["categories"]) is (caller is CLIENT)


@pytest.mark.parametrize("notes", [30, 120])
def test_a_warm_listing_and_decision_open_no_page(
    vault: Path, warm_managed_cell, notes: int
) -> None:
    _unlinked_notes(vault, notes)
    _serve(vault, warm_managed_cell)
    _hold(vault)
    with request_scope(OWNER):
        with pages_opened(vault) as listing_opened:
            listed = _list(vault)
        with pages_opened(vault) as decision_opened:
            decided = _dismiss(vault, listed["items"][-1]["id"])

    assert len(listed["items"]) >= notes
    assert decided["state"] == "dismissed"
    assert listing_opened == set()
    assert decision_opened == set()


def test_a_listing_with_no_held_pass_runs_only_its_own_audit(
    vault: Path, warm_managed_cell, passes: _Passes
) -> None:
    _unlinked_notes(vault, 3)
    _serve(vault, warm_managed_cell)
    passes.runs.clear()  # the due-state projection's own audit
    passes.delay = 0.5   # the worker's pass is still running when the listing returns
    review_pass.start(vault)
    with request_scope(OWNER):
        listed = _list(vault, mode="relation-debt")

    assert len(listed["items"]) >= 3
    assert not _from_held(listed)
    assert len(passes.in_requests()) == 1
    _wait_for(lambda: len(passes.background()) == 1)


def test_a_stale_listing_marks_an_edited_page(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited, untouched = _unlinked_notes(vault, 2)
    _serve(vault, warm_managed_cell)
    _hold(vault)
    _keep(monkeypatch)
    with request_scope(OWNER):
        before = _list(vault)
        _edit(vault, edited)
        after = _list(vault)

    assert after["as_of"] == before["as_of"]
    assert after["refreshing"] is True
    assert _item(after, edited)["page_changed"] is True
    assert "page_changed" not in _item(after, untouched)


def test_a_stale_listing_drops_a_deleted_or_moved_page(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted, kept = _unlinked_notes(vault, 2)
    moved = _source(vault, "moved-import")
    _serve(vault, warm_managed_cell)
    _hold(vault)
    _keep(monkeypatch)
    with request_scope(OWNER):
        before = _list(vault)
        writer_lease.invoke_command(
            _command("manage_memory_file"), vault,
            operation="delete", path=deleted, confirm=True, force_orphan=True,
        )
        writer_lease.invoke_command(
            _command("manage_memory_file"), vault,
            operation="move", old_path=moved, new_path=moved.replace("Articles", "Books"),
        )
        after = _list(vault)

    assert after["as_of"] == before["as_of"]
    assert {deleted, moved, kept} <= set(_paths(before))
    assert deleted not in _paths(after)
    assert moved not in _paths(after)
    assert kept in _paths(after)


def test_a_stale_listing_drops_a_source_that_a_note_compiled(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled, waiting = _source(vault, "compiled-import"), _source(vault, "waiting-import")
    _serve(vault, warm_managed_cell)
    _hold(vault)
    _keep(monkeypatch)
    with request_scope(OWNER):
        before = _list(vault)
        arguments = {
            "content": "## Claim\n\nCompiled from the import.\n",
            "note_type": "insight",
            "title": "Compiled claim",
            "status": "active",
            "sources": [compiled.removesuffix(".md")],
        }
        draft = note_module.note(vault, validate_only=True, **arguments)
        note_module.note(
            vault, **arguments,
            draft_id=draft.draft_id, draft_hash=draft.draft_hash, draft_token=draft.draft_token,
            relation_disposition="reviewed_none", relation_review_hash=draft.draft_hash,
            relation_review_reason="No relation exists in this fixture.",
        )
        after = _list(vault)

    assert after["as_of"] == before["as_of"]
    assert {compiled, waiting} <= set(_paths(before))
    assert compiled not in _paths(after)
    assert waiting in _paths(after)


def test_a_stale_listing_keeps_due_items_when_the_projection_cannot_be_read(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = _source(vault, "waiting-import")
    edited = _unlinked_notes(vault, 1)[0]
    _serve(vault, warm_managed_cell)
    _hold(vault)
    _keep(monkeypatch)
    with request_scope(OWNER):
        before = _list(vault)
        _edit(vault, edited)
        due_state.state_path(vault).unlink()
        after = _list(vault)

    assert after["as_of"] == before["as_of"]
    assert waiting in _paths(before)
    assert waiting in _paths(after)


def test_a_serving_vault_holds_a_pass_before_the_first_listing(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, passes: _Passes
) -> None:
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_QUIET_SECONDS", "0.1")
    _unlinked_notes(vault, 3)
    _serve(vault, warm_managed_cell)
    passes.runs.clear()  # the due-state projection's own audit
    review_pass.start(vault)
    _wait_for(lambda: len(passes.runs) == 2)
    with request_scope(OWNER):
        listed = _list(vault)
        _list(vault, mode="activation")

    assert len(listed["items"]) >= 3
    assert _from_held(listed)
    assert sorted(run[0] for run in passes.runs) == ["audit", "scan"]


def test_an_aged_pass_is_renewed_only_when_a_listing_reads_it(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, passes: _Passes
) -> None:
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_QUIET_SECONDS", "0.1")
    _unlinked_notes(vault, 3)
    _serve(vault, warm_managed_cell)
    _hold(vault)
    held = len(passes.background())
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_MAX_AGE_SECONDS", "0.2")
    # The pass ages past its maximum, the vault stays quiet, and the gap after the pass ends.
    time.sleep(1.5)
    unread = len(passes.background())
    with request_scope(OWNER):
        _list(vault)
    _wait_for(lambda: len(passes.background()) == held + 1)

    assert unread == held


def test_many_stale_listings_start_one_background_pass(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, passes: _Passes
) -> None:
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_QUIET_SECONDS", "0.1")
    edited = _unlinked_notes(vault, 3)[0]
    _serve(vault, warm_managed_cell)
    _hold(vault)
    held = len(passes.background())
    passes.delay = 0.3
    with request_scope(OWNER):
        _edit(vault, edited)
        for _ in range(5):
            _list(vault)
    _wait_for(lambda: len(passes.background()) == held + 1)
    # A further pass would start one gap (three pass durations) after this one ends.
    time.sleep(4 * passes.delay + 0.5)

    assert len(passes.background()) == held + 1


def test_two_background_passes_keep_the_minimum_gap(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, passes: _Passes
) -> None:
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_QUIET_SECONDS", "0.1")
    edited = _unlinked_notes(vault, 3)[0]
    _serve(vault, warm_managed_cell)
    passes.delay = 0.3
    _hold(vault)
    with request_scope(OWNER):
        _edit(vault, edited)
        _list(vault)
    _wait_for(lambda: len(passes.background()) == 2)

    (_, first_start, first_end, _), (_, second_start, _, _) = passes.background()
    assert second_start - first_end >= review_pass.gap_factor() * (first_end - first_start)


def test_a_failed_background_pass_keeps_the_held_pass(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, passes: _Passes
) -> None:
    monkeypatch.setenv("EXOMEM_REVIEW_PASS_QUIET_SECONDS", "0.1")
    edited = _unlinked_notes(vault, 3)[0]
    _serve(vault, warm_managed_cell)
    _hold(vault)
    with request_scope(OWNER):
        before = _list(vault)
        passes.fail = True
        _edit(vault, edited)
        _list(vault)
        _wait_for(lambda: passes.failures >= 1)
        after = _list(vault)

    assert after["as_of"] == before["as_of"]
    assert _paths(after) == _paths(before)


def test_a_failing_check_fails_only_the_queues_that_run_it(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unlinked_notes(vault, 3)
    _serve(vault, warm_managed_cell)
    real, failed = audit.audit, []

    def one_check_fails(root, *, categories=None, **kwargs):
        if "stale_review" in (categories or ()):
            failed.append(threading.current_thread().name)
            raise RuntimeError("a check failed")
        return real(root, categories=categories, **kwargs)

    monkeypatch.setattr(audit, "audit", one_check_fails)
    review_pass.start(vault)
    with request_scope(OWNER):
        first = _list(vault, mode="relation-debt")
        # The worker's pass runs every check, so it fails; the listing ran its own audit.
        _wait_for(lambda: "exomem-review-pass" in failed)
        second = _list(vault, mode="relation-debt")
        with pytest.raises(RuntimeError):
            _list(vault, mode="stale")

    assert len(first["items"]) >= 3
    assert not _from_held(second)
    assert _paths(second) == _paths(first)


def test_an_item_ranked_past_two_hundred_binds(vault: Path, warm_managed_cell) -> None:
    _unlinked_notes(vault, 210)
    _serve(vault, warm_managed_cell)
    with request_scope(OWNER):
        listed = _list(vault)
        last = listed["items"][-1]
        decided = _dismiss(vault, last["id"])

    assert len(listed["items"]) > 200
    assert decided["item_id"] == last["item_id"]
    assert decided["state"] == "dismissed"


def test_a_decision_stamps_no_item_as_surfaced(vault: Path, warm_managed_cell) -> None:
    _unlinked_notes(vault, 5)
    _serve(vault, warm_managed_cell)

    def surfaced() -> set[str]:
        return set(review_state.ReviewStateStore(vault).load().get("surfaced") or {})

    with request_scope(OWNER):
        # An unlinked note has no stored due entry, so its id binds from the queues.
        shown = _list(vault, mode="relation-debt", limit=1)
        before = surfaced()
        _dismiss(vault, shown["items"][0]["id"])

    assert len(before) == 1
    assert surfaced() == before


def test_an_unknown_id_is_refused_without_an_audit(
    vault: Path, warm_managed_cell, passes: _Passes
) -> None:
    _unlinked_notes(vault, 3)
    _serve(vault, warm_managed_cell)
    _hold(vault)
    ran = len(passes.runs)
    with request_scope(OWNER), pytest.raises(OpError) as refused:
        _dismiss(vault, "missing.version")

    assert refused.value.code == "ITEM_NOT_FOUND"
    assert len(passes.runs) == ran
