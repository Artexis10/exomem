"""Where the due-state block reaches the agent, and where it does not.

The substrate detects due work, and review queues run only when explicitly invoked. The
bootstrap payload carries the block, so a session that starts on a hookless surface still
hears about it, and `activate_context` names the one due item that fits its turn.

A write reply and a search reply carry no block. They would tell the agent about review work
unrelated to its call, which it can neither act on nor pass to its next call.

`test_an_overdue_check_by_reaches_bootstrap_and_no_write_or_search_reply` pins that split with
one authored obligation. The rest pins the bootstrap teaching lines and the receipts a write
leaves while its own (now unused) projection still runs.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest

from exomem import commands, review_ids, writer_lease
from exomem import find as find_module
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface

# These tests count dated obligations; the fixture vault's Source backlog would shift every total.
pytestmark = pytest.mark.usefixtures("obligations_only")


def _due_state():
    """Import the projection module lazily.

    The gap proof below must fail on BEHAVIOUR — three carriers that say nothing
    about an authored obligation — and not on an ImportError for the module that
    fixes it. A top-level import would make the red evidence a collection error,
    which proves the module is absent and nothing else.
    """
    from exomem import due_state

    return due_state

INSIGHTS = "Knowledge Base/Notes/Insights"
SCRATCH = "Knowledge Base/Notes/Research/Infrastructure/carrier-scratch.md"


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


@pytest.fixture(autouse=True)
def _clean_emission_state():
    try:
        _due_state().reset_emission_state()
    except ImportError:
        yield
        return
    yield
    _due_state().reset_emission_state()


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    return rel


def _overdue_prediction(vault: Path, slug: str = "backlog", *, check_by: str) -> str:
    """A prediction whose authored check date has already passed."""
    return _write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        "status: active\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Prediction\n\n"
        "- id: p1\n"
        f"- check_by: {check_by}\n\n"
        "The autovacuum backlog clears within a week.\n\n"
        "## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Research/Infrastructure/carrier-scratch]]\n",
    )


def _scratch_page(vault: Path) -> str:
    return _write(
        vault,
        SCRATCH,
        "---\n"
        "title: carrier scratch\n"
        "type: research-note\n"
        "project: infrastructure\n"
        "status: active\n"
        "created: 2026-08-01\n"
        "updated: 2026-08-01\n"
        "---\n\n"
        "# Carrier scratch\n\n"
        "## Observations\n\n"
        "- [finding] The connection pool saturates above 400 concurrent readers.\n\n"
        "## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]\n",
    )


def _yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _govern(vault: Path) -> None:
    """A real (permissive) release policy, so the egress filter actually runs.

    Without a policy `release_walk_filter` takes its empty-policy fast path and
    records nothing at all, which would make any receipt assertion vacuous.
    """
    from exomem.governance import membership, policy

    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "notes.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Patterns\n"
        'paths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "rules" / "notes-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 3\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    from exomem.governance import egress as egress_module

    egress_module.clear_decision_memo()
    find_module.clear_cache()


def _served_ref(vault: Path) -> str:
    """The review ref of the one thing this vault currently owes."""
    rows = _due_state().served_entries(vault)
    assert len(rows) == 1, f"expected exactly one due item, got {len(rows)}"
    return rows[0]["ref"]


def _observe(
    vault: Path, content: str, *, response_detail: str | None = None, path: str = SCRATCH,
) -> dict:
    kwargs: dict = {
        "path": path,
        "operation": "add",
        "category": "finding",
        "content": content,
        "tags": ["infrastructure"],
    }
    if response_detail is not None:
        kwargs["response_detail"] = response_detail
    return writer_lease.invoke_command(_command("observe_memory"), vault, **kwargs)


def _prime(vault: Path) -> None:
    """Build the projection, then start the session that will carry it."""
    _due_state().reconcile(vault)
    commands.op_bootstrap(vault, section="all")
    _fresh_session()


def _fresh_session() -> None:
    """Forget what has already been emitted, as a new conversation would.

    Emission governance is per (session, audience), so probing three carriers in
    one process without this would measure the governor rather than the carriers:
    the first probe emits and the other two correctly go quiet. Before the fix
    exists this is a no-op, so the red evidence is unaffected by it.
    """
    try:
        _due_state().reset_emission_state()
    except ImportError:
        pass


@pytest.mark.usefixtures("anchors_packet")  # relevant-due advice is anchors-only until the maps packet carries turn-relevant material; see Amendment 12
def test_activation_surfaces_the_relevant_sixth_due_item(vault: Path) -> None:
    """A global five-item list must not hide overdue work on the page being discussed."""
    from exomem import lexstore, working_set_index

    for index in range(5):
        _overdue_prediction(vault, f"older-{index}", check_by="2025-01-01")
    target = _overdue_prediction(vault, "current-backlog", check_by=_yesterday())
    _write(vault, target, (vault / target).read_text(encoding="utf-8")
           + "\n## Observations\n\n- [finding] The backlog remains above its operating limit. ^current\n")
    _due_state().reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    row = next(row for row in _due_state().served_entries(vault) if row["path"] == target)
    assert row["ref"] not in {item["ref"] for item in _due_state().served(vault)["top"]}

    packet = writer_lease.invoke_command(
        _command("activate_context"), vault, turn="Review the current backlog.", anchor=target
    )

    assert packet.get("relevant_due_refs") == [{
        "ref": row["ref"], "fingerprint": row["fingerprint"], "id": review_ids.item_id(row),
        "category": row["category"], "due_since": row["due_since"],
        "anchor": packet["anchors"][0]["ref"],
    }]
    assert "due_state" not in packet
    assert _due_state().served(vault)["total"] == 6

    context = writer_lease.invoke_command(
        _command("review_item_context"), vault,
        ref=row["ref"], expected_fingerprint=row["fingerprint"],
    )
    assert context["target"]["path"] == target
    dismissed = writer_lease.invoke_command(
        _command("triage_memory"), vault, ref=row["ref"], action="dismiss",
        expected_fingerprint=row["fingerprint"], why="handled: reviewed during the current work",
    )
    assert dismissed["state"] == "dismissed"
    after = writer_lease.invoke_command(
        _command("activate_context"), vault, turn="Review the current backlog.", anchor=target
    )
    assert "relevant_due_refs" not in after


@pytest.mark.usefixtures("anchors_packet")  # relevant-due advice is anchors-only until the maps packet carries turn-relevant material; see Amendment 12
def test_one_authored_prediction_update_clears_activation_advice(vault: Path) -> None:
    """Resolution writes authored state once; it must not require a second triage write."""
    from exomem import lexstore, working_set_index

    target = _overdue_prediction(vault, "bounded-prediction", check_by=_yesterday())
    source = (vault / target).read_text(encoding="utf-8")
    source = source.replace("type: insight\n", "type: insight\nexomem_id: 00000000-0000-4000-8000-000000000091\n")
    source = source.replace("## Prediction\n\n", "## Prediction\n\n- category: prediction\n")
    source += "\n## Observations\n\n- [finding] The backlog remains above its operating limit. ^current\n"
    _write(vault, target, source)
    _due_state().reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    packet = writer_lease.invoke_command(
        _command("activate_context"), vault, turn="Review this prediction.", anchor=target
    )
    (candidate,) = packet["relevant_due_refs"]
    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        writer_lease.invoke_command(
            _command("review_item_context"), vault, ref=candidate["ref"],
            expected_fingerprint="0" * 24,
        )
    read = writer_lease.invoke_command(
        _command("read_memory"), vault, path=target,
        unit_ref="exomem://memory/00000000-0000-4000-8000-000000000091#p1",
    )
    assert read["status"] == "found"
    assert read["unit"]["fingerprint"] != candidate["fingerprint"]

    updated = writer_lease.invoke_command(
        _command("observe_memory"), vault, path=target, operation="update", kind="prediction",
        category="prediction", content=read["unit"]["content"], verdict="confirmed",
        unit_ref=read["unit_ref"], expected_fingerprint=read["unit"]["fingerprint"],
        expected_hash=read["parent"]["content_hash"],
    )

    assert updated["user_summary"].startswith("Updated")
    assert "verdict: confirmed" in (vault / target).read_text(encoding="utf-8")
    assert not _due_state().served_entries(vault, paths=frozenset({target}), recover=False)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    after = writer_lease.invoke_command(
        _command("activate_context"), vault, turn="Review this prediction.", anchor=target
    )
    assert "relevant_due_refs" not in after


def test_bounded_due_reads_do_not_contaminate_full_served_views(vault: Path) -> None:
    """A partial activation view must not replace the cache used by ordinary due lists."""
    first = _overdue_prediction(vault, "first", check_by="2025-01-01")
    second = _overdue_prediction(vault, "second", check_by=_yesterday())
    _due_state().reconcile(vault)
    before = _due_state().served_entries(vault)
    selected = _due_state().served_entries(vault, paths=frozenset({second}), recover=False)
    assert [row["path"] for row in selected] == [second]
    assert first in {row["path"] for row in before}
    assert _due_state().served_entries(vault) == before


@pytest.mark.usefixtures("anchors_packet")  # relevant-due advice is anchors-only until the maps packet carries turn-relevant material; see Amendment 12
@pytest.mark.parametrize("evidence", ["recent", "partial"])
def test_recent_or_partial_context_does_not_raise_due_advice(vault: Path, evidence: str) -> None:
    """A recent pointer or one partial name cannot associate an unrelated interruption."""
    from exomem import lexstore, working_set_heat, working_set_index

    target = "Knowledge Base/Products/Glacier Cargo.md"
    _write(vault, target,
           "---\ntype: product\nstatus: active\nupdated: 2026-01-01\n---\n\n"
           "# Glacier Cargo\n\nA freight carrier.\n\n"
           "- [constraint] Keep the load below its operating limit. ^limit\n\n"
           "## Prediction\n\n- id: p1\n- check_by: " + _yesterday()
           + "\n\nThe carrier supports the planned load.\n")
    _due_state().reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    if evidence == "recent":
        assert working_set_heat.append(vault, [
            working_set_heat.HeatEvent(time.time_ns(), target, "work", origin="edit_memory")
        ])
    packet = writer_lease.invoke_command(
        _command("activate_context"), vault,
        turn="ok continue" if evidence == "recent" else "Glacier",
    )

    if evidence == "recent":
        assert target in {item["path"] for item in packet["recent_context"]}
    else:
        assert any(item["path"] == target and item["status"] == "partial"
                   for item in packet["anchors"])
    assert not any(item["status"] == "resolved" for item in packet["anchors"])
    assert "relevant_due_refs" not in packet


@pytest.mark.parametrize("projection", ["missing", "unreadable"])
def test_activation_omits_unavailable_due_projection_without_recovery(
    vault: Path, monkeypatch: pytest.MonkeyPatch, projection: str,
) -> None:
    """Advice must not turn an otherwise valid activation into a vault recovery pass."""
    from exomem import lexstore, working_set_index

    target = _scratch_page(vault)
    lexstore.ensure_fresh(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    if projection == "unreadable":
        _due_state().state_path(vault).write_text("{invalid", encoding="utf-8")
    scans: list[Path] = []

    def attempted(root, **kwargs):
        scans.append(root)
        raise AssertionError("activation requested due recovery")

    monkeypatch.setattr(_due_state(), "_schedule_reconcile", attempted)
    packet = writer_lease.invoke_command(
        _command("activate_context"), vault, turn="Review the connection pool.", anchor=target
    )
    assert packet["abstained"] is False
    assert "relevant_due_refs" not in packet
    assert "due_state" not in packet
    assert scans == []


# ==========================================================================
# one obligation, one carrier
# ==========================================================================


def test_an_overdue_check_by_reaches_bootstrap_and_no_write_or_search_reply(vault: Path) -> None:
    """Bootstrap and the review queue report the obligation; a write and a search do not."""
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _due_state().reconcile(vault)

    _fresh_session()
    bootstrap_payload = commands.op_bootstrap(vault, section="all")
    _fresh_session()
    compact = _observe(vault, "Reader saturation reproduces on the replica too.", path=f"{INSIGHTS}/backlog.md")
    _fresh_session()
    full = _observe(
        vault, "The pool saturates above four hundred readers.", response_detail="full",
        path=f"{INSIGHTS}/backlog.md",
    )
    _fresh_session()
    search = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)

    assert bootstrap_payload["due_state"]["total"] == 1
    assert _served_ref(vault).startswith("exomem://review/")
    assert "user_summary" in compact
    assert "due_state" not in json.dumps(compact)
    assert "due_state" not in json.dumps(full)
    assert search["hits"]
    assert "due_state" not in search


# ==========================================================================
# the write path still projects the block, and a fault in it never costs the write
# ==========================================================================


def test_a_public_write_keeps_projection_counts_when_the_ledger_clips_phases(
    vault: Path,
) -> None:
    from exomem import call_ledger, call_spans

    _scratch_page(vault)
    _prime(vault)
    token = "projection-write-counts"
    handle = call_spans.MCP_CALL_TOKEN.set(token)
    try:
        result = _observe(vault, "The connection pool has a bounded queue.")
        spans = call_spans.pop_call_spans(token)
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert result["user_summary"].startswith("Saved")
    phase = next(span for span in spans if span["name"] == "post_commit.due_state")
    counts = phase["fields"]
    assert counts["save_attempts"] == counts["saves"]
    assert counts["saves"] + counts["unchanged"] > 0
    assert (counts["saved_bytes"] > 0) == (counts["saves"] > 0)

    row = call_ledger.build_row(
        sequence=1,
        prev_hash=call_ledger.GENESIS_HASH,
        request_id="projection-write-counts",
        tool="observe_memory",
        outcome="ok",
        duration_ms=None,
        access="write",
        spans=spans + [
            {"name": f"synthetic.phase.{index}", "count": 1, "ms": phase["ms"] / 2}
            for index in range(40)
        ],
    )
    assert row["spans_truncated"] > 0
    retained = next(span for span in row["spans"] if span["name"] == phase["name"])
    assert retained["fields"] == counts


def test_the_legacy_detail_omits_the_block(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    result = _observe(
        vault, "Reader saturation reproduces on the replica too.", response_detail="legacy"
    )

    assert "due_state" not in json.dumps(result)


def test_a_projection_fault_leaves_the_write_committed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same fail-open contract the structural advisory already holds."""
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("due-state projection exploded")

    monkeypatch.setattr(_due_state(), "block_for_write", _boom)

    result = _observe(vault, "Reader saturation reproduces on the replica too.")

    assert result["user_summary"].startswith("Saved")
    assert "due_state" not in result


