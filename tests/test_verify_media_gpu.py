"""The GPU verifier must put native imports and real compute in its child."""

from __future__ import annotations

import importlib.metadata
import io
import runpy
from pathlib import Path

import pytest


def _module() -> dict:
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "verify-media-gpu.py"))


def test_verifier_uses_parent_built_child_runtime_environment() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "verify-media-gpu.py").read_text(
        encoding="utf-8"
    )
    assert "--asr-probe-child" in source
    assert "cuda_runtime_child_env(os.environ)" in source
    assert 'env["EXOMEM_ASR_DEVICE"] = "cuda"' in source
    assert "_verify_runtime_distributions" in source
    assert "bounded CPU fallback is not a pass" in source
    assert "list(segments)" in source


def test_verifier_accepts_native_runtime_versions_at_required_floors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {
        "ctranslate2": "4.6.3",
        "nvidia-cublas-cu12": "12.8.4.1",
        "nvidia-cuda-runtime-cu12": "12.8.90",
        "nvidia-cudnn-cu12": "9.5.0.50",
    }
    monkeypatch.setattr(importlib.metadata, "version", versions.__getitem__)
    assert _module()["_verify_runtime_distributions"]() == versions


def test_verifier_rejects_native_runtime_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {
        "ctranslate2": "4.6.3",
        "nvidia-cublas-cu12": "12.0.0",
        "nvidia-cuda-runtime-cu12": "12.8.90",
        "nvidia-cudnn-cu12": "9.5.0.50",
    }
    monkeypatch.setattr(importlib.metadata, "version", versions.__getitem__)
    with pytest.raises(RuntimeError, match="nvidia-cublas-cu12=12.0.0"):
        _module()["_verify_runtime_distributions"]()


def test_verifier_rejects_shadowed_selected_native_path(monkeypatch, tmp_path: Path) -> None:
    from exomem import asr_runtime

    wheel_root = tmp_path / "site-packages" / "nvidia"
    monkeypatch.setattr(asr_runtime, "_nvidia_roots", lambda: [wheel_root])
    with pytest.raises(RuntimeError, match="not wheel-owned"):
        _module()["_verify_selected_native_paths"](
            {"cublas": "/usr/lib/libcublas.so.12", "cudart": "/usr/lib/libcudart.so.12", "cudnn": "/usr/lib/libcudnn.so.9"}
        )


def test_linux_selected_maps_allow_ct2_path_with_only_cublas(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module["platform"], "system", lambda: "Linux")
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: io.StringIO(
            "7f00-7f01 r-xp 00000000 00:00 0 /wheel/nvidia/cublas/lib/libcublasLt.so.12\n"
            "7f02-7f03 r-xp 00000000 00:00 0 /wheel/nvidia/cublas/lib/libcublas.so.12\n"
        ),
    )

    assert module["_selected_native_paths"]() == {
        "cublas": "/wheel/nvidia/cublas/lib/libcublas.so.12",
        "cudart": None,
        "cudnn": None,
    }


@pytest.mark.parametrize(
    ("cudart", "raises"), [("12.8.0", False), ("12.7.9", True), (None, False)]
)
def test_selected_native_cudart_floor_uses_toolkit_precision(cudart, raises: bool) -> None:
    versions = {"cublas": "12.8.4", "cudart": cudart, "cudnn": None}
    verifier = _module()["_verify_selected_native_versions"]
    if raises:
        with pytest.raises(RuntimeError, match="cudart=12.7.9"):
            verifier(versions)
    else:
        assert verifier(versions) == versions
