"""`exomem doctor` install-readiness preflight.

The checks stay torch-free in the suite: profile-specific dependency availability
is exercised by stubbing the import-spec seam rather than importing heavy extras.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from exomem import doctor as doctor_module
from exomem.__main__ import main


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_doctor_lean_passes_with_fixture_vault(vault: Path) -> None:
    report = doctor_module.doctor(vault=str(vault))

    assert report.profile == "lean"
    assert report.success is True
    checks = {c.id: c for c in report.checks}
    assert checks["python.version"].status == "pass"
    assert checks["vault.path"].status == "pass"
    assert checks["command.registry"].status == "pass"


def test_doctor_json_cli(vault: Path, capsys) -> None:
    code, out, err = _run(["doctor", "--vault", str(vault), "--json"], capsys)

    assert code == 0, err
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["profile"] == "lean"
    assert {"id", "status", "message", "remediation"} <= set(payload["checks"][0])


def test_doctor_missing_vault_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    report = doctor_module.doctor()

    assert report.success is False
    vault_check = next(c for c in report.checks if c.id == "vault.path")
    assert vault_check.status == "fail"
    assert "--vault" in (vault_check.remediation or "")


def test_doctor_profile_missing_dependency_fails(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = doctor_module.importlib.util.find_spec

    def fake_find_spec(name: str):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name)

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module.importlib.util, "find_spec", fake_find_spec)

    report = doctor_module.doctor(vault=str(vault), profile="hybrid")

    assert report.success is False
    dep = next(c for c in report.checks if c.id == "dep.sentence-transformers")
    assert dep.status == "fail"
    assert "uv sync --extra embeddings" in (dep.remediation or "")


def test_doctor_human_output_includes_remediation(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    code, out, err = _run(["doctor"], capsys)

    assert code == 1
    assert err == ""
    assert "FAIL" in out
    assert "vault.path" in out
    assert "fix:" in out


def test_doctor_gpu_advisory_is_safe_on_cp1252_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for Windows legacy consoles: advisory text must be encodable."""
    from exomem import doctor as doctor_module
    from exomem import mode as mode_module
    from exomem import resource_status

    class Cp1252Stdout(io.StringIO):
        encoding = "cp1252"

        def write(self, text: str) -> int:
            text.encode("cp1252")
            return super().write(text)

    stdout = Cp1252Stdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        doctor_module,
        "doctor",
        lambda **kw: doctor_module.DoctorReport(profile=kw.get("profile", "lean"), checks=[]),
    )
    monkeypatch.setattr(resource_status, "gpu_headroom", lambda: {"usable": True})
    monkeypatch.setattr(mode_module, "resolve_mode", lambda: "normal")

    assert main(["doctor"]) == 0
    assert "A capable idle GPU was detected" in stdout.getvalue()


def test_doctor_unknown_profile_exits_2(capsys) -> None:
    code, _out, err = _run(["doctor", "--profile", "bogus"], capsys)

    assert code == 2
    assert "invalid choice" in err


# ---------------------------------------------------------------------------
# _check_embedding_sidecar — LIVE embed+search probe (upgraded from a presence
# check). The guard branches below stay model-free; the real probe is heavy and
# marked `embeddings`.
# ---------------------------------------------------------------------------


def _sidecar(vault: Path) -> Path:
    p = vault / "Knowledge Base" / ".embeddings.sqlite"
    p.touch()
    return p


def test_sidecar_missing_warns(vault: Path) -> None:
    check = doctor_module._check_embedding_sidecar(vault)
    assert check is not None
    assert check.status == "warn"
    assert "missing" in check.message