# ==========================================================================
# search
# ==========================================================================


def test_recall_on_a_quiet_vault_keeps_due_advice_absent(vault: Path) -> None:
    """A search miss carries recovery, without adding a due-state block."""
    result = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)

    assert result["hits"] == []
    assert result["next_action"]["state"] == "search_miss"
    assert "due_state" not in result


# ==========================================================================
# bootstrap payload and its teaching lines
# ==========================================================================


def test_the_bootstrap_payload_carries_the_block(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)

    payload = commands.op_bootstrap(vault, section="all")

    block = payload["due_state"]
    assert block["total"] == 1
    assert block["categories"] == {"prediction_window": 1}
    assert block["top"][0]["due_since"] == _yesterday()


def test_a_quiet_vault_bootstraps_without_the_key(vault: Path) -> None:
    assert "due_state" not in commands.op_bootstrap(vault, section="all")


def test_the_bootstrap_guidance_teaches_how_to_read_the_counts(vault: Path) -> None:
    post_write = commands.op_bootstrap(vault, section="all")["authoring_contract"]["post_write"]

    assert "due_state" in post_write
    assert "due_state_handling" in post_write
    assert "due_state_authority" in post_write

    described = post_write["due_state"].lower()
    assert "global" in described
    assert "write replies carry none" in described

    handling = post_write["due_state_handling"].lower()
    assert "consult review" in handling and "interrupt" in handling
    assert "fingerprint" in handling
    assert "silence" in handling

    authority = post_write["due_state_authority"].lower()
    assert "advisory" in authority
    assert "never" in authority


