"""Unit coverage for the bounded public-MCP common-subset diagnostic."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "durable_closure_common.py"
spec = importlib.util.spec_from_file_location("durable_closure_common", MODULE_PATH)
common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = common
spec.loader.exec_module(common)


def test_semantic_setup_retries_only_warming_and_requires_a_reviewed_draft() -> None:
    calls = []

    class Client:
        async def call(self, tool, arguments, **kwargs):
            calls.append((tool, arguments, kwargs))
            if len(calls) == 1:
                return {"success": False, "error": {"code": "MUTATION_WARMING"}}
            return {"draft_id": "ready", "draft_hash": "hash"}

    asyncio.run(common._await_exomem_mutation(Client(), timeout=1))

    assert len(calls) == 2
    assert all(tool == "remember" and args["validate_only"] for tool, args, _ in calls)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": {"code": "MUTATION_WARMING"}},
        {"success": False, "error": {"code": "VALIDATION_FAILED"}},
        {"success": True},
    ],
)
def test_semantic_setup_rejects_timeout_refusal_and_missing_draft(payload) -> None:
    class Client:
        async def call(self, *args, **kwargs):
            return payload

    with pytest.raises(common.AdapterFault, match="initial public mutation"):
        asyncio.run(common._await_exomem_mutation(Client(), timeout=0))


def test_empty_disposable_roots_reject_nonempty_and_overlapping_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    state.mkdir()
    vault.mkdir()
    (state / "old.json").write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        common.prepare_roots(state=state, vault=vault)
    with pytest.raises(ValueError, match="distinct"):
        common.prepare_roots(state=tmp_path / "same", vault=tmp_path / "same")


def test_deterministic_fixture_has_required_existing_pages_and_digest(tmp_path: Path) -> None:
    first = common.materialize_fixture(tmp_path / "one", pages=7)
    second = common.materialize_fixture(tmp_path / "two", pages=7)

    assert first["digest"] == second["digest"]
    assert first["page_count"] == 7
    assert {"tracker", "background", "stale_link"} <= set(first["named_pages"])
    assert all(entry["bytes"] > 0 and len(entry["sha256"]) == 64 for entry in first["pages"])


def test_fixture_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="4"):
        common.fixture_pages(3)
    with pytest.raises(ValueError, match="8000"):
        common.fixture_pages(8001)


def test_malformed_or_failed_mcp_results_invalidate_instead_of_becoming_product_loss() -> None:
    with pytest.raises(common.AdapterFault, match="malformed"):
        common.decode_result({"structured_content": {"result": "not-an-object"}})
    with pytest.raises(common.AdapterFault, match="failed"):
        common.decode_result({"structured_content": {"result": {"success": False}}})


def test_mcp_error_envelope_overrides_a_parseable_structured_payload() -> None:
    class FailedResult:
        is_error = True
        structured_content = {"message": "tool failed"}
        content = []

    payload = common._decode_payload(FailedResult())

    assert common.result_classification(payload) == "refused"
    with pytest.raises(common.AdapterFault, match="failed"):
        common.decode_result(FailedResult())


def test_exact_marker_and_search_verification_require_every_unique_marker() -> None:
    marker = "common-subset-marker-123"
    payload = {"content": f"# Result\n\n{marker}\n"}
    assert common.exact_marker_present(payload, marker) is True
    assert common.exact_marker_present({"content": "missing"}, marker) is False

    hits = {"results": [{"content": marker}, {"title": "other"}]}
    assert common.search_marker_present(hits, marker) is True
    assert common.search_marker_present({"results": [{"title": marker}]}, marker) is False
    assert common.search_marker_present({"results": []}, marker) is False


def test_common_markdown_payload_is_byte_identical_for_both_adapters() -> None:
    payload = common.common_markdown_payload("common-subset-marker")

    assert "## Observations" in payload["chapter"]
    assert "## Observations" in payload["capture"]
    assert payload["tracker_append"].endswith("common-subset-marker-tracker\n")
    assert common.common_markdown_payload("common-subset-marker") == payload


def test_search_verification_never_treats_query_echo_as_a_hit() -> None:
    marker = "common-subset-marker-123"

    assert common.search_marker_present({"query": marker, "results": []}, marker) is False
    assert common.search_marker_present({"query": marker, "hits": []}, marker) is False
    assert common.search_marker_present({"results": [{"query": marker}]}, marker) is False
    assert (
        common.search_marker_present({"hits": [{"diagnostic": f"no match for {marker}"}]}, marker)
        is False
    )
    assert common.search_marker_present({"hits": [{"excerpt": marker}]}, marker) is True
    assert common.search_marker_present({"result": [{"excerpt": marker}]}, marker) is True
    assert common._search_proof({"result": [{"excerpt": marker}]}) == {
        "classification": "ok",
        "container": "result",
        "hit_count": 1,
        "top_level_keys": ["result"],
    }


def test_exact_read_requires_each_expected_suffix_and_stale_replacement() -> None:
    marker = "common-subset-marker"
    assert common.read_body_has_markers(
        {"content": f"---\ntitle: x\n---\n# Result\n{marker}-chapter\n"},
        [f"{marker}-chapter"],
    )
    assert not common.read_body_has_markers(
        {"content": f"{marker}-chapter\n"}, [f"{marker}-capture"]
    )
    assert common.stale_replacement_verified(
        {"content": "Archived runbook retired\n"},
        old="[[Archived Runbook]]",
        replacement="Archived runbook retired",
    )
    assert not common.stale_replacement_verified(
        {"content": "[[Archived Runbook]]\n"},
        old="[[Archived Runbook]]",
        replacement="Archived runbook retired",
    )
    assert common.read_body_equals(
        {"content": "---\ntitle: x\n---\n# Result\nbody\n"}, "# Result\nbody\n"
    )
    assert not common.read_body_equals({"content": "# Result\nbody\n"}, "# Result\nother\n")


def test_stale_replacement_body_has_the_explicit_common_terminal_newline(tmp_path: Path) -> None:
    fixture = common.materialize_fixture(tmp_path / "fixture", pages=4)
    markdown = common.common_markdown_payload("common-subset-marker")

    expected = common.stale_replacement_body(fixture, markdown)

    assert expected.endswith("-->\n")
    assert markdown["stale_replacement"] in expected
    assert "[[Archived Runbook]]" not in expected


def test_refusal_classification_covers_error_envelopes_and_terminal_states() -> None:
    assert common.result_classification({"success": False, "error": {"code": "NOPE"}}) == "refused"
    assert common.result_classification({"error": {"code": "NOPE"}}) == "refused"
    assert common.result_classification({"outcome": "rejected"}) == "refused"
    assert common.result_classification({"status": "failed"}) == "refused"
    assert common.result_classification({"ok": False}) == "refused"
    assert common.result_classification({"hits": []}) == "ok"


def test_equivalent_plan_uses_only_shared_markdown_operations() -> None:
    plan = common.common_plan("basic_memory")
    assert plan == common.common_plan("exomem")
    assert [step["operation"] for step in plan] == [
        "create_completed_chapter",
        "edit_tracker",
        "correct_stale_link",
        "capture_independent_note",
        "exact_read_changed_pages",
        "search_unique_markers",
    ]
    assert all(step["surface"] == "public_mcp" for step in plan)


def test_phase_clock_keeps_setup_teardown_and_timed_work_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((10.0, 11.0, 13.5, 15.0))
    monkeypatch.setattr(common.time, "perf_counter", lambda: next(ticks))
    clock = common.PhaseClock()
    clock.start_timing()
    clock.finish_closure()
    clock.finish_teardown()

    assert clock.report() == {
        "pre_timing_ms": 1000.0,
        "wall_to_verified_closure_ms": 2500.0,
        "teardown_ms": 1500.0,
    }


def test_phase_clock_reports_partial_startup_failure_without_raising() -> None:
    clock = common.PhaseClock()
    clock.finish_teardown()

    assert clock.report() == {
        "pre_timing_ms": None,
        "wall_to_verified_closure_ms": None,
        "teardown_ms": None,
    }


def test_exomem_runtime_storage_resolves_inside_disposable_state(tmp_path: Path) -> None:
    from exomem.writer_lease import LeaseConfig

    state = tmp_path / "state"
    environment = common.exomem_environment(state, tmp_path / "vault")

    assert LeaseConfig.from_env(environment).state_dir.is_relative_to(state)
    for key in ("EXOMEM_LOG_DIR", "EXOMEM_CALL_LEDGER_DIR"):
        assert Path(environment[key]).is_relative_to(state)


def test_basic_memory_environment_is_fresh_and_only_has_basic_memory_prefixes(
    tmp_path: Path,
) -> None:
    env = common.basic_memory_environment(tmp_path / "state", tmp_path / "vault")

    assert env["BASIC_MEMORY_HOME"] == str(tmp_path / "state" / "home")
    assert env["BASIC_MEMORY_CONFIG_DIR"] == str(tmp_path / "state" / "config")
    assert env["BASIC_MEMORY_FORCE_LOCAL"] == "true"
    assert env["BASIC_MEMORY_EXPLICIT_ROUTING"] == "true"
    assert env["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] == "false"
    assert env["BASIC_MEMORY_AUTO_UPDATE"] == "false"
    assert env["BASIC_MEMORY_NO_PROMOS"] == "1"
    assert not any(key.startswith("EXOMEM_") for key in env)


def test_percentiles_and_call_counts_only_include_public_calls() -> None:
    calls = [
        {"tool": "write", "elapsed_ms": 1.0, "ack": True},
        {"tool": "read", "elapsed_ms": 3.0, "ack": False},
        {"tool": "edit", "elapsed_ms": 2.0, "ack": True},
    ]
    assert common.call_measurements(calls) == {
        "public_call_count": 3,
        "ack_p50_ms": 1.0,
        "ack_p95_ms": 2.0,
        "shared_server_ms": None,
        "shared_server_reason": "not exposed by the public MCP protocol",
        "connector_ms": None,
        "connector_reason": "not exposed by the public MCP protocol",
    }


def test_failed_ack_is_excluded_from_ack_percentiles() -> None:
    calls = [
        {"tool": "write", "elapsed_ms": 1.0, "ack": True, "classification": "ok"},
        {"tool": "edit", "elapsed_ms": 99.0, "ack": True, "classification": "refused"},
    ]
    assert common.call_measurements(calls) == {
        "public_call_count": 2,
        "ack_p50_ms": 1.0,
        "ack_p95_ms": 1.0,
        "shared_server_ms": None,
        "shared_server_reason": "not exposed by the public MCP protocol",
        "connector_ms": None,
        "connector_reason": "not exposed by the public MCP protocol",
    }


def test_fixture_pages_have_deterministic_kilobyte_scale_variation() -> None:
    sizes = [len(body.encode()) for _, body in common.fixture_pages(12)]
    assert max(sizes) - min(sizes) >= 1_000


def test_main_shares_an_explicit_marker_between_both_product_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    async def fake_run_product(**kwargs: object) -> dict[str, object]:
        seen.append(str(kwargs["marker"]))
        return {"product": kwargs["product"], "status": "pass"}

    monkeypatch.setattr(common, "run_product", fake_run_product)

    assert (
        common.main(
            [
                "--product",
                "both",
                "--state",
                str(tmp_path / "state"),
                "--vault",
                str(tmp_path / "vault"),
                "--marker",
                "paired-marker",
                "--basic-memory-executable",
                "/bin/true",
            ]
        )
        == 0
    )
    assert seen == ["paired-marker", "paired-marker"]
    assert [row["product"] for row in json.loads(capsys.readouterr().out)["rows"]] == [
        "exomem",
        "basic_memory",
    ]


def _basic_memory_snapshot(
    state: Path,
    fixture: dict[str, object],
    *,
    entities: list[tuple[int, str, int]] | None = None,
    search_rows: list[tuple[int, str, int]] | None = None,
    project_path: Path | None = None,
    virtual_search: bool = True,
) -> None:
    db = state / "config" / "memory.db"
    db.parent.mkdir(parents=True)
    expected = [str(page["path"]) for page in fixture["pages"]]  # type: ignore[index]
    entities = (
        entities
        if entities is not None
        else [(number, path, 1) for number, path in enumerate(expected, start=1)]
    )
    search_rows = search_rows if search_rows is not None else list(entities)
    with sqlite3.connect(db) as conn:
        search_table = (
            "CREATE VIRTUAL TABLE search_index USING fts5("
            "id UNINDEXED, file_path UNINDEXED, type UNINDEXED, project_id UNINDEXED, "
            "entity_id UNINDEXED);"
            if virtual_search
            else "CREATE TABLE search_index(id INTEGER, file_path TEXT, type TEXT, "
            "project_id INTEGER, entity_id INTEGER);"
        )
        conn.executescript(
            "CREATE TABLE project(id INTEGER, name TEXT, path TEXT);"
            "CREATE TABLE entity(id INTEGER, file_path TEXT, project_id INTEGER);"
            "CREATE TABLE alembic_version(version_num TEXT);" + search_table
        )
        conn.execute(
            "INSERT INTO alembic_version VALUES (?)", (common.BASIC_MEMORY_ALEMBIC_VERSION,)
        )
        conn.execute(
            "INSERT INTO project VALUES (1, 'main', ?)",
            (str(project_path or state / "home"),),
        )
        conn.executemany("INSERT INTO entity VALUES (?, ?, ?)", entities)
        conn.executemany(
            "INSERT INTO search_index(id, file_path, type, project_id, entity_id) "
            "VALUES (?, ?, 'entity', ?, ?)",
            [
                (entity_id, path, project_id, entity_id)
                for entity_id, path, project_id in search_rows
            ],
        )


def _exomem_snapshot(
    state: Path,
    vault: Path,
    fixture: dict[str, object],
    *,
    paths: list[str] | None = None,
    fts_rowids: list[int] | None = None,
    admitted: bool = True,
    virtual_fts: bool = True,
) -> None:
    from exomem.state_paths import vault_state_key

    db = state / "state" / vault_state_key(vault) / ".lexical.sqlite"
    db.parent.mkdir(parents=True)
    expected = [
        "Knowledge Base/Reference/" + str(page["path"])
        for page in fixture["pages"]  # type: ignore[index]
    ]
    paths = paths if paths is not None else expected
    fts_rowids = fts_rowids if fts_rowids is not None else list(range(1, len(paths) + 1))
    with sqlite3.connect(db) as conn:
        fts_table = (
            "CREATE VIRTUAL TABLE fts USING fts5(stemmed);"
            if virtual_fts
            else "CREATE TABLE fts(stemmed TEXT);"
        )
        conn.executescript(
            "CREATE TABLE pages(path TEXT, in_kb INTEGER, in_vault INTEGER);"
            "CREATE TABLE meta(key TEXT, value TEXT);" + fts_table
        )
        conn.execute(
            "INSERT INTO meta VALUES ('schema_version', ?)",
            (str(common.EXOMEM_LEXICAL_SCHEMA_VERSION),),
        )
        conn.executemany(
            "INSERT INTO pages(rowid, path, in_kb, in_vault) VALUES (?, ?, ?, ?)",
            [
                (number, path, int(admitted), int(admitted))
                for number, path in enumerate(paths, start=1)
            ],
        )
        conn.executemany(
            "INSERT INTO fts(rowid, stemmed) VALUES (?, 'indexed')",
            [(rowid,) for rowid in fts_rowids],
        )


def test_indexed_fixture_rejects_a_sentinel_sized_basic_memory_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fixture = common.materialize_fixture(state / "home", pages=4)
    _basic_memory_snapshot(state, fixture, entities=[(1, "background.md", 1)])

    proof = common.inspect_indexed_fixture(
        product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
    )

    assert proof["ready"] is False
    assert proof["observed_path_count"] == 1
    assert proof["expected_path_count"] == 4


def test_indexed_fixture_rejects_equal_counts_with_unexpected_and_duplicate_identities(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    fixture = common.materialize_fixture(state / "home", pages=4)
    paths = [str(page["path"]) for page in fixture["pages"]]
    _basic_memory_snapshot(
        state,
        fixture,
        entities=[(1, paths[0], 1), (2, paths[1], 1), (3, paths[2], 1), (4, "unexpected.md", 1)],
        search_rows=[(1, paths[0], 1), (2, paths[1], 1), (3, paths[2], 1), (3, paths[2], 1)],
    )

    proof = common.inspect_indexed_fixture(
        product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
    )

    assert proof["ready"] is False
    assert proof["proof_method"] == "entity/search_index identity multiset join"


def test_indexed_fixture_requires_search_join_and_exact_basic_memory_project(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    fixture = common.materialize_fixture(state / "home", pages=4)
    paths = [str(page["path"]) for page in fixture["pages"]]
    _basic_memory_snapshot(
        state,
        fixture,
        entities=[(number, path, 2) for number, path in enumerate(paths, start=1)],
        search_rows=[],
        project_path=state / "other-project",
    )

    proof = common.inspect_indexed_fixture(
        product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
    )

    assert proof["ready"] is False
    assert "project" in str(proof["reason"])


def test_indexed_fixture_accepts_full_current_schema_snapshots_for_both_products(
    tmp_path: Path,
) -> None:
    basic_state = tmp_path / "basic"
    basic_fixture = common.materialize_fixture(basic_state / "home", pages=4)
    _basic_memory_snapshot(basic_state, basic_fixture)
    exomem_state = tmp_path / "exomem"
    exomem_vault = tmp_path / "vault"
    exomem_fixture = common.materialize_fixture(
        exomem_vault / "Knowledge Base" / "Reference", pages=4
    )
    _exomem_snapshot(exomem_state, exomem_vault, exomem_fixture)

    basic = common.inspect_indexed_fixture(
        product="basic_memory", state=basic_state, vault=tmp_path / "unused", fixture=basic_fixture
    )
    exomem = common.inspect_indexed_fixture(
        product="exomem", state=exomem_state, vault=exomem_vault, fixture=exomem_fixture
    )

    assert basic["ready"] is True
    assert exomem["ready"] is True
    assert exomem["identity_join_proof"] == "pages.rowid = fts.rowid"


def test_indexed_fixture_rejects_missing_search_and_ineligible_exomem_page(tmp_path: Path) -> None:
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    fixture = common.materialize_fixture(vault / "Knowledge Base" / "Reference", pages=4)
    _exomem_snapshot(state, vault, fixture, fts_rowids=[1, 2, 3], admitted=False)

    proof = common.inspect_indexed_fixture(
        product="exomem", state=state, vault=vault, fixture=fixture
    )

    assert proof["ready"] is False
    assert proof["observed_path_count"] == 4


@pytest.mark.parametrize("product", ["basic_memory", "exomem"])
def test_indexed_fixture_rejects_plain_tables_that_impersonate_search(
    product: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    if product == "basic_memory":
        fixture = common.materialize_fixture(state / "home", pages=4)
        _basic_memory_snapshot(state, fixture, virtual_search=False)
    else:
        fixture = common.materialize_fixture(vault / "Knowledge Base" / "Reference", pages=4)
        _exomem_snapshot(state, vault, fixture, virtual_fts=False)

    with pytest.raises(common.AdapterFault, match="FTS5 virtual table"):
        common.inspect_indexed_fixture(product=product, state=state, vault=vault, fixture=fixture)


def test_indexed_fixture_rejects_an_exomem_store_for_a_different_vault_key(tmp_path: Path) -> None:
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    fixture = common.materialize_fixture(vault / "Knowledge Base" / "Reference", pages=4)
    other_vault = tmp_path / "definitely-not-the-derived-vault-key"
    _exomem_snapshot(state, other_vault, fixture)

    proof = common.inspect_indexed_fixture(
        product="exomem", state=state, vault=vault, fixture=fixture
    )

    assert proof["ready"] is False
    assert proof["store"] is None


def test_indexed_fixture_has_no_incomplete_reason_when_ready(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fixture = common.materialize_fixture(state / "home", pages=4)
    _basic_memory_snapshot(state, fixture)

    proof = common.inspect_indexed_fixture(
        product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
    )

    assert proof["ready"] is True
    assert proof["reason"] is None


def test_indexed_fixture_observes_one_coherent_snapshot_while_index_mutates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    fixture = common.materialize_fixture(state / "home", pages=4)
    _basic_memory_snapshot(state, fixture)
    db = state / "config" / "memory.db"
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")

    original_identity = common._schema_identity

    def mutate_after_snapshot(
        conn: sqlite3.Connection, tables: tuple[str, ...]
    ) -> dict[str, object]:
        identity = original_identity(conn, tables)
        with sqlite3.connect(db) as writer:
            writer.execute("DELETE FROM search_index")
        return identity

    monkeypatch.setattr(common, "_schema_identity", mutate_after_snapshot)

    proof = common.inspect_indexed_fixture(
        product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
    )

    assert proof["ready"] is True


def test_indexed_fixture_reports_missing_store_and_invalid_schema_separately(
    tmp_path: Path,
) -> None:
    fixture = common.materialize_fixture(tmp_path / "home", pages=4)
    pending = common.inspect_indexed_fixture(
        product="basic_memory",
        state=tmp_path / "missing",
        vault=tmp_path / "vault",
        fixture=fixture,
    )
    assert pending["ready"] is False
    assert pending["reason"] == "indexed store is not present"

    state = tmp_path / "invalid"
    db = state / "config" / "memory.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE alembic_version(version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('unsupported')")
    with pytest.raises(common.AdapterFault, match="incompatible indexed-store schema"):
        common.inspect_indexed_fixture(
            product="basic_memory", state=state, vault=tmp_path / "vault", fixture=fixture
        )


def test_main_passes_startup_timeout_to_each_product(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[float] = []

    async def fake_run_product(**kwargs: object) -> dict[str, object]:
        seen.append(float(kwargs["startup_timeout"]))
        return {"product": kwargs["product"], "status": "pass"}

    monkeypatch.setattr(common, "run_product", fake_run_product)
    assert (
        common.main(
            [
                "--product",
                "both",
                "--state",
                str(tmp_path / "state"),
                "--vault",
                str(tmp_path / "vault"),
                "--basic-memory-executable",
                "/bin/true",
                "--startup-timeout",
                "17.5",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert seen == [17.5, 17.5]


def test_run_product_does_not_start_timed_work_when_indexed_fixture_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Transport:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    class Client:
        def __init__(self, transport: object, **kwargs: object) -> None:
            del transport, kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def list_tools(self) -> list[types.SimpleNamespace]:
            return [
                types.SimpleNamespace(name=name)
                for name in ("remember", "edit_memory", "read_memory", "ask_memory")
            ]

    fastmcp = types.ModuleType("fastmcp")
    fastmcp.Client = Client  # type: ignore[attr-defined]
    client_module = types.ModuleType("fastmcp.client")
    transports = types.ModuleType("fastmcp.client.transports")
    transports.StdioTransport = Transport  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.client", client_module)
    monkeypatch.setitem(sys.modules, "fastmcp.client.transports", transports)

    async def public_ready(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def incomplete(*args: object, **kwargs: object) -> None:
        evidence = kwargs["evidence"]
        assert isinstance(evidence, dict)
        evidence.update({"ready": False, "reason": "fixture remains partial"})
        raise common.AdapterFault("initial indexed-corpus proof incomplete before startup timeout")

    async def workflow_started(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("timed workflow must not start")

    monkeypatch.setattr(common, "_await_initial_index", public_ready)
    monkeypatch.setattr(common, "_await_indexed_fixture", incomplete)
    monkeypatch.setattr(common, "_run_exomem", workflow_started)

    row = asyncio.run(
        common.run_product(
            product="exomem",
            state=tmp_path / "state",
            vault=tmp_path / "vault",
            pages=4,
            timeout=1,
            python=Path(sys.executable),
            basic_memory=Path("/bin/true"),
            wheel=None,
            marker="marker",
            startup_timeout=1,
        )
    )

    assert row["status"] == "invalid"
    assert row["phases"]["wall_to_verified_closure_ms"] is None
    assert row["initial_indexed_corpus"]["reason"] == "fixture remains partial"


def test_run_product_rejects_late_success_before_starting_timed_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Transport:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    class Client:
        def __init__(self, transport: object, **kwargs: object) -> None:
            del transport, kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def list_tools(self) -> list[types.SimpleNamespace]:
            return [
                types.SimpleNamespace(name=name)
                for name in ("remember", "edit_memory", "read_memory", "ask_memory")
            ]

    fastmcp = types.ModuleType("fastmcp")
    fastmcp.Client = Client  # type: ignore[attr-defined]
    client_module = types.ModuleType("fastmcp.client")
    transports = types.ModuleType("fastmcp.client.transports")
    transports.StdioTransport = Transport  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.client", client_module)
    monkeypatch.setitem(sys.modules, "fastmcp.client.transports", transports)

    async def ready(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def workflow_started(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("timed workflow must not start after a late readiness result")

    ticks = iter((10.0, 10.0, 16.0))
    monkeypatch.setattr(common.time, "perf_counter", lambda: next(ticks, 16.0))
    monkeypatch.setattr(common, "_await_initial_index", ready)
    monkeypatch.setattr(common, "_await_indexed_fixture", ready)
    monkeypatch.setattr(common, "_await_exomem_mutation", ready)
    monkeypatch.setattr(common, "_run_exomem", workflow_started)

    row = asyncio.run(
        common.run_product(
            product="exomem",
            state=tmp_path / "state",
            vault=tmp_path / "vault",
            pages=4,
            timeout=1,
            python=Path(sys.executable),
            basic_memory=Path("/bin/true"),
            wheel=None,
            marker="marker",
            startup_timeout=5,
        )
    )

    assert row["status"] == "invalid"
    assert "startup timeout" in str(row["reason"])
    assert row["phases"]["wall_to_verified_closure_ms"] is None


def test_startup_failure_becomes_an_invalid_json_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = common.main(
        [
            "--product",
            "exomem",
            "--state",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
            "--python",
            str(tmp_path / "missing-python"),
            "--basic-memory-executable",
            "/bin/true",
        ]
    )

    row = json.loads(capsys.readouterr().out)["rows"][0]
    assert result == 1
    assert row["status"] == "invalid"
    assert row["reason"].startswith("MCP runtime failure:")
    assert row["phases"]["wall_to_verified_closure_ms"] is None


def test_basic_memory_provenance_requires_the_pinned_wheel_version_and_inventory() -> None:
    assert common.basic_memory_provenance_is_pinned(
        {
            "wheel_matches_pinned_digest": True,
            "installed_version_matches_expected": True,
            "dependency_inventory": {"status": "ok"},
        }
    )
    assert not common.basic_memory_provenance_is_pinned(
        {
            "wheel_matches_pinned_digest": False,
            "installed_version_matches_expected": True,
            "dependency_inventory": {"status": "ok"},
        }
    )


def test_exomem_provenance_uses_the_explicit_runtime_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    direct = common.runtime_provenance(
        executable=Path(sys.executable),
        wheel=None,
        python=Path(sys.executable),
        package="exomem",
        expected_version=None,
    )
    provenance = common.runtime_provenance(
        executable=Path(sys.executable),
        wheel=None,
        python=Path(sys.executable),
        package="exomem",
        expected_version=None,
        environment={"PYTHONPATH": str(common.ROOT / "src")},
    )

    identity = provenance["source_identity"]
    assert direct["source_identity"]["runtime_pythonpath_root"] is None
    assert identity["runtime_pythonpath_root"] == str(common.ROOT / "src")
    assert identity["revision"]
    assert identity["tree"]
    assert len(identity["source_digest"]) == 64


@pytest.mark.parametrize("error", ["Entity not found", "AMBIGUOUS_IDENTIFIER"])
def test_basic_memory_string_error_is_a_refusal(error):
    payload = {"file_path": None, "error": error}
    assert common.result_classification(payload) == "refused"
    with pytest.raises(common.AdapterFault, match="failed MCP result"):
        common.decode_result({"structuredContent": payload})


def test_basic_memory_adapter_uses_exact_fixture_and_returned_paths(tmp_path):
    fixture = common.materialize_fixture(tmp_path / "fixture", pages=4)
    calls = []
    returned_paths = iter(("notes/created-a.md", "notes/created-b.md"))

    class Client:
        async def call(self, tool, arguments, **kwargs):
            calls.append((tool, arguments, kwargs))
            if tool == "write_note":
                return {"file_path": next(returned_paths)}
            if tool == "search_notes":
                return {"results": [{"content": arguments["query"]}]}
            return {}

    result = asyncio.run(common._run_basic_memory(Client(), "marker", fixture, 0))
    expected = ["notes/created-a.md", "active-tracker.md", "stale-link.md", "notes/created-b.md"]
    assert result["changed"] == expected
    assert [args["identifier"] for tool, args, _ in calls if tool == "edit_note"] == expected[1:3]
    assert [args["identifier"] for tool, args, _ in calls if tool == "read_note"] == expected


def test_basic_memory_adapter_rejects_a_missing_created_path(tmp_path):
    fixture = common.materialize_fixture(tmp_path / "fixture", pages=4)

    class Client:
        async def call(self, tool, arguments, **kwargs):
            return {}

    with pytest.raises(common.AdapterFault, match="created file path"):
        asyncio.run(common._run_basic_memory(Client(), "marker", fixture, 0))
