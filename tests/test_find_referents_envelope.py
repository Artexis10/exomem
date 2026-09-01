from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from exomem import commands, epistemic_graph, readiness
from exomem import embeddings as embeddings_module
from exomem import find as find_module

TOPIC = "Knowledge Base/Notes/Research/coastal-season.md"
DISTRACTOR_TOPIC = "Knowledge Base/Notes/Research/trip-timing.md"
ENTITY = "Knowledge Base/Entities/People/aria-vale.md"
NOISE = "Knowledge Base/Entities/People/beryl-moss.md"


def _runtime():
    return importlib.import_module("exomem.referent_runtime")


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def referent_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    _write(
        vault,
        TOPIC,
        """---
type: research-note
title: Coastal season advice
status: active
updated: 2026-08-01
---
# Coastal season advice

Two coastal friends discussed autumn travel and harbour weather.
""",
    )
    _write(
        vault,
        DISTRACTOR_TOPIC,
        """---
type: research-note
title: Trip timing advice
status: active
updated: 2026-08-01
---
# Trip timing advice

Two friends discussed when to go and how to compare routes.
""",
    )
    _write(
        vault,
        ENTITY,
        f"""---
type: entity
title: Aria Vale
entity_type: person
status: active
relationship: friend
tags: [coastal, travel]
updated: 2026-08-02
---
# Aria Vale

A coastal friend who discussed when to go.

## Relations
- relates_to [[{TOPIC[:-3]}]]
""",
    )
    _write(
        vault,
        NOISE,
        f"""---
type: entity
title: Beryl Moss
entity_type: person
status: active
relationship: friend
updated: 2026-08-03
---
# Beryl Moss

Two coastal friends are mentioned here only as retrieval wording.

## Relations
- relates_to [[{DISTRACTOR_TOPIC[:-3]}]]
""",
    )
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    def _raise(*_args, **_kwargs):
        raise ImportError("model-free referent envelope test")

    monkeypatch.setattr(embeddings_module, "get_embedding_index", _raise)
    find_module.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    yield vault
    find_module.clear_cache()


def _call(vault: Path, query: str = "my two coastal friends", **kwargs):
    return commands.op_find(
        vault,
        query=query,
        limit=10,
        mode="hybrid",
        rerank=False,
        **kwargs,
    )


def _block(result: object) -> dict:
    assert isinstance(result, dict)
    assert isinstance(result.get("referents"), dict)
    return result["referents"]


def test_referents_block_emitted_only_when_cue_fires(referent_vault: Path) -> None:
    assert "referents" in _call(referent_vault)
    quiet = _call(referent_vault, "coastal autumn weather")
    assert isinstance(quiet, list) or "referents" not in quiet


def test_referents_rides_compact_and_full_detail_identically(referent_vault: Path) -> None:
    assert _block(_call(referent_vault, detail="compact")) == _block(
        _call(referent_vault, detail="full")
    )


def test_referents_coexists_with_pack_timings_and_records_stage(referent_vault: Path) -> None:
    result = _call(referent_vault, pack=True, include_timings=True)
    assert {"hits", "pack", "timings", "referents"} <= set(result)
    assert "referents" in result["timings"]["stages"]


def test_referents_identical_on_hot_cache_hit(referent_vault: Path) -> None:
    first = _call(referent_vault)
    second = _call(referent_vault)
    assert _block(first) == _block(second)


def test_hits_are_byte_identical_with_resolver_on_and_off(
    referent_vault: Path, monkeypatch
) -> None:
    enabled = _call(referent_vault)
    assert "referents" in enabled
    monkeypatch.setenv("EXOMEM_DISABLE_REFERENTS", "1")
    disabled = _call(referent_vault)
    enabled_hits = enabled["hits"] if isinstance(enabled, dict) else enabled
    disabled_hits = disabled["hits"] if isinstance(disabled, dict) else disabled
    assert enabled_hits == disabled_hits


def test_graph_off_omits_graph_evidence(referent_vault: Path) -> None:
    block = _block(_call(referent_vault, graph=False))
    evidence = [e for item in block["resolved"] + block["candidates"] for e in item["evidence"]]
    assert all(e["kind"] != "graph" for e in evidence)


def test_keyword_mode_never_runs_resolver(referent_vault: Path, monkeypatch) -> None:
    runtime = _runtime()

    def boom(*_args, **_kwargs):
        raise AssertionError("resolver ran in keyword mode")

    monkeypatch.setattr(runtime, "resolve_for_find", boom)
    result = commands.op_find(referent_vault, query="my two coastal friends", mode="keyword")
    assert isinstance(result, list)


