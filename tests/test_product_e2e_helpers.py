import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import RootModel

from scripts import e2e_product_loop


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


def test_records_fixture_is_self_contained_and_preserves_manual_template_ownership(
    tmp_path: Path,
) -> None:
    fixture = e2e_product_loop._write_records_fixture(tmp_path)

    assert fixture["collection"] == "Knowledge Base/Records/Health/X3/_collection.md"
    manifest = tmp_path / fixture["collection"]
    log = tmp_path / fixture["log"]
    template = tmp_path / fixture["template"]
    assert manifest.is_file()
    assert log.is_file()
    assert template.is_file()
    assert "semantic_profile: records" in manifest.read_text(encoding="utf-8")
    assert "exomem://memory/81947000-4c22-46e4-9874-23fed028314b" in manifest.read_text(
        encoding="utf-8"
    )
    assert "{{date}}" in template.read_text(encoding="utf-8")

    e2e_product_loop._insert_manual_x3_session(tmp_path, fixture)

    assert "2026-08-03 · Push" in log.read_text(encoding="utf-8")
    assert "{{date}}" in template.read_text(encoding="utf-8")


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
