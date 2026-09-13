"""Measurement integrity checks for the public mixed-load diagnostic."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("mixed_load_benchmark", SCRIPTS / "mixed_load_benchmark.py")
bench = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


def test_tail_requires_a_declared_sample_count():
    result = bench.latency_summary([1.0, 2.0, 200.0])
    assert result == {"count": 3, "median_ms": 2.0, "p95_ms": None,
                      "max_ms": 200.0, "p95_reason": "requires at least 100 observations"}
    result = bench.latency_summary(list(range(1, 101)))
    assert result["p95_ms"] == 95
    assert result["count"] == 100
    assert bench.latency_summary([])["max_ms"] is None


def test_pending_overlap_is_an_interval_not_a_whole_run_label():
    calls = [
        {"phase": "foreground", "tool": "read_memory", "started_ms": 0, "ended_ms": 5, "elapsed_ms": 5, "outcome": "ok"},
        {"phase": "foreground", "tool": "read_memory", "started_ms": 10, "ended_ms": 15, "elapsed_ms": 5, "outcome": "ok"},
        {"phase": "foreground", "tool": "read_memory", "started_ms": 20, "ended_ms": 30, "elapsed_ms": 10, "outcome": "refused"},
        {"phase": "media-poll", "tool": "read_memory", "started_ms": 5, "ended_ms": 9, "elapsed_ms": 4, "outcome": "ok"},
    ]
    report = bench.summarize_calls(calls, (4, 10))
    assert report["read_memory"]["all"]["count"] == 3
    assert report["read_memory"]["successful"]["count"] == 2
    assert report["read_memory"]["pending_window"]["count"] == 1
    assert report["read_memory"]["failed"] == 1


def test_roots_refuse_nesting_reuse_and_symlinks_before_creation(tmp_path):
    with pytest.raises(ValueError, match="overlap"):
        bench.prepare_roots(tmp_path / "state", tmp_path / "state" / "vault")
    assert not (tmp_path / "state").exists()
    live = tmp_path / "live"
    live.mkdir()
    (live / "private.md").write_text("untouched")
    with pytest.raises(ValueError, match="new"):
        bench.prepare_roots(tmp_path / "new-state", live)
    assert not (tmp_path / "new-state").exists()
    alias = tmp_path / "alias"
    alias.symlink_to(live, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        bench.prepare_roots(tmp_path / "new-state", alias)
    assert (live / "private.md").read_text() == "untouched"


def test_preexisting_empty_root_cannot_be_adopted(tmp_path):
    state, vault = tmp_path / "state", tmp_path / "vault"
    state.mkdir()
    with pytest.raises(ValueError, match="new"):
        bench.prepare_roots(state, vault)
    assert not vault.exists()


def test_concurrent_runs_cannot_claim_the_same_new_roots(tmp_path, monkeypatch):
    state, vault = tmp_path / "state", tmp_path / "vault"
    mkdir = Path.mkdir
    barrier = threading.Barrier(2)

    def synchronized_mkdir(path, *args, **kwargs):
        if path == state:
            barrier.wait(timeout=5)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", synchronized_mkdir)

    def claim():
        try:
            bench.prepare_roots(state, vault)
        except ValueError:
            return "refused"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == ["claimed", "refused"]


def test_failed_public_calls_retain_their_interval():
    class Client:
        async def call_tool_mcp(self, *args):
            raise TimeoutError("deliberate transport timeout")

    recorder = bench.Recorder(Client())
    with pytest.raises(TimeoutError):
        asyncio.run(recorder.call("read_memory", {}, phase="foreground"))
    assert len(recorder.calls) == 1
    row = recorder.calls[0]
    assert row["outcome"] == "invalid"
    assert row["ended_ms"] >= row["started_ms"] >= 0


def test_exact_read_and_search_cannot_pass_on_echoed_markers():
    assert bench.verify_read({"body": "# Tracker\n\nnew-marker\n"}, "# Tracker\n\nnew-marker\n")
    assert not bench.verify_read({"body": "old", "query": "new-marker"}, "new-marker")
    assert bench.verify_search({"results": [{"path": "a.md", "snippet": "new-marker"}]}, "a.md", "new-marker")
    assert not bench.verify_search({"results": [{"path": "b.md", "snippet": "new-marker"}]}, "a.md", "new-marker")
    assert not bench.verify_search({"results": [], "query": "new-marker"}, "a.md", "new-marker")


def test_refusal_is_retained_and_raised():
    class Client:
        async def call_tool_mcp(self, *args):
            return SimpleNamespace(structuredContent={"success": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}}, content=[])

    recorder = bench.Recorder(Client())
    with pytest.raises(RuntimeError, match="RETRIEVAL_INDEX_WARMING"):
        asyncio.run(recorder.call("ask_memory", {}, phase="foreground"))
    assert recorder.calls[0]["outcome"] == "refused"


def test_foreground_keeps_fixed_cycles_when_content_proof_fails():
    class Client:
        def __init__(self):
            self.tools = []

        def elapsed(self):
            return 3.0

        async def call(self, tool, args, *, phase):
            self.tools.append((tool, phase))
            return {"body": "mixedloadmarker00000"} if tool == "read_memory" else {
                "results": [{"path": bench.TRACKER, "snippet": "mixedloadmarker00001"}]}

    client = Client()
    result = {"cycles": []}
    with pytest.raises(RuntimeError, match="2 foreground cycles"):
        asyncio.run(bench.foreground(client, "mixedloadmarker00000", 2, result))
    assert len(result["cycles"]) == 2
    assert not any(row["exact_body"] for row in result["cycles"])
    assert result["finished_ms"] == 3.0
    assert client.tools == [(tool, "foreground") for tool in ("edit_memory", "read_memory", "ask_memory")] * 2


def test_public_refusal_is_not_retried_or_allowed_to_hide_later_recovery():
    class Client:
        def __init__(self):
            self.searches = 0
            self.body = "mixedloadmarker00000"

        def elapsed(self):
            return 5.0

        async def call(self, tool, args, *, phase):
            if tool == "edit_memory":
                self.body = self.body.replace(args["operation"]["old_string"], args["operation"]["new_string"])
                return {}
            if tool == "read_memory":
                return {"body": self.body}
            self.searches += 1
            if self.searches == 1:
                raise bench.PublicRefusal("RETRIEVAL_INDEX_WARMING")
            return {"results": [{"path": bench.TRACKER, "snippet": self.body}]}

    client = Client()
    result = {"cycles": []}
    with pytest.raises(RuntimeError, match="1 foreground cycles"):
        asyncio.run(bench.foreground(client, client.body, 2, result))
    assert client.searches == 2
    assert [row["exact_search"] for row in result["cycles"]] == [False, True]


@pytest.mark.parametrize("fault", ["hash", "path", "size"])
def test_media_completion_requires_exact_binary_identity(monkeypatch, fault):
    artifacts = [{"sha256": str(i) * 64, "expected_text": f"unique text {i}", "file_id": str(i),
                  "download_url": "https://example.test/fixture", "mime_type": "image/png", "file_name": f"{i}.png"}
                 for i in range(3)]
    paths = [f"Evidence/{i}.png" for i in range(3)]
    monkeypatch.setattr(bench.media_helpers, "validated_evidence_paths", lambda *args: paths)
    monkeypatch.setattr(bench.media_helpers, "process_media_requests", lambda *args: [{"paths": paths}])

    class Client:
        def elapsed(self):
            return 1.0

        async def call(self, tool, args, *, phase):
            if tool == "preserve_artifacts":
                return {"files": [{"file_id": str(i), "stored_path": p, "outcome": "stored", "size": 123,
                                    "hash": artifacts[i]["sha256"], "hash_algorithm": "sha256"} for i, p in enumerate(paths)]}
            if tool == "process_media":
                return {"media_results": [{"path": p, "sidecar_path": p + ".md", "outcome": "processed", "state": "completed"} for p in paths]}
            return {"frontmatter": {"processing_state": "completed", "binary_sha256": "wrong" if fault == "hash" else artifacts[0]["sha256"],
                                    "evidence_file": "Evidence/other-group/0.png" if fault == "path" else paths[0],
                                    "binary_size": 999 if fault == "size" else 123}, "body": "unique text"}

    with pytest.raises(RuntimeError, match="binary identity"):
        asyncio.run(bench.media_burst(Client(), {"process_media": object()}, artifacts, 1, 1, {"groups": []}))


@pytest.mark.parametrize("size", [None, True, "123", -1, 0])
def test_preservation_refuses_missing_or_invalid_byte_counts(size):
    artifacts = [{"file_id": str(i), "sha256": str(i) * 64} for i in range(3)]
    files = [{"file_id": str(i), "stored_path": f"Evidence/{i}.png", "hash": str(i) * 64,
              "hash_algorithm": "sha256", "outcome": "stored", "size": size} for i in range(3)]
    with pytest.raises(RuntimeError, match="preservation"):
        bench.preservation_proof(artifacts, files)


def test_preservation_refuses_duplicate_receipts():
    artifacts = [{"file_id": str(i), "sha256": str(i) * 64} for i in range(3)]
    files = [{"file_id": str(i), "stored_path": f"Evidence/{i}.png", "hash": str(i) * 64,
              "hash_algorithm": "sha256", "outcome": "stored", "size": 123} for i in range(3)]
    with pytest.raises(RuntimeError, match="preservation"):
        bench.preservation_proof(artifacts, [*files, files[0]])


@pytest.mark.parametrize("fault", ["teardown", "transport"])
def test_transport_and_teardown_faults_invalidate_an_otherwise_successful_run(
    tmp_path, monkeypatch, fault
):
    import fastmcp

    class Client:
        def __init__(self, *args, **kwargs):
            self.body = dict(bench.common.fixture_pages(4))["active-tracker.md"] + bench.RELATION

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if fault == "teardown":
                raise RuntimeError("deliberate teardown failure")

        async def list_tools(self):
            return []

        async def call_tool_mcp(self, tool, args):
            if fault == "transport" and tool == "ask_memory":
                raise TimeoutError("deliberate transport timeout")
            payload = {"success": True}
            if tool == "edit_memory":
                op = args["operation"]
                self.body = self.body.replace(op["old_string"], op["new_string"])
            if tool == "read_memory":
                payload["body"] = self.body
            if tool == "ask_memory":
                payload["results"] = [{"path": bench.TRACKER, "snippet": self.body}]
            return SimpleNamespace(structuredContent=payload, content=[])

    async def ready(*args, **kwargs):
        return {"ready": True}

    monkeypatch.setattr(fastmcp, "Client", Client)
    monkeypatch.setattr(bench, "_runtime", lambda *args: {})
    monkeypatch.setattr(bench, "wait_graph", ready)
    for name in ("_await_indexed_fixture", "_await_initial_index", "_await_exomem_mutation"):
        monkeypatch.setattr(bench.common, name, ready)
    args = SimpleNamespace(state=tmp_path / "state", vault=tmp_path / "vault", pages=4,
                           output=tmp_path / "result.json",
                           case="idle", cycles=1, media_groups=1, timeout=1,
                           startup_timeout=1, graph_timeout=1, python=Path(sys.executable))
    environment = dict(os.environ)
    try:
        report = asyncio.run(bench.run(args))
    finally:
        os.environ.clear()
        os.environ.update(environment)
    assert report["status"] == "invalid"
    if fault == "teardown":
        assert report["foreground"]["cycles"][0]["exact_search"] is True
        assert any("teardown failure" in error for error in report["errors"])
    else:
        assert report["calls"][-1]["outcome"] == "invalid"
        assert any("transport timeout" in error for error in report["errors"])
