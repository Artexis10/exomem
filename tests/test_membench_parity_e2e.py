"""recall_parity end to end: sample, capture over MCP on HTTP, packet, score.

Two real Exomem servers run in this process on loopback, each over its own
vault, so the arms return different rankings through the real MCP client. The
queries are invented and the judge is a fixed table, so every number can be
recomputed here without trusting the scorer.
"""

from __future__ import annotations

import json
import math
import shutil
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import FIXTURE_VAULT, initialize_vault_state_offline
from recall_parity import capture, cli, packet, private
from starlette.middleware import Middleware

from exomem import find as find_module
from exomem import query_log, server

LOCAL_TOKEN = "local-arm-bearer-0f3a9c"
CLOUD_TOKEN = "cloud-arm-bearer-71be42"

#: Per query: the documents a judge would grade 2 and 1 (stem of the file name).
JUDGED = {
    "Retry backoff with jitter": (
        {"retry-with-full-jitter-backoff"},
        {"retry-with-fixed-interval"},
    ),
    "Circuit breaker for downstream failure": ({"circuit-breaker-for-downstream-failures"}, set()),
    "Autovacuum tuning against table bloat": (
        {"autovacuum-thresholds-prevent-table-bloat"},
        {"2026-06-02-postgres-autovacuum-tuning"},
    ),
    "Percentage feature flag rollout": (
        {"percentage-based-feature-flag-rollout"},
        {"kill-switch-for-risky-releases"},
    ),
}
#: The cloud arm's vault lacks these notes, so the arms rank differently.
MISSING_FROM_CLOUD = (
    "retry-with-full-jitter-backoff",
    "retry-with-fixed-interval",
    "autovacuum-thresholds-prevent-table-bloat",
)
#: Both arms return this note; the source vault holds a different text for it, so the
#: packet can show whether the judge's view comes from the source vault or from an arm.
REWRITTEN_IN_SOURCE = "circuit-breaker-for-downstream-failures"
SOURCE_ONLY_BODY = "Text that only the source vault holds, never any arm's answer."
#: Both arms return this note; the source vault does not hold it.
ABSENT_FROM_SOURCE = "kill-switch-for-risky-releases"


class _RecordBearer:
    """Remember every Authorization header this server receives."""

    def __init__(self, app, seen: set[str]):
        self.app, self.seen = app, seen

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"authorization":
                    self.seen.add(value.decode())
        await self.app(scope, receive, send)


class _Served:
    def __init__(self, mcp, seen: set[str]):
        app = mcp.http_app(stateless_http=True, middleware=[Middleware(_RecordBearer, seen=seen)])
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started, "the test server did not start"
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def arms(tmp_path, monkeypatch):
    """Two running servers over two vaults, and the Authorization headers each saw."""

    built = []
    for name in ("local", "cloud"):
        vault = tmp_path / f"vault-{name}"
        shutil.copytree(FIXTURE_VAULT, vault)
        initialize_vault_state_offline(vault, source="parity end-to-end fixture")
        if name == "cloud":
            for stem in MISSING_FROM_CLOUD:
                next(vault.rglob(f"{stem}.md")).unlink()
        # The vault is read when the server is built, so each build sees its own.
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
        find_module.clear_cache()
        built.append((name, server.build_server(require_auth=False)))
    seen = {name: set() for name, _ in built}
    served = {name: _Served(mcp, seen[name]) for name, mcp in built}
    try:
        yield {name: s.url for name, s in served.items()}, seen
    finally:
        for s in served.values():
            s.stop()


def _source_vault(tmp_path: Path) -> Path:
    """The vault the judge's view is read from: a copy of the fixture with two differences."""

    source = tmp_path / "source-vault"
    shutil.copytree(FIXTURE_VAULT, source)
    note = next(source.rglob(f"{REWRITTEN_IN_SOURCE}.md"))
    note.write_text(
        f"---\ntitle: Source Vault Title\n---\n\n{SOURCE_ONLY_BODY}\n", encoding="utf-8"
    )
    next(source.rglob(f"{ABSENT_FROM_SOURCE}.md")).unlink()
    return source


