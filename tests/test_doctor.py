"""`exomem doctor` install-readiness preflight.

The checks stay torch-free in the suite: profile-specific dependency availability
is exercised by stubbing the import-spec seam rather than importing heavy extras.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import doctor as doctor_module
from exomem import vault
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


def test_doctor_includes_observability_check(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    report = doctor_module.doctor(vault=str(vault))
    checks = {c.id: c for c in report.checks}
    assert "observability" in checks
    assert checks["observability"].status in {"pass", "warn"}
    assert checks["observability"].details["log_dir_writable"] is True


def test_observability_check_fails_when_log_dir_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom_resolve_log_dir():
        return tmp_path / "does" / "not" / "exist" / "\x00bad"

    monkeypatch.setattr(
        "exomem.logging_config.resolve_log_dir", boom_resolve_log_dir
    )
    check = doctor_module._check_observability()
    assert check.status == "fail"
    assert check.details["log_dir_writable"] is False


def test_observability_check_warns_on_stale_service_pile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    for i in range(51):
        (log_dir / f"service.out.log.{i}").write_text("x", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    check = doctor_module._check_observability()
    assert check.status == "warn"
    assert check.details["service_pile_count"] == 51


def test_observability_check_warns_on_unparseable_jsonl_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "queries.jsonl").write_text("not json\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    check = doctor_module._check_observability()
    assert check.status == "warn"
    assert "queries.jsonl" in check.message


@pytest.fixture()
def _isolated_lease_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from exomem import writer_lease as writer_lease_module

    writer_lease_module.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "state"))
    yield writer_lease_module.get_manager()
    writer_lease_module.reset_managers_for_tests()


def test_idempotency_store_check_passes_when_healthy(_isolated_lease_manager) -> None:
    check = doctor_module._check_idempotency_store()
    assert check.status == "pass"
    assert check.details["abandoned"] == 0


def test_idempotency_store_check_warns_on_abandoned_receipts(_isolated_lease_manager) -> None:
    store = _isolated_lease_manager.idempotency
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES ('k1', 'd1', 'abandoned', ?, NULL)",
            (0.0,),
        )
    check = doctor_module._check_idempotency_store()
    assert check.status == "warn"
    assert "abandoned" in check.message
    assert check.details["abandoned"] == 1


def test_idempotency_store_check_warns_on_stale_live_receipt(_isolated_lease_manager) -> None:
    store = _isolated_lease_manager.idempotency
    with sqlite3.connect(store.path) as connection:
        connection.executemany(
            "INSERT INTO mutations(key, digest, state, updated_at, owner) "
            "VALUES (?, ?, ?, ?, NULL)",
            [
                ("live-key-secret", "live-digest-secret", "executing", 0.0),
                ("reserved-key-secret", "reserved-digest-secret", "reserved", time.time()),
                ("pending-key-secret", "pending-digest-secret", "pending", time.time()),
            ],
        )
    check = doctor_module._check_idempotency_store()
    assert check.status == "warn"
    assert "oldest pending" in check.message
    assert check.details == {
        "pending": 3,
        "abandoned": 0,
        "oldest_pending_age_seconds": pytest.approx(time.time(), abs=1.0),
    }
    assert "live-key-secret" not in json.dumps(check.details)
    assert "live-digest-secret" not in json.dumps(check.details)
    assert "reserved-key-secret" not in json.dumps(check.details)
    assert "pending-key-secret" not in json.dumps(check.details)


def test_idempotency_store_check_warns_on_completed_outcome_unknown(
    _isolated_lease_manager,
) -> None:
    from exomem import writer_lease as writer_lease_module

    store = _isolated_lease_manager.idempotency
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO mutations(key, digest, state, result, updated_at, owner) "
            "VALUES ('unknown-key-secret', 'unknown-digest-secret', 'completed', ?, ?, NULL)",
            (writer_lease_module._OUTCOME_UNKNOWN_PAYLOAD, 0.0),
        )
    check = doctor_module._check_idempotency_store()
    assert check.status == "warn"
    assert "1 abandoned idempotency receipt" in check.message
    assert check.details == {
        "pending": 0,
        "abandoned": 1,
        "oldest_pending_age_seconds": None,
    }
    assert "unknown-key-secret" not in json.dumps(check.details)
    assert "unknown-digest-secret" not in json.dumps(check.details)


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


@pytest.mark.parametrize(
    "live_suffixes",
    [("-wal",), ("-shm",), ("-wal", "-shm")],
)
def test_lexical_check_refuses_live_sqlite_companions_without_connecting(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_suffixes: tuple[str, ...],
) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    companions = [sidecar.with_name(f"{sidecar.name}{suffix}") for suffix in live_suffixes]
    for index, companion in enumerate(companions):
        companion.write_bytes(f"live-{index}".encode())
    before = {
        companion: (companion.read_bytes(), companion.stat()) for companion in companions
    }

    def reject_connect(*_args, **_kwargs):
        pytest.fail("doctor must not sqlite-connect while WAL/SHM exists")

    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: True)
    monkeypatch.setattr(doctor_module.sqlite3, "connect", reject_connect)

    check = doctor_module._check_lexical(vault)

    assert check.status == "warn"
    assert "unreadable" in check.message
    for companion, (content, info) in before.items():
        after = companion.stat()
        assert companion.read_bytes() == content
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )


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


def test_doctor_infers_hybrid_from_onnx_backend_without_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A torch-free ONNX image holds a working embedder and must not be demoted.

    Demotion to `lean` is not cosmetic: it is how a hosted cell would come to
    advertise keyword-only recall while carrying the weights for semantic search.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        doctor_module,
        "_module_available",
        lambda name: name in {"onnxruntime", "tokenizers"},
    )
    assert doctor_module.infer_profile() == "hybrid"


def test_embedding_requirements_follow_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    extra, requirements = doctor_module._embedding_requirements()
    assert extra == "embeddings-onnx"
    assert [name for _, name in requirements] == ["onnxruntime", "tokenizers"]

    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "torch")
    extra, requirements = doctor_module._embedding_requirements()
    assert extra == "embeddings"
    assert "torch" in {name for _, name in requirements}


def test_embedding_requirements_survive_an_invalid_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """doctor diagnoses configuration; it must not raise on the one it is diagnosing."""
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "tensorflow")
    extra, _requirements = doctor_module._embedding_requirements()
    assert extra == "embeddings"


def test_doctor_omits_torch_probes_on_the_onnx_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    report = doctor_module.doctor(vault=str(tmp_path), profile="hybrid")
    ids = {check.id for check in report.checks}

    assert "dep.torch" not in ids
    assert "torch.cuda" not in ids
    assert "dep.onnxruntime" in ids
    assert "dep.tokenizers" in ids


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


def test_doctor_resolves_profile_explicit_then_env_then_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_PROFILE", "media")
    monkeypatch.setattr(doctor_module.install_info, "configured_local_profile", lambda: "hybrid")

    assert doctor_module.resolve_profile("standard") == "standard"
    assert doctor_module.resolve_profile(None) == "media"

    monkeypatch.delenv("EXOMEM_PROFILE")
    assert doctor_module.resolve_profile(None) == "hybrid"


def test_editable_lock_check_uses_selected_runtime_extras_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        doctor_module.install_info, "editable_project_root_status", lambda: (tmp_path, None)
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)

    def run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 1, stdout="stale lock state", stderr="")

    monkeypatch.setattr(doctor_module.subprocess, "run", run)
    check = doctor_module._check_editable_lock_parity("standard")

    assert check.status == "fail"
    assert calls["command"] == [
        "/bin/uv", "sync", "--check", "--locked", "--no-dev", "--active", "--project", str(tmp_path),
        "--offline", "--no-cache", "--inexact", "--extra", "embeddings", "--extra", "media",
    ]
    assert calls["kwargs"]["env"]["VIRTUAL_ENV"] == sys.prefix
    assert "stale lock state" in check.message
    assert "uv sync --check --locked --no-dev --active --project" in check.remediation


def test_editable_lock_check_warns_when_uv_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        doctor_module.install_info, "editable_project_root_status", lambda: (tmp_path, None)
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: "/bin/uv")
    monkeypatch.setattr(
        doctor_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("uv", 10)),
    )

    check = doctor_module._check_editable_lock_parity("hybrid")

    assert check.status == "warn"
    assert "timed out" in check.message


def test_editable_lock_check_warns_for_unverifiable_editable_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_module.install_info,
        "editable_project_root_status",
        lambda: (None, "malformed direct_url.json"),
    )

    check = doctor_module._check_editable_lock_parity("hybrid")

    assert check is not None
    assert check.status == "warn"
    assert "malformed direct_url.json" in check.message


def test_editable_lock_check_treats_unsupported_safety_flags_as_unverifiable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        doctor_module.install_info, "editable_project_root_status", lambda: (tmp_path, None)
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: "/bin/uv")
    monkeypatch.setattr(
        doctor_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            2,
            "",
            "error: unexpected argument '--inexact'",
        ),
    )

    check = doctor_module._check_editable_lock_parity("hybrid")

    assert check.status == "warn"
    assert "uv version" in check.message


def test_darwin_memory_uses_the_complete_v0_abi_and_physical_footprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import process_memory

    calls: list[tuple[int, int]] = []

    class ProcPidRusage:
        def __call__(self, pid, flavor, usage_pointer):
            calls.append((pid, flavor))
            usage_pointer._obj.ri_phys_footprint = 3 * 1024 * 1024
            return 0

    class LibProc:
        proc_pid_rusage = ProcPidRusage()

    monkeypatch.setattr(process_memory.sys, "platform", "darwin")
    monkeypatch.setattr(process_memory.ctypes, "CDLL", lambda *_args, **_kwargs: LibProc())

    assert process_memory.ctypes.sizeof(process_memory._RusageInfoV0) == 96
    assert process_memory.enrich_process_memory(42, 1.0) == {
        "memory_mb": 3.0,
        "memory_metric": "physical_footprint",
        "physical_footprint_mb": 3.0,
    }
    assert calls == [(42, 0)]


def test_darwin_memory_falls_back_to_labelled_rss_on_native_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import process_memory

    class ProcPidRusage:
        def __call__(self, _pid, _flavor, _usage_pointer):
            return -1

    class LibProc:
        proc_pid_rusage = ProcPidRusage()

    monkeypatch.setattr(process_memory.sys, "platform", "darwin")
    monkeypatch.setattr(process_memory.ctypes, "CDLL", lambda *_args, **_kwargs: LibProc())

    assert process_memory.enrich_process_memory(42, 7.5) == {
        "memory_mb": 7.5,
        "memory_metric": "rss",
        "physical_footprint_mb": None,
    }


def test_process_memory_mixed_rows_keep_physical_and_rss_totals_separate() -> None:
    from exomem import process_memory

    aggregate = process_memory.aggregate_memory([
        {"pid": 1, "rss_mb": 100.0, "memory_mb": 150.0, "memory_metric": "physical_footprint"},
        {"pid": 2, "rss_mb": 80.0, "memory_mb": 80.0, "memory_metric": "rss"},
    ])

    assert aggregate == {
        "memory_metric": "mixed",
        "rss_mb_total": 180.0,
        "physical_footprint_mb_total": 150.0,
        "rss_fallback_mb_total": 80.0,
    }


def test_standard_profile_accepts_missing_tesseract_as_degraded_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import extract as extract_module

    monkeypatch.delenv("EXOMEM_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(extract_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(extract_module, "TESSERACT_INSTALL_CANDIDATES", ())

    check = doctor_module._check_tesseract(required=False)

    assert check.status == "warn"
    assert "Tesseract" in check.message


def test_doctor_finds_tesseract_at_a_standard_install_location_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#302: doctor FAILed on a host where extraction would have worked.

    The UB-Mannheim Windows package installs to a documented location and does
    not add it to PATH. Doctor checked only EXOMEM_TESSERACT_CMD and PATH, so
    `scripts/upgrade.ps1 -Profile media` refused a safe restart on an install
    whose runtime dependency was present and usable.
    """
    from exomem import extract as extract_module

    installed = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    installed.parent.mkdir(parents=True)
    installed.write_text("binary", encoding="utf-8")

    monkeypatch.delenv("EXOMEM_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(extract_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(extract_module, "TESSERACT_INSTALL_CANDIDATES", (str(installed),))

    check = doctor_module._check_tesseract(required=True)

    assert check.status == "pass"
    assert str(installed) in check.message


def test_doctor_and_runtime_cannot_hold_separate_tesseract_candidate_sets() -> None:
    """The drift guard #302 asks for: one discovery function, not two lists."""
    from exomem import extract as extract_module

    source = Path(doctor_module.__file__).read_text(encoding="utf-8")
    assert "resolve_tesseract_cmd" in source, "doctor must use the shared resolver"
    for candidate in extract_module.TESSERACT_INSTALL_CANDIDATES:
        assert candidate not in source, "doctor must not re-declare install locations"


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
    # The finding names the lane actually configured to serve, so an ONNX
    # install is never told the torch stack is the thing it is missing.
    assert "serving stack isn't installed" in check.message


def test_sidecar_probe_uses_the_onnx_lane_when_that_is_what_is_installed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#487: gating the probe on torch reported a working ONNX lane as absent.

    `embeddings-onnx` installs neither torch nor sentence_transformers by design,
    so a torch-shaped probe concludes "no vector stack" on an install whose
    vector search demonstrably works — and doctor then offers no way to prove
    readiness, which `docs/benchmark-fairness-contract.md` requires.
    """
    _sidecar(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    monkeypatch.setattr(
        doctor_module,
        "_module_available",
        lambda m: m in {"onnxruntime", "tokenizers"},
    )

    check = doctor_module._check_embedding_sidecar(vault)

    # It must get past the stack gate. Whatever it reports next is about the
    # sidecar or the model cache — never "the stack isn't installed".
    assert "serving stack isn't installed" not in check.message


def test_rebuild_remediations_name_a_command_the_cli_actually_dispatches() -> None:
    """#479: `kb reconcile` / `kb audit_fix` fall through to the server parser.

    Both are internal registry names; the CLI dispatches on the public product
    names, so the remediation has to name `maintain` / `maintain_memory`.
    """
    assert "reconcile" in doctor_module._REBUILD_VECTORS_CMD
    assert "maintain" in doctor_module._REBUILD_VECTORS_CMD
    source = Path(doctor_module.__file__).read_text(encoding="utf-8")
    assert "kb reconcile" not in source
    assert "kb audit_fix" not in source


def test_orphan_remediation_names_the_command_that_actually_reclaims(
    tmp_path: Path,
) -> None:
    """The orphan fix pointed at the audit, which cannot unlink anything.

    `graph_sync.sweep_abandoned_temporaries` is the only thing that removes
    these files, and it has exactly one caller: inside `reconcile.reconcile()`,
    gated on `not dry_run`. Bare `exomem maintain` is the read-only audit, so an
    operator who followed the old text watched it exit 0, saw nothing reclaimed,
    and had no reason to suspect the command rather than the diagnosis. A remedy
    that reports success while doing nothing is worse than naming no remedy.

    Asserted through the check itself rather than by reading the source: what
    matters is the string an operator is handed, not how it gets assembled.
    """
    kb = tmp_path / doctor_module.kb_dirname()
    kb.mkdir(parents=True)
    stale = time.time() - (vault.REBUILD_TEMP_STALE_AGE_SECONDS + 600)
    # FAIL takes either count or bytes. The live incident tripped it on bytes
    # (one 131 MB file); tripping it on count here buys the same tier without
    # writing 50 MB to disk in a unit test.
    for index in range(doctor_module._REBUILD_TEMP_ORPHAN_FAIL_COUNT + 1):
        orphan = kb / f".lexical.sqlite.rebuild-{index:032x}.tmp"
        orphan.write_bytes(b"x" * 4096)
        os.utime(orphan, (stale, stale))

    check = doctor_module._check_rebuild_temp_orphans(tmp_path)

    assert check.status == "fail", check.message
    assert check.remediation is not None
    assert doctor_module._REBUILD_VECTORS_CMD in check.remediation
    assert "--reconcile" in check.remediation
    # Order matters as much as the verb: reclaiming under a live rebuild would
    # delete a temporary that is still being written.
    assert "Stop the exomem service" in check.remediation


def test_a_fresh_rebuild_temporary_is_not_reported_as_an_orphan(
    tmp_path: Path,
) -> None:
    """Otherwise the remediation would create the orphan it claims to clean up.

    A live rebuild's temporary is identical by name to an abandoned one and can
    legitimately be large, so only mtime separates them. If a fresh one tripped
    the check, an operator following the remediation would stop the service
    mid-rebuild and delete a file that was still being written.
    """
    kb = tmp_path / doctor_module.kb_dirname()
    kb.mkdir(parents=True)
    (kb / ".lexical.sqlite.rebuild-29cf190343754d70ac4bdf2e40358384.tmp").write_bytes(
        b"x" * 4096
    )

    check = doctor_module._check_rebuild_temp_orphans(tmp_path)

    assert check.status == "pass", check.message
    assert check.details is not None
    assert check.details["count"] == 1
    assert check.details["stale_count"] == 0


def test_warm_exits_zero_when_only_the_withheld_torch_models_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """#480: `warm` exited 1 on a successful `embeddings-onnx` run.

    The bi-encoder — the only model that lane can serve — loads fine. The
    reranker and CLIP raise ImportError because `embeddings-onnx` withholds
    sentence-transformers by design, which is the documented trade-off, not a
    failure. Reporting it as one also called the install "lean".
    """
    from exomem import embeddings as embeddings_module

    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)

    def _no_sentence_transformers():
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setattr(embeddings_module, "get_model", lambda: object())
    monkeypatch.setattr(embeddings_module, "get_reranker", _no_sentence_transformers)
    monkeypatch.setattr(embeddings_module, "get_clip_model", _no_sentence_transformers)
    monkeypatch.setattr(embeddings_module, "clip_enabled", lambda: True)

    code, out, _err = _run(["warm"], capsys)

    assert code == 0
    assert "lean install" not in out
    assert "onnx" in out


def test_models_cache_does_not_demand_torch_models_on_the_onnx_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#480/#487: the reranker and CLIP can never be cached on an ONNX install.

    `exomem warm` — the stated remediation — cannot fetch them there either, so
    the WARN is permanent noise that no action clears.
    """
    monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "onnx")
    monkeypatch.setattr(
        doctor_module, "_model_cached", lambda _hub, dirname: "bge-base" in dirname
    )

    check = doctor_module._check_models_cache()

    assert check.status == "pass"
    assert "reranker" not in check.message.lower()


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


