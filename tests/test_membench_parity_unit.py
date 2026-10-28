"""recall_parity: the branches that carry the comparison's integrity.

The end-to-end workflow is proved in test_membench_parity_e2e.py. These cover
the logic that branches: which queries enter the sample, how a failed call is
recorded, what the scorer excludes, and the bootstrap interval. Every query
here is invented.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from recall_parity import capture, packet, private, sampling, scoring

from exomem import query_log


class _Hit:
    def as_dict(self) -> dict:
        return {"path": "Knowledge Base/Notes/a.md", "type": "pattern", "signals": {}}


def _write_log(directory: Path, rows: list[tuple[str, str]], monkeypatch) -> Path:
    """Write queries.jsonl with the product's own writer, so the shape is the producer's."""

    with monkeypatch.context() as patch:
        patch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        patch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
        patch.setenv("EXOMEM_LOG_DIR", str(directory))
        for query, outcome in rows:
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
                hits=[_Hit()],
                outcome=outcome,
                error_code=None if outcome == "success" else "FAILED",
                duration_ms=1.0,
            )
    return directory / "queries.jsonl"


def test_sampler_drops_only_by_structure_and_reports_each_drop(tmp_path, monkeypatch):
    log = _write_log(
        tmp_path / "a",
        [
            ("Retry backoff with jitter", "success"),
            ("retry   backoff with JITTER", "success"),  # same query after folding
            ("ok", "success"),  # shorter than min_chars
            ("?????????", "success"),  # long enough, no letter or digit
            ("Circuit breaker downstream failure", "success"),
            ("failed lookup of something", "error"),
            ("Autovacuum tuning for bloat", "success"),
            ("Percentage feature flag rollout", "success"),
            ("Release kill switch pattern", "success"),
            ("Idempotency key retries", "success"),
        ],
        monkeypatch,
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write('not json\n\n{"no_query": 1}\n')

    queries, read = sampling.read_distinct_queries([log], min_chars=8)

    assert read["counts"] == {
        "records_read": 12,
        "malformed": 2,
        "outcome_not_success": 1,
        "no_alphanumeric": 1,
        "too_short": 1,
        "duplicates": 1,
        "distinct_eligible": 6,
    }
    assert queries[0] == "Retry backoff with jitter"  # first spelling seen is the one kept


def test_sample_is_deterministic_in_seed_and_independent_of_file_order(tmp_path, monkeypatch):
    names = [
        "alpha beta gamma",
        "delta epsilon zeta",
        "eta theta iota",
        "kappa lambda mu",
        "nu xi omicron",
        "pi rho sigma",
    ]
    first = _write_log(tmp_path / "a", [(name, "success") for name in names[:3]], monkeypatch)
    second = _write_log(tmp_path / "b", [(name, "success") for name in names[3:]], monkeypatch)
    options = {"size": 4, "tuning_fraction": 0.5, "min_chars": 8}

    forward, manifest = sampling.build_sample([first, second], seed=3, **options)
    backward, _ = sampling.build_sample([second, first], seed=3, **options)
    other_seed, _ = sampling.build_sample([first, second], seed=4, **options)

    assert forward == backward
    assert [case["query"] for case in forward] != [case["query"] for case in other_seed]
    assert manifest["split_sizes"] == {"tuning": 2, "heldout": 2}
    assert len({case["case_id"] for case in forward}) == 4
    assert not any(
        name in json.dumps(manifest) for name in names
    )  # the manifest carries counts, never query text


def test_private_directory_outside_every_work_tree_is_allowed_and_artifacts_are_immutable(
    tmp_path,
):
    assert private.private_root(tmp_path / "not-yet-created" / "run") == (
        tmp_path / "not-yet-created" / "run"
    )

    private.write_json(tmp_path / "once.json", {})
    with pytest.raises(FileExistsError):
        private.write_json(tmp_path / "once.json", {})
    assert (tmp_path / "once.json").stat().st_mode & 0o077 == 0


def test_private_directory_inside_another_work_tree_must_be_git_ignored(tmp_path):
    repo = tmp_path / "other-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (repo / "src").mkdir()

    # The rule lives in that repository's .gitignore; this code keeps no copy of it.
    assert private.private_root(repo / "scratch") == repo / "scratch"  # not created yet
    assert private.private_root(repo / "scratch" / "run" / "deeper")
    for tracked_place in (repo, repo / "src", repo / "not-ignored" / "run"):
        with pytest.raises(private.PrivateDirError, match="not git-ignored"):
            private.private_root(tracked_place)


class _Client:
    """The two things ``ask`` needs from an MCP client: ``call_tool`` and its result shape."""

    def __init__(self, outcome):
        self.outcome = outcome

    async def call_tool(self, name, arguments, *, raise_on_error, timeout):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome == "hang":
            await asyncio.sleep(5)
        return self.outcome


def _answer(*, structured=None, is_error=False, text=None):
    content = [SimpleNamespace(text=text)] if text else []
    return SimpleNamespace(is_error=is_error, structured_content=structured, content=content)


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        (RuntimeError("upstream said secret-token-value is bad"), "transport"),
        ("hang", "timeout"),
        (_answer(is_error=True, text="boom secret-token-value"), "tool_error"),
        (_answer(structured={"result": {"warming": True, "hits": []}}), "warming"),
        (_answer(structured={"success": False, "error": {"code": "DENIED"}}), "refused"),
        (_answer(structured={"result": "unexpected"}), "malformed"),
    ],
)
def test_a_failed_call_is_recorded_with_its_kind_and_never_with_the_token(outcome, kind):
    row = asyncio.run(
        capture.ask(
            _Client(outcome),
            "search_memory",
            {"query": "q"},
            timeout_s=0.05,
            secrets=["secret-token-value"],
        )
    )

    assert row["ok"] is False
    assert row["error"]["kind"] == kind
    assert "secret-token-value" not in json.dumps(row)
    assert row["results"] == []
    assert row["latency_ms"] >= 0


def test_ranked_results_identify_documents_by_path_and_count_what_they_skip():
    hits = [
        {"path": "a.md", "title": "A", "excerpt": "first"},
        {"path": "a.md", "title": "A", "excerpt": "again"},
        {"title": "no path"},
        {"path": "b.md"},
    ]

    results, dropped, repeated = capture.ranked_results(hits)

    assert [(r["rank"], r["id"], r["snippet"]) for r in results] == [
        (1, "a.md", "first"),
        (2, "b.md", None),
    ]
    assert (dropped, repeated) == (1, 1)


def test_warm_up_calls_send_a_fixed_query_and_never_a_sampled_one(monkeypatch):
    class Recorder:
        def __init__(self):
            self.queries = []

        async def call_tool(self, name, arguments, *, raise_on_error, timeout):
            self.queries.append(arguments["query"])
            return _answer(structured=[])

    clients = {}

    async def open_session(stack, arm, token, **options):
        clients[arm.name] = Recorder()
        return clients[arm.name], "schema-digest"

    monkeypatch.setattr(capture, "_open_session", open_session)
    arms = [
        capture.Arm("a", "http://a.invalid/mcp", "A"),
        capture.Arm("b", "http://b.invalid/mcp", "B"),
    ]
    cases = [{"case_id": "q-1", "query": "first sampled"}, {"case_id": "q-2", "query": "second"}]

    asyncio.run(
        capture.capture_arms(
            arms,
            {"a": "token-a", "b": "token-b"},
            cases,
            tool="search_memory",
            arguments={},
            timeout_s=1.0,
            warmup=1,
            record=lambda arm, row: None,
        )
    )

    sampled = [case["query"] for case in cases]
    for recorder in clients.values():
        warm_up, *measured = recorder.queries
        assert warm_up not in sampled  # a sampled query would prime the cache for its own case
        assert measured == sampled


def _note(vault: Path, path: str, text: str) -> None:
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(text, encoding="utf-8")


def test_judge_view_takes_the_frontmatter_title_and_a_bounded_body_without_frontmatter(tmp_path):
    vault = tmp_path / "vault"
    _note(
        vault, "Notes/titled.md", "---\ntitle: A  Chosen Title\ntype: pattern\n---\n\nbody line\n"
    )
    _note(vault, "Notes/long-note.md", "---\ntype: pattern\n---\n" + "word " * 1000)

    titled = packet.document_view(vault.resolve(), "Notes/titled.md")
    long = packet.document_view(vault.resolve(), "Notes/long-note.md")

    assert titled == {
        "title": "A  Chosen Title",
        "body": "body line",
        "body_truncated": False,
        "missing": False,
    }
    assert long["title"] == "long-note"  # no frontmatter title: the file name
    assert len(long["body"]) <= packet.BODY_CHARS
    assert long["body"].startswith("word word") and "type: pattern" not in long["body"]
    assert long["body_truncated"] is True


def test_judge_view_marks_a_path_the_vault_does_not_hold_as_missing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    view = packet.document_view(vault.resolve(), "Notes/absent.md")

    assert view == {"title": None, "body": None, "body_truncated": False, "missing": True}


@pytest.mark.parametrize("how", ["parent-segment", "absolute", "symlink-out"])
def test_judge_view_refuses_a_path_that_leaves_the_vault(tmp_path, how):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside text", encoding="utf-8")
    (vault / "link.md").symlink_to(outside)
    path = {
        "parent-segment": "../outside.md",
        "absolute": str(outside),
        "symlink-out": "link.md",
    }[how]

    with pytest.raises(ValueError, match="leaves the source vault"):
        packet.document_view(vault.resolve(), path)


def test_bootstrap_interval_on_known_inputs():
    # Two values resample to a mean of 0, 0.5 or 1 with probability 1/4, 1/2, 1/4, so
    # the 2.5% and 97.5% points are exactly the two values, whatever the seed.
    assert scoring.percentile_bootstrap_ci([0.0, 1.0], seed=1) == (0.0, 1.0)
    assert scoring.percentile_bootstrap_ci([0.25] * 7, seed=1) == (0.25, 0.25)

    skewed = [-1.0, 0.0, 0.0, 0.1, 0.2, 0.2, 0.3]
    low, high = scoring.percentile_bootstrap_ci(skewed, seed=9)
    assert (low, high) == scoring.percentile_bootstrap_ci(skewed, seed=9)
    assert min(skewed) <= low < sum(skewed) / len(skewed) < high <= max(skewed)


def _scored_run(tmp_path: Path) -> tuple[Path, Path]:
    """A run whose nDCG@10 values are known by hand.

    Each judged case has one relevant document graded 2 (gain 3, ideal DCG 3), so an
    arm's nDCG is 1 / log2(position + 1): position 1 scores 1.0 and position 3 scores 0.5.
    """

    run = tmp_path / "runs" / "r"
    cases = ["c1", "c2", "c3", "c4", "c5", "c6"]
    (run / "capture").mkdir(parents=True)
    (run / "key").mkdir()
    (run / "manifest.json").write_text(json.dumps({"case_ids": cases}), encoding="utf-8")

    def row(case, ids, latency, *, error=None):
        return {
            "case_id": case,
            "ok": error is None,
            "latency_ms": latency,
            "error": {"kind": error, "detail": None} if error else None,
            "results": [
                {"rank": i + 1, "id": d, "title": None, "snippet": None} for i, d in enumerate(ids)
            ],
        }

    local = [
        row("c1", ["a"], 10),
        row("c2", ["x", "y", "a"], 20),
        row("c3", ["a"], 30),
        row("c4", ["a"], 40),
        row("c5", ["n"], 50),
        row("c6", ["m"], 60),
    ]
    cloud = [
        row("c1", ["x", "y", "a"], 100),
        row("c2", ["a"], 200),
        row("c3", [], 0, error="timeout"),
        row("c4", ["a", "z"], 300),  # z is returned but never judged
        row("c5", ["n"], 400),
        row("c6", ["m"], 500),
    ]
    for arm, rows in (("local", local), ("cloud", cloud)):
        text = "".join(json.dumps(r) + "\n" for r in rows)
        (run / "capture" / f"{arm}.jsonl").write_text(text, encoding="utf-8")

    pairs = {
        pair_id: {"case_id": pair_id[2:4], "id": pair_id[4:], "ranks": {}}
        for pair_id in (
            "p-c1a",
            "p-c1x",
            "p-c1y",
            "p-c2a",
            "p-c2x",
            "p-c2y",
            "p-c3a",
            "p-c4a",
            "p-c4z",
            "p-c5n",
            "p-c6m",
        )
    }
    key = {"k": 10, "seed": 7, "arms": ["local", "cloud"], "pairs": pairs}
    (run / "key" / "arm-key.json").write_text(json.dumps(key), encoding="utf-8")

    graded = [
        ("p-c1a", 2),
        ("p-c1x", 0),
        ("p-c1y", 0),
        ("p-c2a", 2),
        ("p-c2x", 0),
        ("p-c2y", 0),
        ("p-c3a", 2),
        ("p-c4a", 2),
        ("p-c5n", 0),  # p-c4z is not judged
        ("p-c1a", 2),  # the same grade twice is harmless
        ("p-c6m", 1),
        ("p-c6m", 2),
    ]  # two different grades cancel each other
    rows = [{"pair_id": pair, "grade": grade} for pair, grade in graded]
    rows += [
        {"pair_id": "p-unknown", "grade": 1},  # no such pair
        {"pair_id": "p-c5n", "grade": 3},  # not a grade
        {"pair_id": "p-c5n", "grade": True},  # a boolean is not a grade
    ]
    judgments = tmp_path / "judgments.jsonl"
    judgments.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path, judgments


def test_scorer_reports_exclusions_and_counts_an_arm_error_as_zero(tmp_path):
    root, judgments = _scored_run(tmp_path)

    report = scoring.score_run(root, "r", judgments, baseline="local", candidate="cloud")

    assert report["coverage"] == {
        "cases": 6,
        "scored": 3,
        "cases_with_arm_error": 1,
        # c4 returns an unjudged pair, c6's pair has contradictory grades, c5 has nothing relevant
        "excluded": {"no_relevant_in_pool": 1, "unjudged": 2},
        "pairs": 11,
        "pairs_unjudged": 2,
        "judgement_rows": {"rows": 15, "malformed": 2, "unknown_pair": 1, "conflicting_pairs": 1},
    }
    by_case = {row["case_id"]: row for row in report["per_case"]}
    assert by_case["c1"]["ndcg"] == pytest.approx({"local": 1.0, "cloud": 0.5})
    assert by_case["c2"]["ndcg"] == pytest.approx({"local": 0.5, "cloud": 1.0})
    assert by_case["c3"]["ndcg"] == pytest.approx({"local": 1.0, "cloud": 0.0})
    assert by_case["c3"]["errored_arms"] == ["cloud"]
    assert [by_case[c]["difference"] for c in ("c1", "c2", "c3")] == pytest.approx(
        [-0.5, 0.5, -1.0]
    )

    headline = report["headline"]
    assert headline["cases"] == 3
    assert headline["mean"] == pytest.approx({"local": 5 / 6, "cloud": 0.5})
    assert headline["mean_difference"] == pytest.approx(-1 / 3)
    assert -1.0 <= headline["ci95"][0] <= -1 / 3 <= headline["ci95"][1] <= 0.5
    # Without the errored case the difference averages to zero, and two cases bound it exactly.
    assert report["excluding_cases_with_arm_error"]["mean_difference"] == pytest.approx(0.0)
    assert report["excluding_cases_with_arm_error"]["ci95"] == pytest.approx([-0.5, 0.5])

    assert report["latency"]["local"]["p50_ms"] == pytest.approx(35.0)
    assert report["latency"]["local"]["p95_ms"] == pytest.approx(57.5)
    assert report["latency"]["cloud"]["errors"] == {"timeout": 1}
    assert report["latency"]["cloud"]["p50_ms"] == pytest.approx(300.0)  # five successful calls
    assert report["latency"]["cloud"]["p95_ms"] == pytest.approx(480.0)


def test_scorer_refuses_arm_names_the_run_does_not_have(tmp_path):
    root, judgments = _scored_run(tmp_path)

    with pytest.raises(ValueError, match="two arms"):
        scoring.score_run(root, "r", judgments, baseline="local", candidate="elsewhere")
