"""Accepted writes, failed searches and process ownership stay explicit."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def bench(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("parity_benchmark")


def test_status_never_accepts_an_unknown_call(bench):
    assert bench.verdict([{"ok": True}], [{"outcome": "invalid"}]) == "invalid"
    assert bench.verdict([{"ok": False}], [{"outcome": "ok"}]) == "fail"
    assert bench.verdict([{"ok": True}], [{"outcome": "ok"}]) == "pass"
    assert bench.verdict([], []) == "invalid"


def test_acknowledged_tokens_must_appear_exactly_once(bench):
    tokens = ["parityconcurrent000", "parityconcurrent001"]
    assert bench.token_proof("\n".join(tokens), tokens)
    assert not bench.token_proof(tokens[0], tokens)
    assert not bench.token_proof("\n".join(tokens + [tokens[0]]), tokens)
    assert not bench.token_proof("\n".join(token + "suffix" for token in tokens), tokens)


def test_shared_body_policy_only_ignores_single_terminal_lf(bench):
    equals = bench.fixture_module.read_body_equals
    assert equals({"body": "exact\n"}, "exact")
    assert equals({"body": "exact"}, "exact\n")
    assert not equals({"body": "exact\n\n"}, "exact")
    assert not equals({"body": "exact "}, "exact")
    assert not equals({"body": "ex\nact"}, "exact")


def test_refuses_signalling_process_without_exact_owned_token(bench, tmp_path, monkeypatch):
    pid = tmp_path / "server.pid"
    pid.write_text("123")
    monkeypatch.setattr(bench, "open_process_handle", lambda *args: 42)
    monkeypatch.setattr(bench.os, "close", lambda fd: None)
    monkeypatch.setattr(bench, "process_environment", lambda pid: {"PARITY_RUN_TOKEN": "another-run"})
    calls = []
    monkeypatch.setattr(bench, "send_process_kill", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="ownership"):
        bench.kill_owned_server(pid, "expected-run")
    assert calls == []


def test_kills_only_the_owned_child_and_observes_exit(bench, tmp_path):
    import os
    import select
    import subprocess
    import sys
    import uuid

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux process-crash harness")
    token = uuid.uuid4().hex
    pid_file = tmp_path / "owned.pid"
    process = subprocess.Popen([sys.executable, "-c",
                                "import os,time; print(os.environ['PARITY_RUN_TOKEN'],flush=True); time.sleep(30)"],
                               env={**os.environ, "PARITY_RUN_TOKEN": token}, stdout=subprocess.PIPE, text=True)
    pid_file.write_text(str(process.pid))
    try:
        assert select.select([process.stdout], [], [], 5)[0], "child startup did not complete"
        assert process.stdout.readline().strip() == token
        proof = bench.kill_owned_server(pid_file, token)
        assert proof["exit_observed"] is True
        assert process.wait(timeout=2) == -9
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()


@pytest.mark.parametrize("refused_phase", ["concurrent_read", "crash_write", None])
def test_run_retains_setup_retries_and_observed_precrash_refusals(bench, tmp_path, monkeypatch, refused_phase):
    import asyncio
    from types import SimpleNamespace

    import fastmcp
    import parity_exomem as adapter

    monkeypatch.setattr(bench.os, "environ", dict(bench.os.environ))
    body = bench.fixture_module.tracker_body(4)
    reads = 0
    warm_searches = 0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [SimpleNamespace(name=name, inputSchema={}) for name in
                    ("read_memory", "edit_memory", "ask_memory", "connect_memory")]

        async def call_tool_mcp(self, name, arguments):
            nonlocal body, reads, warm_searches
            result = {"success": True}
            if name == "read_memory":
                reads += 1
                if refused_phase == "concurrent_read" and reads == 3:
                    result = {"success": False, "error": "observed read refusal"}
                else:
                    result = {"path": adapter.TRACKER, "body": body}
            elif name == "edit_memory":
                operation = arguments["operation"]
                if refused_phase == "crash_write" and operation["new_string"] == "paritycrashaccepted":
                    result = {"success": False, "error": "observed write refusal"}
                elif operation["kind"] == "replace_string":
                    body = body.replace(operation["old_string"], operation["new_string"])
                else:
                    body += "\n\n" + operation["new_string"]
            elif name == "ask_memory":
                result = {"hits": [{"path": adapter.TRACKER, "snippet": arguments["query"]}]}
                if arguments["query"] == "warming":
                    warm_searches += 1
                    if warm_searches <= 2:
                        result = {"success": False, "error": "warming"}
            return {"structuredContent": result}

    async def ready(*args, **kwargs):
        return {"ready": True}

    async def warm_with_retries(client, **kwargs):
        for _ in range(3):
            await client.call("ask_memory", {"query": "warming"}, allow_refusal=True)

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(bench.common, "_permit_refusal_envelopes", lambda client: None)
    monkeypatch.setattr(bench.common, "_await_initial_index", warm_with_retries)
    monkeypatch.setattr(bench.common, "_await_exomem_mutation", ready)
    monkeypatch.setattr(bench, "wait_for_proof", ready)
    monkeypatch.setattr(bench, "graph_check", ready)
    monkeypatch.setattr(bench, "provenance", lambda *args: {})
    monkeypatch.setattr(adapter, "prepare", lambda *args, **kwargs: ({}, {}, tmp_path / "vault"))
    killed = []

    def kill(*args):
        killed.append(args)
        return {"signal": "SIGKILL"}

    monkeypatch.setattr(bench, "kill_owned_server", kill)
    args = SimpleNamespace(state=tmp_path / "state", vault=tmp_path / "vault", source=bench.ROOT,
                           output=tmp_path / "result.json",
                           python=Path(bench.sys.executable), product="exomem", pages=4, cycles=1,
                           concurrency=1, timeout=1, startup_timeout=1)
    result = asyncio.run(bench.run(args))
    assert [row["classification"] for row in result["setup_calls"][:3]] == ["refused", "refused", "ok"]
    assert result["status"] == ("pass" if refused_phase is None else "fail"), result["errors"]
    assert result["crash_executed"] is (refused_phase is None)
    if refused_phase is None:
        assert len(killed) == 1
    else:
        assert not killed
        assert result["crash"]["skipped_reason"]


@pytest.mark.parametrize("inside", ["state", "vault", "output"])
def test_direct_driver_rejects_generated_paths_inside_source(bench, tmp_path, inside):
    import asyncio
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    args = SimpleNamespace(source=source, state=tmp_path / "state", vault=tmp_path / "vault",
                           output=tmp_path / "result.json")
    setattr(args, inside, source / "new-run")
    with pytest.raises(ValueError, match="outside source"):
        asyncio.run(bench.run(args))
    assert not (source / "new-run").exists()
    assert not (tmp_path / "state").exists()