def test_runtime_process_check_names_physical_footprint_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor_module,
        "_list_exomem_processes",
        lambda: [
            {
                "pid": 101,
                "rss_mb": 100.0,
                "memory_mb": 275.0,
                "memory_metric": "physical_footprint",
                "physical_footprint_mb": 275.0,
                "command": "python -m exomem --transport streamable-http",
            }
        ],
    )

    check = doctor_module._check_runtime_processes()

    assert check is not None
    assert "275.0 MB physical footprint" in check.message
    assert check.details["memory_metric"] == "physical_footprint"



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


# ---- torch.cuda: an NVIDIA host running a CPU wheel is a regression, not a config ----
#
# `uv pip install` ignores [tool.uv.sources], so upgrading a service venv silently
# swaps the CUDA build for the PyPI CPU one. That used to surface as a warn, which
# DoctorReport.success ignores, so every preflight kept passing on a dead GPU.


def _fake_cpu_torch() -> SimpleNamespace:
    """A torch that imports fine but cannot reach any GPU."""
    return SimpleNamespace(
        __version__="2.13.0+cpu",
        cuda=SimpleNamespace(is_available=lambda: False),
        backends=SimpleNamespace(mps=None),
        version=SimpleNamespace(cuda=None),
    )


@pytest.fixture
def _cpu_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "_module_available", lambda name: True)
    monkeypatch.setitem(sys.modules, "torch", _fake_cpu_torch())
    monkeypatch.delenv("EXOMEM_ALLOW_CPU_TORCH", raising=False)


