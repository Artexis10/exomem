from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


SIDECAR_PATH = Path(__file__).with_name("sidecar.py")


def _sidecar():
    assert SIDECAR_PATH.is_file(), "Basic Memory sidecar production module is not implemented"
    spec = importlib.util.spec_from_file_location("memorybench_basic_sidecar", SIDECAR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(route: str, **updates: object) -> bytes:
    base: dict[str, object] = {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "container_tag": "question-7-run-9",
    }
    if route == "/v1/ingest":
        base["session"] = {
            "session_id": "answer_session_7_abs",
            "position": 0,
            "date": "2025-01-02",
            "messages": [
                {"role": "user", "content": "I prefer tea."},
                {"role": "assistant", "content": "Noted."},
            ],
        }
    elif route == "/v1/search":
        base.update(query="What drink is preferred?", limit=10)
    base.update(updates)
    return json.dumps(base, separators=(",", ":"), allow_nan=False).encode()


class FakeRawResult:
    def __init__(self, payload: object, *, error: bool = False, text_payloads: list[str] | None = None):
        self.isError = error
        self.structuredContent = payload
        self.content = [SimpleNamespace(text=text) for text in (text_payloads or [])]


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.result = FakeRawResult({"result": {"results": [{"matched_chunk": "tea", "score": 0.8}]}})
        self.auto_identity = True

    def call_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        structured = self.result.structuredContent
        if isinstance(structured, dict):
            wrapped = structured.get("result")
            rows = wrapped.get("results") if isinstance(wrapped, dict) else None
            if self.auto_identity and isinstance(rows, list) and rows and isinstance(rows[0], dict):
                rows[0]["source_doc_id"] = arguments.get("query")
        return self.result


class FakeHit:
    def __init__(self, text: str = "tea", score: float = 0.8) -> None:
        self.text = text
        self.score = score
        self.metadata = {"title": "neutral"}

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        assert mode == "json"
        return {"text": self.text, "score": self.score, "metadata": self.metadata}


class FakeProvider:
    def __init__(self) -> None:
        self.ingest_calls: list[tuple[Path, object]] = []
        self.search_calls: list[tuple[str, int, object]] = []
        self.cleanup_calls: list[object] = []
        self._mcp = FakeMcp()
        self.hits = [FakeHit()]
        self._resolved_project_names: dict[str, str] = {}

    def _project_name(self, run_config: object) -> str:
        run_id = str(getattr(run_config, "run_id"))
        return self._resolved_project_names.get(run_id, f"bm-bench-{run_id}")

    def ingest(self, corpus_path: Path, run_config: object) -> None:
        self.ingest_calls.append((corpus_path, run_config))
        run_id = str(getattr(run_config, "run_id"))
        self._resolved_project_names.setdefault(run_id, f"bm-bench-{run_id}")

    def search(self, query: str, limit: int, run_config: object) -> list[FakeHit]:
        self.search_calls.append((query, limit, run_config))
        # The real provider makes this call internally. Preserve that shape in the fake boundary.
        self._mcp.call_tool(
            "search_notes",
            {
                "query": query,
                "project": self._project_name(run_config),
                "page": 1,
                "page_size": limit,
                "search_type": "hybrid",
                "output_format": "json",
            },
        )
        return self.hits

    def cleanup(self, run_config: object) -> None:
        self.cleanup_calls.append(run_config)


def _project_info(project: str = "mb-fixture") -> dict[str, object]:
    return {
        "project_name": project,
        "project_path": "<work>/corpus",
        "statistics": {"total_entities": 1},
        "embedding_status": {
            "semantic_search_enabled": True,
            "embedding_provider": "fastembed",
            "embedding_model": "bge-small-en-v1.5",
            "total_indexed_entities": 1,
            "total_entities_with_chunks": 1,
            "total_chunks": 2,
            "total_embeddings": 2,
            "orphaned_chunks": 0,
            "vector_tables_exist": True,
            "reindex_recommended": False,
            "reindex_reason": None,
        },
    }


def _engine(tmp_path: Path, **updates: object):
    sidecar = _sidecar()
    provider = updates.pop("provider", FakeProvider())
    renderer_calls: list[tuple[object, ...]] = []

    def renderer(doc_id: str, date: str, turns: list[dict[str, object]]) -> str:
        renderer_calls.append((doc_id, date, turns))
        return f"# {doc_id} ({date})\n\n{json.dumps(turns, sort_keys=True)}\n"

    engine = sidecar.BasicMemoryEngine(
        work_root=tmp_path / "work",
        evidence_root=tmp_path / "evidence",
        basic_checkout=tmp_path / "basic-memory",
        provider=provider,
        renderer=renderer,
        project_info=updates.pop("project_info", lambda _project: _project_info(_project)),
        startup_lines=updates.pop("startup_lines", lambda: [
            "Starting Basic Memory MCP server (mode=LOCAL)",
            "Config: database_backend=sqlite, semantic_search_enabled=True, default_project=main",
            "Semantic search: provider=fastembed, model=bge-small-en-v1.5, dimensions=auto",
            "Semantic embeddings: 2 embeddings across 2 chunks for 1 entities",
        ]),
        document_probe=updates.pop("document_probe", None),
        **updates,
    )
    return sidecar, engine, provider, renderer_calls


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b"[]",
        b"not-json",
    ],
)
def test_strict_json_rejects_duplicates_nonfinite_and_nonobjects(raw: bytes) -> None:
    sidecar = _sidecar()
    with pytest.raises(sidecar.SidecarError) as caught:
        sidecar.loads_strict_json(raw)
    assert caught.value.code in {"invalid_json", "duplicate_key", "nonfinite_number", "invalid_envelope"}


