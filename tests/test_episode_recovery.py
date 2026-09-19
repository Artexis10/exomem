from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from exomem import (
    freshness,
    memory_refs,
    semantic_index,
    working_set_index,
    working_set_runtime,
)
from exomem.governance import egress, receipts
from exomem.governance.principal import RequestPrincipal, request_scope

_ID = "12345678-1234-5678-1234-567812345678"
_REL = "Knowledge Base/Sources/episode-input.md"


def _owner(name: str, *, surface: str = "mcp") -> RequestPrincipal:
    return RequestPrincipal(audience_id=name, surface=surface)


def _write_page(
    vault: Path,
    body: str,
    *,
    status: str = "active",
    aliases: tuple[str, ...] = (),
) -> str:
    path = vault / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    source = (
        "---\n"
        "type: source\n"
        f"exomem_id: {_ID}\n"
        "title: Episode input\n"
        f"status: {status}\n"
    )
    source += f"aliases: {list(aliases)!r}\n" if aliases else ""
    source += (
        "created: 2026-09-20\n"
        "updated: 2026-09-20\n"
        "sources: []\n"
        "tags: [episode]\n"
        "---\n\n"
        f"{body}"
    )
    path.write_text(
        source,
        encoding="utf-8",
    )
    return memory_refs.memory_ref(_ID)


def _owner_store(vault: Path):
    from exomem.episode_recovery import EpisodeInputOwner

    return EpisodeInputOwner(vault)


def _write_source_rule(vault: Path, *, ceiling: int) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "episode-source.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAA\n"
        "name: Episode source\n"
        'paths: ["Sources/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "episode-source-client-a.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAB\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAA\"]\n"
        "audience: client-a\n"
        f"ceiling: {ceiling}\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()


def _warm_prose_catalogue(vault: Path, warm_managed_cell) -> None:
    warm_managed_cell(vault)
    checkpoint = freshness.live_recall_checkpoint(vault, "kb")
    assert checkpoint is not None
    stamp = working_set_runtime._key_text(
        (checkpoint.triple, checkpoint.policy_version, checkpoint.access_policy_fingerprint)
    )
    working_set_index.WorkingSetIndex(vault).rebuild(freshness_stamp=stamp)


def test_recover_page_input_across_sessions_for_same_audience(vault: Path) -> None:
    reference = _write_page(vault, "Original authorized source body.\n")
    with request_scope(_owner("client-a", surface="mcp")):
        created = _owner_store(vault).create("episode", reference=reference)
    with request_scope(_owner("client-a", surface="rest")):
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered["status"] == "available"
    assert recovered["input_revision"] == 1
    assert recovered["reference"] == reference
    assert len(recovered["digest"]) == 64
    assert recovered["representation"] == "page_body"
    assert recovered["body"] == "Original authorized source body.\n"


def test_different_audience_cannot_inspect_or_recover_same_logical_episode(vault: Path) -> None:
    reference = _write_page(vault, "Audience A only.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("shared-key", reference=reference)
    with request_scope(_owner("client-b")):
        with pytest.raises(ValueError, match="EPISODE_NOT_FOUND"):
            _owner_store(vault).inspect(created["episode_id"])
        with pytest.raises(ValueError, match="EPISODE_NOT_FOUND"):
            _owner_store(vault).recover_input(created["episode_id"])
        independent = _owner_store(vault).create("shared-key", reference=reference)

    assert independent["episode_id"] == created["episode_id"]
    assert independent["journal_digest"] != created["journal_digest"]


def test_facade_never_adopts_a_v1_episode(vault: Path) -> None:
    from exomem.episode_store import EpisodeStore

    reference = _write_page(vault, "Legacy history.\n")
    legacy = EpisodeStore(vault).create("legacy", {"reference": reference})
    with request_scope(_owner("client-a")):
        with pytest.raises(ValueError, match="EPISODE_NOT_FOUND"):
            _owner_store(vault).inspect(legacy["state"]["episode_id"])


def test_digest_only_input_is_stored_but_unrecoverable(vault: Path) -> None:
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("digest", input_digest="a" * 64)
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}