def test_torch_cuda_fails_when_nvidia_present_but_torch_is_cpu_only(
    _cpu_torch: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    check = doctor_module._check_torch_cuda()

    assert check.status == "fail"
    assert "CPU-only build" in check.message
    # The remediation must name the pinned index, since that is the non-obvious part.
    assert "download.pytorch.org/whl/cu132" in (check.remediation or "")


def test_torch_cuda_warns_on_a_host_with_no_gpu_at_all(
    _cpu_torch: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CPU-only box is a supported deployment and must stay non-fatal."""
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: None)

    check = doctor_module._check_torch_cuda()

    assert check.status == "warn"


def test_torch_cuda_escape_hatch_downgrades_the_failure_to_a_warning(
    _cpu_torch: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opting into CPU on a GPU host is legitimate; it just has to be deliberate."""
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setenv("EXOMEM_ALLOW_CPU_TORCH", "1")

    check = doctor_module._check_torch_cuda()

    assert check.status == "warn"


# ------------------------------------------ the process census runs everywhere


def test_the_process_census_is_not_disabled_on_windows() -> None:
    """The check exists to surface what per-session servers cost (#597).

    It returned `[]` on Windows unconditionally, so on the platform where the
    8.1-GB-across-seven measurement was actually taken, the check that reports
    it never fired. Pinned as a source assertion rather than through a live
    census because CI's Linux runners cannot exercise the Windows branch.
    """
    import inspect

    source = inspect.getsource(doctor_module._list_exomem_processes)
    assert 'os.name == "nt"' in source
    assert "return []" not in source, "the Windows branch is back to answering nothing"
    assert "_windows_process_samples" in source


def test_both_platform_censuses_share_one_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane that selects differently reports a different thing under one name.

    The filter is the check's actual definition of "an exomem session": it has
    to tell a server from any other python.exe, and a session from a media
    worker child. Two copies of that would drift, and the drift would be
    invisible -- each platform would look correct on its own runner.
    """
    samples = [
        (101, 300 * 1024 * 1024, "python -m exomem --transport stdio", None),
        (102, 50 * 1024 * 1024, "python -m exomem.media_worker_child --parent 101", None),
        (103, 90 * 1024 * 1024, "python -m http.server", None),
        (104, 10 * 1024 * 1024, "notepad.exe exomem-notes.txt", None),
    ]

    seen = []
    for platform, attribute in (("nt", "_windows_process_samples"), ("posix", "_posix_process_samples")):
        monkeypatch.setattr(doctor_module.os, "name", platform)
        monkeypatch.setattr(doctor_module, attribute, lambda: samples)
        seen.append(doctor_module._list_exomem_processes())

    windows_rows, posix_rows = seen
    assert windows_rows == posix_rows
    # Only the session survives: the worker child, the unrelated server, and the
    # editor that merely has "exomem" in its arguments are all excluded.
    assert [row["pid"] for row in windows_rows] == [101]
    assert windows_rows[0]["rss_mb"] == 300.0


def _stub_shell_lookup(monkeypatch: pytest.MonkeyPatch, found: str | None) -> None:
    """Answer only for the shells this census looks for, defer for everything else.

    `doctor_module.shutil` is the global module, and the repo conftest calls
    `shutil.which("git", path=os.defpath)` while building a failure report -- a
    stub narrower than that takes down the whole session's teardown, not just
    this test.
    """
    real_which = doctor_module.shutil.which

    def which(name, *args, **kwargs):  # noqa: ANN001, ANN202
        if name in {"powershell", "pwsh"}:
            return found
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(doctor_module.shutil, "which", which)


def test_the_windows_census_parses_pipes_inside_a_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The separator is also a legal character in the thing it separates.

    Splitting with a bound keeps the command intact; splitting without one would
    truncate any command containing a pipe, and the filter reads the command.
    """
    _stub_shell_lookup(monkeypatch, "powershell")
    monkeypatch.setattr(
        doctor_module,
        "_run_process_census",
        lambda _command, _timeout: (
            "101|314572800|3900000000|python -m exomem --transport stdio --log 'a|b'\n"
            "notanumber|1|1|python -m exomem\n"
            "\n"
            "103|1048576|2048|\n"
        ),
    )

    samples = doctor_module._windows_process_samples()

    assert samples == [
        (101, 314572800, "python -m exomem --transport stdio --log 'a|b'", 3900000000)
    ]


def test_a_census_that_cannot_answer_is_not_a_doctor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No shell, a non-zero exit, or a timeout all mean "no reading taken".

    A diagnostic that raises is worse than one that declines: it takes down every
    other check in the same run.
    """
    _stub_shell_lookup(monkeypatch, None)
    assert doctor_module._windows_process_samples() == []

    _stub_shell_lookup(monkeypatch, "powershell")
    monkeypatch.setattr(doctor_module, "_run_process_census", lambda _c, _t: None)
    assert doctor_module._windows_process_samples() == []


def test_the_census_command_bounds_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows pays PowerShell startup before the query, so it gets more room.

    Both bounds still exist, because a doctor check may be slow enough to be
    worth waiting for and must never hang.
    """
    recorded: list[float] = []

    def _record(command, timeout):  # noqa: ANN001, ANN202, ARG001
        recorded.append(timeout)
        return None

    monkeypatch.setattr(doctor_module, "_run_process_census", _record)
    _stub_shell_lookup(monkeypatch, "powershell")
    doctor_module._windows_process_samples()
    doctor_module._posix_process_samples()

    assert recorded == [
        doctor_module._WINDOWS_PROCESS_CENSUS_TIMEOUT_SECONDS,
        doctor_module._PROCESS_CENSUS_TIMEOUT_SECONDS,
    ]
    assert 0 < recorded[1] < recorded[0]


def test_a_trimmed_working_set_does_not_read_as_a_free_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows reclaims an idle process's working set; its commit does not move.

    Measured on one box while writing this: the same loaded server read 1195.8 MB
    resident with 3677 MB committed, and minutes later the whole set of three
    read 310.5 MB resident. A check that reported only the resident figure would
    tell a user their idle sessions cost nothing, which is the opposite of what
    this check exists to say (#597).
    """
    monkeypatch.setattr(
        doctor_module,
        "_list_exomem_processes",
        lambda: [
            {
                "pid": 101,
                "rss_mb": 1.1,
                "memory_mb": 1.1,
                "memory_metric": "rss",
                "private_commit_mb": 3677.0,
                "command": "python -m exomem --transport stdio",
            }
        ],
    )

    check = doctor_module._check_runtime_processes()

    assert check is not None
    assert "3677.0 MB private commit" in check.message
    assert check.details["processes"][0]["private_commit_mb"] == 3677.0


def test_a_platform_without_a_commit_figure_says_nothing_about_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ps` has no portable commit column, and Linux RSS does not evaporate.

    The clause has to be absent rather than zero there, or every POSIX run
    reports a number it never measured.
    """
    monkeypatch.setattr(
        doctor_module,
        "_list_exomem_processes",
        lambda: [
            {
                "pid": 101,
                "rss_mb": 300.0,
                "memory_mb": 300.0,
                "memory_metric": "rss",
                "command": "python -m exomem --transport stdio",
            }
        ],
    )

    check = doctor_module._check_runtime_processes()

    assert check is not None
    assert "private commit" not in check.message
    assert "about 300.0 MB RSS total" in check.message