def test_sidecar_present_but_embeddings_disabled_skips_probe(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sidecar(vault)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")  # conftest default, explicit here

    check = doctor_module._check_embedding_sidecar(vault)

    assert check.status == "warn"
    assert "EXOMEM_DISABLE_EMBEDDINGS" in check.message


def test_sidecar_present_but_stack_missing_skips_probe(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sidecar(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module, "_module_available", lambda _m: False)

    check = doctor_module._check_embedding_sidecar(vault)

    assert check.status == "warn"
    assert "vector stack" in check.message


def test_sidecar_present_but_model_not_cached_skips_probe(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """doctor never downloads: an uncached model → skip the live probe, not fetch."""
    _sidecar(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module, "_module_available", lambda _m: True)
    monkeypatch.setattr(doctor_module, "_model_cached", lambda _hub, _dir: False)

    check = doctor_module._check_embedding_sidecar(vault)

    assert check.status == "warn"
    assert "HF cache" in check.message


def test_sidecar_present_but_probe_raises_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-broken sidecar (embed/search raises) → fail, not a false pass."""
    from exomem import embeddings as embeddings_module

    _sidecar(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module, "_module_available", lambda _m: True)
    monkeypatch.setattr(doctor_module, "_model_cached", lambda _hub, _dir: True)

    def _boom(*_a, **_k):
        raise RuntimeError("dimension mismatch")

    monkeypatch.setattr(embeddings_module, "embed_texts", _boom)

    check = doctor_module._check_embedding_sidecar(vault)

    assert check.status == "fail"
    assert "probe failed" in check.message


@pytest.mark.embeddings
def test_sidecar_live_probe_passes_on_built_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: build real vectors, then the live embed+search probe passes."""
    # Skip on lean installs (no `embeddings` extra) — the lean CI `tests` job
    # runs every test, so the @pytest.mark.embeddings marker alone doesn't
    # deselect this; the importorskip is what its siblings use to skip cleanly.
    pytest.importorskip("sentence_transformers")
    from exomem import embeddings as embeddings_module

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    embeddings_module._IMPORT_FAILED = False
    embeddings_module.clear_embedding_indexes()
    rows = embeddings_module.get_embedding_index(vault).rebuild_all()
    assert rows > 0

    check = doctor_module._check_embedding_sidecar(vault)

    assert check.status == "pass", check.message
    assert "live" in check.message


def test_resource_posture_check_cpu_unknown_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import resource_status

    monkeypatch.setattr(
        resource_status,
        "gpu_headroom",
        lambda: {
            "status": "unknown",
            "usable": None,
            "reason": "nvidia-smi not found",
            "min_free_mb": 2048,
        },
    )

    check = doctor_module._check_resource_posture("lean")

    assert check.status == "pass"
    assert "CPU is the supported baseline" in check.message
    assert check.as_dict()["details"]["gpu"]["status"] == "unknown"


def test_resource_posture_check_marginal_gpu_warns_for_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import resource_status

    monkeypatch.setattr(
        resource_status,
        "gpu_headroom",
        lambda: {
            "status": "marginal",
            "usable": False,
            "reason": "free VRAM below policy threshold",
            "free_mb": 512,
            "total_mb": 8192,
            "min_free_mb": 2048,
        },
    )

    check = doctor_module._check_resource_posture("hybrid")

    assert check.status == "warn"
    assert "free VRAM below policy threshold" in check.message
    assert check.as_dict()["details"]["gpu"]["usable"] is False


def test_resource_posture_reports_container_variant_without_cuda_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import resource_status

    monkeypatch.setenv("EXOMEM_CONTAINER_VARIANT", "cuda")
    monkeypatch.setattr(
        resource_status,
        "gpu_headroom",
        lambda: {
            "status": "capable",
            "usable": True,
            "free_mb": 8192,
            "total_mb": 16384,
            "min_free_mb": 2048,
        },
    )
    monkeypatch.setitem(sys.modules, "torch", None)

    check = doctor_module._check_resource_posture("hybrid")
    details = check.as_dict()["details"]

    assert check.status == "pass"
    assert "Runtime is container(cuda)" in check.message
    assert details["runtime"]["kind"] == "container"
    assert details["runtime"]["variant"] == "cuda"
    assert details["cuda"] == {"torch_imported": False, "initialized": False, "memory": None}
