"""Governance policy loader: strict-YAML parse, fingerprint, empty fast path.

Mirrors `test_access.py`'s coverage of `access._load_config`/`policy_fingerprint`
(same fingerprint-then-recompile shape), plus the kernel-specific conflicted-copy
refusal (design decision D3).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from exomem import reserved_paths
from exomem.governance import egress, policy, receipts, store
from exomem.governance.principal import RequestPrincipal, owner_principal


@pytest.fixture(autouse=True)
def _governance_dispatcher_authority():
    with reserved_paths._owner_authority_scope("govern_memory"):
        yield


def _write(vault: Path, kind: str, name: str, text: str) -> Path:
    p = vault / "Knowledge Base" / "_Governance" / kind / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


_SCOPE_A = (
    "governance_version: 1\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
    "name: client-confidential\n"
    "paths: [\"Projects/AcmeCo/**\"]\n"
)

_RULE_A = (
    "governance_version: 1\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
    "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
    "audience: external\n"
    "ceiling: 2\n"
)


class _UnhashablePolicyValue:
    __hash__ = None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose_condition", ["matches"]),
        ("purpose_condition", {"value": "matches"}),
        ("purpose_condition", _UnhashablePolicyValue()),
        ("kind", ["standing"]),
        ("kind", {"value": "standing"}),
        ("kind", _UnhashablePolicyValue()),
    ],
)
def test_rule_enum_fields_reject_unhashable_containers_without_crashing(
    field: str, value: object
) -> None:
    data = {
        "governance_version": 1,
        "id": "01ARZ3NDEKTSV4RRFFQ69G5FB0",
        "scope_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        "audience": "external",
        "ceiling": 2,
        field: value,
    }

    _rule, findings = policy._parse_rule(data, "rules/invalid.yaml")

    assert any(
        finding["code"] == "invalid_field"
        and finding["path"] == f"rules/invalid.yaml:{field}"
        for finding in findings
    )


@pytest.mark.parametrize(
    ("field", "yaml_value"),
    [
        ("purpose_condition", "[matches]"),
        ("purpose_condition", "{value: matches}"),
        ("kind", "[standing]"),
        ("kind", "{value: standing}"),
    ],
)
def test_rule_enum_container_values_block_a_cold_policy_compile(
    vault: Path, field: str, yaml_value: str
) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(
        vault,
        "rules",
        "invalid",
        _RULE_A + f"{field}: {yaml_value}\n",
    )

    compiled = policy.load(vault)

    assert compiled.blocked
    assert any(
        finding["code"] == "invalid_field"
        and finding["path"].endswith(f":{field}")
        for finding in compiled.findings
    )


def test_empty_dir_yields_empty_singleton(vault: Path) -> None:
    (vault / "Knowledge Base" / "_Governance").mkdir(parents=True, exist_ok=True)
    pol = policy.load(vault)
    assert pol is policy.EMPTY_POLICY
    assert pol.empty is True
    assert pol.fingerprint == "missing"


def test_missing_dir_yields_empty_singleton(vault: Path) -> None:
    pol = policy.load(vault)
    assert pol is policy.EMPTY_POLICY
    assert pol.empty is True


def test_valid_scope_and_rule_compile_clean(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    assert pol.empty is False
    assert not pol.findings
    assert set(pol.scopes) == {"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    scope = pol.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert scope.name == "client-confidential"
    assert scope.paths == ("Projects/AcmeCo/**",)
    assert len(pol.rules) == 1
    rule = pol.rules[0]
    assert rule.audience == "external"
    assert rule.ceiling == 2
    assert rule.scope_ids == ("01ARZ3NDEKTSV4RRFFQ69G5FAV",)


def test_future_sidecar_blocks_even_with_warm_last_good(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    assert not policy.load(vault).blocked
    conn = store.open_connection(vault)
    try:
        conn.execute("PRAGMA user_version=5")
        conn.commit()
    finally:
        conn.close()
    assert policy.load(vault).blocked


def test_corrupt_sidecar_blocks_even_with_warm_last_good(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    assert not policy.load(vault).blocked
    path = store.sidecar_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-a-sqlite-database")
    assert policy.load(vault).blocked


def test_v3_without_archive_table_is_structurally_blocked(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    conn = store.open_connection(vault)
    try:
        conn.execute("DROP TABLE governance_policy_archives")
        conn.commit()
    finally:
        conn.close()
    policy._CACHE.clear()
    assert policy.load(vault).blocked


def test_idle_dev_v3_without_purpose_staging_remains_readable(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        conn.execute("DROP TABLE governance_session_purpose_staging")
        conn.commit()
    finally:
        conn.close()

    assert not policy.load(vault).blocked


def test_unknown_field_is_a_compile_error_and_keeps_last_good(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    good = policy.load(vault)
    assert good.empty is False

    bad_rule = _RULE_A + "unexpected_field: true\n"
    _write(vault, "rules", "acmeco-external", bad_rule)
    refused = policy.load(vault)

    assert refused.scopes == good.scopes
    assert refused.rules == good.rules
    assert any(f["code"] == "unknown_field" for f in refused.findings)
    assert any(f["severity"] == "error" for f in refused.findings)


def test_bad_ceiling_is_a_compile_error(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    bad_rule = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\n"
        "ceiling: 9\n"
    )
    _write(vault, "rules", "acmeco-external", bad_rule)
    pol = policy.load(vault)
    assert any(f["code"] == "invalid_ceiling" for f in pol.findings)
    assert not pol.rules


def test_cold_start_compile_error_is_blocked_not_open(vault: Path) -> None:
    """No prior good compile exists (this is the FIRST load ever for this
    vault): a compile-error refusal must not be conflated with `EMPTY_POLICY`
    (which every caller treats as "no governance, fully open"). A cold-start
    refusal is the distinct `.blocked` fail-closed floor."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    bad_rule = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\n"
        "ceiling: 9\n"  # plain typo — out of range
    )
    _write(vault, "rules", "acmeco-external", bad_rule)

    pol = policy.load(vault)  # first-ever load for this vault: no prior good

    assert pol.empty is False
    assert pol.blocked is True
    assert pol.fingerprint == "blocked"
    assert not pol.scopes
    assert not pol.rules
    assert any(f["code"] == "invalid_ceiling" for f in pol.findings)


