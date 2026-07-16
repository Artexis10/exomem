"""`exomem doctor` install-readiness preflight.

The checks stay torch-free in the suite: profile-specific dependency availability
is exercised by stubbing the import-spec seam rather than importing heavy extras.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import doctor as doctor_module
from exomem.__main__ import main


@pytest.fixture(autouse=True)
def _clear_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_PROFILE", raising=False)


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


def test_lexical_check_uses_escaped_immutable_query_only_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import lexstore

    vault = tmp_path / "vault #% name"
    sidecar = lexstore.lexical_path(vault)
    sidecar.parent.mkdir(parents=True)
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    real_connect = sqlite3.connect
    connections: list[tuple[object, dict[str, object]]] = []
    statements: list[str] = []

    def traced_connect(database, *args, **kwargs):
        connections.append((database, kwargs.copy()))
        opened = real_connect(database, *args, **kwargs)
        opened.set_trace_callback(statements.append)
        return opened

    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: True)
    monkeypatch.setattr(doctor_module.sqlite3, "connect", traced_connect)

    check = doctor_module._check_lexical(vault)

    assert check.status == "pass"
    [(database, kwargs)] = connections
    assert database == f"{sidecar.resolve().as_uri()}?mode=ro&immutable=1"
    assert kwargs["uri"] is True
    assert any(statement.upper().startswith("PRAGMA QUERY_ONLY") for statement in statements)


def test_lexical_check_reads_clean_wal_database_without_creating_sidecars(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    conn = sqlite3.connect(sidecar)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO pages DEFAULT VALUES")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    wal = sidecar.with_name(f"{sidecar.name}-wal")
    shm = sidecar.with_name(f"{sidecar.name}-shm")
    assert not wal.exists()
    assert not shm.exists()

    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: True)

    check = doctor_module._check_lexical(vault)

    assert check.status == "pass"
    assert "1 pages indexed" in check.message
    assert not wal.exists()
    assert not shm.exists()


def test_lexical_check_uses_live_readonly_connection_when_wal_sidecars_exist(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    writer = sqlite3.connect(sidecar)
    real_connect = sqlite3.connect
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO pages DEFAULT VALUES")
        writer.commit()
        wal = sidecar.with_name(f"{sidecar.name}-wal")
        shm = sidecar.with_name(f"{sidecar.name}-shm")
        assert wal.exists()
        assert shm.exists()
        connections: list[str] = []

        def record_connect(database, *args, **kwargs):
            connections.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
        monkeypatch.setattr(lexstore, "fts5_available", lambda: True)
        monkeypatch.setattr(doctor_module.sqlite3, "connect", record_connect)

        check = doctor_module._check_lexical(vault)

        assert check.status == "pass"
        assert "1 pages indexed" in check.message
        assert connections == [f"{sidecar.resolve().as_uri()}?mode=ro"]
    finally:
        writer.close()


@pytest.mark.parametrize("change", ["wal", "identity"])
def test_lexical_immutable_snapshot_refuses_state_change_during_query(
    vault: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    wal = sidecar.with_name(f"{sidecar.name}-wal")
    real_connect = sqlite3.connect

    class ChangingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        def execute(self, statement: str, *args, **kwargs):
            result = self.connection.execute(statement, *args, **kwargs)
            if statement.startswith("SELECT count"):
                if change == "wal":
                    wal.write_bytes(b"appeared during snapshot")
                else:
                    info = sidecar.stat()
                    os.utime(
                        sidecar,
                        ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000),
                    )
            return result

    def changing_connect(database, *args, **kwargs):
        return ChangingConnection(real_connect(database, *args, **kwargs))

    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: True)
    monkeypatch.setattr(doctor_module.sqlite3, "connect", changing_connect)

    check = doctor_module._check_lexical(vault)

    assert check.status == "warn"
    assert "unreadable" in check.message


def test_media_runtime_requests_diagnostic_snapshot(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    calls: list[bool] = []

    def fake_status(_vault: Path, *, diagnostic_snapshot: bool = False):
        calls.append(diagnostic_snapshot)
        return {
            "healthy": True,
            "counts": {state: 0 for state in media_jobs.STATES},
        }

    monkeypatch.setattr(media_jobs, "status", fake_status)

    check = doctor_module._check_media_runtime(vault)

    assert check is not None and check.status == "pass"
    assert calls == [True]


def test_doctor_json_cli(vault: Path, capsys) -> None:
    code, out, err = _run(["doctor", "--vault", str(vault), "--json"], capsys)

    assert code == 0, err
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["profile"] == "lean"
    assert {"id", "status", "message", "remediation"} <= set(payload["checks"][0])


def test_doctor_cli_loads_cwd_dotenv_and_promotes_loaded_legacy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXOMEM_PROFILE", raising=False)
    monkeypatch.delenv("KB_MCP_PROFILE", raising=False)
    calls: list[tuple[object, bool]] = []
    seen: dict[str, str | None] = {}

    def load_env(*, dotenv_path=None, override: bool) -> None:
        calls.append((dotenv_path, override))
        monkeypatch.setenv("KB_MCP_PROFILE", "remote")

    def fake_doctor(**_kwargs):
        seen["profile"] = os.environ.get("EXOMEM_PROFILE")
        return doctor_module.DoctorReport(profile="remote", checks=[])

    monkeypatch.setattr("dotenv.load_dotenv", load_env)
    monkeypatch.setattr(doctor_module, "doctor", fake_doctor)

    code, _out, err = _run(["doctor", "--json"], capsys)

    assert code == 0, err
    assert calls == [(tmp_path / ".env", True)]
    assert seen == {"profile": "remote"}


def test_doctor_infers_profile_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_PROFILE", "hybrid")

    assert doctor_module.infer_profile() == "hybrid"


def test_standard_profile_is_valid_for_env_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_PROFILE", "standard")
    assert doctor_module.infer_profile() == "standard"


def test_doctor_infers_hybrid_from_installed_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        doctor_module,
        "_module_available",
        lambda name: name in {"sentence_transformers", "torch", "PIL"},
    )
    assert doctor_module.infer_profile() == "hybrid"


def test_doctor_infers_media_when_full_stack_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module, "_module_available", lambda _name: True)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/usr/bin/tesseract")
    assert doctor_module.infer_profile() == "media"


def test_doctor_infers_lean_without_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(doctor_module, "_module_available", lambda _name: False)
    assert doctor_module.infer_profile() == "lean"


def test_doctor_disable_embeddings_forces_lean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(doctor_module, "_module_available", lambda _name: True)
    assert doctor_module.infer_profile() == "lean"


def test_standard_profile_accepts_missing_tesseract_as_degraded_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)

    check = doctor_module._check_tesseract(required=False)

    assert check.status == "warn"
    assert "Tesseract" in check.message



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


def test_runtime_process_check_warns_for_multiple_stdio_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_module,
        "_list_exomem_processes",
        lambda: [
            {"pid": 101, "rss_mb": 4096.0, "command": "python -m exomem --transport stdio"},
            {"pid": 102, "rss_mb": 4096.0, "command": "python -m exomem --transport stdio"},
        ],
    )

    check = doctor_module._check_runtime_processes()

    assert check is not None
    assert check.status == "warn"
    assert "Each stdio MCP client/session launches its own process" in check.message
    assert check.details["count"] == 2



def test_mps_headroom_reports_policy_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import extract, mode, warmup

    monkeypatch.setattr(doctor_module, "_mps_available_for_doctor", lambda: True)
    monkeypatch.setattr(mode, "resolve_mode", lambda: "normal")
    monkeypatch.setattr(
        mode,
        "watcher_policy",
        lambda: SimpleNamespace(max_embed_files_per_batch=32),
    )
    monkeypatch.setattr(warmup, "model_preload_allowed", lambda _mode: False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)

    check = doctor_module._check_mps_headroom()

    assert check is not None
    assert check.status == "pass"
    assert "macOS does not expose" in check.message
    assert check.details["model_preload_allowed"] is False
    assert check.details["asr_prewarm_enabled"] is False
    assert check.details["watcher_max_embed_files"] == 32
