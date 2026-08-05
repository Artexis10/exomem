"""Adapter fan-out conformance: graybox raw-inbox adapter, Basic Memory CLI
adapter (fake-runner seam; live execution is user-run), and the Track-A bridge
that exposes any membench adapter as an upstream BenchmarkProvider.

The graybox adapter drives the READ-ONLY sibling checkout through its public
sync API against a workspace isolated under the benchmark workdir; when the
checkout is absent the adapter reports honest unavailability
(``AdapterUnsupported``), never a fabricated result.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    MemoryAdapter,
    Profile,
)
from membench.adapters.basic_memory_local import BasicMemoryLocalAdapter
from membench.adapters.graybox_local import GrayboxLocalAdapter, default_checkout
from membench.adapters.track_a_bridge import TrackABridge
from membench.generate import generate_corpus
from membench.native import graybox as graybox_native
from membench.native import load_corpus_view
from membench.schema import ExpectedRecord, QueryRecord, load_jsonl

T00 = "t00_mini_smoke"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


# ------------------------------------------------------------------ graybox

needs_graybox = pytest.mark.skipif(
    not (default_checkout() / "graybox" / "__init__.py").is_file(),
    reason="graybox sibling checkout not present",
)


@needs_graybox
def test_graybox_end_to_end_on_t00(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    native_dir = tmp_path / "native"
    graybox_native.render(view, native_dir)

    adapter = GrayboxLocalAdapter()
    assert isinstance(adapter, MemoryAdapter)
    # Raw-inbox altitude: capture + search only; nothing else is emulated.
    assert adapter.capabilities() == frozenset(
        {Capability.INGEST_API, Capability.SEARCH}
    )

    adapter.setup(tmp_path / "gb", Profile(name="graybox-raw-inbox"))
    try:
        # Isolation: the graybox workspace lives under the benchmark workdir.
        assert str(adapter.workspace_root).startswith(str(tmp_path / "gb"))

        results = adapter.ingest(corpus, native_dir)
        assert results and all(r.ok for r in results), [
            r.detail for r in results if not r.ok
        ]

        # Literal query (the canary source's own title) must retrieve the
        # canary document; sentinel identity survives capture->search.
        queries = load_jsonl(QueryRecord, corpus / "queries.jsonl")
        expected = {
            e.query_id: e for e in load_jsonl(ExpectedRecord, corpus / "expected.jsonl")
        }
        canary = next(q for q in queries if q.canary)
        canary_source_id = expected[canary.query_id].required_citations[0]
        source = next(s for s in view.sources if s.source_id == canary_source_id)

        hits = adapter.search(source.title, 10)
        assert hits, f"literal title query {source.title!r} returned no hits"
        assert any(canary_source_id in h.sentinels for h in hits), (
            f"canary doc {canary_source_id} not among hits: "
            f"{[h.provider_path for h in hits]}"
        )

        info = adapter.version_info()
        assert "raw-inbox" in info.get("profile_note", "")
    finally:
        adapter.cleanup()


def test_graybox_missing_checkout_reports_unavailable(tmp_path: Path) -> None:
    adapter = GrayboxLocalAdapter(checkout=tmp_path / "no-such-checkout")
    with pytest.raises(AdapterUnsupported):
        adapter.setup(tmp_path / "gb", Profile(name="graybox-raw-inbox"))


def test_graybox_ingest_requires_native_stream(corpus: Path, tmp_path: Path) -> None:
    if not (default_checkout() / "graybox" / "__init__.py").is_file():
        pytest.skip("graybox sibling checkout not present")
    adapter = GrayboxLocalAdapter()
    adapter.setup(tmp_path / "gb", Profile(name="graybox-raw-inbox"))
    try:
        with pytest.raises(AdapterEnvironmentError):
            adapter.ingest(corpus, tmp_path / "missing-native")
    finally:
        adapter.cleanup()


# ------------------------------------------------------- basic memory (bm)


class _FakeProc(SimpleNamespace):
    pass


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> _FakeProc:
    return _FakeProc(returncode=returncode, stdout=stdout, stderr=stderr)


_SEARCH_PAYLOAD = {
    "results": [
        {
            "file_path": "notes/fact-one.md",
            "title": "Fact one",
            "matched_chunk": "the fact body [ref:SRC-AAAA1111]",
            "score": 1.25,
        },
        {
            "permalink": "fact-two",
            "title": "Fact two",
            "content": "another fact",
        },
    ]
}


def _bm_fake_runner(calls: list[tuple[list[str], dict[str, str]]]):
    def runner(argv: list[str], env: dict[str, str]) -> _FakeProc:
        calls.append((list(argv), dict(env)))
        if "--version" in argv:
            return _proc(stdout="bm 0.15.0")
        if argv[1:3] == ["project", "add"]:
            return _proc(stdout="project added")
        if argv[1] == "reindex":
            return _proc(stdout="reindexed")
        if argv[1:3] == ["tool", "search-notes"]:
            return _proc(stdout=json.dumps(_SEARCH_PAYLOAD))
        raise AssertionError(f"unexpected bm invocation: {argv}")

    return runner


def test_bm_adapter_drives_cli_with_isolated_env(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    adapter = BasicMemoryLocalAdapter(runner=_bm_fake_runner(calls))
    assert isinstance(adapter, MemoryAdapter)
    assert adapter.capabilities() == frozenset(
        {Capability.FILE_DROP, Capability.SEARCH}
    )

    workdir = tmp_path / "bm"
    adapter.setup(workdir, Profile(name="bm-cli"))
    try:
        # Availability probe ran, against the isolated env: config dir under
        # the benchmark workdir, no ambient home / cloud-mode routing.
        assert calls, "setup() must probe bm availability"
        probe_argv, probe_env = calls[0]
        assert "--version" in probe_argv
        assert probe_env["BASIC_MEMORY_CONFIG_DIR"].startswith(str(workdir))
        assert "BASIC_MEMORY_HOME" not in probe_env
        assert "BASIC_MEMORY_CLOUD_MODE" not in probe_env

        native_dir = tmp_path / "native-bm"
        native_dir.mkdir()
        (native_dir / "fact-one.md").write_text("body", encoding="utf-8")
        results = adapter.ingest(tmp_path / "corpus", native_dir)
        assert [r.op for r in results] == ["project_add", "reindex"]
        assert all(r.ok for r in results)
        add_argv, add_env = calls[1]
        assert add_argv[1:3] == ["project", "add"]
        assert add_argv[4] == str(native_dir)
        assert add_env["BASIC_MEMORY_CONFIG_DIR"].startswith(str(workdir))
        reindex_argv, _ = calls[2]
        assert reindex_argv[1] == "reindex"
        project = add_argv[3]
        assert ["-p", project] == reindex_argv[-2:]

        hits = adapter.search("the fact", 5)
        search_argv, search_env = calls[3]
        assert search_argv[1:3] == ["tool", "search-notes"]
        # Pinned against bm 0.22.1, verified live. QUERY is POSITIONAL and must
        # follow the subcommand; `--query` and `--json` were both removed
        # upstream (search already emits JSON). The previous assertion pinned
        # `--json`, so this test stayed green while the real CLI rejected the
        # invocation with `No such option: --query`, exit 2.
        assert search_argv[3] == "the fact", "query must be positional"
        assert "--query" not in search_argv
        assert "--json" not in search_argv
        # Local routing is forced so a cloud-mode config can never silently
        # redirect a contender's measurement elsewhere.
        assert "--local" in search_argv
        assert search_env["BASIC_MEMORY_CONFIG_DIR"].startswith(str(workdir))
        # A benchmark-owned HF cache: bm resolves an embedding model for search,
        # and an unwritable cache fails as a permission error that reads like
        # "search is broken" and would invalidate every contender run.
        assert search_env["HF_HOME"].startswith(str(workdir))

        assert [h.rank for h in hits] == [1, 2]
        first = hits[0]
        assert isinstance(first, Hit)
        assert first.provider_path == "notes/fact-one.md"
        assert first.title == "Fact one"
        assert first.sentinels == ("SRC-AAAA1111",)
        assert hits[1].provider_path == "fact-two"
        assert hits[1].sentinels == ()
    finally:
        adapter.cleanup()


def test_bm_adapter_unavailable_probe_is_honest_skip(tmp_path: Path) -> None:
    def runner(argv: list[str], env: dict[str, str]):
        raise FileNotFoundError("bm")

    adapter = BasicMemoryLocalAdapter(runner=runner)
    with pytest.raises(AdapterUnsupported, match="bm"):
        adapter.setup(tmp_path / "bm", Profile(name="bm-cli"))


def test_bm_command_injectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BM_COMMAND", "/opt/custom/bm")
    calls: list[tuple[list[str], dict[str, str]]] = []
    adapter = BasicMemoryLocalAdapter(runner=_bm_fake_runner(calls))
    adapter.setup(tmp_path / "bm", Profile(name="bm-cli"))
    assert calls[0][0][0] == "/opt/custom/bm"
    # Constructor arg wins over the env seam.
    calls_b: list[tuple[list[str], dict[str, str]]] = []
    adapter_b = BasicMemoryLocalAdapter(command="/exact/bm", runner=_bm_fake_runner(calls_b))
    adapter_b.setup(tmp_path / "bm2", Profile(name="bm-cli"))
    assert calls_b[0][0][0] == "/exact/bm"


# ------------------------------------------------------------------ bridge


class _StubHit(SimpleNamespace):
    """Local stand-in for the upstream SearchHit shape (package not installed)."""


class _StubSkip(Exception):
    pass


class _StubAdapter:
    name = "stub-mem"
    supports_group_reuse = False

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.setup_calls: list[tuple[Path, Profile]] = []
        self.ingest_calls: list[tuple[Path, Path]] = []
        self.cleaned_up = 0

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INGEST_API, Capability.SEARCH})

    def setup(self, workdir: Path, profile: Profile) -> None:
        if self.unavailable:
            raise AdapterUnsupported("stub-mem is not installed")
        self.setup_calls.append((Path(workdir), profile))

    def ingest(self, corpus_dir: Path, native_dir: Path):
        self.ingest_calls.append((Path(corpus_dir), Path(native_dir)))
        return []

    def search(self, query: str, limit: int) -> list[Hit]:
        return [
            Hit(
                rank=1,
                provider_path="Knowledge Base/Benchmark Corpus/nested/session.md",
                title="Session note",
                excerpt="the matching passage",
                sentinels=("SRC-CANARY01",),
                raw={"score": 0.5},
                text="body [ref:SRC-CANARY01]",
            ),
            Hit(
                rank=2,
                provider_path="Knowledge Base/other-doc.md",
                title=None,
                excerpt=None,
                sentinels=(),
                raw={},
                text=None,
            ),
        ][:limit]

    def export_state(self):
        raise AdapterUnsupported("no state export")

    def cleanup(self) -> None:
        self.cleaned_up += 1

    def version_info(self) -> dict[str, str]:
        return {"provider": self.name}


def _upstream_run_config() -> SimpleNamespace:
    return SimpleNamespace(run_id="r1")


def test_bridge_exposes_upstream_provider_protocol(tmp_path: Path) -> None:
    bridge = TrackABridge(
        _StubAdapter(), hit_factory=_StubHit, skip_exception=_StubSkip,
        workdir_root=tmp_path,
    )
    assert bridge.name == "membench-stub-mem"
    assert bridge.supports_group_reuse is False
    for member in ("ingest", "search", "cleanup", "version_info"):
        assert callable(getattr(bridge, member))


def test_bridge_ingest_walks_corpus_dir_into_native_streams(tmp_path: Path) -> None:
    corpus = tmp_path / "upstream-corpus"
    (corpus / "nested").mkdir(parents=True)
    (corpus / "alpha.md").write_text("# Alpha note\n\nAlpha body.\n", encoding="utf-8")
    (corpus / "nested" / "beta.md").write_text("Beta body.\n", encoding="utf-8")

    inner = _StubAdapter()
    bridge = TrackABridge(
        inner, hit_factory=_StubHit, skip_exception=_StubSkip, workdir_root=tmp_path
    )
    bridge.ingest(corpus, _upstream_run_config())

    assert len(inner.setup_calls) == 1
    workdir, _profile = inner.setup_calls[0]
    assert str(workdir).startswith(str(tmp_path))
    assert len(inner.ingest_calls) == 1
    _corpus_dir, native_dir = inner.ingest_calls[0]

    ops_file = native_dir / "capture-ops.jsonl"
    assert ops_file.is_file(), "bridge must synthesize the capture op stream"
    ops = [json.loads(line) for line in ops_file.read_text().splitlines() if line.strip()]
    assert [op["source_id"] for op in ops] == ["alpha", "beta"]  # sorted walk
    assert ops[0]["content"].strip().endswith("Alpha body.")
    captures_file = native_dir / "captures.jsonl"
    assert captures_file.is_file(), "bridge must synthesize the raw capture stream"
    captures = [
        json.loads(line) for line in captures_file.read_text().splitlines() if line.strip()
    ]
    assert [c["source_id"] for c in captures] == ["alpha", "beta"]


def test_bridge_search_maps_hit_fields(tmp_path: Path) -> None:
    bridge = TrackABridge(
        _StubAdapter(), hit_factory=_StubHit, skip_exception=_StubSkip,
        workdir_root=tmp_path,
    )
    hits = bridge.search("anything", 5, _upstream_run_config())
    assert len(hits) == 2
    first, second = hits
    # Sentinel identity wins when present; vault prefixes are stripped.
    assert first.source_doc_id == "SRC-CANARY01"
    assert first.source_path == "nested/session.md"
    assert first.id == "Knowledge Base/Benchmark Corpus/nested/session.md"
    assert first.text == "the matching passage"
    assert first.score == 0.5
    assert first.metadata["title"] == "Session note"
    # No sentinel -> basename stem fallback (upstream _doc_id semantics).
    assert second.source_doc_id == "other-doc"
    assert second.source_path == "other-doc.md"
    assert second.score is None


def test_bridge_cleanup_and_version_info(tmp_path: Path) -> None:
    inner = _StubAdapter()
    bridge = TrackABridge(
        inner, hit_factory=_StubHit, skip_exception=_StubSkip, workdir_root=tmp_path
    )
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "only.md").write_text("body\n", encoding="utf-8")
    bridge.ingest(corpus, _upstream_run_config())
    info = bridge.version_info()
    assert info["provider"] == "stub-mem"
    assert info["bridge"] == "membench-track-a"
    bridge.cleanup(_upstream_run_config())
    assert inner.cleaned_up == 1


def test_bridge_unavailable_inner_adapter_becomes_skip(tmp_path: Path) -> None:
    corpus = tmp_path / "c"
    corpus.mkdir()
    (corpus / "only.md").write_text("body\n", encoding="utf-8")
    bridge = TrackABridge(
        _StubAdapter(unavailable=True),
        hit_factory=_StubHit,
        skip_exception=_StubSkip,
        workdir_root=tmp_path,
    )
    with pytest.raises(_StubSkip, match="not installed"):
        bridge.ingest(corpus, _upstream_run_config())


def test_every_registered_provider_has_a_native_renderer() -> None:
    """A provider with no renderer is handed an EMPTY corpus and scores zero.

    This is the fairness invariant of the whole harness. `_NATIVE_RENDERERS`
    maps a provider to the code that rewrites the corpus into *its* native
    grammar before ingest. A provider missing from that map receives an empty
    directory, retrieves nothing, and reads as a catastrophic contender result
    while measuring only our omission.

    Until 2026-08-05 the map held `exomem-local` alone, so the benchmark could
    structurally only ever produce zeros for competitors — rigged in our favour
    by accident. `basic_memory.render` and `graybox.render` already existed and
    were tested for grammar and parity; they were simply never wired up. A live
    basic-memory run produced 0 hits on all 236 queries and was caught by the
    retrieval floor rather than published.

    If this test fails because a provider was added, the fix is to write its
    renderer — never to remove it from the registry to make the test pass.
    """

    from membench.adapters import base as adapters_base
    from membench.runner import _NATIVE_RENDERERS

    registered = set(adapters_base._FACTORIES)
    assert registered, "no adapters registered; the import side effect is broken"
    missing = sorted(registered - set(_NATIVE_RENDERERS))
    assert not missing, (
        f"providers with no native renderer: {missing}. Each would be ingested "
        "from an empty directory and score zero on every query, which is a "
        "harness fault reported as a contender result."
    )