def _tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")
    )


def _write_queries_log(directory: Path, monkeypatch) -> Path:
    class Hit:
        def as_dict(self):
            return {"path": "Knowledge Base/Notes/a.md", "type": "pattern", "signals": {}}

    queries = [*JUDGED, "retry  backoff with JITTER", "x", "?????????"]
    with monkeypatch.context() as patch:
        patch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        patch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
        patch.setenv("EXOMEM_LOG_DIR", str(directory))
        for query in queries:
            query_log.log_find_call(
                query=query,
                mode="hybrid",
                scope="kb",
                types=None,
                projects=None,
                tags=None,
                limit=10,
                rerank=False,
                prefer_compiled=True,
                graph=True,
                hits=[Hit()],
            )
    return directory / "queries.jsonl"


def _hand_ndcg(ranked: list[str], grades: dict[str, int]) -> float:
    """nDCG@10 written out: sum of (2**grade - 1) / log2(rank + 1) over the ideal order."""

    def discounted(values: list[int]) -> float:
        return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(values))

    ideal = sorted(grades.values(), reverse=True)[:10]
    return discounted([grades.get(doc, 0) for doc in ranked[:10]]) / discounted(ideal)


def _judge(packet_lines: list[dict]) -> list[dict]:
    """Grade each pair from its query and path alone, as a blind judge would."""

    rows = []
    for line in packet_lines:
        strong, weak = JUDGED[line["query"]]
        stem = Path(line["path"]).stem
        rows.append(
            {"pair_id": line["pair_id"], "grade": 2 if stem in strong else int(stem in weak)}
        )
    return rows


