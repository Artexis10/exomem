"""ASR-specific accelerator policy stays independent from torch."""

from __future__ import annotations

import pytest

from exomem import asr_runtime


def _probe(*, cuda: bool = True, cuda_types: set[str] | None = None, cpu_types: set[str] | None = None, capability: tuple[int, int] = (12, 0)):
    return asr_runtime.ASRProbe(
        cuda_available=cuda,
        cuda_types=frozenset(cuda_types or {"float16", "int8_float16"}),
        cpu_types=frozenset(cpu_types or {"int8", "float32"}),
        compute_capability=capability if cuda else None,
        reason=None if cuda else "no CUDA device",
    )


def test_normal_uses_admitted_cuda_float16_without_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    selected = asr_runtime.select_asr_runtime(probe=_probe())
    assert (selected.device, selected.compute_type) == ("cuda", "float16")


def test_quiet_is_cpu_even_when_cuda_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    selected = asr_runtime.select_asr_runtime(probe=_probe())
    assert (selected.device, selected.compute_type) == ("cpu", "int8")


def test_explicit_cuda_never_falls_back_when_probe_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ASR_DEVICE", "cuda")
    with pytest.raises(asr_runtime.ASRRuntimeRefusal, match="no CUDA device"):
        asr_runtime.select_asr_runtime(probe=_probe(cuda=False))


def test_auto_concrete_override_can_fallback_only_when_cpu_supports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", "float32")
    selected = asr_runtime.select_asr_runtime(
        probe=_probe(cuda=False, cpu_types={"int8", "float32"})
    )
    assert (selected.device, selected.compute_type) == ("cpu", "float32")


@pytest.mark.parametrize("override", ["auto", "banana"])
def test_non_concrete_override_is_refused(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", override)
    with pytest.raises(asr_runtime.ASRRuntimeRefusal):
        asr_runtime.select_asr_runtime(probe=_probe())


def test_blackwell_rejects_ct2_int8_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ASR_DEVICE", "cuda")
    monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", "int8_float16")
    with pytest.raises(asr_runtime.ASRRuntimeRefusal, match="sm_12"):
        asr_runtime.select_asr_runtime(probe=_probe())


@pytest.mark.parametrize("compute_type", ["int8", "int8_float16", "int8_float32", "int8_bfloat16"])
def test_blackwell_rejects_every_int8_family_type(
    monkeypatch: pytest.MonkeyPatch, compute_type: str
) -> None:
    monkeypatch.setenv("EXOMEM_ASR_DEVICE", "cuda")
    monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", compute_type)
    with pytest.raises(asr_runtime.ASRRuntimeRefusal, match="sm_12"):
        asr_runtime.select_asr_runtime(
            probe=_probe(cuda_types={"float16", compute_type})
        )


def test_explicit_cuda_allows_supported_non_default_concrete_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_ASR_DEVICE", "cuda")
    monkeypatch.setenv("EXOMEM_ASR_COMPUTE_TYPE", "float32")
    selected = asr_runtime.select_asr_runtime(
        probe=_probe(cuda_types={"float32"}, capability=(8, 6))
    )
    assert (selected.device, selected.compute_type) == ("cuda", "float32")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED", True),
        ("CUDA driver version is insufficient", True),
        ("invalid audio data", False),
        ("cublas-demo.m4a", False),
        ("CUDA driver interview.mp3", False),
    ],
)
def test_compute_failure_classification_is_conservative(message: str, expected: bool) -> None:
    assert asr_runtime.is_compute_runtime_failure(RuntimeError(message)) is expected


def test_child_env_precedes_host_cuda_libraries(tmp_path) -> None:
    root = tmp_path / "site-packages" / "nvidia"
    dirs = []
    for package in ("cublas", "cuda_runtime", "cudnn"):
        directory = root / package / "lib"
        directory.mkdir(parents=True)
        dirs.append(str(directory))

    env = asr_runtime.cuda_runtime_child_env(
        {"LD_LIBRARY_PATH": "/system/cuda"}, roots=[root], platform_name="linux"
    )

    assert env["LD_LIBRARY_PATH"].split(":")[:3] == dirs
    assert env["LD_LIBRARY_PATH"].endswith("/system/cuda")
