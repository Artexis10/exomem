"""The installed journey consumes retryability without replaying writes or changing pages."""

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.cli_ops import OpError, envelope
from exomem.query_engine.route import details
from exomem.query_engine.runtime import QueryError


@pytest.fixture(scope="module")
def journey():
    script = Path(__file__).parents[1] / "scripts" / "e2e_s1_summary_journey.py"
    spec = importlib.util.spec_from_file_location("s1_query_recovery", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def call_tool(self, tool, arguments, *, raise_on_error):
        self.requests.append((tool, copy.deepcopy(arguments)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(is_error=False, structured_content=response)


def test_continuation_recovers_once_without_changing_cursor_or_reference_rows(journey, capsys):
    error = QueryError("QUERY_TIMEOUT")
    public_error = OpError(error.code, error.message, details=details(error)).as_public_dict()
    client = Client([
        {"rows": [{"id": "first"}], "has_more": True, "next_cursor": "opaque-continuation"},
        envelope(False, error=public_error),
        {"rows": [{"id": "second"}], "has_more": False, "next_cursor": None},
    ])
    query = {"version": 1, "select": ["id"], "execution_profile": "interactive"}
    rows, _ = asyncio.run(journey.paged(journey.Session(client, 1), query, "rows", None, "collection"))
    assert rows == [{"id": "first"}, {"id": "second"}]
    assert len(client.requests) == 3
    assert client.requests[1] == client.requests[2]
    assert client.requests[1][1] == {
        "action": "query", "collection": "collection",
        "query": {**query, "page": {"after": "opaque-continuation"}},
    }
    output = capsys.readouterr().out
    assert "attempt=1 refused=QUERY_TIMEOUT retryable=true" in output
    assert "attempts=2 outcome=complete total_roundtrip_ms=" in output


@pytest.mark.parametrize("code,retryable,attempts", [
    ("QUERY_TIMEOUT", True, 2),
    ("QUERY_TIMEOUT", False, 1),
    ("QUERY_CURSOR_STALE", True, 1),
])
def test_persistent_or_ineligible_refusal_still_fails(journey, code, retryable, attempts, capsys):
    public_error = OpError(code, "read refused", details={"retryable": retryable}).as_public_dict()
    client = Client([envelope(False, error=public_error)] * 2)
    with pytest.raises(journey.Refused) as failed:
        asyncio.run(journey.Session(client, 1).query({"version": 1, "page": {"after": "same-cursor"}}))
    assert failed.value.error == public_error
    assert len(client.requests) == attempts
    if attempts == 2:
        assert client.requests[0] == client.requests[1]
        assert "attempts=2 outcome=failed total_roundtrip_ms=" in capsys.readouterr().out


def test_plain_error_code_does_not_authorize_replay(journey):
    client = Client([envelope(False, error={"code": "QUERY_TIMEOUT", "message": "read refused"})])
    with pytest.raises(journey.Refused):
        asyncio.run(journey.Session(client, 1).query({"version": 1}))
    assert len(client.requests) == 1


def test_write_refusal_does_not_authorize_replay(journey):
    error = OpError("QUERY_TIMEOUT", "write refused", details={"retryable": True}).as_public_dict()
    client = Client([envelope(False, error=error)])
    with pytest.raises(journey.Refused):
        asyncio.run(journey.Session(client, 1).mutate("record_memory", action="update"))
    assert len(client.requests) == 1


def test_transport_timeout_is_not_a_retryable_query_refusal(journey):
    client = Client([TimeoutError("transport timeout")])
    with pytest.raises(TimeoutError):
        asyncio.run(journey.Session(client, 1).query({"version": 1}))
    assert len(client.requests) == 1
