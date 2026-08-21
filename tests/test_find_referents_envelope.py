from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from exomem import commands, epistemic_graph
from exomem import embeddings as embeddings_module
from exomem import find as find_module

TOPIC = "Knowledge Base/Notes/Research/coastal-season.md"
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

A trusted contact.

## Relations
- relates_to [[{TOPIC[:-3]}]]
""",
    )
    _write(
        vault,
        NOISE,
        """---
type: entity
title: Beryl Moss
entity_type: person
status: active
updated: 2026-08-03
---
# Beryl Moss

Two coastal friends are mentioned here only as retrieval wording.
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
