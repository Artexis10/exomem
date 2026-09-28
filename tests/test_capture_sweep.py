"""The write-time capture-sweep advisory: its ledger, its tiers, and its hints.

Nothing here stubs the boundary predicate or the gate. The clock is injected
because a 1,800-second quiet interval is not a thing a test may wait for, and
caller identity is injected because it arrives from an MCP transport that does
not exist inside a unit test. Everything else is the real module.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import capture_sweep


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A cold ledger, a machine config nobody wrote, and a prominent level."""
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "no-such-exomem-config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    capture_sweep.reset_state()
    yield
    capture_sweep.reset_state()


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(capture_sweep, "_clock", fake)
    return fake


def _caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: str | None,
    transport: str | None,
    client_name: str | None = None,
) -> None:
    from exomem import command_surface

    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: scope)
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": client_name,
            "client_version": None,
            "transport": transport,
            "session_id": None,
        },
    )


HTTP_PRINCIPAL = {"scope": "principal:" + "a" * 64, "transport": "http", "client_name": "client-a"}


# --------------------------------------------------------------- the boundary


def test_a_first_ever_write_from_a_key_emits_the_quiet_interval_boundary(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)

    block = capture_sweep.block(tmp_path)

    assert block is not None
    assert block["boundary"] == "quiet-interval"
    assert block["rule"]
    assert block["consider"]


