"""Local install preflight checks for exomem.

`doctor` is deliberately CLI-only and read-only: it inspects the host, Python
environment, vault path, optional dependency imports, and environment variables.
It never initializes a vault, writes `.env`, starts services, downloads models,
or mutates the embedding sidecar.

One deliberate exception to the "imports only" rule: on the hybrid/media
profiles the embedding-sidecar check runs a LIVE embed+search probe, which loads
the embedding model into memory to prove the vector lane actually works (a
presence check passes on an empty or model-mismatched sidecar). It stays within
the other guarantees — it loads only an ALREADY-CACHED model (skips the probe
rather than trigger a download) and the search is read-only (never mutates the
sidecar).

The one network exception is explicit opt-in: `--probe`. The remote profile
verifies the live connector endpoints; the HA profile verifies explicit replica
readiness origins. Without the flag, doctor performs zero network calls.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .kbdir import kb_dirname, kb_prefix

Status = Literal["pass", "warn", "fail"]
Profile = Literal["lean", "hybrid", "standard", "media", "remote", "ha"]
VALID_PROFILES: tuple[Profile, ...] = (
    "lean",
    "hybrid",
    "standard",
    "media",
    "remote",
    "ha",
)
PROFILE_ENV = "EXOMEM_PROFILE"
HA_AUTH_ENV_KEYS = (
    "EXOMEM_WRITER_LEASE_URL",
    "EXOMEM_WRITER_LEASE_VAULT_ID",
    "EXOMEM_WRITER_LEASE_REPLICA_ID",
    "EXOMEM_WRITER_LEASE_TOKEN",
    "EXOMEM_LEASE_COORDINATOR_TOKEN",
    "EXOMEM_OAUTH_STORAGE_URL",
    "EXOMEM_OAUTH_STORAGE_NAMESPACE",
    "EXOMEM_OAUTH_STORAGE_TOKEN",
    "EXOMEM_HA_REPLICA_URLS",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DoctorCheck:
    id: str
    status: Status
    message: str
    remediation: str | None = None
    details: dict | None = None

    def as_dict(self) -> dict:
        data = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
        }
        if self.details is not None:
            data["details"] = self.details
        return data


@dataclass
class DoctorReport:
    profile: Profile
    checks: list[DoctorCheck]

    @property
    def success(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "profile": self.profile,
            "checks": [c.as_dict() for c in self.checks],
        }


def _check(
    id_: str,
    status: Status,
    message: str,
    remediation: str | None = None,
    *,
    details: dict | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        id=id_,
        status=status,
        message=message,
        remediation=remediation,
        details=details,
    )


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def infer_profile() -> Profile:
    """Infer the highest locally installed profile without importing models."""
    raw = (os.environ.get(PROFILE_ENV) or "").strip().lower()
    if raw:
        if raw not in VALID_PROFILES:
            raise ValueError(f"unknown {PROFILE_ENV}: {raw!r}. Valid: {list(VALID_PROFILES)}")
        return raw  # type: ignore[return-value]
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return "lean"
    embeddings_ready = all(
        _module_available(name) for name in ("sentence_transformers", "torch", "PIL")
    )
    if not embeddings_ready:
        return "lean"
    media_ready = all(
        _module_available(name)
        for name in ("faster_whisper", "pytesseract", "fitz", "markitdown")
    )
    if media_ready:
        return "media" if shutil.which("tesseract") else "standard"
    return "hybrid"


def _resolve_vault(vault: str | None) -> tuple[Path | None, DoctorCheck]:
    raw = vault or os.environ.get("EXOMEM_VAULT_PATH")
    if not raw:
        return None, _check(
            "vault.path",
            "fail",
            "No vault path supplied and EXOMEM_VAULT_PATH is unset.",
            "Set EXOMEM_VAULT_PATH to the vault root or pass --vault. Then run "
            "`uv run python -m exomem init --vault <path>` if the vault is new.",
        )

    path = Path(raw).expanduser()
    skill = path / kb_dirname() / "_Schema" / "SKILL.md"
    if not skill.exists():
        return path, _check(
            "vault.path",
            "fail",
            f"{path} does not contain {kb_prefix()}_Schema/SKILL.md.",
            f"Pass the vault root, not the {kb_dirname()} folder. For a new vault, run "
            "`uv run python -m exomem init --vault <path>`.",
        )
    return path, _check("vault.path", "pass", f"Vault found at {path}.")


def _check_python() -> DoctorCheck:
    version = ".".join(str(p) for p in sys.version_info[:3])
    running_version = (sys.version_info.major, sys.version_info.minor)
    required_version = (3, 11)
    if running_version < required_version:
        return _check(
            "python.version",
            "fail",
            f"Python {version} is too old; exomem requires Python 3.11+.",
            "Install Python 3.11+ or let uv provision it with `uv sync`.",
        )
    return _check("python.version", "pass", f"Python {version} satisfies >=3.11.")


def _check_uv() -> DoctorCheck:
    uv = shutil.which("uv")
    if uv:
        return _check("tool.uv", "pass", f"uv found at {uv}.")
    return _check(
        "tool.uv",
        "warn",
        "uv was not found on PATH.",
        "Install uv for the documented deterministic path: https://docs.astral.sh/uv/",
    )


def _check_console_scripts() -> DoctorCheck:
    found = [name for name in ("exomem", "kb") if shutil.which(name)]
    if found:
        return _check("cli.entrypoint", "pass", f"Console script(s) on PATH: {', '.join(found)}.")
    return _check(
        "cli.entrypoint",
        "warn",
        "No `exomem` or `kb` console script found on PATH.",
        "Run through `uv run python -m exomem ...`, or install the package into the active "
        "environment with `uv sync` / `pip install -e .`.",
    )


def _check_package_import() -> DoctorCheck:
    try:
        importlib.import_module("exomem")
    except Exception as e:  # noqa: BLE001 - this is a diagnostic boundary
        return _check(
            "package.import",
            "fail",
            f"Could not import exomem: {e}",
            "Run `uv sync` from the repo root, then retry with `uv run python -m exomem doctor`.",
        )
    return _check("package.import", "pass", "exomem imports successfully.")


def _check_registry() -> DoctorCheck:
    try:
        from . import commands

        names = [c.name for c in commands.commands_for("cli", expose_tier2=True)]
    except Exception as e:  # noqa: BLE001 - report setup/import breakage
        return _check(
            "command.registry",
            "fail",
            f"Command registry failed to build: {e}",
            "Run `uv sync` and retry. If this persists, run the test suite.",
        )
    return _check("command.registry", "pass", f"Command registry built ({len(names)} CLI ops).")


def _check_repo_env() -> DoctorCheck:
    candidates = [Path.cwd() / ".env", _REPO_ROOT / ".env"]
    if any(p.exists() for p in candidates):
        return _check("env.file", "pass", "A .env file is visible.")
    return _check(
        "env.file",
        "warn",
        "No .env file found in the current directory or repo root.",
        "This is fine for stdio if env vars are passed by the client. For service/remote use, "
        "copy env.example to .env and fill it in (or run `exomem setup --remote`).",
    )


def _check_schema_files(vault_root: Path | None) -> list[DoctorCheck]:
    if vault_root is None:
        return []
    kb = vault_root / kb_dirname()
    checks: list[DoctorCheck] = []
    required = [
        ("vault.schema", kb / "_Schema" / "SKILL.md", f"{kb_dirname()} schema contract"),
        ("vault.index", kb / "index.md", f"{kb_prefix()}index.md"),
        ("vault.log", kb / "log.md", f"{kb_prefix()}log.md"),
        (
            "vault.project_keys",
            kb / "_Schema" / "project-keys.yaml",
            "project key registry",
        ),
    ]
    for id_, path, label in required:
        if path.exists():
            checks.append(_check(id_, "pass", f"{label} exists."))
        else:
            status: Status = "fail" if id_ == "vault.schema" else "warn"
            checks.append(_check(
                id_,
                status,
                f"{label} is missing.",
                "Run `uv run python -m exomem init --vault <path>` for a new vault, or "
                "restore the missing scaffold file from src/exomem/_scaffold/.",
            ))
    return checks


def _check_dependency(module: str, extra: str, *, import_name: str | None = None) -> DoctorCheck:
    name = import_name or module
    if _module_available(name):
        return _check(f"dep.{module}", "pass", f"{module} is importable.")
    return _check(
        f"dep.{module}",
        "fail",
        f"{module} is not installed.",
        f"Install the requested capability with `uv sync --extra {extra}`.",
    )


def _check_resource_posture(profile: Profile) -> DoctorCheck:
    from . import resource_status

    posture = resource_status.resource_posture()
    runtime = posture["runtime"]
    runtime_label = runtime["kind"]
    if runtime.get("variant"):
        runtime_label += f"({runtime['variant']})"
    gpu = posture["gpu"]
    mode_name = posture["mode"]
    if gpu.get("usable") is False:
        status: Status = "pass" if profile == "lean" else "warn"
        reason = gpu.get("reason") or "GPU is not usable under current policy"
        return _check(
            "resource.posture",
            status,
            f"Runtime is {runtime_label}; resource mode is {mode_name}; CPU is the "
            f"supported baseline. {reason}.",
            "Use `exomem mode quiet` before foreground GPU work, or `exomem mode "
            "performance` only when enough free VRAM is available.",
            details=posture,
        )
    if gpu.get("usable") is True:
        return _check(
            "resource.posture",
            "pass",
            f"Runtime is {runtime_label}; resource mode is {mode_name}; GPU headroom "
            "probe is capable, but GPU use remains explicit policy opt-in.",
            details=posture,
        )
    return _check(
        "resource.posture",
        "pass",
        f"Runtime is {runtime_label}; resource mode is {mode_name}; CPU is the "
        "supported baseline and GPU headroom is unknown without an available "
        "non-torch probe.",
        details=posture,
    )


def _sqlite_snapshot_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sqlite_companions(path: Path) -> tuple[Path, Path]:
    return path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")


def _sqlite_companion_exists(companions: tuple[Path, Path]) -> bool:
    return any(os.path.lexists(item) for item in companions)


def _lexical_page_count(path: Path) -> int:
    companions = _sqlite_companions(path)
    if _sqlite_companion_exists(companions):
        raise OSError("lexical sidecar has live SQLite companions")
    identity = _sqlite_snapshot_identity(path)
    with path.open("rb") as stream:
        if not stream.read(1):
            raise OSError("lexical sidecar is empty")
    if (
        _sqlite_snapshot_identity(path) != identity
        or _sqlite_companion_exists(companions)
    ):
        raise OSError("lexical sidecar is not a stable standalone snapshot")

    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        conn.execute("PRAGMA query_only = ON")
        count = int(conn.execute("SELECT count(*) FROM pages").fetchone()[0])
    finally:
        conn.close()

    if (
        _sqlite_snapshot_identity(path) != identity
        or _sqlite_companion_exists(companions)
    ):
        raise OSError("lexical sidecar changed during diagnostic snapshot")
    return count


def _check_lexical(vault_root: Path | None) -> DoctorCheck:
    """Lexical FTS5 backend availability + sidecar health.

    `warn`, never `fail` — every unavailable case soft-falls back to the
    in-process rank-bm25/substring paths with unchanged results. Runs on the
    lean profile: the bm25/keyword lanes this serves are lean-install lanes.
    """
    from . import lexstore

    if lexstore.backend() == "python":
        return _check(
            "dep.fts5-lexical",
            "warn",
            "EXOMEM_LEXICAL_BACKEND=python: the indexed lexical backend is "
            "switched off; bm25/keyword lanes scan in-process (O(N) per query).",
            "Unset EXOMEM_LEXICAL_BACKEND (or set it to `auto`) to re-enable.",
        )
    if not lexstore.fts5_available():
        return _check(
            "dep.fts5-lexical",
            "warn",
            "This Python's SQLite lacks FTS5/trigram; bm25/keyword lanes scan "
            "in-process (O(N) per query).",
            "Use a CPython build with the standard bundled SQLite (3.34+).",
        )
    if vault_root is None:
        return _check("dep.fts5-lexical", "pass", "FTS5 + trigram are available.")
    side = lexstore.lexical_path(vault_root)
    if not side.exists():
        return _check(
            "dep.fts5-lexical",
            "pass",
            "FTS5 + trigram are available; the lexical sidecar will be built "
            "on first search (or by warm-up).",
        )
    try:
        n = _lexical_page_count(side)
    except (OSError, sqlite3.Error) as e:
        return _check(
            "dep.fts5-lexical",
            "warn",
            f"Lexical sidecar exists but is unreadable ({e}); lanes fall back "
            "to the in-process paths.",
            f"Delete {side.name} — it is rebuilt from markdown on next use.",
        )
    return _check(
        "dep.fts5-lexical",
        "pass",
        f"FTS5 lexical sidecar healthy ({n} pages indexed).",
    )


def _check_sqlite_vec() -> DoctorCheck:
    """vec0 backend availability: package import + a live loadability probe.

    `warn`, never `fail` — an importable package can still be unloadable when this
    Python's sqlite3 was compiled without loadable-extension support, and in every
    unavailable case vector search soft-falls back to the exact in-memory scan.
    """
    if not _module_available("sqlite_vec"):
        return _check(
            "dep.sqlite-vec",
            "warn",
            "sqlite-vec is not installed; vector search uses the in-memory scan.",
            "Install with `uv sync --extra embeddings` for SQL-native vector KNN "
            "inside the sidecars.",
        )
    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            version = conn.execute("SELECT vec_version()").fetchone()[0]
        finally:
            conn.close()
    except (AttributeError, sqlite3.Error) as e:
        return _check(
            "dep.sqlite-vec",
            "warn",
            f"sqlite-vec is installed but this Python cannot load it ({e}); "
            "vector search uses the in-memory scan.",
            "This Python's sqlite3 lacks loadable-extension support; use a CPython "
            "build with extension loading enabled.",
        )
    return _check("dep.sqlite-vec", "pass", f"sqlite-vec loads (vec_version {version}).")


def _check_embeddings_disabled() -> DoctorCheck:
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return _check(
            "embeddings.enabled",
            "fail",
            "EXOMEM_DISABLE_EMBEDDINGS is set, so hybrid/vector search is disabled.",
            "Unset EXOMEM_DISABLE_EMBEDDINGS for hybrid search after installing "
            "`uv sync --extra embeddings`.",
        )
    return _check("embeddings.enabled", "pass", "EXOMEM_DISABLE_EMBEDDINGS is not set.")


def _check_torch_cuda() -> DoctorCheck:
    if not _module_available("torch"):
        return _check(
            "torch.cuda",
            "fail",
            "torch is not installed, so GPU acceleration cannot be checked.",
            "Install embeddings with `uv sync --extra embeddings`.",
        )
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            arches = ", ".join(torch.cuda.get_arch_list())
            return _check("torch.cuda", "pass", f"CUDA visible to torch: {name} ({arches}).")
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available() and mps.is_built():
            return _check(
                "torch.cuda",
                "pass",
                "Apple Silicon MPS (Metal) backend available — bge/CLIP embeddings will "
                "use the GPU. Note: faster-whisper (ASR) has no Metal path and stays on CPU.",
            )
        # An NVIDIA host running CPU torch is a regression, not a configuration.
        # `uv pip install` (unlike `uv sync`) ignores [tool.uv.sources], so any
        # plain upgrade of the service venv silently swaps the CUDA wheel for the
        # PyPI CPU one and moves embeddings/media onto the CPU with no other
        # symptom. Warning was too quiet: DoctorReport.success is fail-driven, so
        # every preflight kept passing while the GPU sat idle.
        if shutil.which("nvidia-smi") and not os.environ.get("EXOMEM_ALLOW_CPU_TORCH"):
            build = getattr(getattr(torch, "version", None), "cuda", None)
            detail = (
                "this is a CPU-only build"
                if build is None
                else f"the build targets CUDA {build} but sees no device"
            )
            return _check(
                "torch.cuda",
                "fail",
                f"An NVIDIA driver is present (nvidia-smi found) but torch "
                f"{getattr(torch, '__version__', '?')} cannot use CUDA — {detail}.",
                "Reinstall the CUDA build of the SAME version from the pinned index, e.g. "
                "`uv pip install --python <venv> --default-index "
                "https://download.pytorch.org/whl/cu132 torch==<version>+cu132` — or run "
                "scripts/upgrade.ps1, which repairs this automatically. Set "
                "EXOMEM_ALLOW_CPU_TORCH=1 to accept CPU on this host deliberately.",
            )
        return _check(
            "torch.cuda",
            "warn",
            "torch imports but no GPU (CUDA or MPS) is available; embeddings/media run on CPU.",
            "This is supported. On NVIDIA hosts verify the uv torch source and driver; on "
            "Apple Silicon ensure a recent arm64 torch wheel (default PyPI ships MPS).",
        )
    except Exception as e:  # noqa: BLE001
        return _check(
            "torch.cuda",
            "warn",
            f"torch imports failed during GPU probe: {e}",
            "Re-run `uv sync --extra embeddings`; on GPU hosts, verify the CUDA/torch wheel.",
        )


def _check_torch_device() -> DoctorCheck:
    """Report the device the torch models (bge/CLIP) will actually select — read-only,
    loads no model."""
    if not _module_available("torch"):
        return _check(
            "torch.device",
            "warn",
            "torch not installed; embeddings fall back to lexical/CPU.",
            "Install with `uv sync --extra embeddings`.",
        )
    try:
        from . import accel

        return _check("torch.device", "pass", f"bge/CLIP embeddings will run on: {accel.select_device()}.")
    except Exception as e:  # noqa: BLE001
        return _check("torch.device", "warn", f"torch device probe failed: {e}")


def _check_asr_backend() -> DoctorCheck:
    """Report which ASR backend get_transcriber() selects — read-only, loads no model."""
    try:
        from . import extract

        backend = type(extract.get_transcriber()).__name__
    except Exception as e:  # noqa: BLE001
        return _check("asr.backend", "warn", f"ASR backend probe failed: {e}")
    if backend == "MlxWhisperBackend":
        return _check("asr.backend", "pass", "ASR: mlx-whisper (Apple Silicon Metal GPU).")
    return _check(
        "asr.backend",
        "pass",
        "ASR: faster-whisper (CUDA/CPU). On Apple Silicon, add `--extra media-mlx` for Metal.",
    )


def _mps_available_for_doctor() -> bool:
    if sys.platform != "darwin" or not _module_available("torch"):
        return False
    try:
        import torch

        mps = getattr(torch.backends, "mps", None)
        return bool(mps is not None and mps.is_available() and mps.is_built())
    except Exception:  # noqa: BLE001
        return False


def _check_mps_headroom() -> DoctorCheck | None:
    if not _mps_available_for_doctor():
        return None
    from . import extract, mode, warmup

    policy = mode.watcher_policy()
    return _check(
        "mps.headroom",
        "pass",
        "Apple Silicon MPS is available. macOS does not expose a stable non-torch "
        "free-memory probe for Metal, so Exomem uses policy controls: lazy model "
        "preload by default, live-write burst deferral, and explicit indexing for imports.",
        details={
            "model_preload_allowed": warmup.model_preload_allowed(mode.resolve_mode()),
            "asr_prewarm_enabled": extract.asr_prewarm_enabled(),
            "watcher_max_embed_files": policy.max_embed_files_per_batch,
        },
    )


def _check_asr_prewarm() -> DoctorCheck:
    from . import extract

    enabled = extract.asr_prewarm_enabled()
    if enabled:
        return _check(
            "asr.prewarm",
            "pass",
            "ASR prewarm is enabled; the media worker may load the ASR model at startup.",
            "Set EXOMEM_ASR_PREWARM=0 to lazy-load ASR on the first media job.",
        )
    return _check(
        "asr.prewarm",
        "pass",
        "ASR prewarm is disabled by policy; the model lazy-loads on the first media job.",
    )


def _check_media_runtime(vault_root: Path | None) -> DoctorCheck | None:
    if vault_root is None:
        return None
    from . import media_jobs

    status = media_jobs.status(vault_root, diagnostic_snapshot=True)
    if not status["healthy"]:
        return _check(
            "media.runtime",
            "warn",
            "The durable media job store is unreadable.",
            "Check permissions on Knowledge Base/.media-jobs.sqlite or remove the derived "
            "sidecar and restart so pending evidence can be reconstructed.",
            details=status,
        )
    counts = status["counts"]
    blocked = int(counts.get("blocked", 0))
    failed = int(counts.get("failed", 0))
    if blocked or failed:
        return _check(
            "media.runtime",
            "warn",
            f"Media work needs attention: {blocked} blocked, {failed} failed.",
            "Install the missing media engine or fix the failed input, then restart the "
            "service to retry blocked work.",
            details=status,
        )
    queued = int(counts.get("pending", 0)) + int(counts.get("running", 0))
    return _check(
        "media.runtime",
        "pass",
        f"Durable media runtime healthy ({queued} queued/running).",
        details=status,
    )


def _list_exomem_processes() -> list[dict[str, object]]:
    if os.name == "nt":
        return []
    ps = shutil.which("ps")
    if not ps:
        return []
    try:
        result = subprocess.run(
            [ps, "-axo", "pid=,rss=,command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return []
    if result.returncode != 0:
        return []
    rows: list[dict[str, object]] = []
    current_pid = os.getpid()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        if pid == current_pid:
            continue
        command_l = command.lower()
        if "exomem" not in command_l:
            continue
        if "exomem.media_worker_child" in command_l:
            continue
        if "--transport" not in command_l and "python -m exomem" not in command_l:
            continue
        rows.append({"pid": pid, "rss_mb": round(rss_kb / 1024, 1), "command": command[:180]})
    return rows


def _check_runtime_processes() -> DoctorCheck | None:
    rows = _list_exomem_processes()
    if not rows:
        return None
    total = round(sum(float(row.get("rss_mb") or 0.0) for row in rows), 1)
    count = len(rows)
    status: Status = "warn" if count > 1 else "pass"
    return _check(
        "runtime.processes",
        status,
        f"Detected {count} other exomem server process(es) using about {total} MB RSS total. "
        "Each stdio MCP client/session launches its own process; use HTTP service mode "
        "or lazy/quiet policies on small-memory Macs.",
        details={"count": count, "rss_mb_total": total, "processes": rows[:8]},
    )


def _check_embedding_sidecar(vault_root: Path | None) -> DoctorCheck | None:
    """LIVE embed+search probe of the embedding sidecar.

    A static file-existence check passes on a sidecar that is empty, schema-
    drifted, or was built by an incompatible model — the exact silent-degradation
    states in which hybrid search quietly falls back to BM25. So when the sidecar
    is present AND probing is possible without a model DOWNLOAD (doctor never
    downloads), this actually embeds a query and searches the index: a real hit
    proves the whole vector lane works end to end; an exception (fail) or an empty
    result (warn) surfaces the broken-but-present case a presence check missed.
    The probe is read-only — it never writes or rebuilds the sidecar.
    """
    if vault_root is None:
        return None
    sidecar = vault_root / kb_dirname() / ".embeddings.sqlite"
    if not sidecar.exists():
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar is missing; hybrid search will degrade until vectors are built.",
            "After installing embeddings, run `kb reconcile` or `kb audit_fix --rebuild-embeddings true`.",
        )
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar exists but EXOMEM_DISABLE_EMBEDDINGS is set, so the live "
            "probe was skipped.",
            "Unset EXOMEM_DISABLE_EMBEDDINGS to run the embed+search probe against it.",
        )
    if not (_module_available("sentence_transformers") and _module_available("torch")):
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar exists but the vector stack isn't installed, so it can't "
            "be probed.",
            "Install it with `uv sync --extra embeddings` to enable hybrid search.",
        )
    from . import embeddings

    bge_dir = "models--" + embeddings.MODEL_NAME.replace("/", "--")
    if not _model_cached(_hf_hub_dir(), bge_dir):
        # doctor must never trigger a download — skip the live probe rather than
        # let embed_texts() fetch the model over the network.
        return _check(
            "embeddings.sidecar",
            "warn",
            f"Embedding sidecar exists but {embeddings.MODEL_NAME} is not in the local HF "
            "cache, so the live probe was skipped (doctor never downloads).",
            "Run `exomem warm` to fetch the model, then re-run doctor for the live probe.",
        )
    try:
        index = embeddings.get_embedding_index(vault_root)
        query_vec = embeddings.embed_texts(["knowledge"], is_query=True)[0]
        hits = index.search(query_vec, k=1)
    except Exception as e:  # noqa: BLE001 — diagnostic boundary
        return _check(
            "embeddings.sidecar",
            "fail",
            f"Embedding sidecar is present but a live embed+search probe failed: {e}",
            "Rebuild vectors: `kb reconcile` or `kb audit_fix --rebuild-embeddings true`.",
        )
    if not hits:
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar loads and the model embeds, but a probe query returned no "
            "vectors — the index is empty.",
            "Build vectors: `kb reconcile` or `kb audit_fix --rebuild-embeddings true`.",
        )
    return _check(
        "embeddings.sidecar",
        "pass",
        f"Embedding sidecar live: embed+search returned {len(hits)} hit(s).",
    )


def _hf_hub_dir() -> Path:
    """The local HuggingFace hub cache directory (honors HF_HUB_CACHE / HF_HOME).

    Directory resolution only — never touches the network.
    """
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _model_cached(hub: Path, dirname: str) -> bool:
    """True if a model's snapshot dir exists and is non-empty — a pure directory
    check, so a caller can gate model-loading work on it WITHOUT risking a
    download (doctor never fetches)."""
    snapshots = hub / dirname / "snapshots"
    try:
        return snapshots.is_dir() and any(snapshots.iterdir())
    except OSError:
        return False


def _check_models_cache() -> DoctorCheck:
    """Local HF-cache presence for the three search models. Read-only: this
    inspects directories only — doctor never downloads anything."""
    from . import embeddings

    hub = _hf_hub_dir()

    expected = {
        embeddings.MODEL_NAME: "models--" + embeddings.MODEL_NAME.replace("/", "--"),
        embeddings.RERANKER_NAME: "models--" + embeddings.RERANKER_NAME.replace("/", "--"),
        # sentence-transformers resolves bare names under its org.
        embeddings.CLIP_MODEL_NAME: "models--sentence-transformers--" + embeddings.CLIP_MODEL_NAME,
    }

    missing = [name for name, dirname in expected.items() if not _model_cached(hub, dirname)]
    if not missing:
        return _check("models.cache", "pass", "Search models are present in the local HF cache.")
    return _check(
        "models.cache",
        "warn",
        f"Not yet in the local HF cache: {', '.join(missing)}. The first server "
        "start downloads them in the background; hybrid finds are lexical-only meanwhile.",
        "Run `exomem warm` to pre-download them now (~1-2 GB).",
    )


def _check_tesseract(*, required: bool = True) -> DoctorCheck:
    configured = os.environ.get("EXOMEM_TESSERACT_CMD")
    if configured and Path(configured).exists():
        return _check("tool.tesseract", "pass", f"Tesseract configured at {configured}.")
    found = shutil.which("tesseract")
    if found:
        return _check("tool.tesseract", "pass", f"Tesseract found at {found}.")
    return _check(
        "tool.tesseract",
        "fail" if required else "warn",
        "Tesseract OCR binary was not found.",
        "Install Tesseract (Windows: `winget install UB-Mannheim.TesseractOCR`) or set "
        "EXOMEM_TESSERACT_CMD.",
    )


def _check_remote_env() -> list[DoctorCheck]:
    required = {
        "EXOMEM_BASE_URL": "Set the public HTTPS base URL, e.g. https://kb.example.com.",
        "GITHUB_CLIENT_ID": "Create a GitHub OAuth app and set its client id.",
        "GITHUB_CLIENT_SECRET": "Set the GitHub OAuth app client secret.",
        "EXOMEM_GITHUB_USERNAME": "Set the single GitHub login allowed to authenticate.",
        "EXOMEM_JWT_SIGNING_KEY": "Generate a stable signing key, e.g. python -c \"import secrets; print(secrets.token_urlsafe(48))\".",
    }
    checks: list[DoctorCheck] = []
    for key, remediation in required.items():
        if os.environ.get(key):
            checks.append(_check(f"env.{key}", "pass", f"{key} is set."))
        else:
            checks.append(_check(f"env.{key}", "fail", f"{key} is not set.", remediation))

    raw_user_id = os.environ.get("EXOMEM_GITHUB_USER_ID", "").strip()
    try:
        user_id = int(raw_user_id)
        valid_user_id = user_id > 0 and raw_user_id.isdecimal()
    except ValueError:
        valid_user_id = False
    if valid_user_id:
        checks.append(_check(
            "env.EXOMEM_GITHUB_USER_ID",
            "pass",
            "EXOMEM_GITHUB_USER_ID is a positive immutable GitHub subject.",
        ))
    else:
        checks.append(_check(
            "env.EXOMEM_GITHUB_USER_ID",
            "fail",
            "EXOMEM_GITHUB_USER_ID is missing or invalid.",
            "Set EXOMEM_GITHUB_USER_ID to the positive numeric ID returned by GitHub.",
        ))

    host = os.environ.get("EXOMEM_HOST", "127.0.0.1")
    checks.append(_check("env.EXOMEM_HOST", "pass", f"EXOMEM_HOST resolves to {host}."))
    if os.environ.get("EXOMEM_REST_API_KEY"):
        checks.append(_check("env.EXOMEM_REST_API_KEY", "pass", "REST API key is set."))
    else:
        checks.append(_check(
            "env.EXOMEM_REST_API_KEY",
            "warn",
            "EXOMEM_REST_API_KEY is unset; /api/* stays disabled.",
            "Run `uv run --no-sync python scripts/set-rest-key.py` if you want REST access.",
        ))
    if os.environ.get("EXOMEM_UPLOAD_TOKEN"):
        checks.append(_check("env.EXOMEM_UPLOAD_TOKEN", "pass", "Upload token is set."))
    else:
        checks.append(_check(
            "env.EXOMEM_UPLOAD_TOKEN",
            "warn",
            "EXOMEM_UPLOAD_TOKEN is unset; upload/download token minting stays disabled.",
            "Run `uv run python scripts/set-upload-token.py` if you want binary upload/download.",
        ))
    return checks


def _check_ha_env() -> list[DoctorCheck]:
    required = {
        "EXOMEM_BASE_URL": "Set the stable public OAuth origin.",
        "EXOMEM_JWT_SIGNING_KEY": "Set the stable durable-session signing root.",
        "EXOMEM_WRITER_LEASE_URL": "Set the provider-neutral writer coordinator URL.",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "Set the stable vault coordination identifier.",
        "EXOMEM_WRITER_LEASE_REPLICA_ID": "Set a unique identifier for this replica.",
        "EXOMEM_OAUTH_STORAGE_URL": "Set the authoritative coordinator state URL.",
        "EXOMEM_OAUTH_STORAGE_TOKEN": "Set the coordinator bearer credential for auth state.",
        "EXOMEM_LEASE_COORDINATOR_TOKEN": "Set the bearer enforced by the coordinator service.",
    }
    checks: list[DoctorCheck] = []
    for key, remediation in required.items():
        if os.environ.get(key, "").strip():
            checks.append(_check(f"ha.env.{key}", "pass", f"{key} is set."))
        else:
            checks.append(_check(f"ha.env.{key}", "fail", f"{key} is not set.", remediation))
    if os.environ.get("EXOMEM_WRITER_LEASE_TOKEN", "").strip():
        checks.append(_check("ha.env.EXOMEM_WRITER_LEASE_TOKEN", "pass", "Writer lease token is set."))
    else:
        checks.append(_check(
            "ha.env.EXOMEM_WRITER_LEASE_TOKEN",
            "fail",
            "Writer lease token is not set.",
            "Set EXOMEM_WRITER_LEASE_TOKEN to the same bearer as EXOMEM_OAUTH_STORAGE_TOKEN.",
        ))
    namespace = (
        os.environ.get("EXOMEM_OAUTH_STORAGE_NAMESPACE", "").strip()
        or os.environ.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip()
    )
    checks.append(_check(
        "ha.env.EXOMEM_OAUTH_STORAGE_NAMESPACE",
        "pass" if namespace else "fail",
        "OAuth storage namespace is set."
        if namespace
        else "OAuth storage namespace is not set.",
        None if namespace else (
            "Set EXOMEM_OAUTH_STORAGE_NAMESPACE or EXOMEM_WRITER_LEASE_VAULT_ID."
        ),
    ))
    raw_user_id = os.environ.get("EXOMEM_GITHUB_USER_ID", "").strip()
    try:
        valid_user_id = raw_user_id.isdecimal() and int(raw_user_id) > 0
    except ValueError:
        valid_user_id = False
    checks.append(_check(
        "ha.env.EXOMEM_GITHUB_USER_ID",
        "pass" if valid_user_id else "fail",
        "Immutable GitHub user ID is valid."
        if valid_user_id
        else "EXOMEM_GITHUB_USER_ID is missing or invalid.",
        None if valid_user_id else "Set a positive numeric EXOMEM_GITHUB_USER_ID.",
    ))
    credential_values = [
        os.environ.get("EXOMEM_LEASE_COORDINATOR_TOKEN", "").strip(),
        os.environ.get("EXOMEM_WRITER_LEASE_TOKEN", "").strip(),
        os.environ.get("EXOMEM_OAUTH_STORAGE_TOKEN", "").strip(),
    ]
    credentials_match = all(credential_values) and len(set(credential_values)) == 1
    checks.append(_check(
        "ha.auth.credentials_match",
        "pass" if credentials_match else "fail",
        "HA coordinator credentials are present and match."
        if credentials_match
        else "HA coordinator credentials are missing or do not match.",
        None if credentials_match else (
            "Use one bearer value for writer lease, OAuth storage, and the coordinator."
        ),
    ))
    raw_contracts = os.environ.get("EXOMEM_HA_SUPPORTED_RUNTIME_CONTRACTS", "").strip()
    try:
        contracts = _parse_runtime_contracts(raw_contracts)
    except ValueError as exc:
        checks.append(_check(
            "ha.supported_contracts",
            "fail",
            str(exc),
            "Set EXOMEM_HA_SUPPORTED_RUNTIME_CONTRACTS to comma-separated positive integers.",
        ))
    else:
        checks.append(_check(
            "ha.supported_contracts",
            "pass",
            f"Accepted runtime contracts: {', '.join(map(str, sorted(contracts)))}.",
        ))
    return checks


def _parse_runtime_contracts(raw: str = "") -> set[int]:
    from .runtime_readiness import RUNTIME_CONTRACT

    if not raw:
        return {RUNTIME_CONTRACT}
    values: set[int] = set()
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"Invalid HA runtime contract {text!r}.") from None
        if value <= 0:
            raise ValueError(f"Invalid HA runtime contract {text!r}.")
        values.add(value)
    if not values:
        raise ValueError("No valid HA runtime contracts were configured.")
    return values


def _ha_replica_urls(explicit: list[str] | tuple[str, ...] | None) -> list[str]:
    raw_values = list(explicit or ())
    if not raw_values:
        raw_values = os.environ.get("EXOMEM_HA_REPLICA_URLS", "").split(",")
    urls: list[str] = []
    for raw in raw_values:
        value = raw.strip().rstrip("/")
        if not value:
            continue
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid HA replica URL {value!r}.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"HA replica URL must be a credential-free origin: {value!r}.")
        if parsed.path not in {"", "/"}:
            raise ValueError(f"HA replica URL must not include a path: {value!r}.")
        origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if origin not in urls:
            urls.append(origin)
    return urls


def _evaluate_ha_readiness(
    body: object, *, supported_contracts: set[int]
) -> tuple[list[str], dict[str, object]]:
    from .runtime_readiness import HTTP_TRANSPORT

    if not isinstance(body, dict):
        return ["invalid readiness payload"], {}
    reasons: list[str] = []
    if body.get("status") != "ready" or body.get("service") != "exomem":
        reasons.append("runtime is not ready")
    contract = body.get("runtime_contract")
    if isinstance(contract, bool) or not isinstance(contract, int) or contract not in supported_contracts:
        reasons.append("runtime contract is unsupported")
    if body.get("transport") != HTTP_TRANSPORT:
        reasons.append("HTTP transport is not stateless")
    replica_id = body.get("replica_id")
    if not isinstance(replica_id, str) or not replica_id:
        reasons.append("replica identity is missing")
    coordination = body.get("coordination")
    if not isinstance(coordination, dict) or coordination.get("enabled") is not True:
        reasons.append("writer coordination is disabled")
    elif coordination.get("coordinator_healthy") is not True:
        reasons.append("writer coordinator is unavailable")
    if body.get("takeover_eligible") is not True:
        reasons.append("replica is not takeover eligible")
    release = body.get("release")
    if not isinstance(release, str) or not release:
        reasons.append("release identity is missing")
    return reasons, {
        "replica_id": replica_id,
        "release": release,
        "runtime_contract": contract,
        "transport": body.get("transport"),
    }


def _check_ha_probes(replica_urls: list[str]) -> list[DoctorCheck]:
    if len(replica_urls) < 2:
        return [_check(
            "ha.replica_urls",
            "fail",
            "HA probing requires at least two explicit replica origins.",
            "Pass --replica-url once per replica or set EXOMEM_HA_REPLICA_URLS.",
        )]
    try:
        supported = _parse_runtime_contracts(
            os.environ.get("EXOMEM_HA_SUPPORTED_RUNTIME_CONTRACTS", "").strip()
        )
    except ValueError as exc:
        return [_check("ha.compatibility", "fail", str(exc))]

    checks: list[DoctorCheck] = []
    identities: list[str] = []
    releases: list[str] = []
    failed = False
    for index, origin in enumerate(replica_urls, start=1):
        url = f"{origin}/health/ready"
        try:
            status, body = _probe_get(url)
        except Exception as exc:  # noqa: BLE001 - network failure is a diagnostic result
            checks.append(_check(
                f"ha.replica.{index}",
                "fail",
                f"Could not reach runtime readiness at {origin}: {exc}",
                "Start or upgrade the replica and verify its private/public origin routing.",
            ))
            failed = True
            continue
        reasons, details = _evaluate_ha_readiness(body, supported_contracts=supported)
        if status != 200:
            reasons.insert(0, f"readiness returned HTTP {status}")
        if reasons:
            checks.append(_check(
                f"ha.replica.{index}",
                "fail",
                f"Replica {origin} is ineligible: {', '.join(reasons)}.",
                "Upgrade or repair this replica before enabling HA failover.",
                details=details,
            ))
            failed = True
            continue
        replica_id = str(details["replica_id"])
        release = str(details["release"])
        identities.append(replica_id)
        releases.append(release)
        checks.append(_check(
            f"ha.replica.{index}",
            "pass",
            f"Replica {replica_id} at {origin} is runtime-compatible (release {release}).",
            details=details,
        ))

    duplicates = len(identities) != len(set(identities))
    if duplicates:
        failed = True
    checks.append(_check(
        "ha.compatibility",
        "fail" if failed else "pass",
        (
            "HA replicas are not safely compatible."
            if failed
            else "All HA replicas are compatible and have unique identities."
        ),
        (
            "Fix failing replica checks and ensure every replica ID is unique."
            if failed
            else None
        ),
    ))
    if duplicates:
        checks.append(_check(
            "ha.replica_identity",
            "fail",
            "Two or more ready replicas report the same replica identity.",
            "Set a unique EXOMEM_WRITER_LEASE_REPLICA_ID on every replica.",
        ))
    if len(set(releases)) > 1:
        checks.append(_check(
            "ha.release_drift",
            "warn",
            f"Compatible replicas run different releases: {', '.join(sorted(set(releases)))}.",
            "Finish the rolling deployment when convenient; exact release equality is not required.",
        ))
    elif releases:
        checks.append(_check(
            "ha.release_drift",
            "pass",
            f"All ready replicas run release {releases[0]}.",
        ))
    return checks


def _probe_get(url: str) -> tuple[int, object]:
    """GET `url` with a short timeout; returns (status, parsed-JSON-or-text).

    Module-level seam so tests fake the transport. httpx rides in via the
    fastmcp dependency; imported lazily to keep doctor's import cost nil.
    """
    import httpx

    resp = httpx.get(url, timeout=5.0, follow_redirects=False)
    try:
        body: object = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON bodies are fine for probes
        body = resp.text
    return resp.status_code, body


def _probe_state(url: str, namespace: str, token: str | None) -> tuple[int, object]:
    """Read a deliberately absent coordinator key, optionally authenticated."""
    import httpx

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.post(
        f"{url.rstrip('/')}/v1/state/{namespace}/get",
        json={
            "key": "__exomem_doctor_absent_sentinel__",
            "collection": "exomem-doctor-auth-probe",
        },
        headers=headers,
        timeout=5.0,
        follow_redirects=False,
    )
    try:
        body: object = response.json()
    except Exception:  # noqa: BLE001 - non-JSON error bodies are diagnostic only
        body = response.text
    return response.status_code, body


def _check_ha_auth_probes(*, prefix: str = "ha.auth") -> list[DoctorCheck]:
    url = os.environ.get("EXOMEM_OAUTH_STORAGE_URL", "").strip().rstrip("/")
    namespace = (
        os.environ.get("EXOMEM_OAUTH_STORAGE_NAMESPACE", "").strip()
        or os.environ.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip()
    )
    token = os.environ.get("EXOMEM_OAUTH_STORAGE_TOKEN", "").strip()
    if not (url and namespace and token):
        return [_check(
            f"{prefix}.storage_credential",
            "fail",
            "Cannot probe authoritative auth storage because its URL, namespace, or token is missing.",
            "Set EXOMEM_OAUTH_STORAGE_URL, its namespace, and EXOMEM_OAUTH_STORAGE_TOKEN.",
        )]

    checks: list[DoctorCheck] = []
    try:
        anonymous_status, _ = _probe_state(url, namespace, None)
    except Exception as error:  # noqa: BLE001 - network diagnostic boundary
        checks.append(_check(
            f"{prefix}.anonymous_rejected",
            "fail",
            f"Could not reach coordinator for anonymous auth enforcement probe: {error}",
            "Check coordinator routing and availability.",
        ))
    else:
        checks.append(_check(
            f"{prefix}.anonymous_rejected",
            "pass" if anonymous_status == 401 else "fail",
            "Coordinator rejects anonymous state access with 401."
            if anonymous_status == 401
            else f"Coordinator anonymous state probe returned HTTP {anonymous_status}, expected 401.",
            None if anonymous_status == 401 else (
                "Require bearer authentication on every coordinator state route."
            ),
        ))

    try:
        authenticated_status, authenticated_body = _probe_state(url, namespace, token)
    except Exception:  # noqa: BLE001 - network diagnostic boundary
        checks.append(_check(
            f"{prefix}.storage_credential",
            "fail",
            "Could not reach authoritative auth storage.",
            "Check coordinator routing and availability; the configured token was not printed.",
        ))
    else:
        sentinel_absent = (
            isinstance(authenticated_body, dict)
            and "result" in authenticated_body
            and authenticated_body.get("result") is None
        )
        if authenticated_status == 200 and sentinel_absent:
            checks.append(_check(
                f"{prefix}.storage_credential",
                "pass",
                "Authenticated read-only auth-storage probe succeeded.",
            ))
        elif authenticated_status in {401, 403}:
            checks.append(_check(
                f"{prefix}.storage_credential",
                "fail",
                "Coordinator rejected the configured auth-storage credential.",
                "Set the same bearer on coordinator, writer lease, and OAuth storage.",
            ))
        elif authenticated_status == 200:
            checks.append(_check(
                f"{prefix}.storage_credential",
                "fail",
                "Authenticated auth-storage probe returned an unexpected sentinel value.",
                "Check coordinator state routing and namespace configuration.",
            ))
        else:
            checks.append(_check(
                f"{prefix}.storage_credential",
                "fail",
                f"Authoritative auth storage returned HTTP {authenticated_status}.",
                "Repair coordinator availability before serving authenticated traffic.",
            ))
    return checks


def _ha_auth_configured() -> bool:
    """Whether this environment declares any part of a replica/HA topology."""
    return any(
        os.environ.get(key, "").strip()
        for key in HA_AUTH_ENV_KEYS
    )


def _check_probe_local(port: int = 8765) -> DoctorCheck:
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        status, _ = _probe_get(url)
    except Exception as e:  # noqa: BLE001 — any transport error = server not reachable
        return _check(
            "probe.local_mcp",
            "fail",
            f"Could not reach {url}: {e}",
            "Start the server (`exomem --transport http`) or the installed service, then re-run.",
        )
    if status == 401:
        return _check("probe.local_mcp", "pass", "Local /mcp answers 401 — server up, auth enforced.")
    if status == 200:
        return _check(
            "probe.local_mcp",
            "fail",
            "Local /mcp answered 200 without auth — the HTTP transport must require OAuth.",
            "Serve with the http transport (auth is mandatory there); check what is bound to the port.",
        )
    return _check(
        "probe.local_mcp",
        "fail",
        f"Local /mcp answered {status}, expected 401.",
        "Check the service log (logs/exomem.log) for startup errors.",
    )


def _check_probe_oauth_discovery(base_url: str) -> DoctorCheck:
    url = f"{base_url}/.well-known/oauth-authorization-server"
    try:
        status, _ = _probe_get(url)
    except Exception as e:  # noqa: BLE001
        return _check(
            "probe.oauth_discovery",
            "fail",
            f"Could not reach {url}: {e}",
            "Is the tunnel running and forwarding to 127.0.0.1:8765?",
        )
    if status == 200:
        return _check("probe.oauth_discovery", "pass", "OAuth discovery answers 200 through the tunnel.")
    return _check(
        "probe.oauth_discovery",
        "fail",
        f"{url} answered {status}, expected 200.",
        "Verify the tunnel forwards to the server port and EXOMEM_BASE_URL matches the public hostname.",
    )


def _check_probe_protected_resource(base_url: str) -> DoctorCheck:
    """The claude.ai registration gate: the connector probes the BARE
    /.well-known/oauth-protected-resource path, and a 404 there aborts the
    connect flow with `mcp_registration_failed`. exomem serves the path; this
    proves it is live through the actual tunnel."""
    url = f"{base_url}/.well-known/oauth-protected-resource"
    try:
        status, body = _probe_get(url)
    except Exception as e:  # noqa: BLE001
        return _check(
            "probe.protected_resource",
            "fail",
            f"Could not reach {url}: {e}",
            "Is the tunnel running and forwarding to 127.0.0.1:8765?",
        )
    if status == 404:
        return _check(
            "probe.protected_resource",
            "fail",
            "The bare oauth-protected-resource path 404s — claude.ai aborts connector "
            "registration with mcp_registration_failed when this happens.",
            "Update exomem (the server ships this route) and confirm the tunnel points at this server.",
        )
    if status != 200:
        return _check(
            "probe.protected_resource",
            "fail",
            f"{url} answered {status}, expected 200.",
            "Check the tunnel and the service log.",
        )
    expected = f"{base_url}/mcp"
    resource = body.get("resource") if isinstance(body, dict) else None
    if resource != expected:
        return _check(
            "probe.protected_resource",
            "fail",
            f"resource metadata is {resource!r}, expected {expected!r}.",
            "EXOMEM_BASE_URL must exactly match the public origin the connector uses (scheme + host).",
        )
    return _check(
        "probe.protected_resource",
        "pass",
        "Bare oauth-protected-resource metadata is live and points at /mcp.",
    )


def _check_remote_probes() -> list[DoctorCheck]:
    checks = [_check_probe_local()]
    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    if base_url:
        checks.append(_check_probe_oauth_discovery(base_url))
        checks.append(_check_probe_protected_resource(base_url))
    else:
        for check_id in ("probe.oauth_discovery", "probe.protected_resource"):
            checks.append(_check(
                check_id,
                "fail",
                "EXOMEM_BASE_URL is not set; cannot probe the public endpoint.",
                "Set EXOMEM_BASE_URL to the public HTTPS origin, e.g. https://kb.example.com.",
            ))
    return checks


def doctor(
    *,
    vault: str | None = None,
    profile: Profile | None = None,
    probe: bool = False,
    replica_urls: list[str] | tuple[str, ...] | None = None,
) -> DoctorReport:
    profile = profile or infer_profile()
    if profile not in VALID_PROFILES:
        raise ValueError(f"unknown profile: {profile!r}. Valid: {list(VALID_PROFILES)}")

    vault_root, vault_check = _resolve_vault(vault)
    checks: list[DoctorCheck] = [
        _check_python(),
        _check_uv(),
        _check_console_scripts(),
        _check_package_import(),
        vault_check,
        *_check_schema_files(vault_root),
        _check_repo_env(),
        _check_registry(),
        _check_resource_posture(profile),
        _check_lexical(vault_root),
    ]
    runtime_processes = _check_runtime_processes()
    if runtime_processes is not None:
        checks.append(runtime_processes)
    media_runtime = _check_media_runtime(vault_root)
    if media_runtime is not None:
        checks.append(media_runtime)

    if profile in ("hybrid", "standard", "media"):
        checks.extend([
            _check_embeddings_disabled(),
            _check_dependency("sentence-transformers", "embeddings", import_name="sentence_transformers"),
            _check_dependency("torch", "embeddings"),
            _check_dependency("pillow", "embeddings", import_name="PIL"),
            _check_torch_cuda(),
            _check_torch_device(),
            _check_models_cache(),
            _check_sqlite_vec(),
        ])
        mps_headroom = _check_mps_headroom()
        if mps_headroom is not None:
            checks.append(mps_headroom)
        sidecar = _check_embedding_sidecar(vault_root)
        if sidecar is not None:
            checks.append(sidecar)

    if profile in ("standard", "media"):
        checks.extend([
            _check_dependency("faster-whisper", "media", import_name="faster_whisper"),
            _check_dependency("pytesseract", "media"),
            _check_dependency("pymupdf", "media", import_name="fitz"),
            _check_dependency("markitdown", "media"),
            _check_tesseract(required=profile == "media"),
            _check_asr_backend(),
            _check_asr_prewarm(),
        ])

    if profile == "remote":
        checks.extend(_check_remote_env())
        if _ha_auth_configured():
            checks.extend(_check_ha_env())
        # Opt-in live-endpoint verification (three read-only GETs). The
        # default stays fully offline — doctor never touches the network
        # unless --probe is passed explicitly.
        if probe:
            checks.extend(_check_remote_probes())
            if _ha_auth_configured():
                checks.extend(_check_ha_auth_probes(prefix="probe.auth"))

    if profile == "ha":
        checks.extend(_check_ha_env())
        if probe:
            checks.extend(_check_ha_auth_probes())
            try:
                urls = _ha_replica_urls(replica_urls)
            except ValueError as exc:
                checks.append(_check(
                    "ha.replica_urls",
                    "fail",
                    str(exc),
                    "Pass credential-free replica origins such as https://replica.example.com.",
                ))
            else:
                checks.extend(_check_ha_probes(urls))

    return DoctorReport(profile=profile, checks=checks)


def render_human(report: DoctorReport) -> str:
    lines = [
        f"exomem doctor ({report.profile})",
        f"overall: {'PASS' if report.success else 'FAIL'}",
    ]
    by_status: dict[Status, list[DoctorCheck]] = {"fail": [], "warn": [], "pass": []}
    for check in report.checks:
        by_status[check.status].append(check)

    for status in ("fail", "warn", "pass"):
        rows = by_status[status]
        if not rows:
            continue
        lines.append("")
        lines.append(status.upper())
        for check in rows:
            lines.append(f"- {check.id}: {check.message}")
            if check.remediation:
                lines.append(f"  fix: {check.remediation}")
    return "\n".join(lines)