def test_cold_start_conflicted_copy_is_blocked_not_open(vault: Path) -> None:
    """Same cold-start distinction, for the conflicted-copy refusal path."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)  # first-ever load: the conflict is there from the start

    assert pol.empty is False
    assert pol.blocked is True
    assert not pol.scopes
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)


def test_unrecognized_file_is_a_warning_not_a_refusal(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    stray = vault / "Knowledge Base" / "_Governance" / "notes.txt"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("not a policy document", encoding="utf-8")
    pol = policy.load(vault)
    assert pol.empty is False
    assert set(pol.scopes) == {"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    assert any(f["code"] == "unknown_file" and f["severity"] == "warning" for f in pol.findings)


def test_fingerprint_stable_across_reparse_and_changes_on_timestamp_preserving_replace(
    vault: Path,
) -> None:
    """Fingerprint analogue of `test_access.py:111`: content-hash based, so a
    same-mtime replacement (bytes changed, mtime explicitly restored) still
    invalidates — a bare stat comparison alone would miss it."""
    path = _write(vault, "scopes", "acmeco", _SCOPE_A)
    first = policy.load(vault)
    again = policy.load(vault)
    assert first.fingerprint == again.fingerprint
    assert first is again  # unchanged signature -> cache hit, same object

    old_mtime = path.stat().st_mtime
    path.write_text(_SCOPE_A + "tags: [\"extra\"]\n", encoding="utf-8")
    os.utime(path, (old_mtime, old_mtime))

    changed = policy.load(vault)
    assert changed.fingerprint != first.fingerprint
    assert changed.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"].tags == ("extra",)


def test_conflicted_copy_sibling_refuses_compile(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    good = policy.load(vault)
    assert good.empty is False

    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    refused = policy.load(vault)
    assert refused.scopes == good.scopes
    assert any(f["code"] == "conflicted_copy" for f in refused.findings)
    # A prior good compile exists: it stays in effect (D3) — NOT the cold-start
    # `.blocked` floor, since there's a real policy to fall back on.
    assert refused.empty is False
    assert refused.blocked is False
    assert refused.fingerprint == good.fingerprint

    # Resolving the conflict (deleting the sibling) lets the next load recompile clean.
    conflict.unlink()
    time.sleep(0.01)
    recovered = policy.load(vault)
    assert recovered.empty is False
    assert not recovered.findings


def test_conflicted_copy_marker_is_case_insensitive(vault: Path) -> None:
    """A capital-C 'Conflicted copy' must be refused as a conflict, not
    silently parsed as a second scope document (which would misreport as
    `duplicate_id` instead of the real problem)."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (Conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)  # cold start: both files present from the first load

    assert pol.blocked is True
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)
    assert not any(f["code"] == "duplicate_id" for f in pol.findings)


def test_dir_of_only_conflicted_copies_is_blocked_not_empty(vault: Path) -> None:
    """When every file in `_Governance/` matches the conflicted-copy marker,
    `_iter_policy_files` recognizes none of them — but that must surface as
    the fail-closed `.blocked` state (with a `conflicted_copy` finding), not
    silently resolve to `EMPTY_POLICY` as if no policy existed at all."""
    stray = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)

    assert pol.empty is False
    assert pol.blocked is True
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)


# --------------------------------------------------------------------------
# DEFECT A — a scope that cannot select non-markdown items
# --------------------------------------------------------------------------


def test_scope_without_a_path_selector_warns_that_media_is_uncovered(
    vault: Path,
) -> None:
    """A tag/type/class scope cannot cover a sidecar-less binary.

    `board-call.mp4` beside a `tags: [confidential]` note comes back at
    DISCLOSURE_MAX while the tagged `.md` is withheld, because a binary has no
    frontmatter to match and no sidecar to borrow it from. The SEMANTICS are
    deliberately unchanged — changing them would make an unreadable file
    guess — but a control that fails closed everywhere else should not leave
    an authoring foot-gun silent, so the compile says so.
    """
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "confidential.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Confidential\ntags: ["confidential"]\n',
        encoding="utf-8",
    )
    pol = policy.load(vault)
    media_findings = [
        f for f in pol.findings if f.get("code") == "SCOPE_CANNOT_SELECT_MEDIA"
    ]
    assert media_findings, f"no media-coverage finding in {pol.findings}"
    assert media_findings[0]["severity"] == "warning"
    assert "paths" in media_findings[0]["detail"]
    # A warning must not refuse the compile.
    assert pol.blocked is False
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in pol.scopes