def test_a_second_write_inside_the_interval_is_silent_but_still_recorded(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    assert capture_sweep.block(tmp_path) is not None

    clock.advance(capture_sweep.QUIET_SECONDS - 1)
    assert capture_sweep.block(tmp_path) is None

    # The interval is measured from the LATEST write, not the latest advisory:
    # one second past the first write's interval is still inside the second's.
    clock.advance(2)
    assert capture_sweep.block(tmp_path) is None


def test_a_write_after_the_interval_emits_again(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    assert capture_sweep.block(tmp_path) is not None

    clock.advance(capture_sweep.QUIET_SECONDS)

    assert capture_sweep.block(tmp_path) is not None


def test_a_second_vault_is_not_silenced_by_the_first(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    assert capture_sweep.block(tmp_path / "one") is not None

    assert capture_sweep.block(tmp_path / "two") is not None


def test_the_quiet_interval_is_a_constant_with_no_environment_override(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    for name in (
        "EXOMEM_CAPTURE_SWEEP_QUIET_SECONDS",
        "EXOMEM_CAPTURE_SWEEP_DISABLE",
        "EXOMEM_CAPTURE_SWEEP",
    ):
        monkeypatch.setenv(name, "1")

    assert capture_sweep.QUIET_SECONDS == 1800
    assert capture_sweep.block(tmp_path) is not None


# ------------------------------------------------------------------ the tiers


@pytest.mark.parametrize("transport", (None, "stdio"))
def test_stdio_and_outside_a_call_key_on_the_process_lifetime(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path, transport: str | None
) -> None:
    _caller(monkeypatch, scope=None, transport=transport)

    key = capture_sweep.ledger_key(tmp_path)

    assert key is not None
    assert key[0] == capture_sweep._PROCESS_KEY
    assert capture_sweep.block(tmp_path) is not None
    assert capture_sweep.block(tmp_path) is None


@pytest.mark.parametrize("scope", ("session:abc123", None))
def test_an_http_call_without_a_stable_scope_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path, scope: str | None
) -> None:
    _caller(monkeypatch, scope=scope, transport="http", client_name="client-a")

    assert capture_sweep.ledger_key(tmp_path) is None
    assert capture_sweep.block(tmp_path) is None


def test_a_bearer_scope_is_accepted_as_a_key(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, scope="bearer:" + "b" * 64, transport="http", client_name="client-a")

    assert capture_sweep.block(tmp_path) is not None
    assert capture_sweep.block(tmp_path) is None


def test_two_clients_on_one_principal_do_not_silence_each_other(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    scope = "principal:" + "a" * 64
    _caller(monkeypatch, scope=scope, transport="http", client_name="client-a")
    assert capture_sweep.block(tmp_path) is not None

    _caller(monkeypatch, scope=scope, transport="http", client_name="client-b")
    assert capture_sweep.block(tmp_path) is not None


# -------------------------------------------------------------------- the gate


@pytest.mark.parametrize("level", ("light", "off"))
def test_a_quiet_prominence_level_never_emits(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path, level: str
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    assert capture_sweep.block(tmp_path) is None


@pytest.mark.parametrize("level", ("balanced", "maximal"))
def test_a_prominent_level_emits(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path, level: str
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    assert capture_sweep.block(tmp_path) is not None


def test_a_quiet_level_still_records_the_write(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    """The ledger is a fact about the caller, not about what reached them."""
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setenv("EXOMEM_PROMINENCE", "off")
    assert capture_sweep.block(tmp_path) is None

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    clock.advance(10)

    assert capture_sweep.block(tmp_path) is None


# ------------------------------------------------------------ written_recently


def test_written_recently_is_newest_first_and_capped(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    refs = [f"exomem://memory/0000000{index}-0000-4000-8000-000000000000" for index in range(10)]
    for ref in refs:
        capture_sweep.block(tmp_path, ref=ref)
        clock.advance(1)

    clock.advance(capture_sweep.QUIET_SECONDS)
    block = capture_sweep.block(tmp_path, ref=refs[0])

    assert block is not None
    written = block["written_recently"]
    assert len(written) == capture_sweep.MAX_WRITTEN_RECENTLY
    assert written[0] == refs[0], "the write being acknowledged is the newest"
    assert written == [refs[0], *reversed(refs[3:])][: capture_sweep.MAX_WRITTEN_RECENTLY]


def test_written_recently_is_absent_rather_than_empty(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)

    block = capture_sweep.block(tmp_path)

    assert block is not None
    assert "written_recently" not in block
    assert "unpaged_mentions" not in block


# -------------------------------------------------------------------- the hints


def _page(path: str, wikilinks: tuple[tuple[str, int], ...]) -> SimpleNamespace:
    return SimpleNamespace(path=path, frontmatter={}, body_wikilinks=wikilinks)


def _corpus(tmp_path: Path, *, titles: dict[str, tuple[str, ...]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        vault_root=tmp_path,
        pages={},
        resolver_full_paths=frozenset(),
        resolver_kb_stripped=frozenset(),
        resolver_stems={},
        resolver_titles=titles or {},
    )


def test_unresolved_wikilinks_become_unpaged_mentions(tmp_path: Path) -> None:
    page = _page(
        "Knowledge Base/Notes/Insights/client-meeting.md",
        (("Venue booking system", 4), ("Client meeting", 9)),
    )

    found = capture_sweep.hints(page, _corpus(tmp_path))

    assert found["unpaged_mentions"] == ["Venue booking system", "Client meeting"]


def test_a_resolved_wikilink_is_not_an_unpaged_mention(tmp_path: Path) -> None:
    page = _page(
        "Knowledge Base/Notes/Insights/client-meeting.md",
        (("Venue booking system", 4), ("Client meeting", 9)),
    )
    corpus = _corpus(
        tmp_path,
        titles={"client meeting": ("Knowledge Base/Entities/organization/Client meeting",)},
    )

    found = capture_sweep.hints(page, corpus)

    assert found["unpaged_mentions"] == ["Venue booking system"]


def test_unpaged_mentions_dedupe_and_cap(tmp_path: Path) -> None:
    links = tuple((f"Name {index}", index) for index in range(9))
    page = _page("Knowledge Base/Notes/Insights/a.md", links + (("Name 0", 20),))

    found = capture_sweep.hints(page, _corpus(tmp_path))

    assert found["unpaged_mentions"] == [f"Name {index}" for index in range(5)]
    assert len(found["unpaged_mentions"]) == capture_sweep.MAX_UNPAGED_MENTIONS


def test_hints_without_a_corpus_promise_nothing(tmp_path: Path) -> None:
    page = _page("Knowledge Base/Notes/Insights/a.md", (("Venue booking system", 4),))

    assert capture_sweep.hints(page, None) == {}


def test_the_block_carries_the_hints(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    page = _page("Knowledge Base/Notes/Insights/a.md", (("Venue booking system", 4),))

    block = capture_sweep.block(tmp_path, page_state=page, corpus=_corpus(tmp_path))

    assert block is not None
    assert block["unpaged_mentions"] == ["Venue booking system"]


# ---------------------------------------------------------------- the posture


def test_the_consider_list_is_presented_as_examples_not_an_enumeration(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    """An enumeration inside a capture contract reads to an agent as a closed
    boundary, which would make this carrier NARROW capture instead of widening
    it. The RULE TEXT has to say so itself, because the rule is the only
    sentence on the wire -- the `consider` key is a bare list with no prose
    around it, so an agent that reads the block and not this repository has
    nothing else telling it the list is open."""
    _caller(monkeypatch, **HTTP_PRINCIPAL)

    block = capture_sweep.block(tmp_path)

    assert block is not None
    rule = block["rule"].lower()
    assert "example" in rule, rule
    assert "only" not in rule.split()
    assert len(block["consider"]) >= 8


def test_a_broken_identity_costs_an_advisory_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    from exomem import command_surface

    def explode() -> None:
        raise RuntimeError("no transport here")

    monkeypatch.setattr(command_surface, "mcp_caller_identity", explode)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", explode)

    assert capture_sweep.block(tmp_path) is None


def test_a_broken_gate_costs_an_advisory_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    from exomem import envelope

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no envelope here")

    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setattr(envelope, "active", explode)

    assert capture_sweep.block(tmp_path) is None


def test_broken_hints_cost_the_hint_and_never_the_block(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    exploding = SimpleNamespace()

    block = capture_sweep.block(tmp_path, page_state=exploding, corpus=object())

    assert block is not None
    assert "unpaged_mentions" not in block


def test_the_ledger_is_bounded_and_evicts_the_oldest(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    for index in range(capture_sweep.LEDGER_CAP + 4):
        _caller(
            monkeypatch,
            scope=f"principal:{index:064d}",
            transport="http",
            client_name="client-a",
        )
        assert capture_sweep.block(tmp_path) is not None

    assert len(capture_sweep._LAST_WRITE) <= capture_sweep.LEDGER_CAP

    # The first key was evicted, so it is re-armed rather than remembered --
    # the stated cost of a bounded ledger, and the safe direction.
    _caller(monkeypatch, scope=f"principal:{0:064d}", transport="http", client_name="client-a")
    assert capture_sweep.block(tmp_path) is not None


def test_a_batch_scope_produces_nothing_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    """Inside a batch the seam is silent; the command's own carrier decides once."""
    from exomem import due_state

    _caller(monkeypatch, **HTTP_PRINCIPAL)

    with due_state.batch_scope(tmp_path):
        assert capture_sweep.block(tmp_path) is None
        assert capture_sweep.block(tmp_path) is None

    assert capture_sweep.block(tmp_path) is not None


def test_a_batch_command_composes_exactly_one_sweep_block(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    """Twelve governed writes are one episode boundary, not twelve."""
    from exomem import commands, due_state, writer_lease

    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setattr(writer_lease, "active_mutation_committed", lambda: True)

    with due_state.batch_scope(tmp_path):
        for _ in range(12):
            assert capture_sweep.block(tmp_path) is None

    first = commands._carrying_capture_sweep(tmp_path, {"ok": True})
    second = commands._carrying_capture_sweep(tmp_path, {"ok": True})

    assert first["capture_sweep"]["boundary"] == "quiet-interval"
    assert "capture_sweep" not in second


def test_a_batch_that_committed_nothing_carries_no_block(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    from exomem import commands, writer_lease

    _caller(monkeypatch, **HTTP_PRINCIPAL)
    monkeypatch.setattr(writer_lease, "active_mutation_committed", lambda: False)

    assert "capture_sweep" not in commands._carrying_capture_sweep(tmp_path, {"ok": True})


# ------------------------------------------------------------- contract bytes


#: `"for example"` -> `"e.g."` 2026-09-19 (`capture-identities-at-write-time`
#: task 3.1): `_EPISODE_SWEEP_CAPTURE` was tightened to pay for the bootstrap
#: link instruction; the em-dash "for example" became a plain-ASCII "e.g.",
#: which costs less served JSON and instructs exactly the same thing.
CLAUSE_MARKERS = ("episode", "e.g.", "written recently", "silent")


@pytest.mark.parametrize("level", ("balanced", "maximal"))
def test_the_prominent_capture_contracts_name_the_bounded_pass(level: str) -> None:
    from exomem import prominence

    capture = prominence.CONTRACTS[level].capture.lower()

    for marker in CLAUSE_MARKERS:
        assert marker in capture, marker
    assert "only the following" not in capture


@pytest.mark.parametrize("level", ("light", "off"))
def test_the_quiet_capture_contracts_are_untouched(level: str) -> None:
    from exomem import prominence

    capture = prominence.CONTRACTS[level].capture.lower()

    assert "episode" not in capture
    assert "sweep" not in capture


def test_bootstrap_teaches_how_to_read_the_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import commands

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    (tmp_path / "Knowledge Base").mkdir()

    payload = commands.op_bootstrap(tmp_path, profile="compact")
    handling = payload["authoring_contract"]["post_write"]["capture_sweep_handling"]

    assert "example" in handling.lower()
    assert "capture_sweep_handling" in commands._SESSION_POST_WRITE_KEYS


def test_the_session_projection_carries_the_handling_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import commands

    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    (tmp_path / "Knowledge Base").mkdir()

    session = commands.op_bootstrap(tmp_path, profile="session")

    assert "capture_sweep_handling" in session["authoring_contract"]["post_write"]


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_shipped_scaffold_carries_the_same_pass() -> None:
    reference = (
        REPO_ROOT
        / "src"
        / "exomem"
        / "_scaffold"
        / "_Schema"
        / "references"
        / "engagement.md"
    )

    prose = reference.read_text(encoding="utf-8").lower()

    assert "episode" in prose
    assert "for example" in prose


def test_the_copyable_instruction_blocks_have_no_room_for_the_clause() -> None:
    """Why the two paste blocks do NOT carry the clause, recorded as a measurement.

    `test_personal_baseline_contract.py` caps every copyable instruction block at
    1,500 bytes, because that is the custom-instruction field they are pasted
    into. The prominent blocks sit at 1,484 and 1,495 — 16 and 5 bytes of
    headroom — so no wording of this clause fits, and the only way to make one
    fit is to shorten a capture class that is already there. That is precisely
    the move a KB failure note records as having silently dropped the
    Planning/Records transition rule past seven reviews, so it was not made.

    The doctrine still reaches every hookless client: these very blocks open by
    telling the client to call `bootstrap(profile="compact")` and follow it, and
    the compact payload carries both the clause and `capture_sweep_handling`.

    This test is here so the next person to look does not re-derive the
    arithmetic, and so it turns red the day a trim frees the room.
    """
    doc = (REPO_ROOT / "docs" / "prominence.md").read_text(encoding="utf-8")
    sizes = {}
    for name, heading in (
        ("maximal", "### Maximal — recommended for web and hosted"),
        ("balanced", "### Balanced — the default where hooks exist"),
    ):
        fence = doc.index("```", doc.index(heading))
        block = doc[fence + 3 : doc.index("```", fence + 3)]
        sizes[name] = len(block.rstrip().encode("utf-8"))
        assert "Never save transcripts." in block, name

    assert sizes == {"maximal": 1484, "balanced": 1495}, sizes
    assert all(1500 - size < 100 for size in sizes.values()), sizes


def test_the_compact_bootstrap_budget_is_not_raised() -> None:
    from tests.test_bootstrap_compact_budget import COMPACT_BYTE_CEILING

    assert COMPACT_BYTE_CEILING == 63_300


# ----------------------------------------------------- registry exclusion (D6)


def test_a_registry_alias_is_not_an_unpaged_mention(tmp_path: Path) -> None:
    """A name the entity registry answers to HAS a page, under another spelling.

    This is the branch that stops the hint firing on every alias in the vault:
    the wikilink `[[Client Org]]` resolves against no file, but the registry
    entity titled `Client Organisation` lists it as an alias, so the page it
    names exists and the mention is not unpaged. The genuinely unpaged link on
    the same page must still be listed, or the exclusion is just suppression.
    """
    entity = SimpleNamespace(
        path="Knowledge Base/Entities/Organizations/Client Organisation.md",
        frontmatter={
            "type": "entity",
            "entity_type": "organization",
            "status": "active",
            "title": "Client Organisation",
            "aliases": ["Client Org"],
        },
        body_wikilinks=(),
    )
    corpus = SimpleNamespace(
        vault_root=tmp_path,
        pages={entity.path: entity},
        resolver_full_paths=frozenset(),
        resolver_kb_stripped=frozenset(),
        resolver_stems={},
        resolver_titles={},
    )
    page = _page(
        "Knowledge Base/Notes/Insights/client-meeting.md",
        (("Client Org", 4), ("Venue booking system", 5)),
    )

    found = capture_sweep.hints(page, corpus)

    assert found["unpaged_mentions"] == ["Venue booking system"]


def test_an_unmatched_name_survives_the_registry_filter(tmp_path: Path) -> None:
    """The control: the same corpus, a name the registry does not answer to."""
    entity = SimpleNamespace(
        path="Knowledge Base/Entities/Organizations/Client Organisation.md",
        frontmatter={
            "type": "entity",
            "entity_type": "organization",
            "status": "active",
            "title": "Client Organisation",
            "aliases": ["Client Org"],
        },
        body_wikilinks=(),
    )
    corpus = SimpleNamespace(
        vault_root=tmp_path,
        pages={entity.path: entity},
        resolver_full_paths=frozenset(),
        resolver_kb_stripped=frozenset(),
        resolver_stems={},
        resolver_titles={},
    )
    page = _page("Knowledge Base/Notes/Insights/a.md", (("Client Organisations Ltd", 4),))

    found = capture_sweep.hints(page, corpus)

    assert found["unpaged_mentions"] == ["Client Organisations Ltd"]


# ------------------------------------------------------------- atomic boundary


def test_two_concurrent_writes_on_one_key_produce_exactly_one_boundary(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    """`boundary` then `record_write` is two lock acquisitions and a race.

    Two threads that both read the ledger before either writes it both see a
    quiet interval and both emit, which is the one duplicate this carrier's
    whole governance exists to prevent. `check_and_record` closes it by deciding
    and recording under a single acquisition.
    """
    import threading

    _caller(monkeypatch, **HTTP_PRINCIPAL)
    key = capture_sweep.ledger_key(tmp_path)
    assert key is not None

    start = threading.Barrier(8)
    results: list[str | None] = []
    lock = threading.Lock()

    def racer() -> None:
        start.wait()
        found = capture_sweep.check_and_record(key, clock.now)
        with lock:
            results.append(found)

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8, results
    assert results.count("quiet-interval") == 1, results


def test_check_and_record_returns_the_boundary_and_records_the_write(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _caller(monkeypatch, **HTTP_PRINCIPAL)
    key = capture_sweep.ledger_key(tmp_path)
    assert key is not None
    ref = "exomem://memory/33333333-3333-4333-8333-333333333333"

    assert capture_sweep.check_and_record(key, 1_000.0, ref=ref) == "quiet-interval"
    assert capture_sweep.recent_writes(key) == [ref]
    assert capture_sweep.check_and_record(key, 1_000.0 + capture_sweep.QUIET_SECONDS - 1) is None
    assert capture_sweep.check_and_record(key, 1_000.0 + capture_sweep.QUIET_SECONDS * 2) == (
        "quiet-interval"
    )
    assert capture_sweep.check_and_record(None, 1_000.0) is None


# --------------------------------------------- every batch scope has a carrier


def test_every_batch_scope_is_paired_with_the_shared_batch_carrier() -> None:
    """A batch scope without the carrier swallows its episode silently.

    Inside `due_state.batch_scope` the page and structured seams deliberately
    neither produce nor record, so the command's own terminal is the only place
    its sweep can be decided. A command that opens the scope and never calls
    `_carrying_batch_advisories` therefore reports nothing about a batch that
    genuinely happened -- fail-closed, but invisible. The two sets were 10 and 10
    when this shipped, and nothing else pins them together.
    """
    import ast
    import inspect

    from exomem import commands

    tree = ast.parse(inspect.getsource(commands))

    def _names(node: ast.AST) -> set[str]:
        """Attribute and bare names this function actually CALLS.

        Deliberately not a substring scan over `ast.dump`: that also matches the
        docstrings which explain the pairing, and a test whose subject is named
        in its own prose is a test that passes on prose.
        """
        found: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Attribute):
                found.add(func.attr)
            elif isinstance(func, ast.Name):
                found.add(func.id)
        return found

    opens_scope: set[str] = set()
    carries: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = _names(node)
        if "batch_scope" in called:
            opens_scope.add(node.name)
        if "_carrying_batch_advisories" in called:
            carries.add(node.name)
    assert opens_scope, "no command opens a due-state batch scope; the pairing moved"
    assert opens_scope <= carries, (
        "these commands open a batch scope without the shared batch carrier, so "
        f"their episode boundary is silently swallowed: {sorted(opens_scope - carries)}"
    )