def test_the_teaching_lines_survive_a_reduced_surface(vault: Path) -> None:
    """`_filter_bootstrap_payload` deletes any string naming an unavailable command.

    The doctrine has to reach exactly the reduced surfaces that have no hooks, so
    a teaching line phrased as a tool call would vanish where it matters most.
    """
    descriptor = ActiveSurfaceDescriptor(
        surface="test",
        profile="tier-one-only",
        tier2_enabled=False,
        product_commands=("bootstrap", "search_memory"),
    )
    with active_surface(descriptor):
        payload = commands.op_bootstrap(vault, section="all")

    post_write = payload["authoring_contract"]["post_write"]
    for key in ("due_state", "due_state_handling", "due_state_authority"):
        assert key in post_write, key

    serialized = json.dumps(
        {key: post_write[key] for key in post_write if key.startswith("due_state")}
    )
    for unavailable in set(commands.PRODUCT_PUBLIC_NAMES) - set(
        descriptor.product_commands
    ):
        assert unavailable not in serialized, unavailable


#: The advisory fields the DEFAULT compact response can actually carry. Each is
#: proved elsewhere against a real response rather than asserted here:
#: `structure_suggestion` by tests/test_note_cohesion.py, and `user_summary`, `next_action`,
#: `notice`, `warnings` and `warnings_count` by tests/test_mutation_terminal.py.
_DEFAULT_COMPACT_ADVISORY_FIELDS = frozenset(
    {"structure_suggestion", "user_summary", "next_action", "notice", "warnings", "warnings_count"}
)