def test_operational_evidence_does_not_invalidate_policy_cache_or_enforcement(vault: Path) -> None:
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "confidential.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Confidential\npaths: ["Notes/**"]\n',
        encoding="utf-8",
    )
    before = policy.load(vault)
    (gov / "events" / ("f" * 32)).mkdir(parents=True, exist_ok=True)
    (gov / "events" / ("f" * 32) / "2026-07.jsonl").write_text("{}\n", encoding="utf-8")
    (gov / "deletion-tombstones" / "batch.json").parent.mkdir(parents=True, exist_ok=True)
    (gov / "deletion-tombstones" / "batch.json").write_text("{}\n", encoding="utf-8")
    assert policy.load(vault) is before
    policy._CACHE.clear()
    after = policy.load(vault)
    assert after.fingerprint == before.fingerprint
    assert not [f for f in after.findings if f["code"] == "unknown_file"]


def test_receipt_conflict_does_not_disable_active_policy_but_blocks_append(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "external", _RULE_A)
    active = policy.load(vault)
    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    events = vault / "Knowledge Base" / "_Governance" / "events"
    (events / "conflicted copy.jsonl").write_text("{}\n", encoding="utf-8")
    policy._CACHE.clear()
    assert policy.load(vault).fingerprint == active.fingerprint
    try:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})
    except receipts.ReceiptError as exc:
        assert "conflicted receipt evidence" in str(exc)
    else:  # pragma: no cover - fail closed is the assertion
        raise AssertionError("conflicted receipt evidence was appended")
    assert any(
        issue["code"] == "evidence_conflict" for issue in receipts.verify_chain(vault)["issues"]
    )


def test_scope_with_a_path_selector_emits_no_media_warning(vault: Path) -> None:
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "patterns.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Patterns\npaths: ["Notes/Patterns/**"]\ntags: ["confidential"]\n',
        encoding="utf-8",
    )
    pol = policy.load(vault)
    assert not [
        f for f in pol.findings if f.get("code") == "SCOPE_CANNOT_SELECT_MEDIA"
    ]


# --------------------------------------------------------------------------
# DEFECT 1 — the last-good cache must never be able to serve an OPEN policy
# --------------------------------------------------------------------------

_PATTERN_GLOB = "Knowledge Base/Notes/Patterns/**"
_PATTERN_PATH = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
# Deliberately distinct from `_SCOPE_A`/`_RULE_A` so a proposal can be layered
# over a vault that already carries a hand-written policy.
_PROPOSAL_SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC0"
_PROPOSAL_RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC1"


def _proposal_documents(*, ceiling: int = 1) -> dict[str, str]:
    return {
        "scopes/confidential-patterns.yaml": (
            "governance_version: 1\n"
            f"id: {_PROPOSAL_SCOPE_ID}\n"
            "name: Confidential patterns\n"
            f'paths: ["{_PATTERN_GLOB}"]\n'
        ),
        "rules/confidential-patterns.yaml": (
            "governance_version: 1\n"
            f"id: {_PROPOSAL_RULE_ID}\n"
            f'scope_ids: ["{_PROPOSAL_SCOPE_ID}"]\n'
            "audience: external\n"
            f"ceiling: {ceiling}\n"
        ),
    }


def _external() -> RequestPrincipal:
    return RequestPrincipal(
        audience_id="external", surface="mcp", authorization_session_id="conversation-a"
    )


def _propose(vault: Path, *, ceiling: int = 1) -> dict:
    from exomem.governance.tool import op_govern_memory

    return op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Treat pattern notes as confidential for the external audience",
        documents=_proposal_documents(ceiling=ceiling),
        selector_paths=[_PATTERN_GLOB],
        target_ceiling=ceiling,
        duration="standing",
    )


def _pending_first_policy_commit(vault: Path) -> None:
    """Leave the vault's FIRST governance mutation pending mid-activation.

    Same crash seam `test_govern_memory_tool` drives: the commit writes its
    target document and dies before activation, so the sidecar journal stays
    `pending` and every later `policy.load` takes the guarded fallback.
    """
    from exomem.governance.tool import GovernanceCrash, op_govern_memory

    proposal = _propose(vault)
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at="after_target_write:1",
        )


def _disclosure_decision(vault: Path) -> dict | None:
    """One content-returning surface's decision for one item and audience."""
    return egress.annotate_page(
        vault, {"path": _PATTERN_PATH, "body": "hello", "frontmatter": {}},
        principal=_external(),
    )


def test_pending_mutation_on_a_previously_open_vault_fails_closed(vault: Path) -> None:
    """Task 1.1 — a warm cache seeded by an OPEN load must not reopen the vault.

    A long-lived process that served this vault before governance existed has
    `EMPTY_POLICY` in `_LAST_GOOD`. Once the first policy mutation is pending,
    the guarded fallback must reach the fail-closed floor, not hand back an
    empty-looking (and therefore fully open) policy.
    """
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    assert policy.load(vault) is policy.EMPTY_POLICY  # process predates governance

    _pending_first_policy_commit(vault)

    pol = policy.load(vault)
    assert any(f["code"] == "governance_mutation_pending" for f in pol.findings), (
        f"the guarded fallback was not reached: {pol.findings}"
    )
    assert pol.empty is False, "the guarded fallback served the OPEN empty singleton"
    assert pol.blocked is True
    assert _disclosure_decision(vault) is None


def test_empty_open_singleton_is_never_retained_as_last_good(vault: Path) -> None:
    """Task 1.2 — a load resolving to the open singleton leaves the cache alone."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    (vault / "Knowledge Base" / "_Governance").mkdir(parents=True, exist_ok=True)

    assert policy.load(vault) is policy.EMPTY_POLICY

    key = str(policy.governance_root(vault))
    assert key not in policy._LAST_GOOD, (
        "the open singleton was retained as a policy worth falling back to"
    )


def test_warm_and_cold_caches_agree_during_a_pending_first_mutation(vault: Path) -> None:
    """Task 1.3 — process uptime must not change the disclosure decision.

    The warm side is a process that has served this vault since before
    governance existed; the cold side is a freshly started one. The defect is
    process-state dependent, so both halves are exercised here and their
    decisions compared, rather than asserting the invariant once.
    """
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    assert policy.load(vault) is policy.EMPTY_POLICY  # warm process, pre-governance

    _pending_first_policy_commit(vault)

    warm_policy = policy.load(vault)
    warm_decision = _disclosure_decision(vault)

    # A freshly started process: no `_CACHE`, no `_LAST_GOOD`, same vault on disk.
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    egress.clear_decision_memo()
    cold_policy = policy.load(vault)
    cold_decision = _disclosure_decision(vault)

    assert cold_policy.blocked is True  # the cold process already fails closed today
    assert (warm_policy.empty, warm_policy.blocked) == (
        cold_policy.empty,
        cold_policy.blocked,
    ), "process uptime changed the policy state for the same vault"
    assert warm_decision == cold_decision, (
        "process uptime changed the disclosure decision for the same item"
    )


def test_governed_last_good_is_still_served_through_the_guard(vault: Path) -> None:
    """Task 1.4 — a real compiled policy still survives a pending mutation."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    good = policy.load(vault)
    assert good.empty is False and good.blocked is False

    _pending_first_policy_commit(vault)

    guarded = policy.load(vault)
    assert any(f["code"] == "governance_mutation_pending" for f in guarded.findings)
    assert guarded.blocked is False
    assert guarded.empty is False
    assert guarded.fingerprint == good.fingerprint
    assert guarded.scopes == good.scopes
    assert guarded.rules == good.rules
    assert guarded.grants == good.grants
    assert guarded.release_grants == good.release_grants


def test_ungoverned_vault_never_reaches_the_guarded_fallback(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.6 — no `_Governance/` tree keeps the byte-identical fast path."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    assert not (vault / "Knowledge Base" / "_Governance").exists()

    calls: list[str] = []
    real = policy._guarded_policy
    monkeypatch.setattr(
        policy,
        "_guarded_policy",
        lambda *a, **kw: (calls.append(a[1]), real(*a, **kw))[1],
    )

    for _ in range(3):
        assert policy.load(vault) is policy.EMPTY_POLICY

    assert calls == []


# --------------------------------------------------------------------------
# DEFECT 2 — a synchronisation conflict copy must never act as policy
# --------------------------------------------------------------------------

_GRANT_A = (
    "governance_version: 1\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n"
    "kind: standing\n"
    "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
    "audience: external\n"
    "ceiling: 6\n"
)


def _sync_conflict_name(stem: str) -> str:
    """Syncthing's conflict-copy filename for `<stem>.yaml`."""
    return f"{stem}.sync-conflict-20260731-120000-ABCDEFG.yaml"


def test_sync_conflict_copy_of_a_deleted_grant_does_not_restore_access(
    vault: Path,
) -> None:
    """Task 2.1 — deleting a grant revokes it; a conflict copy must not undo that."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    grant_path = _write(vault, "grants", "acmeco-external", _GRANT_A)
    assert len(policy.load(vault).grants) == 1

    grant_path.unlink()  # revoke by deletion
    time.sleep(0.01)
    revoked = policy.load(vault)
    assert revoked.grants == ()
    assert revoked.blocked is False

    # Syncthing lands the deleted document back under a conflict-copy name.
    resurrected = grant_path.parent / _sync_conflict_name("acmeco-external")
    resurrected.write_text(_GRANT_A, encoding="utf-8")
    time.sleep(0.01)

    pol = policy.load(vault)
    assert any(f["code"] == "conflicted_copy" for f in pol.findings), (
        f"the sync-conflict copy was not recognised as a conflict: {pol.findings}"
    )
    assert pol.grants == (), "a revoked grant was resurrected by a sync-conflict copy"
    # The last good governed policy stays in effect (design D3).
    assert pol.empty is False
    assert pol.blocked is False
    assert pol.fingerprint == revoked.fingerprint
    assert pol.scopes == revoked.scopes
    assert pol.rules == revoked.rules


def test_sync_conflict_copy_beside_its_original_is_a_conflict_not_a_duplicate(
    vault: Path,
) -> None:
    """Task 2.2 — the conflict is detected before duplicate-id compilation.

    A `duplicate_id` error reads as an authoring typo and drops the load onto
    the last in-process compile, which during a tightening operation is the
    pre-lockdown policy. The refusal must name the real problem instead, and
    must not fall back below the last good governed policy.
    """
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    rule_path = _write(vault, "rules", "acmeco-external", _RULE_A)
    weaker = policy.load(vault)
    assert weaker.rules[0].ceiling == 2

    # The owner tightens the ceiling; Syncthing lands a copy of the old revision
    # beside it before the tightened document is ever compiled.
    rule_path.write_text(_RULE_A.replace("ceiling: 2", "ceiling: 0"), encoding="utf-8")
    (rule_path.parent / _sync_conflict_name("acmeco-external")).write_text(
        _RULE_A, encoding="utf-8"
    )
    time.sleep(0.01)

    pol = policy.load(vault)
    assert any(f["code"] == "conflicted_copy" for f in pol.findings), (
        f"the sync-conflict copy was not recognised as a conflict: {pol.findings}"
    )
    assert not any(f["code"] == "duplicate_id" for f in pol.findings), (
        "duplicate-identifier compilation ran on a conflict copy"
    )
    # Refused, but never below the last good governed policy — and never open.
    assert pol.empty is False
    assert pol.blocked is False
    assert pol.scopes == weaker.scopes
    assert pol.rules == weaker.rules


def test_cold_start_sync_conflict_copy_is_blocked_not_open(vault: Path) -> None:
    """Task 2.2 — the same refusal with no prior compile hits the closed floor."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    (vault / "Knowledge Base" / "_Governance" / "scopes" / _sync_conflict_name("acmeco")).write_text(
        _SCOPE_A, encoding="utf-8"
    )

    pol = policy.load(vault)

    assert pol.empty is False
    assert pol.blocked is True
    assert not pol.scopes
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)
    assert not any(f["code"] == "duplicate_id" for f in pol.findings)


def test_policy_document_without_either_conflict_marker_compiles_clean(
    vault: Path,
) -> None:
    """Task 2.4 — a merely conflict-ish filename is an ordinary document."""
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    _write(vault, "scopes", "acmeco-conflict-resolution", _SCOPE_A)

    pol = policy.load(vault)

    assert pol.empty is False
    assert pol.blocked is False
    assert not any(f["code"] == "conflicted_copy" for f in pol.findings)
    assert set(pol.scopes) == {"01ARZ3NDEKTSV4RRFFQ69G5FAV"}


def test_a_sync_conflict_copy_refuses_policy_AUTHORING_not_reading(vault: Path) -> None:
    """The authoring half of conflict handling, kept separate from live reading.

    A conflict copy must remain visible to the guarded prospective snapshot even
    though it is never a compilable policy document. Otherwise a mutation can be
    accepted and receipted while the unresolved sibling still decides which
    bytes a later live compile sees.

    Reads must NOT be refused: flooring a warm vault to L0 because a sync tool
    dropped a sibling file would be worse than the defect it prevents.
    """
    from exomem.governance.tool import op_govern_memory

    _write(vault, "scopes", "client", _SCOPE_A)
    baseline = policy.load(vault)
    assert not baseline.blocked and not baseline.empty

    conflict = (
        vault / "Knowledge Base" / "_Governance" / "scopes"
        / _sync_conflict_name("client")
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")
    # Deliberately NOT clearing `_CACHE`: this models a warm runtime, which is
    # the case that matters. The conflict fallback serves the last in-process
    # compile, so clearing the cache here would exercise the cold-start path
    # (correctly `.blocked`) and prove nothing about reads surviving.

    assert policy.has_conflict_copy(vault), "the probe must see the conflict copy"
    assert policy.load(vault).conflicted

    with pytest.raises(Exception) as excinfo:
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="tighten patterns",
            documents=_proposal_documents(ceiling=1),
            selector_paths=[_PATTERN_GLOB],
            target_ceiling=1,
            duration="standing",
        )
    assert getattr(excinfo.value, "code", "") == "GOVERNANCE_CONFLICTED", (
        f"authoring proceeded under a sync conflict: {excinfo.value!r}"
    )

    # Reading is unaffected: the last good policy is still served.
    served = policy.load(vault)
    assert not served.blocked, "a conflict must not floor reads to L0"
    assert served.scopes, "the last good policy must still be served"


def test_the_authoring_gate_creates_no_state_when_it_refuses(vault: Path) -> None:
    """`op_govern_memory` promises a refused operation creates no sidecar,
    policy directory, receipt or marker. The conflict probe therefore reads the
    directory listing rather than going through `load()`, which opens the
    governance sidecar via the guard probe."""
    from exomem.governance.tool import GovernanceError, op_govern_memory

    _write(vault, "scopes", "client", _SCOPE_A)
    conflict = (
        vault / "Knowledge Base" / "_Governance" / "scopes"
        / _sync_conflict_name("client")
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")
    policy._CACHE.clear()

    def _snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(vault)): p.read_bytes()
            for p in sorted(vault.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()
    with pytest.raises(GovernanceError):
        op_govern_memory(
            vault,
            operation="propose",
            principal=owner_principal(),
            intent="tighten patterns",
            documents=_proposal_documents(ceiling=1),
            selector_paths=[_PATTERN_GLOB],
            target_ceiling=1,
            duration="standing",
        )
    assert _snapshot() == before, "the refused authoring gate mutated the vault"


def _workspace_files(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted(vault.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_compile_documents_uses_only_the_supplied_pinned_bytes(vault: Path) -> None:
    source = _write(vault, "scopes", "client", _SCOPE_A)
    pinned = {"scopes/client.yaml": source.read_bytes()}
    source.write_text(_SCOPE_A.replace("client-confidential", "changed-live"), encoding="utf-8")

    compiled = policy.compile_documents(pinned)

    assert compiled.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"].name == "client-confidential"
    assert compiled.fingerprint != policy.load(vault).fingerprint


def test_compile_documents_matches_live_fingerprint_order_across_kinds(
    vault: Path,
) -> None:
    _write(vault, "scopes", "client", _SCOPE_A)
    _write(vault, "rules", "external", _RULE_A)
    root = policy.governance_root(vault)
    pinned = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for _kind, path in policy._iter_policy_files(root)
    }

    assert policy.compile_documents(pinned).fingerprint == policy.load(vault).fingerprint


def test_compile_prospective_returns_a_bound_authoring_snapshot(vault: Path) -> None:
    _write(vault, "scopes", "client", _SCOPE_A)

    prospective = policy.compile_prospective(vault, {})

    assert isinstance(prospective, policy.ProspectiveCompile)
    assert prospective.policy.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"].name == "client-confidential"
    assert prospective.snapshot.source_fingerprint == prospective.policy.fingerprint
    assert dict(prospective.snapshot.documents)["scopes/client.yaml"] == _SCOPE_A.encode()
    assert prospective.snapshot.conflict_set_digest
    assert prospective.snapshot.guard_generation
    assert prospective.snapshot.file_identities[0].path == "scopes/client.yaml"


@pytest.mark.parametrize(
    "name",
    (
        "client (conflicted copy 2026-08-21).yaml",
        "client.sync-conflict-20260821-120000-ABCDEFG.yaml",
    ),
)
def test_compile_prospective_refuses_every_supported_preexisting_conflict_without_state(
    vault: Path,
    name: str,
) -> None:
    _write(vault, "scopes", "client", _SCOPE_A)
    conflict = vault / "Knowledge Base" / "_Governance" / "scopes" / name
    conflict.write_text(_SCOPE_A, encoding="utf-8")
    before = _workspace_files(vault)

    assert policy.compile_prospective(vault, {}) is None
    assert _workspace_files(vault) == before
    assert not store.sidecar_path(vault).exists()


@pytest.mark.parametrize("transition", ("appear", "disappear"))
def test_compile_prospective_refuses_a_conflict_set_change_between_probes(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    _write(vault, "scopes", "client", _SCOPE_A)
    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "client.sync-conflict-20260821-120000-ABCDEFG.yaml"
    )
    if transition == "disappear":
        conflict.write_text(_SCOPE_A, encoding="utf-8")
    changed = False

    def barrier(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase != "after_before" or changed:
            return
        changed = True
        if transition == "appear":
            conflict.write_text(_SCOPE_A, encoding="utf-8")
        else:
            conflict.unlink()

    monkeypatch.setattr(policy, "_authoring_snapshot_barrier", barrier, raising=False)

    assert policy.compile_prospective(vault, {}) is None
    assert changed
    assert conflict.exists() is (transition == "appear")
    assert not store.sidecar_path(vault).exists()


@pytest.mark.parametrize("change", ("replace-bytes", "add-file", "delete-file"))
def test_compile_prospective_refuses_policy_tree_changes_between_read_and_after_probe(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    source = _write(vault, "scopes", "client", _SCOPE_A)
    changed = False

    def barrier(phase: str, _path: str | None = None) -> None:
        nonlocal changed
        if phase != "after_read" or changed:
            return
        changed = True
        if change == "replace-bytes":
            before = source.stat()
            source.write_text(
                _SCOPE_A.replace("client-confidential", "client-classified"),
                encoding="utf-8",
            )
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "add-file":
            _write(vault, "rules", "external", _RULE_A)
        else:
            source.unlink()

    monkeypatch.setattr(policy, "_authoring_snapshot_barrier", barrier, raising=False)

    assert policy.compile_prospective(vault, {}) is None
    assert changed
    assert not store.sidecar_path(vault).exists()


def test_compile_prospective_refuses_pending_guard_generation_drift(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(vault, "scopes", "client", _SCOPE_A)
    probes = iter(
        (
            {"state": "clear", "generation": "guard-before", "event_ids": ()},
            {"state": "clear", "generation": "guard-after", "event_ids": ()},
        )
    )
    monkeypatch.setattr(store, "guard_generation_probe", lambda _vault: next(probes))

    assert policy.compile_prospective(vault, {}) is None


def test_compile_prospective_refuses_a_symlinked_policy_document(vault: Path) -> None:
    outside = vault / "outside.yaml"
    outside.write_text(_SCOPE_A, encoding="utf-8")
    link = vault / "Knowledge Base" / "_Governance" / "scopes" / "client.yaml"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - host capability dependent
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert policy.compile_prospective(vault, {}) is None
    assert not store.sidecar_path(vault).exists()


def test_compile_prospective_refuses_a_hard_linked_policy_document(vault: Path) -> None:
    source = _write(vault, "scopes", "client", _SCOPE_A)
    alias = source.with_name("client-alias.yaml")
    try:
        os.link(source, alias)
    except OSError as exc:  # pragma: no cover - host capability dependent
        pytest.skip(f"hard-link creation unavailable: {exc}")

    assert policy.compile_prospective(vault, {}) is None
    assert not store.sidecar_path(vault).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fixture")
def test_compile_prospective_refuses_a_non_regular_policy_document(vault: Path) -> None:
    fifo = vault / "Knowledge Base" / "_Governance" / "scopes" / "client.yaml"
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)

    assert policy.compile_prospective(vault, {}) is None
    assert not store.sidecar_path(vault).exists()


# ---------------------------------------------------------------------------
# A scope may declare that unnamed audiences get nothing
# (add-default-deny-scope-cap, task 1)
# ---------------------------------------------------------------------------

_SCOPE_A_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_a_scope_may_declare_a_default_deny(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A + "default_deny: true\n")
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    assert pol.blocked is False
    assert [f for f in pol.findings if f["severity"] == "error"] == []
    assert pol.scopes[_SCOPE_A_ID].default_deny is True


def test_a_scope_declaring_false_is_the_same_as_omitting_it(vault: Path) -> None:
    """Not just "is False" — the two documents must compile to the same scope,
    so `default_deny: false` is a no-op an author can write to be explicit."""
    _write(vault, "scopes", "acmeco", _SCOPE_A + "default_deny: false\n")
    _write(vault, "rules", "acmeco-external", _RULE_A)
    explicit = policy.load(vault).scopes[_SCOPE_A_ID]

    policy._CACHE.clear()
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    omitted = policy.load(vault).scopes[_SCOPE_A_ID]

    assert explicit == omitted
    assert explicit.default_deny is False


def test_a_scope_omitting_the_declaration_compiles_exactly_as_before(vault: Path) -> None:
    """The change is opt-in: an untouched document must compile clean, keep its
    content fingerprint, and carry the permissive value."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    assert pol.blocked is False
    assert pol.findings == ()
    assert pol.scopes[_SCOPE_A_ID].default_deny is False


@pytest.mark.parametrize(
    "value",
    [
        '"true"',      # a quoted string is not a boolean
        "1",           # an int is not a boolean
        "[true]",      # a list is not a boolean
        "{}",          # a mapping is not a boolean
        "",            # `default_deny:` with the value forgotten -> YAML null
        "deny",        # a bare word that is not a YAML boolean
    ],
)
def test_a_malformed_declaration_is_an_error_finding_not_a_silent_default(
    vault: Path, value: str
) -> None:
    """A confidentiality control must never fall back to permissive because the
    author mistyped it. The finding is an ERROR, so the compile is refused.

    The code has to be `invalid_field`, not `unknown_field`: before the field
    existed every one of these values was rejected merely because the compiler
    had never heard of the key. That accident would keep this test green while
    the recognised field silently accepted a non-boolean."""
    _write(vault, "scopes", "acmeco", _SCOPE_A + f"default_deny:{f' {value}' if value else ''}\n")
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    findings = [f for f in pol.findings if f["path"].endswith(":default_deny")]
    assert findings, f"no finding for default_deny: {value!r}"
    assert [f["code"] for f in findings] == ["invalid_field"]
    assert all(f["severity"] == "error" for f in findings)
    # Cold start with no prior good compile -> the fail-closed floor, not open.
    assert pol.blocked is True


def test_a_yaml_boolean_spelling_is_accepted(vault: Path) -> None:
    """`yes` is a YAML 1.1 boolean, so it must not be reported as malformed."""
    _write(vault, "scopes", "acmeco", _SCOPE_A + "default_deny: yes\n")
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    assert pol.blocked is False
    assert pol.scopes[_SCOPE_A_ID].default_deny is True


@pytest.mark.parametrize(
    "option_yaml",
    [
        "credential_scrubber: off\n",
        "credential_scrubber: true\n",
        "unknown: value\n",
        "notice: [not, a, string]\n",
        'suspended: "no"\n',
        "constraint_source: scope\n",
    ],
)
def test_rule_options_are_a_closed_typed_registry(vault: Path, option_yaml: str) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "external", _RULE_A + "options:\n  " + option_yaml.replace("\n", "\n  "))

    compiled = policy.load(vault)

    assert compiled.blocked is True
    assert any(finding["path"].startswith("rules/external.yaml:options") for finding in compiled.findings)


@pytest.mark.parametrize(
    "option_yaml",
    [
        "1: value\n",
        "1: value\nunknown: value\n",
        "[list]: value\n",
        "notice: !!set {value: null}\n",
        "notice: !!python/name:os.system ''\n",
        "notice: .nan\n",
        "notice: " + "x" * 501 + "\n",
        "bridge: [wrong]\n",
        "credential_scrubber: on\n",
        "credential_scrubber: false\n",
        "credential_scrubber: no\n",
        'credential_scrubber: "disabled"\n',
        "credential_scrubber: [off]\n",
    ],
)
def test_rule_option_malformed_values_always_block_without_parser_crash(
    vault: Path, option_yaml: str
) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "external", _RULE_A + "options:\n  " + option_yaml.replace("\n", "\n  "))

    compiled = policy.load(vault)

    assert compiled.blocked is True
    assert compiled.findings


def _release_document(*, top_extra: str = "", dependency_extra: str = "", options_extra: str = "") -> str:
    return (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB2\n"
        "kind: release\n"
        "path: Knowledge Base/Notes/Patterns/released.md\n"
        "ref: exomem://memory/00000000-0000-0000-0000-000000000001\n"
        f"content_hash: {'a' * 64}\n"
        "to_audience: external\n"
        "released_at: '2026-08-13T12:00:00Z'\n"
        "why: Owner reviewed the exact release\n"
        "bridge_scope: review\n"
        "bridge_of:\n"
        "  - ref: exomem://memory/00000000-0000-0000-0000-000000000002\n"
        "    path: Knowledge Base/Sources/Other/source.md\n"
        f"    content_hash: {'b' * 64}\n"
        f"    restriction_signature: {'c' * 64}\n"
        f"{dependency_extra}"
        "options:\n"
        "  strip_provenance:\n"
        "    - exomem://memory/00000000-0000-0000-0000-000000000002\n"
        f"{options_extra}"
        f"{top_extra}"
    )


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        ("scopes", _SCOPE_A + "unknown: mixed\n1: mixed\n"),
        (
            "scopes",
            _SCOPE_A + "exclude:\n  paths: []\n  unknown: mixed\n  1: mixed\n",
        ),
        ("rules", _RULE_A + "unknown: mixed\n1: mixed\n"),
        ("grants", _GRANT_A + "unknown: mixed\n1: mixed\n"),
        ("grants", _release_document(top_extra="unknown: mixed\n1: mixed\n")),
        (
            "grants",
            _release_document(
                dependency_extra="    unknown: mixed\n    1: mixed\n"
            ),
        ),
        (
            "grants",
            _release_document(options_extra="  unknown: mixed\n  1: mixed\n"),
        ),
    ],
)
def test_every_strict_policy_map_blocks_mixed_keys_without_type_error(
    vault: Path, kind: str, document: str
) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, kind, "mixed-keys", document)

    compiled = policy.load(vault)

    assert compiled.blocked is True
    assert any(
        finding["code"] == "invalid_field"
        and "keys must be strings" in finding["detail"]
        for finding in compiled.findings
    )


def test_credential_scrubber_occurrence_emits_owner_migration_finding(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(
        vault,
        "rules",
        "legacy-scrubber",
        _RULE_A + "options:\n  credential_scrubber: off\n",
    )

    compiled = policy.load(vault)

    assert compiled.blocked is True
    migration = [
        finding
        for finding in compiled.findings
        if finding["code"] == "owner_migration_required"
    ]
    assert len(migration) == 1
    assert migration[0]["path"].endswith(":options.credential_scrubber")
    assert "remove" in migration[0]["detail"].lower()


@pytest.mark.parametrize(
    "audience_yaml",
    [r'"\0unnamed"', r'"\0unresolved"', r'"external\0suffix"'],
)
@pytest.mark.parametrize(
    ("kind", "field", "document"),
    [
        (
            "rules",
            "audience",
            "governance_version: 1\n"
            "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
            'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
            "audience: {audience}\n"
            "ceiling: 2\n",
        ),
        (
            "grants",
            "audience",
            "governance_version: 1\n"
            "id: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n"
            "kind: standing\n"
            'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
            "audience: {audience}\n"
            "ceiling: 6\n",
        ),
        (
            "grants",
            "to_audience",
            "governance_version: 1\n"
            "id: 01ARZ3NDEKTSV4RRFFQ69G5FB2\n"
            "kind: release\n"
            "path: Knowledge Base/Notes/Patterns/released.md\n"
            "ref: exomem://memory/00000000-0000-0000-0000-000000000001\n"
            f"content_hash: {'a' * 64}\n"
            "to_audience: {audience}\n"
            "released_at: '2026-07-28T12:00:00Z'\n"
            "why: Owner reviewed the exact release\n"
            "bridge_scope: review\n"
            "bridge_of:\n"
            "  - ref: exomem://memory/00000000-0000-0000-0000-000000000002\n"
            "    path: Knowledge Base/Sources/Other/source.md\n"
            f"    content_hash: {'b' * 64}\n"
            f"    restriction_signature: {'c' * 64}\n"
            "options:\n"
            "  strip_provenance:\n"
            "    - exomem://memory/00000000-0000-0000-0000-000000000002\n",
        ),
    ],
)
def test_authored_audiences_cannot_enter_the_reserved_nul_namespace(
    vault: Path, audience_yaml: str, kind: str, field: str, document: str
) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, kind, "reserved-audience", document.format(audience=audience_yaml))

    compiled = policy.load(vault)
    audience_findings = [
        finding for finding in compiled.findings
        if finding["path"].endswith(f":{field}")
    ]

    assert [finding["code"] for finding in audience_findings] == ["invalid_field"]
    assert all(finding["severity"] == "error" for finding in audience_findings)
    assert compiled.blocked is True
    assert compiled.rules == ()
    assert compiled.grants == ()
    assert compiled.release_grants == ()