def test_recovery_marks_changed_page_stale_without_replacement(vault: Path) -> None:
    reference = _write_page(vault, "Original source.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("changed", reference=reference)
    _write_page(vault, "Replacement source must not leak.\n")
    with request_scope(_owner("client-a")):
        with egress.disclosure_boundary(vault, "episode-recovery") as collector:
            recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "stale", "input_revision": 1}
    assert collector.outcomes == []


def test_recover_exact_unit_preserves_only_its_source_span(vault: Path) -> None:
    _write_page(
        vault,
        "Private neighbouring text.\n- [finding] Exact unit text ^exact\nMore neighbour text.\n",
    )
    state = semantic_index.current_parent_index_state(vault, _REL)
    unit_ref = state.document.units[0].unit_ref
    assert unit_ref
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("unit", reference=unit_ref)
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered["status"] == "available"
    assert recovered["input_revision"] == 1
    assert recovered["reference"] == unit_ref
    assert len(recovered["digest"]) == 64
    assert recovered["representation"] == "semantic_unit_span"
    assert recovered["text"] == "- [finding] Exact unit text ^exact"


def test_append_input_uses_cas_and_facade_never_returns_raw_history(vault: Path) -> None:
    first = _write_page(vault, "First input.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("append", reference=first)
    second = _write_page(vault, "Second input.\n")
    with request_scope(_owner("client-a")):
        appended = _owner_store(vault).append_input(
            created["episode_id"],
            expected_revision=created["revision"],
            expected_digest=created["journal_digest"],
            reference=second,
        )
        with pytest.raises(ValueError, match="EPISODE_REVISION_CONFLICT"):
            _owner_store(vault).append_input(
                created["episode_id"],
                expected_revision=created["revision"],
                expected_digest=created["journal_digest"],
                input_digest="b" * 64,
            )
        inspected = _owner_store(vault).inspect(created["episode_id"])

    assert appended["input_revision"] == 2
    assert inspected == appended
    assert set(inspected) == {
        "episode_id",
        "revision",
        "journal_digest",
        "input_revision",
        "coverage_current",
    }


def test_unbound_and_reserved_audiences_fail_closed(vault: Path) -> None:
    reference = _write_page(vault, "No implicit owner.\n")
    with pytest.raises(ValueError, match="EPISODE_OWNER_UNRESOLVED"):
        _owner_store(vault).create("unbound", reference=reference)
    with request_scope(RequestPrincipal(audience_id="\x00forbidden", surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_OWNER_UNRESOLVED"):
            _owner_store(vault).create("reserved", reference=reference)


def test_recovery_becomes_unavailable_when_current_policy_revokes_source(vault: Path) -> None:
    reference = _write_page(vault, "Revocable input.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("revoked", reference=reference)
    _write_source_rule(vault, ceiling=0)
    with request_scope(_owner("client-a")):
        with egress.disclosure_boundary(vault, "episode-recovery") as collector:
            recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}
    assert collector.outcomes == []


def test_recovery_refuses_l5_excerpt_as_incomplete_page_input(vault: Path) -> None:
    reference = _write_page(vault, "A complete page must not degrade to an excerpt.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("l5", reference=reference)
    _write_source_rule(vault, ceiling=5)
    with request_scope(_owner("client-a")):
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}


@pytest.mark.parametrize("link", ["Private title", "Secret alias", "Private\u00a0title"])
def test_recovery_withheld_title_alias_or_unicode_link_does_not_silently_edit_source(
    vault: Path, warm_managed_cell, link: str
) -> None:
    _write_source_rule(vault, ceiling=6)
    private = vault / "Knowledge Base" / "Private" / "private-target.md"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text(
        "---\ntype: source\nexomem_id: 32345678-1234-5678-1234-567812345678\n"
        "title: Private title\naliases: ['Secret alias']\nstatus: active\n---\n\nPrivate.\n",
        encoding="utf-8",
    )
    reference = _write_page(vault, f"See [[{link}]].\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("private-link", reference=reference)
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes" / "private.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAC\n"
        "name: Private\npaths: [\"Private/**\"]\n",
        encoding="utf-8",
    )
    (root / "rules" / "private-client-a.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAC\"]\n"
        "audience: client-a\nceiling: 0\n",
        encoding="utf-8",
    )
    _write_source_rule(vault, ceiling=6)
    _warm_prose_catalogue(vault, warm_managed_cell)
    with request_scope(_owner("client-a")):
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}


def test_recovery_refuses_oversized_page_without_truncating(vault: Path) -> None:
    reference = _write_page(vault, "x" * 8193)
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("oversized", reference=reference)
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}


def test_recovery_refuses_malformed_v2_journal_without_hiding_integrity_failure(vault: Path) -> None:
    from exomem.episode_store import EpisodeStore

    reference = _write_page(vault, "Journal evidence.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("journal", reference=reference)
    path = EpisodeStore(vault, owner_audience_id="client-a").path(created["episode_id"])
    journal = json.loads(path.read_text(encoding="utf-8"))
    journal["owner_audience_id"] = "client-b"
    path.write_text(json.dumps(journal), encoding="utf-8")

    with request_scope(_owner("client-a")):
        with pytest.raises(ValueError, match="EPISODE_JOURNAL_INVALID"):
            _owner_store(vault).recover_input(created["episode_id"])


def test_recovery_rejects_empty_policy_snapshot_swap(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import episode_recovery

    reference = _write_page(vault, "Bound snapshot.\n")
    original = egress.annotate_page

    def swap_after_authorization(*args, **kwargs):
        released = original(*args, **kwargs)
        _write_page(vault, "Replacement after authorization.\n")
        return released

    monkeypatch.setattr(episode_recovery.egress, "annotate_page", swap_after_authorization)
    with request_scope(_owner("client-a")):
        with pytest.raises(ValueError, match="EPISODE_INPUT_UNAVAILABLE"):
            _owner_store(vault).create("swapped", reference=reference)


def test_terminal_credential_scrub_refuses_complete_recovery(vault: Path) -> None:
    secret = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
    reference = _write_page(vault, f"{secret}\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("secret", reference=reference)
        with egress.disclosure_boundary(vault, "episode-recovery") as collector:
            recovered = _owner_store(vault).recover_input(created["episode_id"])
            egress.emit_boundary_receipt(collector)

    assert recovered == {"status": "unavailable", "input_revision": 1}
    assert collector.outcomes == []
    assert collector.credential_redactions == 1


def test_recovery_keeps_an_allowed_wikilink(vault: Path, warm_managed_cell) -> None:
    visible = vault / "Knowledge Base" / "Sources" / "visible-link.md"
    visible.write_text(
        "---\ntype: source\nexomem_id: 22345678-1234-5678-1234-567812345678\n"
        "title: Visible link\nstatus: active\n---\n\nVisible.\n",
        encoding="utf-8",
    )
    reference = _write_page(vault, "See [[visible-link]].\n")
    _write_source_rule(vault, ceiling=6)
    _warm_prose_catalogue(vault, warm_managed_cell)
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("visible-link", reference=reference)
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered["status"] == "available"
    assert recovered["body"] == "See [[visible-link]].\n"


def test_recovery_refuses_unknown_wikilink_when_current_catalogue_cannot_prove_it(
    vault: Path, warm_managed_cell
) -> None:
    reference = _write_page(vault, "See [[Unknown title]].\n")
    _write_source_rule(vault, ceiling=6)
    _warm_prose_catalogue(vault, warm_managed_cell)
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("unknown-link", reference=reference)
        recovered = _owner_store(vault).recover_input(created["episode_id"])

    assert recovered == {"status": "unavailable", "input_revision": 1}


def test_recovery_catalogue_check_does_not_record_unreturned_target_content(
    vault: Path, warm_managed_cell
) -> None:
    target = vault / "Knowledge Base" / "Sources" / "visible-title.md"
    target.write_text(
        "---\ntype: source\nexomem_id: 42345678-1234-5678-1234-567812345678\n"
        "title: Visible title\nstatus: active\n---\n\nTarget body is never returned.\n",
        encoding="utf-8",
    )
    reference = _write_page(vault, "See [[Visible title]].\n")
    _write_source_rule(vault, ceiling=6)
    _warm_prose_catalogue(vault, warm_managed_cell)
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("receipt-target", reference=reference)
        with egress.disclosure_boundary(vault, "episode-recovery") as collector:
            recovered = _owner_store(vault).recover_input(created["episode_id"])
            egress.emit_boundary_receipt(collector)

    assert recovered["status"] == "available"
    released = [item.value for item in collector.outcomes if item.value.get("decision") == "released"]
    assert len(released) == 1
    assert released[0]["command"] == "episode-recovery"
    assert released[0]["ref"] == reference
    assert released[0]["content_hash"] == hashlib.sha256(
        recovered["body"].encode()
    ).hexdigest()
    assert released[0]["size"] == len(recovered["body"].encode())
    assert released[0]["representation"] == "page_body"
    assert released[0]["principal"] == released[0]["audience"] == "client-a"
    assert released[0]["level"] == 6
    assert "policy_fingerprint" in released[0]
    emitted = [
        event
        for event in receipts.event_records(vault)
        if event["event_type"] == "disclosure"
    ]
    assert emitted[-1]["outcomes"] == released


def test_create_and_append_do_not_receipt_retained_input(vault: Path) -> None:
    reference = _write_page(vault, "Retained input is not a response body.\n")
    _write_source_rule(vault, ceiling=6)
    with request_scope(_owner("client-a")):
        with egress.disclosure_boundary(vault, "episode-create") as create_collector:
            created = _owner_store(vault).create("create-receipt", reference=reference)
        _write_page(vault, "New retained input is not a response body.\n")
        with egress.disclosure_boundary(vault, "episode-append") as append_collector:
            _owner_store(vault).append_input(
                created["episode_id"],
                expected_revision=created["revision"],
                expected_digest=created["journal_digest"],
                reference=reference,
            )

    assert create_collector.outcomes == []
    assert append_collector.outcomes == []


def test_unit_recovery_receipts_only_the_returned_span(vault: Path) -> None:
    _write_page(
        vault,
        "Private neighbour.\n- [finding] Returned unit ^exact\nPrivate tail.\n",
    )
    _write_source_rule(vault, ceiling=6)
    state = semantic_index.current_parent_index_state(vault, _REL)
    unit_ref = state.document.units[0].unit_ref
    assert unit_ref
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("unit-receipt", reference=unit_ref)
        with egress.disclosure_boundary(vault, "episode-recover") as collector:
            recovered = _owner_store(vault).recover_input(created["episode_id"])
            egress.emit_boundary_receipt(collector)

    released = [item.value for item in collector.outcomes if item.value.get("decision") == "released"]
    assert released[0]["ref"] == unit_ref
    assert released[0]["command"] == "episode-recover"
    assert released[0]["representation"] == "semantic_unit_span"
    assert released[0]["size"] == len(recovered["text"].encode())


@pytest.mark.parametrize("revision", [True, False, 0, -1, "1"])
def test_recovery_rejects_non_positive_or_non_integer_revision(vault: Path, revision: object) -> None:
    reference = _write_page(vault, "Revision input.\n")
    with request_scope(_owner("client-a")):
        created = _owner_store(vault).create("revision", reference=reference)
        with pytest.raises(ValueError, match="EPISODE_INPUT_INVALID"):
            _owner_store(vault).recover_input(created["episode_id"], input_revision=revision)