def test_strict_request_rejects_unknown_fields_and_bad_protocol() -> None:
    sidecar = _sidecar()
    for body in (
        _request("/v1/search", extra="no"),
        _request("/v1/search", protocol_version=2),
        _request("/v1/search", request_id="not-a-uuid"),
        _request("/v1/search", limit=0),
    ):
        with pytest.raises(sidecar.SidecarError):
            sidecar.parse_request("/v1/search", body)


def test_body_cap_is_exactly_four_mib() -> None:
    sidecar = _sidecar()
    assert sidecar.MAX_BODY_BYTES == 4 * 1024 * 1024
    with pytest.raises(sidecar.SidecarError, match="body") as caught:
        sidecar.loads_strict_json(b"{" + b" " * sidecar.MAX_BODY_BYTES + b"}")
    assert caught.value.http_status == 413


def test_neutral_identity_and_namespace_hide_raw_abs_marker() -> None:
    sidecar = _sidecar()
    raw = "question_answer_42_abs-run"
    namespace = sidecar.namespace_for(raw)
    document_id = sidecar.neutral_document_id(raw, 7)
    assert namespace.startswith("mb-") and len(namespace) == 27
    assert document_id.startswith("mb-doc-")
    assert raw not in namespace + document_id
    assert "_abs" not in namespace + document_id
    assert namespace == sidecar.namespace_for(raw)
    assert document_id == sidecar.neutral_document_id(raw, 7)


