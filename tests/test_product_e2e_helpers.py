import importlib.util
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import RootModel

SCRIPT = Path(__file__).parents[1] / "scripts" / "e2e_product_loop.py"
SPEC = importlib.util.spec_from_file_location("e2e_product_loop_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
e2e_product_loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e2e_product_loop)


class _Hit(RootModel[dict[str, object]]):
    pass


@dataclass
class _SchemaHit:
    path: str


def test_port_reservations_are_distinct_and_release_individually() -> None:
    reservations = e2e_product_loop._reserve_port_reservations(3)
    ports = [reservation.getsockname()[1] for reservation in reservations]

    try:
        assert len(set(ports)) == 3
        for reservation, port in zip(reservations, ports, strict=True):
            with socket.socket() as contender:
                with pytest.raises(OSError):
                    contender.bind(("127.0.0.1", port))
            reservation.close()
            with socket.socket() as contender:
                contender.bind(("127.0.0.1", port))
    finally:
        for reservation in reservations:
            reservation.close()


def test_port_reservations_close_a_socket_that_fails_to_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingReservation:
        closed = False

        def bind(self, _address: tuple[str, int]) -> None:
            raise OSError("port reservation failed")

        def close(self) -> None:
            self.closed = True

    reservation = FailingReservation()
    monkeypatch.setattr(e2e_product_loop.socket, "socket", lambda: reservation)

    with pytest.raises(OSError, match="port reservation failed"):
        e2e_product_loop._reserve_port_reservations(1)

    assert reservation.closed


def test_records_fixture_cannot_prewrite_collection_or_source(tmp_path: Path) -> None:
    fixture = e2e_product_loop._write_records_fixture(tmp_path)

    assert fixture["collection"] == "Knowledge Base/Records/Health/X3/_collection.md"
    manifest = tmp_path / fixture["collection"]
    source = tmp_path / fixture["source"]
    assert not manifest.exists()
    assert not source.exists()
    assert "semantic_profile: records" in fixture["manifest_text"]
    assert "record_presentation:" in fixture["manifest_text"]
    assert "field: movements" in fixture["manifest_text"]
    assert "views:" in fixture["manifest_text"]


def test_records_presentation_query_assertion_requires_safe_complete_pagination() -> None:
    parent = {
        "rows": [
            {
                "record_id": "item",
                "movements": [
                    {"movement": "A", "band": "one", "repetitions": "1"},
                    {"movement": "B", "band": "two", "repetitions": "2"},
                    {"movement": "C", "band": "three", "repetitions": "3"},
                ],
            }
        ]
    }
    pages = [
        {
            "rows": [
                {"child_field": "movements", "child_index": 0},
                {"child_field": "movements", "child_index": 1},
            ],
            "continuation": "next",
        },
        {
            "rows": [{"child_field": "movements", "child_index": 2}],
            "continuation": None,
        },
    ]

    e2e_product_loop._assert_records_presentation_rows(parent, pages)

    pages[1]["rows"][0]["private"] = "escaped"
    with pytest.raises(RuntimeError, match="safe child projection"):
        e2e_product_loop._assert_records_presentation_rows(parent, pages)


def test_manual_records_fixture_preserves_template_ownership(tmp_path: Path) -> None:
    fixture = e2e_product_loop._write_manual_records_fixture(tmp_path)

    manifest = tmp_path / fixture["collection"]
    log = tmp_path / fixture["log"]
    template = tmp_path / fixture["template"]
    before_template = template.read_bytes()

    e2e_product_loop._insert_manual_x3_session(tmp_path, fixture)

    assert manifest.is_file()
    assert "semantic_profile: records" in manifest.read_text(encoding="utf-8")
    assert "exomem://memory/81947000-4c22-46e4-9874-23fed028314b" in manifest.read_text(
        encoding="utf-8"
    )
    assert "2026-08-03 · Push" in log.read_text(encoding="utf-8")
    assert template.read_bytes() == before_template


def test_records_rebaseline_assertion_requires_reader_v2_and_discontinuity() -> None:
    inspection = {
        "audit": {
            "status": "acknowledged_gap",
            "gaps": [],
            "discontinuity": {
                "provenance_continuity": False,
                "prior_head": "a" * 24,
                "acknowledged_gap_codes": ["current-container-mismatch"],
                "rationale": "acknowledge direct installed edit",
            },
        },
        "lifecycle_guards": {
            "expected_manifest_hash": "b" * 64,
            "expected_container_hash": "c" * 64,
        },
    }
    history = {"events": [{"operation": "rebaseline", "minimum_reader_version": 2}]}

    e2e_product_loop._assert_records_rebaseline(inspection, history)

    history["events"][0]["minimum_reader_version"] = 1
    with pytest.raises(RuntimeError, match="reader marker 2"):
        e2e_product_loop._assert_records_rebaseline(inspection, history)


def test_planning_fixtures_cover_software_and_nonsoftware_journeys(tmp_path: Path) -> None:
    fixtures = e2e_product_loop._write_planning_fixtures(tmp_path)

    software = fixtures["software"]
    nonsoftware = fixtures["nonsoftware"]
    assert not (tmp_path / software["collection"]).exists()
    assert not (tmp_path / nonsoftware["collection"]).exists()
    assert "semantic_profile: planning" in software["manifest_text"]
    assert "semantic_profile: planning" in nonsoftware["manifest_text"]
    assert "execution: {type: array, items: {type: object}}" in software["manifest_text"]
    assert "progress_evidence: {type: array, items: {type: object}}" in nonsoftware["manifest_text"]
    assert "domain: home" in nonsoftware["manifest_text"]
    assert fixtures["nonsoftware"]["records_view"] == {
        "collection": "exomem://memory/9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8",
        "role": "progress",
        "view": "completed-sessions",
    }


