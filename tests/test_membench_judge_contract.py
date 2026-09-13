"""Judge-layer contract: default OFF, blind by construction, gates FINAL.

Lean and offline — no model, no network, no corpus generation. Run dirs are
built by hand mimicking runner.py's exact artifact layout.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from membench.judge import (
    ClaudeCliBackend,
    HandshakeRequest,
    LeakageError,
    NoneBackend,
    OpenAICompatBackend,
    RequestItem,
    collect_responses,
    default_backend,
    deterministic_permutation,
    leakage_scan,
    load_requests,
    make_judge_item,
    normalize_for_judge,
    structural_leakage_scan,
    write_requests,
)
from membench.judge.blinding import NEUTRAL_SYSTEM_TOKEN, BlindingMap
from membench.reporting import (
    GATE_CONFLICT_NOTE,
    build_comparison_report,
    merge_judge_scores,
)
from membench.runner import MembenchResultManifest
from protocol.contracts import derive_preregistration_identity

from exomem.vault import yaml_scalar

_DIMENSIONS_OK = {
    "factual_qa": {"pass": 1, "fail": 0, "not_applicable": 0, "unsupported": 0},
    "governance": {"pass": 0, "fail": 1, "not_applicable": 0, "unsupported": 0},
    "_run": {"failures": 0, "queries_scored": 1},
}

_PER_QUERY_OK = [
    {
        "query_id": "Q-0001",
        "status": "ok",
        "gates": [
            {"gate": "value", "dimension": "factual_qa", "status": "pass", "evidence": None},
            {
                "gate": "no_leak",
                "dimension": "governance",
                "status": "fail",
                "evidence": "leaked: ['w']",
            },
        ],
        "retrieval": {
            "relevant": ["S-1"],
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "mrr": 1.0,
            "first_relevant_rank": 1,
            "hit_count": 3,
        },
    }
]


def _make_run_dir(
    tmp_path: Path,
    run_id: str,
    *,
    provider: str = "provider-one",
    profile: str = "lexical",
    invalid: bool = False,
    invalid_reason: str | None = None,
    dimensions: dict | None = None,
    per_query: list | None = None,
    latencies: tuple[float, ...] = (10.0, 20.0, 30.0),
) -> Path:
    """Fake run dir with runner.py's exact filenames and shapes."""

    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    identity = derive_preregistration_identity(
        Path(__file__).resolve().parents[1],
        contract_revision="7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f",
    )
    manifest = MembenchResultManifest(
        run_id=run_id,
        status="INVALID" if invalid else "VALID",
        preregistration_identity=identity,
        provider=provider,
        profile={"name": profile, "settings": {}},
        top_k=10,
        corpus_dir="corpus/s1",
        # Wired keeps governance dimensions comparable in these fixtures
        # (matching runner.py, which always records governance_state); the
        # default-open exclusion path has its own tests in the wiring suite.
        governance_state="wired",
        started_utc="20260101T000000Z",
        ended_utc="20260101T000001Z",
        invalid=invalid,
        invalid_reason=invalid_reason,
        environment_verification={"status": "unverified"},
        retrieval_floor={"status": "not_applicable"},
        ingestion_altitude="raw_source",
        answer_mode="harness",
        run_failures=0,
    )
    (run_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "failures.jsonl").write_text("", encoding="utf-8")
    if not invalid:
        (run_dir / "deterministic-scores.json").write_text(
            json.dumps(
                {
                    "dimensions": dimensions if dimensions is not None else _DIMENSIONS_OK,
                    "per_query": per_query if per_query is not None else _PER_QUERY_OK,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with (run_dir / "retrieval.jsonl").open("w", encoding="utf-8") as handle:
            for index, latency in enumerate(latencies):
                handle.write(
                    json.dumps(
                        {"query_id": f"Q-{index:04d}", "latency_ms": latency, "hits": []},
                        sort_keys=True,
                    )
                    + "\n"
                )
        (run_dir / "answers.jsonl").write_text(
            json.dumps({"query_id": "Q-0001", "answer_text": "w", "citations": []}) + "\n",
            encoding="utf-8",
        )
    return run_dir


def _clean_judge_items(count: int = 1) -> list[RequestItem]:
    blinding = BlindingMap.mint(["provider-one", "provider-two"], seed="run-42")
    token = blinding.token_for("provider-one")
    return [
        make_judge_item(
            f"Q-{index:04d}",
            question="When did the deadline move?",
            expected_summary="It moved to week nine.",
            candidate_answer="The deadline moved to week nine.",
            provider_token=token,
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------- default off


def test_default_backend_is_none_and_flow_needs_no_judge_artifacts(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-plain")
    backend = default_backend()
    assert isinstance(backend, NoneBackend)
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=3)
    assert outcome.status == "not_run"
    assert "judge: not run" in outcome.note
    assert not (run_dir / "judge-requests").exists()
    assert not (run_dir / "judge-responses").exists()
    assert not (run_dir / "judge-scores.json").exists()

    out = build_comparison_report([run_dir], tmp_path / "compare.md")
    report = out.read_text(encoding="utf-8")
    assert "factual_qa" in report
    assert "Judge: not run" in report
    assert "gate_conflict" not in report


# ------------------------------------------------------------------- blinding


def test_serialized_requests_are_blind_grep_the_bytes(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-blind")
    blinding = BlindingMap.mint(["provider-one", "provider-two"], seed="run-42")
    item = make_judge_item(
        "Q-0001",
        question=(
            "What does [ref:SRC-AAAA1111] say about the deadline in "
            "Knowledge Base/Notes/plan-notes.md?"
        ),
        expected_summary=(
            "Per SRC-AAAA1111 the deadline moved; see exomem://notes/plan "
            "and roadmap.md for details."
        ),
        candidate_answer=(
            "Exomem and basic-memory (also mem0, GrayBox) report the deadline "
            "moved, per [ref:SRC-AAAA1111]."
        ),
        provider_token=blinding.token_for("provider-one"),
    )
    batch = write_requests(run_dir, "judge", [item], samples=2, seed="run-42")

    raw = batch.read_bytes().decode("utf-8")
    lowered = raw.lower()
    for forbidden in (
        "exomem",  # covers exomem:// refs too
        "mem0",
        "graybox",
        "basic-memory",
        "basic memory",
        "basic_memory",
        "[ref:",
        "src-",
        "knowledge base",
        "knowledge_base",
        ".md",
    ):
        assert forbidden not in lowered, f"provider-identifying bytes leaked: {forbidden!r}"

    # Blinded shape is present: neutral tokens + a system token, nothing raw.
    assert "[ctx:1]" in raw
    assert '"blinded_provider_token": "system-' in raw

    # Stable per-source numbering within one request: the same source id maps
    # to the same [ctx:N] in question, expected summary, and candidate answer.
    payload = json.loads(raw.splitlines()[0])["payload"]
    tokens_q = set(re.findall(r"\[ctx:\d+\]", payload["question"]))
    tokens_e = set(re.findall(r"\[ctx:\d+\]", payload["expected_summary"]))
    tokens_c = set(re.findall(r"\[ctx:\d+\]", payload["candidate_answer"]))
    assert "[ctx:1]" in tokens_q and "[ctx:1]" in tokens_e and "[ctx:1]" in tokens_c


def test_leakage_scan_catches_leaks_and_writer_refuses(tmp_path: Path) -> None:
    leaks = leakage_scan(
        "Answer per [ref:SRC-BBBB2222] in Knowledge Base/x and exomem://n/p via mem0."
    )
    assert leaks, "planted leaks must be detected"
    joined = " ".join(leaks).lower()
    for expected in ("[ref:", "src-bbbb2222", "knowledge base/", "exomem", "mem0"):
        assert expected in joined

    assert leakage_scan("The deadline moved to week nine per [ctx:1].") == []

    run_dir = _make_run_dir(tmp_path, "run-leaky")
    leaky = RequestItem(
        item_id="Q-0001",
        blinded_provider_token="system-A",
        payload={"prompt": "grade this: exomem said so, see [ref:SRC-BBBB2222]"},
    )
    with pytest.raises(LeakageError) as excinfo:
        write_requests(run_dir, "judge", [leaky], samples=2, seed="s")
    assert "Q-0001" in str(excinfo.value)
    assert not (run_dir / "judge-requests").exists(), "refusal must write nothing"


# --------------------------------------------- structural blinding (task 4b.18)
#
# Token blinding alone loses to schema shape: a human recognised the vendor at a
# glance across 84 documents while `leakage_scan` returned zero hits, because the
# frontmatter KEY SET names the product even when every value is neutral. The two
# shapes below are copied key-for-key, in order, from the producers that actually
# emit them in this repo -- no invented convenience shape.

_SWAP_TITLE = "Quarterly deadline moved"
#: A title that splits its own frontmatter line. `SourceRecord.title` is an
#: unconstrained `str` (`membench/schema.py:229`) and `basic_memory.py:158`
#: interpolates it unescaped, so this reaches a rendered note as written.
_NEWLINE_TITLE = "Quarterly deadline\nmoved to week nine"
_SWAP_BODY = (
    "The deadline moved to week nine after the review board met.\n"
    "Two teams confirmed the new date in writing.\n"
)


def _exomem_shaped(title: str = _SWAP_TITLE, body: str = _SWAP_BODY) -> str:
    """One payload in exomem's native metadata shape.

    Producer: ``src/exomem/add.py`` source-page writer (:639-671) -- the key run
    ``type / exomem_id / title / source_type / captured / tags / ingested_into``
    inside a ``---`` fence. This is exactly the key list task 4b.18 names.
    """

    return (
        "---\n"
        "type: source\n"
        "exomem_id: 6b0f2a41-6f0e-4a1a-9a2e-2c9b7f5d1e33\n"
        # `add.py:642` renders the title through `yaml_scalar`, so a title
        # carrying YAML-special characters arrives quoted here while the other
        # producer writes it bare. Called, not imitated -- the fixture has to be
        # what the producer emits.
        f"title: {yaml_scalar(title.strip())}\n"
        "source_type: other\n"
        "captured: 2026-01-14\n"
        "tags: [bench]\n"
        "ingested_into: []\n"
        "---\n"
        "\n"
        f"{body}"
    )


def _basic_memory_shaped(title: str = _SWAP_TITLE, body: str = _SWAP_BODY) -> str:
    """The same payload in the other contender's native metadata shape.

    Producer: ``benchmarks/membench/native/basic_memory.py`` (:78-84 conclusion
    notes, :135-141 entity notes, :155-161 source notes) -- the key run
    ``title / type / permalink / tags`` inside a ``---`` fence.
    """

    return (
        "---\n"
        f"title: {title}\n"
        "type: note\n"
        "permalink: quarterly-deadline-moved\n"
        "tags: [bench]\n"
        "---\n"
        "\n"
        f"{body}"
    )


def _scrubbed_exomem_shaped(title: str = _SWAP_TITLE, body: str = _SWAP_BODY) -> str:
    """The exomem shape as it survives today's TOKEN scrubber.

    ``normalize_for_judge`` rewrites the product name inside the ``exomem_id:``
    key, so the key reaches the judge as ``[system]_id:``. That residue carries
    zero token hits while still advertising both the vendor key run and the fact
    that a product-named field was scrubbed -- 4b.18's second sentence.
    """

    return _exomem_shaped(title, body).replace(
        "exomem_id:", f"{NEUTRAL_SYSTEM_TOKEN}_id:"
    )


_TOKEN_LEAK_NEEDLES = (
    "exomem",
    "basic-memory",
    "basic memory",
    "basic_memory",
    "mem0",
    "graybox",
    "[ref:",
    "src-",
    "knowledge base/",
    ".md",
)


def test_frontmatter_structure_swap_is_byte_identical_after_normalization() -> None:
    """The failure predicate of the structure-swap test, on the key-run axis.

    One identical semantic payload, each vendor's native FRONTMATTER KEY RUN:
    the frontmatter bytes that reach the judge must be identical. Byte identity
    is what proves "no classifier distinguishes them" on this axis -- a sampled
    classifier could only fail to find the tell.

    Scope is the key run, and the fixtures hold the body constant to say so
    honestly. The producers also differ in body scaffolding (`## Capture` vs
    `## Observations`), which this module does not normalize; that axis is the
    named remainder in `blinding.py`'s docstring, not something this test
    quietly claims to have closed.
    """

    from_exomem = normalize_for_judge(_exomem_shaped())
    from_basic_memory = normalize_for_judge(_basic_memory_shaped())

    assert from_exomem == from_basic_memory, (
        "structure swap is attributable: identical content in two native shapes "
        f"reaches the judge as different bytes\n--- exomem ---\n{from_exomem!r}\n"
        f"--- basic-memory ---\n{from_basic_memory!r}"
    )

    # ... and the shape both collapsed into is the ONE canonical block: fence,
    # the fixed neutral key set in its fixed order, fence. Dropping the fences
    # would still be "identical", and identically wrong -- a bare `title:` line
    # left in running prose is not a normalized frontmatter block.
    assert from_exomem.splitlines()[:3] == ["---", f"title: {_SWAP_TITLE}", "---"], (
        f"canonical block is not fence-delimited: {from_exomem.splitlines()[:3]!r}"
    )

    # ... and normalization must not have achieved identity by deleting the
    # payload: the semantic content the judge grades survives.
    assert _SWAP_TITLE in from_exomem
    assert "The deadline moved to week nine" in from_exomem
    assert "Two teams confirmed the new date in writing." in from_exomem

    # ... and it must hold for a title the two producers spell differently.
    # `yaml_scalar` quotes a title containing YAML-special characters; the
    # other producer never quotes. Real corpus titles do contain colons, so
    # quoting is a shape the swap has to survive, not an edge case.
    # A title that itself carries quotes is DOUBLE-wrapped by `yaml_scalar` and
    # written bare by the other producer, so one unwrap is not enough: it leaves
    # exomem still quoted and basic-memory bare, which is a one-way tell.
    # `"Quarterly"` is the load-bearing one: it is wholly wrapped, so
    # `yaml_scalar` double-wraps it and a single unwrap leaves exomem at
    # `"Quarterly"` while basic-memory reaches `Quarterly`.
    for quoted_title in ("Deadline: moved to week nine", '"Quarterly"'):
        from_exomem = normalize_for_judge(_exomem_shaped(quoted_title))
        from_basic_memory = normalize_for_judge(_basic_memory_shaped(quoted_title))
        assert f"title: {quoted_title}" in _basic_memory_shaped(quoted_title)
        assert f"title: {quoted_title}\n" not in _exomem_shaped(quoted_title), (
            f"{quoted_title!r} is not a case yaml_scalar quotes; it proves nothing"
        )
        assert from_exomem == from_basic_memory, (
            "producer-specific quoting of one title is still an attributable "
            f"shape\n--- exomem ---\n{from_exomem!r}\n"
            f"--- basic-memory ---\n{from_basic_memory!r}"
        )

    # ... and unwrapping stops at a fixed point rather than eating quotes that
    # are content: this title is not surrounded by a matched pair.
    content_quotes = 'He said "go": deadline moved'
    assert content_quotes in normalize_for_judge(_exomem_shaped(content_quotes))


def test_leakage_scan_flags_vendor_frontmatter_structure() -> None:
    """Structure is a leak with zero token hits -- the whole of 4b.18."""

    for label, shaped in (
        ("basic-memory", _basic_memory_shaped()),
        ("exomem (token-scrubbed)", _scrubbed_exomem_shaped()),
        # `basic_memory.py:158` writes `title: {source.title}` unescaped over an
        # unconstrained `SourceRecord.title` (`schema.py:229`), so a newline in
        # a title splits the key run. Bounded by the fence, the run survives it.
        ("basic-memory (title carries a newline)", _basic_memory_shaped(_NEWLINE_TITLE)),
    ):
        lowered = shaped.lower()
        for needle in _TOKEN_LEAK_NEEDLES:
            assert needle not in lowered, (
                f"{label} fixture must carry ZERO token hits, found {needle!r}"
            )
        assert leakage_scan(shaped), (
            f"{label} frontmatter key run identifies the vendor and went undetected"
        )


def test_every_registered_structural_signature_is_load_bearing() -> None:
    """Each registered signature catches a key run no other signature covers.

    On a whole page the registry overlaps on purpose -- an exomem source block
    satisfies several entries at once -- so a whole-page fixture cannot show
    that any one entry earns its place. A quoted excerpt can, and an excerpt is
    what a candidate answer quoting part of a page actually contains. Each run
    below is a contiguous slice of its producer's block, and the assertion is
    exact equality: one marker, so exactly one registered signature fired.
    """

    cases = (
        # `src/exomem/add.py` :640, :643-648 -- the block minus its id/title/tags
        (
            "exomem-source-frontmatter",
            "type: source\n"
            "source_type: other\n"
            "captured: 2026-01-14\n"
            "ingested_into: []\n",
        ),
        # `src/exomem/add.py` :641 alone
        ("exomem-id-key", "exomem_id: 6b0f2a41-6f0e-4a1a-9a2e-2c9b7f5d1e33\n"),
        # ... and the same line once the token scrubber has been over it
        (
            "system-id-residue",
            f"{NEUTRAL_SYSTEM_TOKEN}_id: 6b0f2a41-6f0e-4a1a-9a2e-2c9b7f5d1e33\n",
        ),
        # `benchmarks/membench/native/basic_memory.py` :135-140, fences dropped
        (
            "basic-memory-note-frontmatter",
            "title: Quarterly deadline moved\n"
            "type: note\n"
            "permalink: quarterly-deadline-moved\n"
            "tags: [bench]\n",
        ),
        # ... and the same excerpt indented, which is how a candidate answer
        # quoting a document normally carries it. A key-line rule anchored hard
        # at column 0 would miss every indented quotation of a vendor block.
        (
            "basic-memory-note-frontmatter",
            "    title: Quarterly deadline moved\n"
            "    type: note\n"
            "    permalink: quarterly-deadline-moved\n"
            "    tags: [bench]\n",
        ),
    )
    for signature, excerpt in cases:
        assert structural_leakage_scan(excerpt) == [f"structure:{signature}"], (
            f"{signature} is not the one signature this excerpt matches: "
            f"{structural_leakage_scan(excerpt)}"
        )
        assert not structural_leakage_scan(normalize_for_judge(excerpt)), (
            f"{signature} survives normalization"
        )


def test_system_id_residue_cannot_survive() -> None:
    """`[system]_id:` is itself a fingerprint; the fix removes the key, not the token."""

    out = normalize_for_judge(_exomem_shaped())
    assert "exomem_id" not in out
    assert f"{NEUTRAL_SYSTEM_TOKEN}_id" not in out
    for key in ("source_type:", "captured:", "ingested_into:", "permalink:"):
        assert key not in out, f"vendor-identifying key {key!r} reached the judge"
    assert leakage_scan(out) == [], f"normalized output still leaks: {leakage_scan(out)}"

    # D4 binds on every vendor-identifying key, so a title that splits its own
    # key run must not carry `permalink:` past the gate either. Reachable:
    # `basic_memory.py:158` interpolates an unconstrained title unescaped.
    split = normalize_for_judge(_basic_memory_shaped(_NEWLINE_TITLE))
    for key in ("permalink:", "tags:", "type:"):
        assert key not in split, (
            f"a newline in a title let {key!r} walk past the gate:\n{split!r}"
        )
    assert leakage_scan(split) == [], f"normalized output still leaks: {leakage_scan(split)}"


def test_write_requests_refuses_structural_leak_before_writing(tmp_path: Path) -> None:
    """The gate binds at the writer: a structural leak is refused fail-closed."""

    run_dir = _make_run_dir(tmp_path, "run-structural")
    leaky = RequestItem(
        item_id="Q-0002",
        blinded_provider_token="system-A",
        payload={"prompt": "grade this excerpt:\n\n" + _scrubbed_exomem_shaped()},
    )
    with pytest.raises(LeakageError) as excinfo:
        write_requests(run_dir, "judge", [leaky], samples=2, seed="s")
    assert "Q-0002" in str(excinfo.value)
    assert not (run_dir / "judge-requests").exists(), "refusal must write nothing"


def test_structural_scan_runs_on_serialized_request_bytes() -> None:
    """Pin: the structural check reads the EXACT serialized line, escapes and all.

    ``write_requests`` scans ``json.dumps(request.model_dump(), ...)`` -- one
    physical line in which every document newline is the two-character escape
    ``\\n``. A structural check that only understands real newlines would see no
    key run there and the gate would pass a leak through.
    """

    request = HandshakeRequest(
        request_id="Q-0003",
        sample_index=0,
        blinded_provider_token="system-A",
        payload={"prompt": "grade this excerpt:\n\n" + _scrubbed_exomem_shaped()},
    )
    line = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
    assert "\n" not in line, "the writer scans one serialized line, not raw text"
    assert "\\n" in line, "document newlines reach the scan JSON-escaped"

    assert leakage_scan(line), (
        "structural scan missed the vendor key run in the serialized request line"
    )


# -------------------------------------------------------------- order shuffle


def test_order_shuffle_deterministic_per_seed(tmp_path: Path) -> None:
    perm = deterministic_permutation(16, "alpha")
    assert perm == deterministic_permutation(16, "alpha")
    assert sorted(perm) == list(range(16))
    assert perm != deterministic_permutation(16, "beta")

    items = _clean_judge_items(count=4)
    run_a = _make_run_dir(tmp_path, "run-ord-a")
    run_b = _make_run_dir(tmp_path, "run-ord-b")
    run_c = _make_run_dir(tmp_path, "run-ord-c")
    bytes_a = write_requests(run_a, "judge", items, samples=2, seed="alpha").read_bytes()
    bytes_b = write_requests(run_b, "judge", items, samples=2, seed="alpha").read_bytes()
    bytes_c = write_requests(run_c, "judge", items, samples=2, seed="beta").read_bytes()
    assert bytes_a == bytes_b, "same seed must serialize identically"

    def order(raw: bytes) -> list[tuple[str, int]]:
        return [
            (row["request_id"], row["sample_index"])
            for row in map(json.loads, raw.decode("utf-8").splitlines())
        ]

    assert order(bytes_a) != order(bytes_c), "different seed must reorder"
    assert sorted(order(bytes_a)) == sorted(order(bytes_c)), "same requests either way"


# ------------------------------------------- N samples, collect, denominators


def _verdict(match: bool, quality: int, reason: str = "because") -> str:
    return json.dumps(
        {"semantic_match": match, "explanation_quality": quality, "reason": reason}
    )


def test_sample_expansion_collect_and_merge_keep_denominators(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "run-samples")
    items = _clean_judge_items(count=2)  # Q-0000, Q-0001
    write_requests(run_dir, "judge", items, samples=3, seed="s5")

    requests = load_requests(run_dir, "judge")
    assert len(requests) == 6
    by_id: dict[str, set[int]] = {}
    for request in requests:
        by_id.setdefault(request.request_id, set()).add(request.sample_index)
    assert by_id == {"Q-0000": {0, 1, 2}, "Q-0001": {0, 1, 2}}

    responses_dir = run_dir / "judge-responses"
    responses_dir.mkdir()
    lines = [
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 0,
                "model_id": "judge-model-x",
                "response": _verdict(True, 4),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 1,
                "model_id": "judge-model-x",
                "response": _verdict(True, 5),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0000",
                "sample_index": 2,
                "model_id": "judge-model-x",
                "response": _verdict(False, 3),
            }
        ),
        json.dumps(
            {
                "request_id": "Q-0001",
                "sample_index": 0,
                "model_id": "judge-model-x",
                "response": _verdict(True, 2),
            }
        ),
        "{this is not json",  # malformed response LINE
        json.dumps(
            {
                "request_id": "Q-0001",
                "sample_index": 2,
                "model_id": "judge-model-x",
                "response": "I think it matches, quality four.",  # invalid verdict
            }
        ),
        # Q-0001 sample 1 never gets a valid response -> missing
    ]
    (responses_dir / "batch-001.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )

    paired, stats = collect_responses(run_dir, "judge")
    assert stats["requests"] == 6
    assert stats["paired"] == 5
    assert stats["malformed"] == 1
    assert stats["missing_response"] == 1

    failures = [
        json.loads(line)
        for line in (run_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(1 for f in failures if "malformed" in f["detail"]) == 1
    assert sum(1 for f in failures if "missing response" in f["detail"]) == 1

    out = merge_judge_scores(run_dir, paired)
    scores = json.loads(out.read_text(encoding="utf-8"))
    rows = {row["query_id"]: row for row in scores["per_query"]}

    q0 = rows["Q-0000"]
    assert q0["samples_total"] == 3 and q0["samples_valid"] == 3
    assert q0["mean"] == pytest.approx(4.0)
    assert q0["stdev"] == pytest.approx(1.0)
    assert q0["majority"] is True  # 2 of 3 matched
    assert [s["sample_index"] for s in q0["samples"]] == [0, 1, 2]

    q1 = rows["Q-0001"]
    assert q1["samples_total"] == 3, "malformed/missing samples stay in the denominator"
    assert q1["samples_valid"] == 1
    assert q1["semantic_matches"] == 1
    assert q1["majority"] is False, "1 match of 3 total (errors never count as matches)"
    assert q1["mean"] == pytest.approx(2.0)
    errors = [s for s in q1["samples"] if "error" in s]
    assert len(errors) == 2  # one missing/malformed, one invalid verdict

    # invalid verdict was recorded as a failure too
    failures = (run_dir / "failures.jsonl").read_text(encoding="utf-8")
    assert "judge-verdict" in failures


# ----------------------------------------------- gate conflict + invalid runs


def test_gate_conflict_annotation_and_invalid_run_rendering(tmp_path: Path) -> None:
    run_ok = _make_run_dir(
        tmp_path,
        "run-conflict",
        provider="provider-one",
        dimensions={
            **_DIMENSIONS_OK,
            "abstention": {"pass": 1, "fail": 0, "not_applicable": 0, "unsupported": 0},
        },
    )
    (run_ok / "judge-scores.json").write_text(
        json.dumps(
            {
                "per_query": [
                    {
                        "query_id": "Q-0001",
                        "samples": [
                            {
                                "sample_index": 0,
                                "model_id": "judge-model-x",
                                "semantic_match": True,
                                "explanation_quality": 4,
                                "reason": "matches",
                            }
                        ],
                        "samples_total": 1,
                        "samples_valid": 1,
                        "semantic_matches": 1,
                        "majority": True,
                        "mean": 4.0,
                        "stdev": None,
                    }
                ],
                "meta": {"kind": "judge", "queries": 1},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    run_bad = _make_run_dir(
        tmp_path,
        "run-broken",
        provider="provider-two",
        invalid=True,
        invalid_reason="environment: simulated fault",
    )

    out = build_comparison_report([run_ok, run_bad], tmp_path / "compare.md")
    report = out.read_text(encoding="utf-8")

    # Judge said match, deterministic no_leak gate failed -> annotated conflict.
    assert GATE_CONFLICT_NOTE in report
    assert "no_leak" in report

    # The deterministic table still shows the fail and never judge values.
    dims_section = report.split("## Dimensions")[1].split("## Retrieval")[0]
    assert "fail=1" in dims_section
    assert "match" not in dims_section and "4.0" not in dims_section

    # Invalid run renders INVALID, never numbers.
    assert "INVALID: environment: simulated fault" in report
    for row in dims_section.splitlines():
        if row.startswith("| factual_qa") or row.startswith("| governance"):
            assert row.rstrip().endswith("INVALID |")

    # Cross-contender latency is structurally incomparable, and an INVALID
    # harness-mode answer decision stays INVALID rather than being masked as
    # withheld.
    latency_section = report.split("## Latency")[1].split("## Failures")[0]
    assert "withheld: transport asymmetry (4b.40)" in latency_section
    assert not any(value in latency_section for value in ("10.000", "20.000", "30.000"))
    abstention = next(line for line in dims_section.splitlines() if line.startswith("| abstention |"))
    assert abstention.rstrip().endswith("INVALID |")


# ------------------------------------------------------- skip, never fabricate


def test_claude_cli_backend_missing_binary_returns_skip_with_command(
    tmp_path: Path,
) -> None:
    run_dir = _make_run_dir(tmp_path, "run-cli")
    backend = ClaudeCliBackend(binary="claude-membench-not-a-real-binary")
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=1, seed="s8")

    assert outcome.status == "skipped"
    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.status == "skip"
    assert result.response is None, "a missing binary must never fabricate a response"

    prompt = load_requests(run_dir, "judge")[0].payload["prompt"]
    expected = shlex.join(
        ["claude-membench-not-a-real-binary", "-p", prompt, "--output-format", "json"]
    )
    assert result.command == expected
    assert not (run_dir / "judge-responses").exists()


def test_openai_backend_unset_env_returns_skip_with_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEMBENCH_TEST_API_KEY", raising=False)
    run_dir = _make_run_dir(tmp_path, "run-http")
    backend = OpenAICompatBackend(
        base_url="https://models.invalid/v1",
        model="neutral-model",
        api_key_env="MEMBENCH_TEST_API_KEY",
    )
    outcome = backend.run_phase(run_dir, "judge", _clean_judge_items(), samples=1, seed="s9")

    assert outcome.status == "skipped"
    result = outcome.results[0]
    assert result.status == "skip" and result.response is None
    assert result.command is not None
    assert "https://models.invalid/v1/chat/completions" in result.command
    assert "$MEMBENCH_TEST_API_KEY" in result.command
    assert '\\"temperature\\": 0' in result.command or '"temperature": 0' in result.command
    assert not (run_dir / "judge-responses").exists()
