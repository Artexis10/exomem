"""The three due-state carriers, and the gap they exist to close.

The substrate has detected due work since the epistemic loop primitives shipped, and the
conversation never heard about it: review queues run only when explicitly invoked, hooks
exist only on the CLI clients, and every hookless surface — hosted agents, claude.ai,
ChatGPT — carries instruction prose that decays. The measured result is that mid-session
accumulation is invisible until an expert user goes looking.

`test_an_overdue_check_by_reaches_no_carrier_today` is the red-first proof of exactly that,
and it is deliberately written to fail on ALL THREE carriers at once rather than to stop at
the first, because the gap is the whole delivery path and not one response shape.

Everything after it pins the fix's posture rather than its prose:

- the block rides the DEFAULT compact response (an advisory nobody sees is the bug being
  fixed), the legacy detail omits it, and no outcome key moves;
- recall carries deltas only, so a reading turn is not a second nagging surface;
- emission is governed — first-of-session or change-of-totals, never an identical repeat,
  and at most once for a batch — because an unconditional per-mutation attachment would be
  the system nagging about its own anti-nagging machinery;
- the bootstrap payload teaches how to read it, and those teaching lines survive
  `_filter_bootstrap_payload` on a reduced surface, which is where the doctrine matters
  most and where a tool-named string would silently vanish.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import commands, writer_lease
from exomem import find as find_module
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface


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
        "The autovacuum backlog clears within a week.\n",
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


def _observe(vault: Path, content: str, *, response_detail: str | None = None) -> dict:
    kwargs: dict = {
        "path": SCRATCH,
        "operation": "add",
        "category": "finding",
        "content": content,
        "tags": ["infrastructure"],
    }
    if response_detail is not None:
        kwargs["response_detail"] = response_detail
    return writer_lease.invoke_command(_command("observe_memory"), vault, **kwargs)


def _prime(vault: Path) -> None:
    """Build the projection the way a real session does — at bootstrap."""
    commands.op_bootstrap(vault)
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


# ==========================================================================
# THE GAP PROOF — this must fail red before any implementation exists
# ==========================================================================


def test_an_overdue_check_by_reaches_no_carrier_today(vault: Path) -> None:
    """RED-FIRST. One authored obligation, three delivery paths, zero deliveries.

    Collected rather than asserted one at a time: the defect is that the whole
    portability floor is missing, so the failure has to name every carrier at
    once instead of stopping at whichever one happens to be checked first.
    """
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)

    _fresh_session()
    bootstrap_payload = commands.op_bootstrap(vault)
    _fresh_session()
    mutation = _observe(vault, "Reader saturation reproduces on the replica too.")
    _fresh_session()
    recall = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)

    silent: list[str] = []
    if "due_state" not in bootstrap_payload:
        silent.append("bootstrap payload")
    if "due_state" not in mutation:
        silent.append("default compact mutating response")
    if not (isinstance(recall, dict) and "due_state" in recall):
        silent.append("recall response")

    assert silent == [], (
        "an authored, overdue `check_by` reached none of these carriers: "
        + ", ".join(silent)
    )


# ==========================================================================
# carrier 1 — the default compact mutating response
# ==========================================================================


def test_the_default_compact_response_carries_the_block(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    result = _observe(vault, "Reader saturation reproduces on the replica too.")

    assert result["status"] == "committed"
    block = result["due_state"]
    assert block["total"] == 1
    assert block["categories"] == {"prediction_window": 1}
    assert len(block["top"]) == 1
    assert block["top"][0]["ref"].startswith("exomem://review/")
    assert block["top"][0]["due_since"] == _yesterday()


def test_the_legacy_detail_omits_the_block(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    result = _observe(
        vault, "Reader saturation reproduces on the replica too.", response_detail="legacy"
    )

    assert "due_state" not in json.dumps(result)


def test_the_mutation_outcome_keys_are_byte_identical_apart_from_the_block(
    vault: Path,
) -> None:
    """The advisory is additive or it is a wire change wearing an advisory's clothes."""
    _scratch_page(vault)
    _prime(vault)
    clean = _observe(vault, "The pool saturates above four hundred readers.")

    _overdue_prediction(vault, check_by=_yesterday())
    _due_state().reconcile(vault)
    _due_state().reset_emission_state()
    carrying = _observe(vault, "The saturation point moves with the work_mem setting.")

    assert "due_state" in carrying
    assert "due_state" not in clean
    volatile = {"request_id", "receipt_id", "operation_id", "due_state"}
    assert set(carrying) - volatile == set(clean) - volatile
    for key in ("ok", "state", "status", "terminal", "mutated", "warnings_count"):
        assert carrying[key] == clean[key], key