def test_post_write_guidance_names_only_fields_the_default_response_carries(
    vault: Path,
) -> None:
    """The recorded failure: guidance that promises a field the default never carries.

    `write_feedback` reaches no default MCP client — it lives under
    `response_detail='full'` — and prose that said otherwise is why the advisory
    channel was believed to work when it did not. So the rule is one-directional
    and checkable: a post-write entry may claim arrival on the default response
    only if the compact projection can actually put it there, and anything else
    must say how to reach it.
    """
    post_write = commands.op_bootstrap(vault, section="all")["authoring_contract"]["post_write"]

    for field, description in post_write.items():
        root = field.split("_handling")[0].split("_authority")[0]
        claims_default = "default" in description and "response" in description
        if claims_default:
            assert root in _DEFAULT_COMPACT_ADVISORY_FIELDS, (
                f"post_write entry {field!r} claims the default response carries "
                f"{root!r}, which the compact projection never emits"
            )
        elif root not in _DEFAULT_COMPACT_ADVISORY_FIELDS and root in {
            "write_feedback",
            "remember_suggestions",
        }:
            assert "response_detail" in description, (
                f"post_write names {field!r}, which the default compact response "
                f"does not carry, without saying how to reach it"
            )


# ==========================================================================
# emission governance
# ==========================================================================


