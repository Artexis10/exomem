from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline

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
        # Policy sits beside residency so the promise/reality gap is readable here
        # rather than in the log. Both are pure env/config reads — no import.
        "preload_policy": False,
        "reap_when_idle": True,
    }
    assert status["media"]["worker_active"] is False
    assert status["asr"] == {
        "device_request_raw": None,
        "compute_type_request_raw": None,
        "device_request": "",
        "compute_type_request": None,
        "effective_policy": "bounded CPU int8 (quiet automatic policy)",
        "mode": "quiet",
        "runtime": "not probed (allocation-free status)",
    }
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


def test_asr_status_discloses_invalid_raw_device_without_probe(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_ASR_DEVICE", "wat")
    status = resource_status.collect()
    assert status["asr"]["device_request_raw"] == "wat"
    assert status["asr"]["device_request"] == "invalid"


@pytest.mark.parametrize(
    ("mode_value", "device", "compute", "expected"),
    [
        ("quiet", None, None, "bounded CPU int8"),
        ("normal", "cpu", None, "CUDA is explicitly disabled"),
        ("normal", "cuda", None, "CUDA float16 required"),
        ("performance", None, "float32", "that exact override"),
        ("normal", "wat", None, "refusal"),
    ],
)
def test_asr_status_resolves_policy_without_runtime_probe(monkeypatch, mode_value, device, compute, expected) -> None:
    monkeypatch.setenv("EXOMEM_MODE", mode_value)
    if device is not None:
        monkeypatch.setenv("EXOMEM_ASR_DEVICE", device)
    if compute is not None:
        monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", compute)
    assert expected in resource_status.asr_runtime_status()["effective_policy"]


@pytest.mark.parametrize(
    ("mode_value", "device", "compute", "expected"),
    [
        ("quiet", None, "float32", "bounded CPU float32 (quiet automatic policy)"),
        ("normal", "cpu", "float32", "bounded CPU float32; CUDA is explicitly disabled"),
        (
            "normal",
            "cuda",
            "bfloat16",
            "CUDA bfloat16 required; no CPU fallback after refusal or runtime failure",
        ),
        ("normal", None, None, "automatic CUDA float16 when admitted, otherwise bounded CPU int8"),
    ],
)
def test_asr_status_discloses_effective_override_policy_exactly(
    monkeypatch, mode_value, device, compute, expected
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", mode_value)
    if device is not None:
        monkeypatch.setenv("EXOMEM_ASR_DEVICE", device)
    if compute is not None:
        monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", compute)
    assert resource_status.asr_runtime_status()["effective_policy"] == expected


def test_status_cli_json_is_resource_status(monkeypatch, capsys, tmp_path: Path) -> None:
    _forbid_torch_import(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    initialize_vault_state_offline(tmp_path, source="resource status CLI fixture")

    from exomem.__main__ import main

    assert main(["status", "--resources", "--json", "--vault", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "normal"
    assert data["policy"]["retain_cpu_caches"] is False
    assert data["cuda"]["torch_imported"] is False


def _hide_from_which(monkeypatch, name: str) -> None:
    """Make `shutil.which` miss one name only.

    `shutil.which` is a module global that pytest's own error reporting also
    calls (`which("git", path=...)`). A blanket stub with an incompatible
    signature turns any failure in these tests into an INTERNALERROR that hides
    the real assertion.
    """
    real = resource_status.shutil.which

    def _which(cmd, *args, **kwargs):
        return None if cmd == name else real(cmd, *args, **kwargs)

    monkeypatch.setattr(resource_status.shutil, "which", _which)


def test_nvidia_smi_is_found_off_path_under_wsl(monkeypatch, tmp_path: Path) -> None:
    """WSL exposes the host driver but leaves it off a login shell's PATH.

    Missing it makes the probe report `unknown`, which auto_quiet.decide() turns
    into "pressure probe unavailable" — the detector fails silent, not loud.
    """
    shim = tmp_path / "nvidia-smi"
    shim.write_text("#!/bin/sh\n")
    shim.chmod(0o755)
    _hide_from_which(monkeypatch, "nvidia-smi")
    monkeypatch.setattr(resource_status, "_EXTRA_NVIDIA_SMI_PATHS", (str(shim),))

    assert resource_status._find_nvidia_smi() == str(shim)


def test_a_non_executable_fallback_is_not_treated_as_a_probe(
    monkeypatch, tmp_path: Path
) -> None:
    """os.X_OK, not existence: a present-but-unrunnable path is not a probe."""
    shim = tmp_path / "nvidia-smi"
    shim.write_text("#!/bin/sh\n")
    shim.chmod(0o644)
    _hide_from_which(monkeypatch, "nvidia-smi")
    monkeypatch.setattr(resource_status, "_EXTRA_NVIDIA_SMI_PATHS", (str(shim),))

    assert resource_status._find_nvidia_smi() is None


def test_gpu_headroom_is_unknown_when_no_probe_exists(monkeypatch) -> None:
    _forbid_torch_import(monkeypatch)
    _hide_from_which(monkeypatch, "nvidia-smi")
    monkeypatch.setattr(resource_status, "_EXTRA_NVIDIA_SMI_PATHS", ())

    gpu = resource_status.gpu_headroom()

    assert gpu["status"] == "unknown"
    assert gpu["usable"] is None