def test_unauthenticated_records_request_requires_exact_challenge_and_no_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = "http://127.0.0.1:8765"
    calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def raw_post(
        url: str, *, body: dict[str, object], headers: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        calls.append((url, body, headers))
        assert timeout == 2.0
        return (
            401,
            {
                "www-authenticate": (
                    'Bearer resource_metadata="http://127.0.0.1:8765/'
                    '.well-known/oauth-protected-resource/mcp"'
                )
            },
            b"",
        )

    monkeypatch.setattr(e2e_product_loop, "_http_post_raw", raw_post)

    e2e_product_loop._assert_unauthenticated_records_refusal(base_url, timeout=2.0)

    assert calls == [
        (
            f"{base_url}/mcp",
            {
                "jsonrpc": "2.0",
                "id": "records-auth-refusal",
                "method": "tools/call",
                "params": {
                    "name": "record_memory",
                    "arguments": {
                        "action": "inspect",
                        "collection": "Knowledge Base/Records/Health/X3/_collection.md",
                    },
                },
            },
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    ]


def test_unauthenticated_planning_request_requires_exact_challenge_and_no_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = "http://127.0.0.1:8765"
    calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def raw_post(
        url: str, *, body: dict[str, object], headers: dict[str, str], timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        calls.append((url, body, headers))
        assert timeout == 2.0
        return (
            401,
            {
                "www-authenticate": (
                    'Bearer resource_metadata="http://127.0.0.1:8765/'
                    '.well-known/oauth-protected-resource/mcp"'
                )
            },
            b"",
        )

    monkeypatch.setattr(e2e_product_loop, "_http_post_raw", raw_post)

    e2e_product_loop._assert_unauthenticated_planning_refusal(base_url, timeout=2.0)

    assert calls[0][1] == {
        "jsonrpc": "2.0",
        "id": "planning-auth-refusal",
        "method": "tools/call",
        "params": {
            "name": "plan_memory",
            "arguments": {
                "action": "inspect",
                "collection": "Knowledge Base/Planning/Software/_collection.md",
            },
        },
    }


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (500, {}, b""),
        (401, {"www-authenticate": "Bearer"}, b""),
        (
            401,
            {
                "www-authenticate": (
                    'Bearer resource_metadata="http://127.0.0.1:8765/'
                    '.well-known/oauth-protected-resource/mcp"'
                )
            },
            b"Knowledge Base/Records/Health/X3/Training Log.md",
        ),
    ],
)
def test_unauthenticated_records_request_rejects_generic_or_disclosing_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    monkeypatch.setattr(
        e2e_product_loop,
        "_http_post_raw",
        lambda _url, **_kwargs: (status, headers, body),
    )

    with pytest.raises(RuntimeError):
        e2e_product_loop._assert_unauthenticated_records_refusal(
            "http://127.0.0.1:8765", timeout=2.0
        )


def test_unwrap_result_normalizes_nested_typed_mcp_values() -> None:
    value = {
        "hits": [
            _Hit({"path": "Knowledge Base/example.md"}),
            _SchemaHit(path="Knowledge Base/generated.md"),
        ]
    }

    assert e2e_product_loop._unwrap_result(value) == {
        "hits": [
            {"path": "Knowledge Base/example.md"},
            {"path": "Knowledge Base/generated.md"},
        ]
    }


def test_mutation_diagnostics_requires_and_unwraps_committed_full_envelope() -> None:
    diagnostics = {"saved": {"content_hash": "after"}}

    assert e2e_product_loop._mutation_diagnostics(
        {
            "ok": True,
            "status": "committed",
            "mutated": True,
            "request_id": "request-1",
            "diagnostics": diagnostics,
        },
        operation="schema_memory",
    ) == diagnostics


def test_mutation_diagnostics_rejects_explicit_graph_sync_failure() -> None:
    with pytest.raises(
        RuntimeError,
        match="capture_source graph synchronization failed: GRAPH_SYNC_STABILIZATION_EXHAUSTED",
    ):
        e2e_product_loop._mutation_diagnostics(
            {
                "ok": True,
                "status": "committed",
                "mutated": True,
                "diagnostics": {
                    "graph_sync": "failed",
                    "graph_sync_code": "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
                },
            },
            operation="capture_source",
        )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"graph_status": "current"}, {"graph_status": "current"}),
        (
            {
                "ok": True,
                "status": "committed",
                "mutated": True,
                "diagnostics": {"graph_status": "refreshed"},
            },
            {"graph_status": "refreshed"},
        ),
    ],
)
def test_maintenance_diagnostics_accepts_raw_noop_or_committed_full_result(
    result: dict[str, object],
    expected: dict[str, object],
) -> None:
    assert e2e_product_loop._maintenance_diagnostics(
        result,
        operation="maintain_memory",
    ) == expected


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "status": "retryable", "mutated": False, "diagnostics": {}},
        {"ok": True, "status": "committed", "mutated": True},
    ],
)
def test_mutation_diagnostics_rejects_noncommitted_or_nonfull_results(
    result: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="remember mutation"):
        e2e_product_loop._mutation_diagnostics(result, operation="remember")