def _outcomes_on_the_mutation_receipt(vault: Path, content: str) -> int:
    """How many egress outcomes one write leaves on the MUTATION's own receipt.

    The boundary is opened here explicitly because that is what the command layer
    does around a governed mutation, and it is the thing being protected: without
    an ambient collector there is no receipt to pollute and any assertion would be
    vacuous. Counting is the only honest measure — `_outcome_for_decision`
    projects decisions "without carrying a path/title", so counting by path would
    always find nothing.
    """
    from exomem.governance import egress as egress_module

    with egress_module.disclosure_boundary(vault, "observe_memory") as collector:
        _observe(vault, content)
        return len(collector.outcomes)


def test_a_writes_governance_receipt_gains_nothing_from_the_due_state_count(
    vault: Path, tmp_path: Path
) -> None:
    """The advisory's egress decisions belong to the advisory, not to the mutation.

    Counting IS an egress decision per page, so on a governed vault the filter
    records one outcome per due item. Joining the mutation's collector would hand
    the caller a receipt for a one-page write swollen with decisions about pages
    the write never touched.

    Measured as an A/B against an otherwise identical vault that owes nothing: the
    mutation's own receipt must be the same size either way.
    """
    from exomem.governance import egress as egress_module
    from exomem.governance.principal import owner_principal, request_scope

    quiet = tmp_path / "quiet-vault"
    (quiet / INSIGHTS).mkdir(parents=True)
    for target in (vault, quiet):
        _scratch_page(target)
        _govern(target)
    # Only this one owes anything.
    for slug in ("one", "two", "three"):
        _overdue_prediction(vault, slug, check_by=_yesterday())
    for target in (vault, quiet):
        _prime(target)
    assert egress_module.release_walk_filter(vault) is not None, (
        "an ungoverned vault takes the empty-policy fast path and records nothing, "
        "which would make this assertion vacuous"
    )

    with request_scope(owner_principal(surface="cli")):
        assert _due_state().served(vault)["total"] == 3
        assert _due_state().served(quiet) is None

        owing = _outcomes_on_the_mutation_receipt(
            vault, "Governed write on a vault that owes three."
        )
        owed_nothing = _outcomes_on_the_mutation_receipt(
            quiet, "Governed write on a quiet vault."
        )

    assert owing == owed_nothing, (
        f"the write's own receipt grew from {owed_nothing} to {owing} outcomes "
        "because the due-state count joined the mutation's collector"
    )


