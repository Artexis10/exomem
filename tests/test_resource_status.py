from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

from exomem import resource_status


def _forbid_torch_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    for module in (
        "exomem.embeddings",
        "exomem.bm25",
        "exomem.find",
        "exomem.index_sync",
    ):
        monkeypatch.delitem(sys.modules, module, raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("resource status must not import torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_collect_does_not_import_torch_or_probe_cuda(monkeypatch, tmp_path: Path) -> None:
    _forbid_torch_import(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "quiet")

    status = resource_status.collect(tmp_path)

    assert status["mode"] == "quiet"
    assert status["cuda"] == {
        "torch_imported": False,
        "initialized": False,
        "memory": None,
    }
    assert status["models"] == {
        "module_loaded": False,
        "embeddings": False,
        "reranker": False,
        "clip": False,
    }
    assert status["media"]["worker_active"] is False
    assert not (tmp_path / "Knowledge Base" / ".media-jobs.sqlite").exists()


def test_collect_reports_already_loaded_modules_without_loading_missing_ones(
    monkeypatch, tmp_path: Path
) -> None:
    fake_embeddings = types.SimpleNamespace(
        _MODEL=object(),
        _RERANKER=None,
        _CLIP_MODEL=object(),
        index_cache_status=lambda: {
            "embedding": {"loaded": 1, "indexes": 1, "rows": 3, "bytes": 96},
            "clip": {"loaded": 0, "indexes": 0, "rows": 0, "bytes": 0},
        },
    )
    fake_bm25 = types.SimpleNamespace(
        cache_status=lambda: {
            "loaded": True,
            "corpora": 1,
            "documents": 2,
            "tokenized_documents": 2,
            "tokens": 7,
        }
    )
    fake_find = types.SimpleNamespace(
        cache_status=lambda: {
            "pages": {"entries": 2, "body_chars": 10},
            "resolver": {"entries": 1},
            "hot_results": {"entries": 1, "hits": 3},
        }
    )
    monkeypatch.setitem(sys.modules, "exomem.embeddings", fake_embeddings)
    monkeypatch.setitem(sys.modules, "exomem.bm25", fake_bm25)
    monkeypatch.setitem(sys.modules, "exomem.find", fake_find)
    from exomem import deferred_index

    deferred_index.add(tmp_path, ["Knowledge Base/Notes/x.md"])

    status = resource_status.collect(tmp_path)

    assert status["models"]["embeddings"] is True
    assert status["models"]["reranker"] is False
    assert status["models"]["clip"] is True
    assert status["caches"]["vector_matrices"]["embedding"]["rows"] == 3
    assert status["caches"]["bm25"]["tokens"] == 7
    assert status["caches"]["find"]["hot_results"]["hits"] == 3
    assert status["deferred_work"]["semantic_upserts"]["count"] == 1


def test_status_cli_json_is_resource_status(monkeypatch, capsys, tmp_path: Path) -> None:
    _forbid_torch_import(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "normal")

    from exomem.__main__ import main

    assert main(["status", "--resources", "--json", "--vault", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "normal"
    assert data["policy"]["retain_cpu_caches"] is False
    assert data["cuda"]["torch_imported"] is False