def test_inert_default_and_all_configured_paths_stay_under_work_root(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    config_path = engine.preseed_config()
    payload = json.loads(config_path.read_text())
    assert payload["default_project"] == "main"
    assert payload["projects"]["main"]["path"]
    work_root = (tmp_path / "work").resolve()
    for entry in payload["projects"].values():
        assert Path(entry["path"]).resolve().is_relative_to(work_root)
    assert not (Path.home() / "basic-memory").is_relative_to(work_root)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_preseed_uses_pinned_defaults_exactly_and_records_source_provenance(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    config_path = engine.preseed_config()
    inert = str((tmp_path / "work" / "inert-default-main").resolve())
    assert json.loads(config_path.read_text()) == {
        "default_project": "main",
        "projects": {"main": {"path": inert}},
        "database_backend": "sqlite",
        "semantic_search_enabled": True,
        "semantic_embedding_provider": "fastembed",
        "semantic_embedding_model": "bge-small-en-v1.5",
        "log_level": "INFO",
        "logfire_enabled": False,
        "logfire_send_to_logfire": False,
    }
    assert sidecar.BASIC_CONFIG_DEFAULTS_PROVENANCE == {
        "commit": "816accaa9befe8281668ba8819eaf74d11ce2385",
        "source": "src/basic_memory/config_models.py",
    }


def test_config_is_seeded_once_and_preserves_real_registry_projects(tmp_path: Path) -> None:
    _, engine, _, _ = _engine(tmp_path)
    config_path = engine.preseed_config()
    configured = json.loads(config_path.read_text())
    preserved_path = (tmp_path / "work" / "corpora" / "preserved").resolve()
    preserved_path.mkdir(parents=True)
    configured["projects"]["bm-bench-preserved"] = {"path": str(preserved_path)}
    config_path.write_text(json.dumps(configured, sort_keys=True, separators=(",", ":")) + "\n")

    engine.handle("/v1/ingest", _request("/v1/ingest"))

    after = json.loads(config_path.read_text())
    assert after["projects"]["bm-bench-preserved"] == {"path": str(preserved_path)}
    assert after["projects"]["main"] == configured["projects"]["main"]


def test_ingest_calls_exact_renderer_and_provider_once_then_replay_zero_calls(tmp_path: Path) -> None:
    _, engine, provider, renderer_calls = _engine(tmp_path)
    body = _request("/v1/ingest")
    first = engine.handle("/v1/ingest", body)
    second = engine.handle("/v1/ingest", body)

    assert first == second
    assert len(renderer_calls) == 1
    neutral_id, date, turns = renderer_calls[0]
    assert neutral_id.startswith("mb-doc-")
    assert "answer_session_7_abs" not in neutral_id
    assert date == "2025-01-02"
    assert turns[0] == {"role": "user", "content": "I prefer tea."}
    assert len(provider.ingest_calls) == 1
    assert first["document_id"] == "answer_session_7_abs"
    assert first["readiness"]["verified"] is True


def test_identical_session_with_fresh_request_id_is_still_zero_new_calls(tmp_path: Path) -> None:
    _, engine, provider, renderer_calls = _engine(tmp_path)
    first = _request("/v1/ingest")
    replay = json.loads(first)
    replay["request_id"] = str(uuid4())

    original = engine.handle("/v1/ingest", first)
    repeated = engine.handle("/v1/ingest", json.dumps(replay, separators=(",", ":")).encode())

    assert repeated == original
    assert len(renderer_calls) == 1
    assert len(provider.ingest_calls) == 1


def test_request_id_collision_and_in_progress_replay_refuse(tmp_path: Path) -> None:
    sidecar, engine, provider, _ = _engine(tmp_path)
    body = _request("/v1/ingest")
    payload = json.loads(body)
    engine.handle("/v1/ingest", body)
    payload["session"]["messages"][0]["content"] = "changed"
    changed = json.dumps(payload, separators=(",", ":")).encode()
    with pytest.raises(sidecar.SidecarError) as collision:
        engine.handle("/v1/ingest", changed)
    assert collision.value.http_status == 409
    assert collision.value.code == "request_id_collision"
    assert len(provider.ingest_calls) == 1

    pending = _request("/v1/ingest")
    pending_id = json.loads(pending)["request_id"]
    engine.replays.begin(pending_id, pending)
    with pytest.raises(sidecar.SidecarError) as in_progress:
        engine.handle("/v1/ingest", pending)
    assert in_progress.value.http_status == 409
    assert in_progress.value.code == "request_in_progress"


def test_concurrent_same_request_observes_in_progress_conflict(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    body = _request("/v1/ingest")
    entered = threading.Event()
    release = threading.Event()
    original = engine._dispatch

    def blocked(route: str, payload: dict[str, object]):
        entered.set()
        assert release.wait(timeout=5)
        return original(route, payload)

    engine._dispatch = blocked
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _capture_call(engine, body, errors))
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(sidecar.SidecarError) as caught:
            engine.handle("/v1/ingest", body)
        assert caught.value.http_status == 409
        assert caught.value.code == "request_in_progress"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not errors


def _capture_call(engine: object, body: bytes, errors: list[BaseException]) -> None:
    try:
        engine.handle("/v1/ingest", body)
    except BaseException as exc:  # pragma: no cover - asserted by caller
        errors.append(exc)


def test_changed_rendered_bytes_for_same_public_session_conflict(tmp_path: Path) -> None:
    sidecar, engine, provider, _ = _engine(tmp_path)
    first = _request("/v1/ingest")
    changed = json.loads(first)
    changed["request_id"] = str(uuid4())
    changed["session"]["messages"][0]["content"] = "coffee now"
    with pytest.raises(sidecar.SidecarError) as caught:
        engine.handle("/v1/ingest", first)
        engine.handle("/v1/ingest", json.dumps(changed, separators=(",", ":")).encode())
    assert caught.value.http_status == 409
    assert caught.value.code == "session_content_conflict"
    assert len(provider.ingest_calls) == 1


def test_growing_corpus_calls_one_full_ingest_per_unique_session(tmp_path: Path) -> None:
    _, engine, provider, renderer_calls = _engine(tmp_path)
    engine.handle("/v1/ingest", _request("/v1/ingest"))
    second = json.loads(_request("/v1/ingest"))
    second["session"]["session_id"] = "filler-session"
    second["session"]["position"] = 1
    engine.handle("/v1/ingest", json.dumps(second, separators=(",", ":")).encode())

    assert len(provider.ingest_calls) == 2
    assert len(renderer_calls) == 2
    first_corpus, _ = provider.ingest_calls[0]
    second_corpus, _ = provider.ingest_calls[1]
    assert first_corpus == second_corpus
    assert len(list(first_corpus.glob("*.md"))) == 2


def test_resolved_basic_project_identity_drives_all_public_proofs_search_and_cleanup(tmp_path: Path) -> None:
    sidecar = _sidecar()

    class ConflictProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.commands: list[list[str]] = []

        def ingest(self, corpus_path: Path, run_config: object) -> None:
            super().ingest(corpus_path, run_config)
            run_id = str(getattr(run_config, "run_id"))
            self._resolved_project_names[run_id] = f"bm-bench-{run_id}-resolved-conflict"

        def _run_bm(self, args: list[str], *, check: bool = True):
            assert check is True
            self.commands.append(list(args))
            return SimpleNamespace(stdout=json.dumps([{"name": "main"}]), stderr="", returncode=0)

    provider = ConflictProvider()
    project_calls: list[str] = []
    document_calls: list[tuple[str, str]] = []

    def project_info(project: str) -> dict[str, object]:
        project_calls.append(project)
        if not project.startswith("bm-bench-"):
            raise sidecar.SidecarError("invalid_envelope", "bare namespace is not a Basic project identity")
        return _project_info(project)

    def document_probe(project: str, document_id: str) -> dict[str, object]:
        document_calls.append((project, document_id))
        return {"document_id": document_id, "found": True, "matched_identity": document_id}

    _, engine, _, _ = _engine(
        tmp_path,
        provider=provider,
        project_info=project_info,
        document_probe=document_probe,
    )
    ingested = engine.handle("/v1/ingest", _request("/v1/ingest"))
    namespace = ingested["namespace"]
    resolved = f"bm-bench-{namespace}-resolved-conflict"
    assert namespace.startswith("mb-") and resolved != namespace
    assert project_calls == [resolved]
    assert document_calls[0][0] == resolved

    engine.handle("/v1/search", _request("/v1/search", limit=3))
    assert provider._mcp.calls[-1][1]["project"] == resolved
    engine.handle("/v1/cleanup", _request("/v1/cleanup"))
    assert ["project", "remove", resolved, "--local", "--delete-notes"] in provider.commands
    assert ["project", "list", "--local", "--json"] in provider.commands


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (("semantic_search_enabled", False), "semantic_disabled"),
        (("embedding_provider", None), "embedding_identity_missing"),
        (("embedding_model", ""), "embedding_identity_missing"),
        (("vector_tables_exist", False), "vector_tables_missing"),
        (("total_chunks", 0), "semantic_counts_invalid"),
        (("total_embeddings", 1), "semantic_counts_invalid"),
        (("orphaned_chunks", 1), "orphaned_chunks"),
        (("reindex_recommended", True), "reindex_recommended"),
    ],
)
def test_readiness_predicate_matrix_fails_closed(mutation: tuple[str, object], code: str) -> None:
    sidecar = _sidecar()
    info = _project_info()
    key, value = mutation
    info["embedding_status"][key] = value
    with pytest.raises(sidecar.SidecarError) as caught:
        sidecar.validate_readiness(
            project_info=info,
            startup_lines=["semantic_search_enabled=True", "provider=fastembed, model=bge"],
            document_proof={"document_id": "mb-doc-a", "found": True, "matched_identity": "mb-doc-a"},
            fallback_detected=False,
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    "startup_lines",
    [
        ["semantic_search_enabled=True", "provider=fastembed, model=bge-small-en-v1.5"],
        [
            "semantic_search_enabled=True",
            "provider=fastembed, model=wrong-model",
            "Semantic embeddings: 2 embeddings across 2 chunks for 1 entities",
        ],
        [
            "semantic_search_enabled=True",
            "provider=fastembed, model=bge-small-en-v1.5",
            "Semantic embeddings: 3 embeddings across 3 chunks for 1 entities",
        ],
    ],
)
def test_startup_log_requires_exact_semantic_identity_and_reconciled_first_counts(
    startup_lines: list[str],
) -> None:
    sidecar = _sidecar()
    with pytest.raises(sidecar.SidecarError) as caught:
        sidecar.validate_readiness(
            project_info=_project_info(),
            startup_lines=startup_lines,
            document_proof={"document_id": "mb-doc-a", "found": True, "matched_identity": "mb-doc-a"},
            fallback_detected=False,
        )
    assert caught.value.code in {"embedding_identity_missing", "startup_log_invalid", "semantic_counts_invalid"}