def test_resolver_exception_soft_fails_to_unchanged_response(
    referent_vault: Path, monkeypatch
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(
        runtime,
        "resolve_for_find",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = _call(referent_vault)
    assert isinstance(result, list) or "referents" not in result


def test_resolver_exception_is_logged_and_soft_fails(
    referent_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(
        runtime,
        "load_entity_registry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("registry boom")),
    )

    with caplog.at_level("WARNING", logger="exomem.referent_runtime"):
        result = _call(referent_vault)

    assert isinstance(result, list) or "referents" not in result
    assert "referent resolution failed" in caplog.text
    assert "registry boom" in caplog.text


def test_managed_referent_stage_requires_live_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    observed: list[bool] = []
    expected_proofs: list[dict[str, object]] = []
    projected_scopes: list[str] = []
    proof_scopes: list[str] = []
    build_permissions: list[bool] = []
    scheduled: list[dict] = []
    vault = Path("unused-vault")

    class Snapshot:
        def __init__(
            self,
            _root: Path,
            *,
            require_live_recall: bool = False,
            expected_recall_checkpoints=None,
            **_kwargs,
        ):
            observed.append(require_live_recall)
            expected_proofs.append(expected_recall_checkpoints or {})

        def projection_key(self, scope: str):
            projected_scopes.append(scope)
            return ("live-projection",)

    def cache_only(
        _root: Path,
        *,
        freshness_key: tuple,
        type_registry,
        allow_build: bool = True,
        expected_recall_checkpoint=None,
    ):
        build_permissions.append(allow_build)
        return None

    monkeypatch.setattr(runtime, "FreshnessSnapshot", Snapshot)
    monkeypatch.setattr(runtime, "load_entity_types", lambda _root: {})
    monkeypatch.setattr(runtime, "load_entity_registry", cache_only)
    monkeypatch.setattr(
        runtime.freshness,
        "recall_checkpoint_is_current",
        lambda _root, scope, _expected: proof_scopes.append(scope) or True,
    )
    monkeypatch.setattr(
        runtime,
        "schedule_entity_registry_warm",
        lambda _root, **kwargs: scheduled.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda _root=None: {"state": "ready", "admitted": True},
    )
    checkpoint = runtime.freshness.RecallFreshnessCheckpoint(
        "instance",
        7,
        (1, 2, "digest"),
        "policy",
        "access",
    )
    proof = {scope: checkpoint for scope in runtime.freshness.SCOPES}
    cue = runtime.detect_cue("my two coastal friends")
    assert cue is not None

    result = runtime.resolve_for_find(
        vault,
        query="my two coastal friends",
        hits=[],
        mode="vector",
        graph=False,
        release=object(),
        purpose=None,
        cue=cue,
        expected_recall_checkpoints=proof,
    )

    assert result is None
    assert observed == [True]
    assert expected_proofs == [proof]
    assert projected_scopes == list(runtime.freshness.SCOPES)
    assert proof_scopes == list(runtime.freshness.SCOPES)
    assert build_permissions == [False]
    assert len(scheduled) == 1
    assert scheduled[0]["freshness_key"] == checkpoint
    assert scheduled[0]["expected_recall_checkpoint"] == checkpoint


def test_managed_referent_stage_omits_block_when_vault_proof_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    checkpoint = runtime.freshness.RecallFreshnessCheckpoint(
        "instance",
        7,
        (1, 2, "digest"),
        "policy",
        "access",
    )
    proof = {scope: checkpoint for scope in runtime.freshness.SCOPES}
    checked: list[str] = []
    verdicts = iter((True, True, True, False))

    class Snapshot:
        def __init__(self, *_args, **_kwargs):
            pass

        def projection_key(self, _scope: str):
            return ("projection",)

    class References:
        def __init__(self, _root: Path):
            pass

        def refs_for_paths(self, _paths):
            return {}

    block = {
        "resolved": [],
        "candidates": [{"path": ENTITY}],
        "expected_count": 1,
    }
    monkeypatch.setattr(runtime, "FreshnessSnapshot", Snapshot)
    monkeypatch.setattr(runtime, "load_entity_types", lambda _root: {})
    monkeypatch.setattr(runtime, "load_entity_registry", lambda *_a, **_k: {ENTITY: object()})
    monkeypatch.setattr(runtime, "_hit_facts", lambda _hits: ())
    monkeypatch.setattr(runtime, "_edge_facts", lambda *_a, **_k: ())
    monkeypatch.setattr(
        runtime,
        "resolve_referents",
        lambda **_kwargs: SimpleNamespace(as_dict=lambda: block),
    )
    monkeypatch.setattr(runtime.egress, "guard_referents", lambda *_a, **_k: block)
    monkeypatch.setattr(runtime.memory_refs, "ReferenceIndex", References)
    monkeypatch.setattr(
        runtime.freshness,
        "recall_checkpoint_is_current",
        lambda _root, scope, _expected: checked.append(scope) or next(verdicts),
    )
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda _root=None: {"state": "ready", "admitted": True},
    )
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    cue = runtime.detect_cue("my two coastal friends")
    assert cue is not None

    result = runtime.resolve_for_find(
        Path("unused-vault"),
        query="my two coastal friends",
        hits=[],
        mode="vector",
        graph=False,
        release=object(),
        purpose=None,
        cue=cue,
        expected_recall_checkpoints=proof,
    )

    assert result is None
    assert checked == ["kb", "vault", "kb", "vault"]


def test_commands_pass_find_catalog_proof_to_referents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    checkpoint = find_module.freshness.RecallFreshnessCheckpoint(
        "instance",
        11,
        (3, 4, "proof"),
        "policy",
        "access",
    )
    proof = {scope: checkpoint for scope in find_module.freshness.SCOPES}
    emitted: list[dict[str, object]] = []
    received: list[dict[str, object] | None] = []

    def fake_find(_root: Path, **kwargs):
        catalog_proof_out = kwargs["catalog_proof_out"]
        catalog_proof_out.update(proof)
        emitted.append(catalog_proof_out)
        return []

    monkeypatch.setattr(find_module, "find", fake_find)
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "load_active_projection_runtime",
        lambda _root: None,
    )
    monkeypatch.setattr(
        commands.projection_runtime_module,
        "requires_projected_read_boundary",
        lambda _root: False,
    )
    monkeypatch.setattr(runtime, "cue_for_find", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "resolve_for_find",
        lambda *_args, **kwargs: received.append(kwargs["expected_recall_checkpoints"]),
    )

    commands.op_find(
        tmp_path,
        query="my two coastal friends",
        limit=10,
        mode="hybrid",
        rerank=False,
    )

    assert received == [proof]
    assert received[0] is emitted[0]


def test_kill_switch_env_disables_resolver(referent_vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_REFERENTS", "1")
    result = _call(referent_vault)
    assert isinstance(result, list) or "referents" not in result


def test_ask_memory_and_find_share_the_referents_leaf(referent_vault: Path) -> None:
    find_result = _call(referent_vault)
    ask_result = commands.op_ask_memory(
        referent_vault,
        query="my two coastal friends",
        limit=10,
        rerank=False,
    )
    assert _block(find_result) == _block(ask_result)


def test_product_case_two_counted_friends_one_captured(referent_vault: Path) -> None:
    block = _block(_call(referent_vault))
    assert block["status"] == "partial"
    assert [item["path"] for item in block["resolved"]] == [ENTITY]
    assert block["unresolved_count"] == 1
    assert NOISE in [item["path"] for item in block["candidates"]]


def test_envelope_threads_anchor_descriptor_knowledge(referent_vault: Path) -> None:
    block = _block(_call(referent_vault, "my two coastal friends when to go"))
    distractor = next(item for item in block["candidates"] if item["path"] == NOISE)
    graph = next(item for item in distractor["evidence"] if item["kind"] == "graph")

    assert graph["seed"] == DISTRACTOR_TOPIC
    assert block["status"] == "partial"
    assert block["unresolved_count"] == 1


def test_referents_resolve_a_vault_defined_entity_type_end_to_end(
    referent_vault: Path,
) -> None:
    _write(
        referent_vault,
        "Knowledge Base/_Schema/entity-types.yaml",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "place": {
                        "folder": "Places",
                        "label": "Place",
                        "aliases": ["location"],
                        "cue_nouns": ["venue"],
                        "capture_guidance": "A stable place identity.",
                        "parent": "concept",
                    }
                },
            },
            sort_keys=False,
        ),
    )
    place = "Knowledge Base/Entities/Places/aster-hall.md"
    _write(
        referent_vault,
        place,
        "---\ntype: entity\ntitle: Aster Hall\nentity_type: place\n"
        "status: active\ntags: [local]\nupdated: 2026-08-04\n---\n# Aster Hall\n",
    )
    find_module.clear_cache()
    importlib.import_module("exomem.entity_registry").clear_entity_registry_cache()

    block = _block(_call(referent_vault, "which venue is Aster Hall"))

    assert block["entity_type"] == "place"
    assert [item["path"] for item in block["resolved"]] == [place]