def test_a_vault_with_nothing_due_carries_no_key_at_all(vault: Path) -> None:
    """Absent, never null and never an empty block."""
    _scratch_page(vault)
    _prime(vault)

    result = _observe(vault, "The pool saturates above four hundred readers.")

    assert "due_state" not in result


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

    assert result["ok"] is True
    assert result["status"] == "committed"
    assert result["mutated"] is True
    assert "due_state" not in result


def test_a_malformed_block_is_dropped_rather_than_widening_the_wire(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal re-validates rather than trusting what the leaf attached."""
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    monkeypatch.setattr(
        _due_state(),
        "block_for_write",
        lambda *a, **k: {"total": "lots", "categories": {}, "top": []},
    )

    result = _observe(vault, "Reader saturation reproduces on the replica too.")

    assert result["status"] == "committed"
    assert "due_state" not in result


# ==========================================================================
# carrier 2 — recall, deltas only
# ==========================================================================


def test_recall_carries_the_block_once_then_goes_quiet(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _prime(vault)

    first = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)
    second = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)

    assert isinstance(first, dict) and first["due_state"]["total"] == 1
    assert "hits" in first
    assert not (isinstance(second, dict) and "due_state" in second)


def test_recall_carries_the_block_again_after_the_projection_changes(
    vault: Path,
) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _prime(vault)

    first = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)
    assert isinstance(first, dict) and first["due_state"]["total"] == 1

    _overdue_prediction(vault, "second-call", check_by=_yesterday())
    _due_state().reconcile(vault)

    third = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)
    assert isinstance(third, dict) and third["due_state"]["total"] == 2


def test_recall_on_a_quiet_vault_keeps_its_existing_shape(vault: Path) -> None:
    """No block means no envelope: the bare-list return must not be disturbed."""
    result = commands.op_ask_memory(vault, query="autovacuum backlog", limit=5)

    assert isinstance(result, list)


# ==========================================================================
# carrier 3 — bootstrap payload and its teaching lines
# ==========================================================================


def test_the_bootstrap_payload_carries_the_block(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())

    payload = commands.op_bootstrap(vault)

    block = payload["due_state"]
    assert block["total"] == 1
    assert block["categories"] == {"prediction_window": 1}
    assert block["top"][0]["due_since"] == _yesterday()


def test_a_quiet_vault_bootstraps_without_the_key(vault: Path) -> None:
    assert "due_state" not in commands.op_bootstrap(vault)


def test_the_bootstrap_guidance_teaches_how_to_read_the_counts(vault: Path) -> None:
    post_write = commands.op_bootstrap(vault)["authoring_contract"]["post_write"]

    assert "due_state" in post_write
    assert "due_state_handling" in post_write
    assert "due_state_authority" in post_write

    described = post_write["due_state"].lower()
    assert "ordinary" in described  # counts arrive on ordinary responses

    handling = post_write["due_state_handling"].lower()
    assert "invitation" in handling and "interrupt" in handling
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
        product_commands=("bootstrap", "ask_memory"),
    )
    with active_surface(descriptor):
        payload = commands.op_bootstrap(vault)

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
#: `structure_suggestion` by tests/test_structure_promotion.py, `due_state` by
#: `test_the_default_compact_response_carries_the_block` above, and `warnings` /
#: `warnings_count` by tests/test_mutation_terminal.py.
_DEFAULT_COMPACT_ADVISORY_FIELDS = frozenset(
    {"structure_suggestion", "due_state", "warnings", "warnings_count"}
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
    post_write = commands.op_bootstrap(vault)["authoring_contract"]["post_write"]

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


def test_three_writes_with_unchanged_totals_emit_once(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    results = [_observe(vault, f"Observation number {index}.") for index in range(3)]

    carried = [index for index, row in enumerate(results) if "due_state" in row]
    assert carried == [0], f"expected only the first response to carry the block, got {carried}"


def test_a_change_in_totals_re_emits(vault: Path) -> None:
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    first = _observe(vault, "Observation one.")
    assert "due_state" in first
    quiet = _observe(vault, "Observation two.")
    assert "due_state" not in quiet

    _overdue_prediction(vault, "second-call", check_by=_yesterday())
    _due_state().reconcile(vault)

    changed = _observe(vault, "Observation three.")
    assert changed["due_state"]["total"] == 2


def test_a_forty_write_batch_emits_once_at_the_moment_the_totals_change(
    vault: Path,
) -> None:
    """The spec scenario, not a restatement of the identical-totals rule.

    Forty compiled writes while the projection's totals change ONCE. The earlier
    version of this test issued forty writes with unchanging totals, which only
    re-proved "identical consecutive totals go quiet" — it would have passed with
    no batch handling at all. Here a `check_by` comes due in the middle, so the
    interesting question is asked: exactly one response carries a block, and it is
    the one at the change.
    """
    from exomem import attention as attention_module

    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    # Start the batch owing nothing: the item is real but already triaged. The
    # totals are moved mid-batch by REOPENING it through the shipped review
    # surface rather than by writing a page, because creating a note in the middle
    # of a forty-write batch destabilises the epistemic-graph rebuild and would
    # make this a test of that instead.
    ref = _served_ref(vault)
    item = attention_module.item_by_ref(vault, ref)
    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="later")
    _fresh_session()

    results: list[dict] = []
    for index in range(40):
        if index == 20:
            commands.op_triage_memory(vault, ref=ref, action="reopen")
        results.append(_observe(vault, f"Batch observation {index}."))

    carrying = [index for index, row in enumerate(results) if "due_state" in row]
    assert len(carrying) == 1, (
        f"forty writes emitted {len(carrying)} blocks (at {carrying}); the totals "
        "changed exactly once"
    )
    assert carrying[0] >= 20, (
        "the block must arrive at or after the write that changed the totals, "
        f"not at index {carrying[0]}"
    )
    assert results[carrying[0]]["due_state"]["total"] == 1


def test_a_legacy_response_does_not_burn_the_sessions_emission(vault: Path) -> None:
    """Emission is decided where the block is DELIVERED, not where it is produced.

    `response_detail='legacy'` strips every advisory. Recording the emission at
    production meant the first legacy write consumed the session's single
    emission, and the next compact write with identical totals went quiet about
    something the caller had never been told.
    """
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    legacy = _observe(vault, "Legacy detail write.", response_detail="legacy")
    assert "due_state" not in legacy

    compact = _observe(vault, "Compact detail write.")
    assert "due_state" in compact, (
        "the legacy response carried nothing, so the emission it would have used "
        "must still be available"
    )


def test_a_terminal_validation_drop_does_not_burn_the_sessions_emission(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule, the other way a block fails to reach the caller.

    A malformed block is dropped by the terminal's validation. If the emission was
    already recorded, the caller is told nothing and stays told nothing.
    """
    from exomem import mutation_terminal

    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    real = mutation_terminal._due_state_projection
    monkeypatch.setattr(
        mutation_terminal, "_due_state_projection", lambda leaf: (None, "")
    )
    dropped = _observe(vault, "Block dropped by validation.")
    assert "due_state" not in dropped

    monkeypatch.setattr(mutation_terminal, "_due_state_projection", real)
    recovered = _observe(vault, "Block survives validation.")
    assert "due_state" in recovered, (
        "a dropped block must leave the emission unspent"
    )


def test_emission_is_scoped_to_one_command_invocation_at_the_terminal(
    vault: Path,
) -> None:
    """D9's "at most once, at its terminal" holds by construction, not by habit.

    Every mutating command reaches `project_terminal` exactly once, and that is
    the only place `due_state` enters a response — so a command that committed
    several pages internally could not emit per commit even if it wanted to. This
    pins the structural fact the guarantee rests on; today all four compiled
    writers commit once, so there is no multi-commit seam to drive.
    """
    from exomem import mutation_terminal

    source = mutation_terminal.project_terminal.__code__
    admits = [
        name for name in source.co_names if name == "_admit_due_state"
    ]
    assert admits, "the terminal no longer decides emission; D9 is unpinned"

    seen: list[str] = []
    real = mutation_terminal._admit_due_state

    def _counting(block: dict, hint: str) -> bool:
        seen.append(hint)
        return real(block, hint)

    mutation_terminal._admit_due_state = _counting
    try:
        _overdue_prediction(vault, check_by=_yesterday())
        _scratch_page(vault)
        _prime(vault)
        seen.clear()
        _observe(vault, "One command, one terminal.")
    finally:
        mutation_terminal._admit_due_state = real

    assert len(seen) == 1, (
        f"one command invocation consulted the governor {len(seen)} times"
    )


def test_emission_governance_is_per_audience(vault: Path) -> None:
    from exomem.governance.principal import RequestPrincipal, request_scope

    _overdue_prediction(vault, check_by=_yesterday())
    _prime(vault)

    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        first = commands.op_ask_memory(vault, query="autovacuum", limit=5)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        second = commands.op_ask_memory(vault, query="autovacuum", limit=5)
    with request_scope(RequestPrincipal(audience_id="other", surface="mcp")):
        other = commands.op_ask_memory(vault, query="autovacuum", limit=5)

    assert isinstance(first, dict) and "due_state" in first
    assert not (isinstance(second, dict) and "due_state" in second)
    assert isinstance(other, dict) and "due_state" in other, (
        "a second audience has its own first-of-session emission"
    )


def test_emission_governance_is_per_vault_through_the_shipped_recall(
    vault: Path, tmp_path: Path
) -> None:
    """One process can serve two vaults; being told about one must not silence the other.

    Driven through `op_ask_memory` rather than `due_state.should_emit`, because the
    defect this pins is a MISSING ARGUMENT at the call site. The module function
    keys correctly when it is handed a vault, so a test that hands it one directly
    is blind to a carrier that forgets to — which is exactly how the recall carrier
    shipped keyless and green.

    The digest is over totals, so two vaults that each owe one prediction produce
    the SAME digest. Keyed by (session, audience) alone, the second vault goes quiet
    about an item nobody has been told about — a wrong answer, not a quiet one.
    """
    second = tmp_path / "second-vault"
    (second / INSIGHTS).mkdir(parents=True)
    _overdue_prediction(vault, check_by=_yesterday())
    _overdue_prediction(second, check_by=_yesterday())

    assert _due_state().served(vault) == _due_state().served(second), (
        "identical totals, so the digests coincide"
    )

    first = commands.op_ask_memory(vault, query="autovacuum", limit=5)
    other = commands.op_ask_memory(second, query="autovacuum", limit=5)
    repeat = commands.op_ask_memory(vault, query="autovacuum", limit=5)

    assert isinstance(first, dict) and "due_state" in first
    assert isinstance(other, dict) and "due_state" in other, (
        "the second vault has its own first-of-session emission"
    )
    assert not (isinstance(repeat, dict) and "due_state" in repeat), (
        "the first vault's identical repeat still goes quiet"
    )


def test_a_recall_after_a_write_is_quiet_when_the_totals_have_not_moved(
    vault: Path,
) -> None:
    """The write and the recall have to agree on the key, or the session hears it twice.

    `mutation_terminal._admit_due_state` passes the vault. The recall carrier did
    not, so a write recorded under `(session, audience, '/vault')` while the very
    next recall asked about `(session, audience, '')` — a miss by construction.
    Identical totals delivered twice in one session is what
    `specs/command-surface/spec.md:26` forbids.
    """
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    mutation = _observe(vault, "Reader saturation reproduces on the replica too.")
    assert "due_state" in mutation, "the write is this session's first delivery"
    delivered = mutation["due_state"]

    recall = commands.op_ask_memory(vault, query="autovacuum", limit=5)

    assert _due_state().served(vault, purpose="recall") == delivered, (
        "the totals have not moved, so the question is only about the key"
    )
    assert not (isinstance(recall, dict) and "due_state" in recall), (
        "the recall repeats totals the write already delivered"
    )


def test_a_recall_after_bootstrap_is_quiet_when_the_totals_have_not_moved(
    vault: Path,
) -> None:
    """Bootstrap attaches unconditionally, so it must also RECORD the delivery.

    The contract makes bootstrap's attachment unconditional — a session that starts
    on a reduced surface has no other way to hear about it. That is a delivery, and
    a delivery the governor never learned about let the first recall of the session
    repeat the identical block.
    """
    _overdue_prediction(vault, check_by=_yesterday())
    _fresh_session()

    payload = commands.op_bootstrap(vault)
    recall = commands.op_ask_memory(vault, query="autovacuum", limit=5)

    assert "due_state" in payload, "bootstrap always carries it"
    assert not (isinstance(recall, dict) and "due_state" in recall), (
        "bootstrap DELIVERED this block, so the first recall with the same totals "
        "is a repeat"
    )


def test_a_write_after_bootstrap_carries_the_block_when_the_totals_move(
    vault: Path,
) -> None:
    """Recording bootstrap's delivery must silence repeats, not the next real change.

    The inverse of the test above, and the reason `mark_emitted` is keyed on a
    digest rather than on a flag: a change of totals is always news.
    """
    from exomem import attention as attention_module

    _overdue_prediction(vault, "backlog", check_by=_yesterday())
    _overdue_prediction(vault, "replica-lag", check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    rows = _due_state().served_entries(vault)
    assert len(rows) == 2, f"expected two due items, got {len(rows)}"
    parked = rows[0]["ref"]
    item = attention_module.item_by_ref(vault, parked)
    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="later")
    _fresh_session()

    payload = commands.op_bootstrap(vault)
    assert payload["due_state"]["total"] == 1

    commands.op_triage_memory(vault, ref=parked, action="reopen")
    mutation = _observe(vault, "Replica lag reproduces under the same load.")

    assert "due_state" in mutation, (
        "bootstrap recorded total=1; this write is total=2, which is news"
    )
    assert mutation["due_state"]["total"] == 2


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


def test_removing_emission_governance_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal: with the governor always saying yes, the quiet tests break.

    The mutating carrier decides at DELIVERY, so `would_emit` is the mechanism to
    remove here — `should_emit` is the decide-and-record convenience the read
    surfaces use, and patching it would leave the write path governed and this
    test green for the wrong reason.
    """
    _overdue_prediction(vault, check_by=_yesterday())
    _scratch_page(vault)
    _prime(vault)

    monkeypatch.setattr(_due_state(), "would_emit", lambda *a, **k: True)
    results = [_observe(vault, f"Observation number {index}.") for index in range(3)]

    assert sum("due_state" in row for row in results) == 3, (
        "with the governor removed every response must carry the block — otherwise "
        "the quiet assertions above never proved it was load-bearing"
    )


# ==========================================================================
# the block never becomes a branch key
# ==========================================================================


def test_the_block_is_not_an_envelope_branch_key(vault: Path) -> None:
    from exomem import mutation_terminal

    assert "due_state" not in mutation_terminal._ENVELOPE_KEYS
    assert "due_state" not in mutation_terminal.STATES


def test_removing_the_recall_carrier_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal for carrier 2, so the recall assertions above are load-bearing."""
    _overdue_prediction(vault, check_by=_yesterday())
    _prime(vault)

    monkeypatch.setattr(commands, "_with_due_state", lambda vault_root, result, **k: result)

    assert isinstance(commands.op_ask_memory(vault, query="autovacuum", limit=5), list)


def test_removing_the_bootstrap_carrier_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal for carrier 3."""
    _overdue_prediction(vault, check_by=_yesterday())
    assert "due_state" in commands.op_bootstrap(vault)

    monkeypatch.setattr(_due_state(), "served", lambda *a, **k: None)

    assert "due_state" not in commands.op_bootstrap(vault)