def test_emission_governance_is_deterministic_without_an_agent(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _prime(vault)

    block = _due_state().served(vault)
    assert _due_state().should_emit(block) is True
    assert _due_state().should_emit(block) is False
    assert _due_state().should_emit(block) is False

    changed = {"total": 2, "categories": {"prediction_window": 2}, "top": []}
    assert _due_state().should_emit(changed) is True
    assert _due_state().should_emit(changed) is False




# ==========================================================================
# the block never becomes a branch key
# ==========================================================================


def test_the_block_is_not_an_envelope_branch_key(vault: Path) -> None:
    from exomem import mutation_terminal

    assert "due_state" not in mutation_terminal._ENVELOPE_KEYS
    assert "due_state" not in mutation_terminal.STATES


def test_removing_the_bootstrap_carrier_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal for carrier 3."""
    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)
    assert "due_state" in commands.op_bootstrap(vault, section="all")

    monkeypatch.setattr(_due_state(), "served", lambda *a, **k: None)

    assert "due_state" not in commands.op_bootstrap(vault, section="all")


# ==========================================================================
# Bootstrap records a delivery only where the full block is handed over
# ==========================================================================


def _would_emit(vault: Path) -> bool:
    module = _due_state()
    return module.would_emit(module.served(vault), vault_root=vault)


def test_a_compact_bootstrap_does_not_burn_the_sessions_emission(vault: Path) -> None:
    """The core serves a counts summary, not the list. Recording that as the delivery
    made the next recall omit a list nobody had been shown, and let the first-surfaced
    ledger mark refs as shown. Real due state, nothing patched."""
    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)
    _fresh_session()
    assert _would_emit(vault)

    core = commands.op_bootstrap(vault)

    assert core["due_state"]["total"] == 1 and "top" not in core["due_state"]
    assert _would_emit(vault), "the summary is not a delivery of the block"


def test_another_section_does_not_record_a_delivery_either(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)
    _fresh_session()

    commands.op_bootstrap(vault, section="authoring")

    assert _would_emit(vault)


@pytest.mark.parametrize("carrier", ["epistemics", "all", "session"])
def test_serving_the_full_block_records_the_delivery(vault: Path, carrier: str) -> None:
    from exomem import workflow_skills

    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)
    _fresh_session()
    assert _would_emit(vault)

    if carrier == "session":
        served = commands.op_bootstrap(
            vault, profile="session", skill_contract=workflow_skills.skill_contract()
        )
    else:
        served = commands.op_bootstrap(vault, section=carrier)

    assert served["due_state"]["top"], "the full block was handed over"
    assert not _would_emit(vault)
    recall = commands.op_ask_memory(vault, query="autovacuum", limit=5)
    assert not (isinstance(recall, dict) and "due_state" in recall)