def test_paired_run_from_query_log_to_scored_difference(tmp_path, monkeypatch, capsys, arms):
    urls, seen = arms
    monkeypatch.setenv("PARITY_LOCAL_TOKEN", LOCAL_TOKEN)
    monkeypatch.setenv("PARITY_CLOUD_TOKEN", CLOUD_TOKEN)
    log = _write_queries_log(tmp_path / "logs", monkeypatch)
    root = tmp_path / "private"
    base = ["--private-dir", str(root)]
    source = _source_vault(tmp_path)
    source_before = _tree_state(source)

    def run(*argv: str) -> int:
        return cli.main([argv[0], *base, *argv[1:]])

    # 1. sample: three structural drops (a one-character query among them) leave exactly
    # the four invented queries
    assert (
        run(
            "sample",
            "--queries",
            str(log),
            "--seed",
            "5",
            "--size",
            "4",
            "--tuning-fraction",
            "0.5",
        )
        == 0
    )
    sample = private.read_json(root / "sample" / "sample-manifest.json")
    assert sample["counts"]["distinct_eligible"] == 4
    assert sample["split_sizes"] == {"tuning": 2, "heldout": 2}

    # 2. capture both arms through the real MCP client over HTTP
    arm_args = [
        "--arm",
        "local",
        urls["local"],
        "PARITY_LOCAL_TOKEN",
        "--arm",
        "cloud",
        urls["cloud"],
        "PARITY_CLOUD_TOKEN",
    ]
    assert run("capture", "--run", "all", "--split", "all", *arm_args) == 0
    assert run("capture", "--run", "held", "--split", "heldout", *arm_args) == 0
    manifest = private.read_json(root / "runs" / "all" / "manifest.json")
    assert manifest["tool"] == "search_memory"
    assert manifest["arguments"] == {"limit": 10, "detail": "full"}
    assert [(a["name"], a["url"], a["token_env"]) for a in manifest["arms"]] == [
        ("local", urls["local"], "PARITY_LOCAL_TOKEN"),
        ("cloud", urls["cloud"], "PARITY_CLOUD_TOKEN"),
    ]
    assert manifest["sample"]["seed"] == 5
    assert manifest["sample"]["split_sizes"] == {"tuning": 2, "heldout": 2}
    assert manifest["outcomes"] == {"local": {"ok": 4}, "cloud": {"ok": 4}}
    sampled = {c["query"] for c in private.read_jsonl(root / "sample" / "cases.jsonl")}
    assert manifest["warmup_query"] == capture.WARMUP_QUERY not in sampled
    assert len(private.read_json(root / "runs" / "held" / "manifest.json")["case_ids"]) == 2
    assert seen == {"local": {f"Bearer {LOCAL_TOKEN}"}, "cloud": {f"Bearer {CLOUD_TOKEN}"}}

    # 3. packet: one line per distinct pair; title and body come from the source vault alone
    assert run("packet", "--run", "all", "--vault", str(source)) == 0
    lines = private.read_jsonl(root / "runs" / "all" / "judge" / "packet.jsonl")
    assert {frozenset(line) for line in lines} == {
        frozenset({"pair_id", "query", "path", "title", "body", "body_truncated", "missing"})
    }
    assert not any(arm in json.dumps(lines) for arm in ("local", "cloud"))
    assert len({(line["query"], line["path"]) for line in lines}) == len(lines)
    key = private.read_json(root / "runs" / "all" / "key" / "arm-key.json")["pairs"]

    def lines_of(stem: str) -> list[dict]:
        return [line for line in lines if Path(line["path"]).stem == stem]

    # A note both arms returned is one pair, and its view is the source vault's text.
    rewritten = lines_of(REWRITTEN_IN_SOURCE)
    assert any(len(key[line["pair_id"]]["ranks"]) == 2 for line in rewritten)
    assert {(line["title"], line["body"], line["missing"]) for line in rewritten} == {
        ("Source Vault Title", SOURCE_ONLY_BODY, False)
    }
    arm_excerpts = {
        r["snippet"]
        for arm in ("local", "cloud")
        for row in private.read_jsonl(root / "runs" / "all" / "capture" / f"{arm}.jsonl")
        for r in row["results"]
        if r["id"].endswith(f"{REWRITTEN_IN_SOURCE}.md")
    }
    assert arm_excerpts and None not in arm_excerpts  # the arms did send an excerpt for it
    shown = " ".join(f"{line['title']} {line['body']}" for line in lines)
    assert not [excerpt for excerpt in arm_excerpts if excerpt in shown]

    # A note only the local arm returned gets the same kind of view: frontmatter stripped.
    retry = lines_of("retry-with-full-jitter-backoff")
    assert any(set(key[line["pair_id"]]["ranks"]) == {"local"} for line in retry)
    original = next(source.rglob("retry-with-full-jitter-backoff.md")).read_text(encoding="utf-8")
    expected = original.split("\n---\n", 1)[1].strip()[: packet.BODY_CHARS].rstrip()
    assert {(line["title"], line["body"]) for line in retry} == {
        ("retry-with-full-jitter-backoff", expected)
    }

    # A note the source vault lacks is shown as missing, and counted.
    absent = lines_of(ABSENT_FROM_SOURCE)
    assert {(line["title"], line["body"], line["missing"]) for line in absent} == {
        (None, None, True)
    }
    counts = private.read_json(root / "runs" / "all" / "key" / "packet-manifest.json")
    assert counts["view"]["pairs_missing_from_vault"] == sum(line["missing"] for line in lines)
    assert counts["view"]["pairs_missing_from_vault"] == len(absent)
    assert counts["view"]["pairs_missing_from_vault_returned_by"] == {
        arm: sum(arm in key[line["pair_id"]]["ranks"] for line in absent)
        for arm in ("local", "cloud")
    }
    assert _tree_state(source) == source_before  # the source vault was only read

    # 4. score: the judge sees the packet only; the scorer reads the arm key
    judgments = tmp_path / "judgments.jsonl"
    private.write_jsonl(judgments, _judge(lines))
    assert (
        run(
            "score",
            "--run",
            "all",
            "--judgments",
            str(judgments),
            "--baseline",
            "local",
            "--candidate",
            "cloud",
        )
        == 0
    )
    [report_path] = (root / "runs" / "all").glob("score-*.json")
    report = private.read_json(report_path)

    assert report["coverage"]["scored"] == 4
    assert report["coverage"]["excluded"] == {}
    assert report["coverage"]["pairs_unjudged"] == 0
    captured = {
        arm: {
            row["case_id"]: [r["id"] for r in row["results"]]
            for row in private.read_jsonl(root / "runs" / "all" / "capture" / f"{arm}.jsonl")
        }
        for arm in ("local", "cloud")
    }
    grades_by_doc = {
        (line["query"], line["path"]): row["grade"]
        for line, row in zip(lines, _judge(lines), strict=True)
    }
    cases = {c["case_id"]: c["query"] for c in private.read_jsonl(root / "sample" / "cases.jsonl")}
    expected = []
    for row in report["per_case"]:
        query = cases[row["case_id"]]
        grades = {path: g for (q, path), g in grades_by_doc.items() if q == query}
        local = _hand_ndcg(captured["local"][row["case_id"]], grades)
        cloud = _hand_ndcg(captured["cloud"][row["case_id"]], grades)
        assert row["ndcg"] == pytest.approx({"local": local, "cloud": cloud})
        assert row["difference"] == pytest.approx(cloud - local)
        expected.append(cloud - local)
    # The cloud vault lacks the retry notes, so the retry query must come out behind.
    retry = next(
        r for r in report["per_case"] if cases[r["case_id"]] == "Retry backoff with jitter"
    )
    assert retry["ndcg"]["cloud"] == 0.0 < retry["ndcg"]["local"]
    low, high = report["headline"]["ci95"]
    assert report["headline"]["mean_difference"] == pytest.approx(sum(expected) / 4)
    assert low <= report["headline"]["mean_difference"] <= high
    assert report["latency"]["local"]["ok"] == report["latency"]["cloud"]["ok"] == 4

    # Neither token reached a file or a terminal; the arm key stays out of the judge directory.
    out = capsys.readouterr()
    artifacts = [p for p in root.rglob("*") if p.is_file()]
    for secret in (LOCAL_TOKEN, CLOUD_TOKEN):
        assert secret not in out.out + out.err
        assert not [p for p in artifacts if secret in p.read_text(encoding="utf-8")]
    assert {p.name for p in (root / "runs" / "all" / "judge").iterdir()} == {"packet.jsonl"}
    assert all(p.stat().st_mode & 0o077 == 0 for p in artifacts)


def test_an_unreachable_arm_stops_the_run_before_any_artifact_is_kept(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PARITY_LOCAL_TOKEN", LOCAL_TOKEN)
    root = tmp_path / "private"
    private.write_jsonl(
        root / "sample" / "cases.jsonl",
        [{"case_id": "q-1", "split": "heldout", "query": "retry backoff"}],
    )
    private.write_json(
        root / "sample" / "sample-manifest.json",
        {"seed": 1, "split_sizes": {"tuning": 0, "heldout": 1}},
    )

    code = cli.main(
        [
            "capture",
            "--private-dir",
            str(root),
            "--run",
            "r",
            "--split",
            "all",
            "--arm",
            "local",
            "http://127.0.0.1:9/mcp",
            "PARITY_LOCAL_TOKEN",
            "--arm",
            "cloud",
            "http://127.0.0.1:9/mcp",
            "PARITY_LOCAL_TOKEN",
            "--timeout",
            "2",
        ]
    )

    err = capsys.readouterr().err
    assert code == 2
    assert "cannot open a session" in err
    assert LOCAL_TOKEN not in err
    assert not (root / "runs" / "r" / "manifest.json").exists()