@pytest.mark.parametrize(
    "row",
    [
        {"source_doc_id": "mb-doc-exact"},
        {"permalink": "memory://notes/mb-doc-exact.md"},
        {"file_path": "/owned/corpus/mb-doc-exact.md"},
    ],
)
def test_document_proof_requires_exact_neutral_identity_match(tmp_path: Path, row: dict[str, str]) -> None:
    _, engine, provider, _ = _engine(tmp_path)
    provider._mcp.auto_identity = False
    provider._mcp.result = FakeRawResult({"result": {"results": [row]}})
    assert engine._public_document_proof("bm-bench-project", "mb-doc-exact")["found"] is True

    provider._mcp.result = FakeRawResult(
        {"result": {"results": [{"source_doc_id": "unrelated-but-nonempty"}]}}
    )
    proof = engine._public_document_proof("bm-bench-project", "mb-doc-exact")
    assert proof["found"] is False


def test_each_unique_ingest_requires_fresh_readiness_and_fallback_proof(tmp_path: Path) -> None:
    sidecar, engine, provider, _ = _engine(tmp_path)
    calls = 0

    def readiness(_project: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            payload = _project_info(_project)
            payload["embedding_status"]["total_embeddings"] = 0
            return payload
        return _project_info(_project)

    engine.project_info = readiness
    engine.handle("/v1/ingest", _request("/v1/ingest"))
    second = json.loads(_request("/v1/ingest"))
    second["session"]["session_id"] = "second"
    second["session"]["position"] = 1
    with pytest.raises(sidecar.SidecarError, match="semantic"):
        engine.handle("/v1/ingest", json.dumps(second, separators=(",", ":")).encode())
    assert calls == 2
    assert len(provider.ingest_calls) == 2


def test_growing_corpus_reuses_immutable_startup_snapshot_but_reconciles_only_first_ingest(
    tmp_path: Path,
) -> None:
    startup_snapshot = [
        "Starting Basic Memory MCP server (mode=LOCAL)",
        "Config: database_backend=sqlite, semantic_search_enabled=True, default_project=main",
        "Semantic search: provider=fastembed, model=bge-small-en-v1.5, dimensions=auto",
        "Semantic embeddings: 2 embeddings across 2 chunks for 1 entities",
    ]

    mismatched = _project_info()
    mismatched["statistics"]["total_entities"] = 2
    mismatched["embedding_status"].update(
        total_indexed_entities=2,
        total_entities_with_chunks=2,
        total_chunks=4,
        total_embeddings=4,
    )
    mismatched_sidecar, mismatched_engine, _, _ = _engine(
        tmp_path / "mismatched-first",
        project_info=lambda _project: mismatched,
        startup_lines=lambda: list(startup_snapshot),
    )
    with pytest.raises(mismatched_sidecar.SidecarError) as caught:
        mismatched_engine.handle("/v1/ingest", _request("/v1/ingest"))
    assert caught.value.code == "semantic_counts_invalid"

    project_calls = 0
    startup_reads: list[list[str]] = []
    proofs: list[tuple[str, str]] = []

    def growing_project(project: str) -> dict[str, object]:
        nonlocal project_calls
        project_calls += 1
        info = _project_info(project)
        if project_calls == 2:
            info["statistics"]["total_entities"] = 2
            info["embedding_status"].update(
                total_indexed_entities=2,
                total_entities_with_chunks=2,
                total_chunks=4,
                total_embeddings=4,
            )
        return info

    def immutable_startup() -> list[str]:
        snapshot = list(startup_snapshot)
        startup_reads.append(snapshot)
        return snapshot

    def exact_new_document(project: str, document_id: str) -> dict[str, object]:
        proofs.append((project, document_id))
        return {"document_id": document_id, "matched_identity": document_id, "found": True}

    sidecar, engine, provider, _ = _engine(
        tmp_path / "growing",
        project_info=growing_project,
        startup_lines=immutable_startup,
        document_probe=exact_new_document,
    )
    first = engine.handle("/v1/ingest", _request("/v1/ingest"))
    second_body = json.loads(_request("/v1/ingest"))
    second_body["session"]["session_id"] = "second-public-session"
    second_body["session"]["position"] = 1
    second = engine.handle(
        "/v1/ingest",
        json.dumps(second_body, separators=(",", ":")).encode(),
    )

    assert first["readiness"]["verified"] is True
    assert second["readiness"]["verified"] is True
    assert project_calls == 2
    assert len(provider.ingest_calls) == 2
    assert startup_reads == [startup_snapshot, startup_snapshot]
    assert proofs[0][1] != proofs[1][1]
    assert proofs[1][1] == sidecar.neutral_document_id("second-public-session", 1)


def test_detected_embedding_fallback_invalidates_ingest(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    engine.fallback_probe = lambda: True
    with pytest.raises(sidecar.SidecarError) as caught:
        engine.handle("/v1/ingest", _request("/v1/ingest"))
    assert caught.value.code == "semantic_fallback"


def test_search_calls_provider_once_with_exact_limit_and_raw_mcp_arguments(tmp_path: Path) -> None:
    _, engine, provider, _ = _engine(tmp_path)
    engine.handle("/v1/ingest", _request("/v1/ingest"))
    response = engine.handle("/v1/search", _request("/v1/search", limit=7))

    assert len(provider.search_calls) == 1
    query, limit, _ = provider.search_calls[0]
    assert query == "What drink is preferred?"
    assert limit == 7
    assert provider._mcp.calls[-1] == (
        "search_notes",
        {
            "query": query,
            "project": provider._project_name(provider.search_calls[0][2]),
            "page": 1,
            "page_size": 7,
            "search_type": "hybrid",
            "output_format": "json",
        },
    )
    assert response["hits"][0]["metadata"] == {"title": "neutral"}


@pytest.mark.parametrize("kind", ["empty", "overlimit", "ambiguous", "nonjson"])
def test_search_refuses_empty_overlimit_and_ambiguous_raw_results(tmp_path: Path, kind: str) -> None:
    sidecar, engine, provider, _ = _engine(tmp_path)
    engine.handle("/v1/ingest", _request("/v1/ingest"))
    if kind == "empty":
        provider.hits = []
        provider._mcp.result = FakeRawResult({"result": {"results": []}})
    elif kind == "overlimit":
        provider.hits = [FakeHit(str(index)) for index in range(3)]
        provider._mcp.result = FakeRawResult({"result": {"results": [{}, {}, {}]}})
    elif kind == "ambiguous":
        provider._mcp.result = FakeRawResult(
            {"result": {"results": [{}]}}, text_payloads=['{"results":[{"different":true}]}']
        )
    else:
        provider._mcp.result = FakeRawResult(None, text_payloads=["guidance, not json"])
    with pytest.raises(sidecar.SidecarError) as caught:
        engine.handle("/v1/search", _request("/v1/search", limit=2))
    assert caught.value.code in {
        "empty_search_results",
        "overlimit_search_results",
        "ambiguous_mcp_result",
        "non_json_mcp_result",
    }


def test_cleanup_is_intermediate_then_final_and_calls_public_cleanup_once(tmp_path: Path) -> None:
    _, engine, provider, _ = _engine(tmp_path)
    engine.handle("/v1/ingest", _request("/v1/ingest"))
    other = json.loads(_request("/v1/ingest"))
    other["request_id"] = str(uuid4())
    other["container_tag"] = "other-container"
    other["session"]["session_id"] = "other"
    engine.handle("/v1/ingest", json.dumps(other, separators=(",", ":")).encode())

    intermediate = engine.handle("/v1/cleanup", _request("/v1/cleanup"))
    assert intermediate["final"] is False
    assert not provider.cleanup_calls
    final = engine.handle(
        "/v1/cleanup", _request("/v1/cleanup", container_tag="other-container")
    )
    assert final["final"] is True
    assert len(provider.cleanup_calls) == 1
    assert engine.shutdown_after_flush is True


def test_safe_error_and_evidence_never_leak_token_payload_traceback_or_home(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    token = "secret-bearer-token"
    absolute = str(Path.home() / "sensitive" / "file.md")
    safe = sidecar.safe_error(RuntimeError(f"{token} payload {absolute}"), secrets=[token])
    encoded = json.dumps(safe)
    assert safe == {"code": "internal_error", "message": "operation failed"}
    assert token not in encoded and absolute not in encoded
    assert "traceback" not in encoded.lower()

    engine.evidence.append(
        "test",
        {"token": token, "path": absolute, "payload": "private"},
        secrets=[token],
    )
    evidence_bytes = b"".join(path.read_bytes() for path in (tmp_path / "evidence").rglob("*.json"))
    assert token.encode() not in evidence_bytes
    assert str(Path.home()).encode() not in evidence_bytes


def test_error_and_evidence_scrubbing_removes_encoded_token_path_and_payload_variants(tmp_path: Path) -> None:
    _, engine, _, _ = _engine(tmp_path)
    token = "printable-private-token"
    absolute = str(Path.home() / "private" / "payload.json")
    variants = [
        token,
        absolute,
        base64.b64encode(token.encode()).decode(),
        absolute.encode().hex(),
    ]
    engine.evidence.append("remote-failure", {"message": " ".join(variants)}, secrets=[token])
    evidence_bytes = b"".join(path.read_bytes() for path in (tmp_path / "evidence").rglob("*.json"))
    for variant in variants:
        assert variant.encode() not in evidence_bytes


def test_http_boundary_auth_content_type_and_exact_routes(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    token = "test-token"
    with sidecar.serve_for_tests(engine, token=token) as client:
        assert client.post("/v1/search", _request("/v1/search"), token="wrong").status == 401
        assert client.post(
            "/v1/search", _request("/v1/search"), token=token, content_type="text/plain"
        ).status == 415
        assert client.post("/v1/health", b"{}", token=token).status == 404
        assert client.get("/v1/search", token=token).status in {404, 405}


def test_http_success_and_error_envelopes_are_exact_and_echo_request_id(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    token = "test-token"
    ingest_raw = _request("/v1/ingest")
    ingest_id = json.loads(ingest_raw)["request_id"]
    search_raw = _request("/v1/search", container_tag="not-ingested")
    search_id = json.loads(search_raw)["request_id"]
    with sidecar.serve_for_tests(engine, token=token) as client:
        success = json.loads(client.post("/v1/ingest", ingest_raw, token=token).body)
        assert set(success) == {"protocol_version", "request_id", "ok", "data"}
        assert success["protocol_version"] == 1 and success["request_id"] == ingest_id
        assert success["ok"] is True

        response = client.post("/v1/search", search_raw, token=token)
        assert response.status == 422
        failure = json.loads(response.body)
        assert set(failure) == {"protocol_version", "request_id", "ok", "error"}
        assert failure["request_id"] == search_id and failure["ok"] is False
        assert set(failure["error"]) == {
            "code", "message", "retryable", "retry_after_ms", "evidence_ref"
        }
        assert failure["error"]["retryable"] is False
        assert failure["error"]["retry_after_ms"] is None


def test_operations_are_serialized(tmp_path: Path) -> None:
    sidecar, engine, _, _ = _engine(tmp_path)
    active = 0
    maximum = 0
    gate = threading.Barrier(3)
    original = engine._dispatch

    def observed(route: str, payload: dict[str, object]):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            return original(route, payload)
        finally:
            active -= 1

    engine._dispatch = observed

    errors: list[BaseException] = []

    def worker(position: int) -> None:
        try:
            body = json.loads(_request("/v1/ingest"))
            body["request_id"] = str(uuid4())
            body["session"]["session_id"] = f"session-{position}"
            body["session"]["position"] = position
            gate.wait()
            engine.handle("/v1/ingest", json.dumps(body, separators=(",", ":")).encode())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join()
    assert not errors
    assert maximum == 1
