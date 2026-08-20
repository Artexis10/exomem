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
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from . import __version__, install_info, process_memory
from .cli_ops import OpError
from .kbdir import kb_dirname, kb_prefix

if TYPE_CHECKING:
    from .writer_lease import LeaseConfig

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

#: How a user actually reconciles from a shell. `reconcile` and `audit_fix`
#: are internal registry names, not CLI surface — the CLI dispatches on the
#: PRODUCT_PUBLIC_NAMES, where this operation is `maintain_memory --mode
#: reconcile` (or the `maintain` alias). Remediation strings named the internal
#: ops, so every one of them fell through to the server argument parser.
#:
#: The `--reconcile` is not decoration, and the name here understates the reach:
#: this is also how the derived graph is recovered and how orphaned rebuild
#: temporaries are reclaimed. Bare `exomem maintain` is the read-only audit. It
#: parses, runs, and exits 0 without repairing anything, which is the worse of
#: the two failure modes — an operator who follows it sees success and concludes
#: the diagnosis was wrong. Every remediation that asks for repair routes
#: through this constant so no site can drift back to the audit.
_REBUILD_VECTORS_CMD = "exomem maintain --reconcile"

#: Absolute deferred-queue FAIL threshold (total semantic_upserts + full_upserts
#: items), independent of vault size. The existing WARN tier is a 10% fraction
#: of indexed pages — fine for scaling with a vault, but it never fires an
#: absolute ceiling: a large vault can carry thousands of queued items and stay
#: under 10%, and by then every operation on that vault is durably slow. The
#: incident this guards: 3,724 items (2,116 semantic + 1,608 full) queued on a
#: 2,872-page vault took per-operation latency from ~300ms to ~60s. 300 sits
#: comfortably below that (12x headroom) and comfortably above a healthy
#: transient of "a few dozen" items (7-12x headroom), so neither end flips the
#: other's verdict.
_DEFERRED_BACKLOG_FAIL_TOTAL = 300

#: Orphaned rebuild-temporary files left by an interrupted background rebuild
#: (either the `.graph-rebuild-*.sqlite[-journal|-wal|-shm]` or the
#: `.lexical.sqlite.rebuild-*.tmp[-wal|-shm|-journal]` family — see
#: `_check_rebuild_temp_orphans`). WARN above a small count (a single
#: interrupted rebuild can leave a handful of sibling files for one attempt);
#: FAIL once either count or size indicates an unbounded leak rather than one
#: incomplete attempt. Incidents this guards: 18 graph-rebuild orphans totaling
#: 527 MB; separately, 74 lexical-rebuild orphans totaling 5.84 GiB across 30
#: abandoned rebuilds with nothing reaping them.
_REBUILD_TEMP_ORPHAN_FAIL_COUNT = 5
_REBUILD_TEMP_ORPHAN_FAIL_BYTES = 50 * 1024 * 1024

#: Age (by mtime) above which a matching rebuild-temp file is treated as an
#: orphan candidate rather than a legitimate in-flight rebuild. Below this
#: age, size is NOT evidence of a leak either: the incident's own abandoned
#: files averaged ~79 MB, well above the 50 MB fail-by-bytes threshold, but a
#: same-sized in-flight rebuild is routine and must not fail. Defined once in
#: `vault.REBUILD_TEMP_STALE_AGE_SECONDS` (see its own comment for the full
#: rationale) and reused here rather than copied — the same threshold also
#: gates `graph_sync.sweep_abandoned_temporaries`'s actual deletion of the
#: lexical family, not just this read-only diagnostic.
#:
#: `vault.is_lexical_rebuild_runtime_file_name` is likewise the one PUBLIC
#: matcher for lexstore.rebuild_atomic()'s (lexstore.py:2469) detached build
#: sibling `<lexical sidecar name>.rebuild-<uuid4().hex>.tmp[-wal|-shm|
#: -journal]`, which it reaps in its own `finally` on any graceful return —
#: so a surviving one means the process was killed mid-build.
#: governance/tool.py carries an independent PRIVATE regex
#: (`_LEXICAL_REBUILD_TEMP_RE`) for its own unrelated fail-closed
#: non-Markdown-membership purpose — private, and governance/tool.py is out
#: of this module's scope to touch.


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
    embeddings_ready = all(_module_available(name) for _, name in _embedding_requirements()[1])
    if not embeddings_ready:
        return "lean"
    media_ready = all(
        _module_available(name)
        for name in ("faster_whisper", "pytesseract", "fitz", "markitdown")
    )
    if media_ready:
        return "media" if shutil.which("tesseract") else "standard"
    return "hybrid"


def resolve_profile(explicit: Profile | None) -> Profile:
    """Resolve explicit, environment, persisted, then inferred profile intent."""
    selected = explicit or (os.environ.get(PROFILE_ENV) or "").strip().lower()
    if selected:
        if selected not in VALID_PROFILES:
            raise ValueError(f"unknown profile: {selected!r}. Valid: {list(VALID_PROFILES)}")
        return selected  # type: ignore[return-value]
    persisted = install_info.configured_local_profile()
    if persisted:
        return persisted  # type: ignore[return-value]
    return infer_profile()


def _profile_extras(profile: Profile) -> list[str]:
    extras = ["embeddings"] if profile in {"hybrid", "standard", "media"} else []
    if profile in {"standard", "media"}:
        extras.append("media")
        if os.environ.get("EXOMEM_ASR_BACKEND", "").strip().lower() == "mlx":
            extras.append("media-mlx")
    return extras


def _check_editable_lock_parity(profile: Profile) -> DoctorCheck | None:
    root, metadata_warning = install_info.editable_project_root_status()
    if root is None:
        if metadata_warning is None:
            return None
        return _check(
            "install.lock_parity",
            "warn",
            f"Editable install lock parity could not be verified: {metadata_warning}.",
        )
    uv = shutil.which("uv")
    if not uv:
        return _check("install.lock_parity", "warn", "Editable install lock parity could not be verified: uv is unavailable.")
    command = [
        uv, "sync", "--check", "--locked", "--no-dev", "--active", "--project", str(root),
        "--offline", "--no-cache", "--inexact",
        *[option for extra in _profile_extras(profile) for option in ("--extra", extra)],
    ]
    identity = install_info.report()
    details = {
        "project_root": str(root),
        "source_version": __version__,
        "distribution_version": identity["version"],
        "python_executable": sys.executable,
        "profile": profile,
    }
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "VIRTUAL_ENV": sys.prefix},
            timeout=15.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _check(
            "install.lock_parity",
            "warn",
            "Editable install lock parity check timed out after 15 seconds.",
            details=details,
        )
    except OSError as exc:
        return _check(
            "install.lock_parity",
            "warn",
            f"Editable install lock parity could not run: {exc}.",
            details=details,
        )
    output = ((result.stderr or "") + (result.stdout or "")).strip()[:2000]
    remediation = "Run `uv " + " ".join(command[1:]) + "` from the editable checkout."
    if result.returncode == 0:
        return _check(
            "install.lock_parity",
            "pass",
            "Editable install matches the selected locked runtime dependencies.",
            details=details,
        )
    unsupported = ("unknown option", "unrecognized option", "unexpected argument", "no such option")
    if any(marker in output.lower() for marker in unsupported):
        return _check(
            "install.lock_parity",
            "warn",
            "Editable install lock parity could not be verified by this uv version.",
            remediation,
            details=details,
        )
    detail = f" ({output})" if output else ""
    return _check(
        "install.lock_parity",
        "fail",
        f"Editable install is stale against its selected locked runtime dependencies.{detail}",
        remediation,
        details=details,
    )


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
    from .vault import shipped_schema_root

    skill = shipped_schema_root(path) / "SKILL.md"
    if not skill.exists():
        return path, _check(
            "vault.path",
            "fail",
            f"{path} contains no exomem schema contract "
            f"(looked in .exomem/schema/ and {kb_prefix()}_Schema/).",
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
    from .vault import shipped_schema_root

    kb = vault_root / kb_dirname()
    checks: list[DoctorCheck] = []
    required = [
        ("vault.schema", shipped_schema_root(vault_root) / "SKILL.md", "schema contract"),
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


def _resolved_embedding_backend() -> str:
    """The runtime actually configured to serve the bi-encoder.

    Every embedding-related finding must be phrased about *this* lane. Probing
    for torch and reporting its absence as "the vector stack isn't installed"
    tells an ONNX install that a working vector lane is missing, which is how a
    correct deployment comes to be benchmarked as lexical-only.
    """
    from . import embedding_backend

    try:
        return embedding_backend.resolve_backend(is_available=_module_available)
    except ValueError:
        # A misconfigured backend is reported by its own check; fall back to the
        # default lane rather than raising out of a diagnostic command.
        return embedding_backend.TORCH


def _vector_stack_available(backend: str) -> bool:
    """Whether the resolved lane's serving modules are importable."""
    from . import embedding_backend

    if backend == embedding_backend.ONNX:
        return _module_available("onnxruntime") and _module_available("tokenizers")
    return _module_available("sentence_transformers") and _module_available("torch")


def _serves_reranker_and_clip(backend: str) -> bool:
    """Whether this lane can load the reranker and CLIP at all.

    Both are sentence-transformers models, so the ONNX lane withholds them by
    design. Listing them as cache misses there produces a WARN that no action
    can ever clear.
    """
    from . import embedding_backend

    return backend != embedding_backend.ONNX


def _embedding_requirements() -> tuple[str, list[tuple[str, str]]]:
    """`(extra, [(distribution, import name)])` for the configured embedding backend.

    Health belongs to the runtime that is actually configured to serve. A hosted
    image built on ONNX Runtime carries no torch by design, and reporting that as
    a missing dependency would mark a correct install unhealthy — and, through
    `infer_profile`, silently demote it to `lean`, which is how a cell would come
    to advertise keyword-only recall while holding a working embedder.
    """
    from . import embedding_backend

    backend = _resolved_embedding_backend()
    if backend == embedding_backend.ONNX:
        return "embeddings-onnx", [
            ("onnxruntime", "onnxruntime"),
            ("tokenizers", "tokenizers"),
        ]
    return "embeddings", [
        ("sentence-transformers", "sentence_transformers"),
        ("torch", "torch"),
        ("pillow", "PIL"),
    ]


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


def _check_deferred_index_backlog(vault_root: Path | None) -> DoctorCheck:
    """FAIL/WARN when durable index work threatens or already degrades every op.

    Two independent tiers: an absolute FAIL ceiling
    (`_DEFERRED_BACKLOG_FAIL_TOTAL`) that fires regardless of vault size, and
    the pre-existing relative WARN tier (10% of indexed pages) below it.
    Reconcile only re-embeds paths it itself changes — it never drains this
    queue — so remediation always names the one command that does:
    `exomem index --vault <root> --scope vault`.
    """
    details: dict[str, object] = {
        "indexed_pages": 0,
        "warn_fraction": 0.10,
        "fail_total": _DEFERRED_BACKLOG_FAIL_TOTAL,
        "semantic_upserts": 0,
        "full_upserts": 0,
    }
    if vault_root is None:
        return _check(
            "deferred_index_backlog",
            "pass",
            "No vault configured; deferred index backlog was not inspected.",
            details=details,
        )
    from . import index_sync, lexstore

    sidecar = lexstore.lexical_path(vault_root)
    if sidecar.exists():
        try:
            details["indexed_pages"] = _lexical_page_count(sidecar)
        except (OSError, sqlite3.Error):
            # The lexical health check owns the unreadable-sidecar diagnostic.
            pass
    status = index_sync.deferred_work_status(vault_root)
    for queue in ("semantic_upserts", "full_upserts"):
        details[queue] = int(status.get(queue, {}).get("count", 0))
    indexed_pages = int(details["indexed_pages"])
    semantic = int(details["semantic_upserts"])
    full = int(details["full_upserts"])
    total = semantic + full
    command = f'exomem index --vault "{vault_root}" --scope vault'

    if total >= _DEFERRED_BACKLOG_FAIL_TOTAL:
        return _check(
            "deferred_index_backlog",
            "fail",
            f"Deferred index backlog is {total} item(s) (semantic_upserts={semantic}, "
            f"full_upserts={full}) — at this depth every vault operation degrades. "
            "Reconcile only re-embeds paths it itself changes; it does not drain "
            "this backlog.",
            f"Run `{command}` to drain the backlog now.",
            details=details,
        )

    warn_above = indexed_pages * float(details["warn_fraction"])
    offenders = [
        f"{queue}={details[queue]}"
        for queue in ("semantic_upserts", "full_upserts")
        if int(details[queue]) > warn_above
    ]
    if offenders:
        return _check(
            "deferred_index_backlog",
            "warn",
            f"Deferred index backlog exceeds 10% of {indexed_pages} indexed page(s): "
            + ", ".join(offenders),
            f"Run `{command}` to drain it — reconcile does not drain this queue on "
            "its own.",
            details=details,
        )
    return _check(
        "deferred_index_backlog",
        "pass",
        f"Deferred index backlog is below 10% of {indexed_pages} indexed page(s).",
        details=details,
    )


def _check_graph_sync_state(vault_root: Path | None) -> DoctorCheck:
    """graph_sync epoch health: whether the derived graph is servable.

    Reads graph_sync.status() (current/recovery_required/unavailable) plus
    checkpoint_state() (whose "malformed" outcome status() alone cannot
    distinguish). current -> pass; a malformed checkpoint always fails, even if
    status() otherwise reports current; recovery_required fails using the same
    canned remediation graph_sync itself hands to callers via
    committed_graph_failure() when a real checkpoint is available, falling back
    to an honest message when it is not; unavailable fails with an honest
    message. Never claims to fix this itself — recovery is reconcile's job.

    committed_graph_failure()'s own default remediation ("Run reconcile to
    recover the derived graph.") names an internal op, not a runnable command
    (see `_REBUILD_VECTORS_CMD`'s own comment for that history) — so this
    always ensures the printed remediation includes the actual runnable
    command, augmenting the canned text rather than discarding it when it is
    more specific than the default.
    """
    if vault_root is None:
        return _check(
            "graph_sync.state",
            "pass",
            "No vault configured; graph_sync state was not inspected.",
        )
    from . import graph_sync

    status = graph_sync.status(vault_root)
    state = status.get("state")
    generation = status.get("generation")
    checkpoint_status, checkpoint = graph_sync.checkpoint_state(vault_root)
    details: dict[str, object] = {
        "state": state,
        "generation": generation,
        "checkpoint_state": checkpoint_status,
    }

    if checkpoint_status == "malformed":
        return _check(
            "graph_sync.state",
            "fail",
            f"graph_sync checkpoint is malformed (generation {generation}); the "
            "derived graph cannot be trusted until it is recovered.",
            f"Run `{_REBUILD_VECTORS_CMD}` to recover the derived graph.",
            details=details,
        )
    if state == "current":
        return _check(
            "graph_sync.state",
            "pass",
            f"graph_sync is current at generation {generation}.",
            details=details,
        )
    if state == "recovery_required":
        if checkpoint is not None:
            remediation = graph_sync.committed_graph_failure(checkpoint)[
                "graph_sync_remediation"
            ]
            if _REBUILD_VECTORS_CMD not in remediation:
                # The canned text (its own default: "Run reconcile to recover
                # the derived graph.") names an internal op, not a runnable
                # shell command. Augment rather than replace so a more
                # specific canned message (e.g. a threaded
                # GraphRebuildRegistrationError remediation) is preserved.
                remediation = f"{remediation} Run `{_REBUILD_VECTORS_CMD}`."
        else:
            remediation = f"Run `{_REBUILD_VECTORS_CMD}` to recover the derived graph."
        return _check(
            "graph_sync.state",
            "fail",
            f"graph_sync requires recovery at generation {generation}; the derived "
            "graph is not current.",
            remediation,
            details=details,
        )
    # state == "unavailable" (or any value outside the documented set).
    return _check(
        "graph_sync.state",
        "fail",
        f"graph_sync is unavailable at generation {generation}; graph-backed reads "
        "cannot be trusted.",
        f"Run `{_REBUILD_VECTORS_CMD}` to recover the derived graph.",
        details=details,
    )


def _check_rebuild_temp_orphans(vault_root: Path | None) -> DoctorCheck:
    """Leaked rebuild-temporary files from interrupted background rebuilds.

    Two independent families, both normally self-cleaned on a graceful exit and
    both leakable only by a hard process kill mid-rebuild:

    - `.graph-rebuild-*.sqlite[-journal|-wal|-shm]` — graph_sync's rebuild path.
      Matched via `vault.is_graph_rebuild_runtime_file_name`, a strict regex
      that deliberately excludes `.graph-rebuild-user-copy.sqlite`, a real
      (tested) user file.
    - `.lexical.sqlite.rebuild-<32-hex>.tmp[-wal|-shm|-journal]` —
      lexstore.rebuild_atomic()'s detached build sibling (lexstore.py:2469),
      matched via `vault.is_lexical_rebuild_runtime_file_name` (the same
      matcher `graph_sync.sweep_abandoned_temporaries` uses to gate its
      actual deletion of this family).

    A matching name alone is NOT evidence of an orphan: a legitimate in-flight
    rebuild has a matching name too, and can legitimately be large (the
    incident's own abandoned files averaged ~79 MB). Only a file whose mtime is
    older than `vault.REBUILD_TEMP_STALE_AGE_SECONDS` is treated as a stale
    orphan candidate; WARN/FAIL thresholds evaluate STALE counts/bytes
    exclusively, so a fresh in-flight temporary of any size or count never
    trips them — its remediation ("stop the service...") would itself create a
    real orphan if followed against a rebuild that is still running.

    `details` reports, per family (`details["graph_rebuild"]`,
    `details["lexical_rebuild"]`) and combined: total `count`/`total_bytes`
    (every matching name, regardless of age) and `stale_count`/`stale_bytes`
    (age-gated, the population thresholds actually act on), plus
    `stale_age_seconds`. Reclaiming a stale file requires the service stopped
    and no rebuild in flight, so remediation names `_REBUILD_VECTORS_CMD` run
    out-of-process (it reads the vault from EXOMEM_VAULT_PATH, not `--vault`).
    That command must be the reconciling one: the sweeper that actually unlinks
    these files (`graph_sync.sweep_abandoned_temporaries`) has exactly one
    caller, inside `reconcile.reconcile()` and gated on `not dry_run`. Plain
    `exomem maintain` runs the read-only audit, so naming it told an operator
    to run something that exits 0 having reclaimed nothing -- a worse failure
    than naming no command at all, because success is indistinguishable from
    the remedy not applying.
    """
    from . import vault as vault_module

    empty_family = {"count": 0, "total_bytes": 0, "stale_count": 0, "stale_bytes": 0}
    details: dict[str, object] = {
        "count": 0,
        "total_bytes": 0,
        "stale_count": 0,
        "stale_bytes": 0,
        "stale_age_seconds": vault_module.REBUILD_TEMP_STALE_AGE_SECONDS,
        "graph_rebuild": dict(empty_family),
        "lexical_rebuild": dict(empty_family),
    }
    if vault_root is None:
        return _check(
            "rebuild_temp.orphans",
            "pass",
            "No vault configured; rebuild temporaries were not inspected.",
            details=details,
        )

    kb = Path(vault_root) / kb_dirname()
    graph_orphans: list[Path] = []
    lexical_orphans: list[Path] = []
    if kb.is_dir():
        for entry in kb.iterdir():
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            name = entry.name
            if vault_module.is_graph_rebuild_runtime_file_name(name):
                graph_orphans.append(entry)
            elif vault_module.is_lexical_rebuild_runtime_file_name(name):
                lexical_orphans.append(entry)

    now = time.time()

    def _family_stats(paths: list[Path]) -> dict[str, int]:
        total = 0
        stale_count = 0
        stale_total = 0
        for entry in paths:
            try:
                st = entry.stat()
            except OSError:
                continue
            total += st.st_size
            if now - st.st_mtime > vault_module.REBUILD_TEMP_STALE_AGE_SECONDS:
                stale_count += 1
                stale_total += st.st_size
        return {
            "count": len(paths),
            "total_bytes": total,
            "stale_count": stale_count,
            "stale_bytes": stale_total,
        }

    graph_stats = _family_stats(graph_orphans)
    lexical_stats = _family_stats(lexical_orphans)
    details["graph_rebuild"] = graph_stats
    details["lexical_rebuild"] = lexical_stats
    count = graph_stats["count"] + lexical_stats["count"]
    total_bytes = graph_stats["total_bytes"] + lexical_stats["total_bytes"]
    stale_count = graph_stats["stale_count"] + lexical_stats["stale_count"]
    stale_bytes = graph_stats["stale_bytes"] + lexical_stats["stale_bytes"]
    details["count"] = count
    details["total_bytes"] = total_bytes
    details["stale_count"] = stale_count
    details["stale_bytes"] = stale_bytes
    mb = total_bytes / (1024 * 1024)
    stale_mb = stale_bytes / (1024 * 1024)
    age_minutes = vault_module.REBUILD_TEMP_STALE_AGE_SECONDS // 60

    if count == 0:
        return _check(
            "rebuild_temp.orphans",
            "pass",
            "No orphaned rebuild temporaries found.",
            details=details,
        )
    if stale_count == 0:
        return _check(
            "rebuild_temp.orphans",
            "pass",
            f"{count} rebuild temporary file(s) totaling {mb:.1f} MB present, none "
            f"older than {age_minutes} minutes — likely an in-flight rebuild, not "
            "an orphan.",
            details=details,
        )
    remediation = (
        f"Stop the exomem service, then run `{_REBUILD_VECTORS_CMD}` "
        "out-of-process (it reads the vault from EXOMEM_VAULT_PATH, not "
        "--vault) to reclaim orphaned rebuild temporaries — reclaim is only "
        "safe once no rebuild is in flight."
    )
    summary = (
        f"graph={graph_stats['stale_count']}/{graph_stats['count']}, "
        f"lexical={lexical_stats['stale_count']}/{lexical_stats['count']} "
        "(stale/total)"
    )
    if (
        stale_count > _REBUILD_TEMP_ORPHAN_FAIL_COUNT
        or stale_bytes > _REBUILD_TEMP_ORPHAN_FAIL_BYTES
    ):
        return _check(
            "rebuild_temp.orphans",
            "fail",
            f"{stale_count} rebuild temporary file(s) older than {age_minutes} "
            f"minutes, totaling {stale_mb:.1f} MB, are leaking disk space from "
            f"interrupted rebuilds ({summary}).",
            remediation,
            details=details,
        )
    if stale_count > 1:
        return _check(
            "rebuild_temp.orphans",
            "warn",
            f"{stale_count} rebuild temporary file(s) older than {age_minutes} "
            f"minutes, totaling {stale_mb:.1f} MB ({summary}).",
            remediation,
            details=details,
        )
    return _check(
        "rebuild_temp.orphans",
        "pass",
        f"{stale_count} rebuild temporary file older than {age_minutes} minutes, "
        f"totaling {stale_mb:.1f} MB (below the warn threshold).",
        details=details,
    )


def _check_write_path_env_flags(vault_root: Path | None) -> DoctorCheck:
    """Env kill-switches that change or disable write-path background work.

    Doctor is out-of-process and read-only: it can only see environment
    configuration, never the running service's in-memory state. It
    deliberately does NOT invent an accessor for the in-process corpus-context
    cache dict (semantic_contract.py) — that structure is process-private and
    meaningless from here. What it can honestly report: whether the cache is
    switched on at all (EXOMEM_DISABLE_CORPUS_CACHE), whether the service runs
    any periodic drain (EXOMEM_DISABLE_EVENT_INDEXES — when set, file_watcher
    starts no reconcile thread at all), and whether new graph work is being
    scheduled (EXOMEM_DISABLE_GRAPH_SCHEDULING). A disabled periodic drain
    combined with an already non-empty deferred queue is the sharpest signal:
    that backlog will not shrink on its own.
    """
    from . import epistemic_graph, freshness, semantic_contract

    corpus_cache = semantic_contract.corpus_context_cache_enabled()
    event_indexes = freshness.event_indexes_enabled()
    graph_scheduling = epistemic_graph.graph_scheduling_enabled()
    details: dict[str, object] = {
        "corpus_context_cache_enabled": corpus_cache,
        "event_indexes_enabled": event_indexes,
        "graph_scheduling_enabled": graph_scheduling,
    }

    if not event_indexes and vault_root is not None:
        from . import index_sync

        status = index_sync.deferred_work_status(vault_root)
        semantic = int(status.get("semantic_upserts", {}).get("count", 0))
        full = int(status.get("full_upserts", {}).get("count", 0))
        total = semantic + full
        details["semantic_upserts"] = semantic
        details["full_upserts"] = full
        if total > 0:
            command = f'exomem index --vault "{vault_root}" --scope vault'
            level: Status = "fail" if total >= _DEFERRED_BACKLOG_FAIL_TOTAL else "warn"
            return _check(
                "write_path.env_flags",
                level,
                "EXOMEM_DISABLE_EVENT_INDEXES is set: the service runs no periodic "
                f"drain, and {total} item(s) are already queued (semantic_upserts="
                f"{semantic}, full_upserts={full}). This backlog will not shrink on "
                "its own.",
                f"Unset EXOMEM_DISABLE_EVENT_INDEXES, or run `{command}` manually.",
                details=details,
            )

    notes = []
    if not corpus_cache:
        notes.append("EXOMEM_DISABLE_CORPUS_CACHE is set (corpus-context cache off).")
    if not event_indexes:
        notes.append("EXOMEM_DISABLE_EVENT_INDEXES is set (no periodic drain).")
    if not graph_scheduling:
        notes.append("EXOMEM_DISABLE_GRAPH_SCHEDULING is set (no new graph work).")
    if notes:
        return _check(
            "write_path.env_flags",
            "warn",
            " ".join(notes),
            "These are deliberate kill switches; unset the variable(s) to restore "
            "default write-path behavior.",
            details=details,
        )
    return _check(
        "write_path.env_flags",
        "pass",
        "No write-path kill switches are set.",
        details=details,
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


#: How long the process census may take before the check gives up on it.
#:
#: `ps` answers in milliseconds. Windows has no `ps`, and the only supported way
#: to read another process's command line is a CIM query, which pays PowerShell
#: startup first -- typically under a second, but a cold shell on a loaded box is
#: slower. A doctor check may be slow enough to be worth waiting for and must
#: never hang, so the two platforms get bounds sized to what they actually do.
_PROCESS_CENSUS_TIMEOUT_SECONDS = 2.0
_WINDOWS_PROCESS_CENSUS_TIMEOUT_SECONDS = 8.0

#: One line per process: pid, working set / RSS in bytes, full command line.
#: Pipe-separated with the command last, so a command containing a pipe still
#: parses. `$_.CommandLine` is null for processes this session cannot see into;
#: those print empty and are dropped by the shared filter, exactly as a `ps` line
#: without a command is.
_WINDOWS_PROCESS_CENSUS = (
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$_.ProcessId.ToString() + '|' + $_.WorkingSetSize.ToString() + '|' "
    "+ $_.PrivatePageCount.ToString() + '|' + $_.CommandLine }"
)


def _run_process_census(command: list[str], timeout: float) -> str | None:
    """Run one census command, or None if it cannot answer."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a diagnostic may never raise
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _posix_process_samples() -> list[tuple[int, int, str, int | None]]:
    ps = shutil.which("ps")
    if not ps:
        return []
    stdout = _run_process_census(
        [ps, "-axo", "pid=,rss=,command="], _PROCESS_CENSUS_TIMEOUT_SECONDS
    )
    if stdout is None:
        return []
    samples: list[tuple[int, int, str, int | None]] = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            # `ps` reports RSS in kilobytes; the shared filter takes bytes. No
            # commit figure: `ps` has no portable column for it, and on Linux
            # RSS does not evaporate the way a trimmed Windows working set does.
            samples.append((int(parts[0]), int(parts[1]) * 1024, parts[2], None))
        except ValueError:
            continue
    return samples


def _windows_process_samples() -> list[tuple[int, int, str, int | None]]:
    """The same census on Windows, where there is no `ps`.

    `tasklist` cannot report a command line, and the check's whole filter is a
    command-line filter -- it has to tell an exomem stdio server from any other
    python.exe, and a media worker child from a session. So this reads
    `Win32_Process` through CIM, which carries `CommandLine` and
    `WorkingSetSize` together.

    Windows PowerShell 5.1 ships with every supported Windows and is preferred
    for that reason; `pwsh` is accepted where it is the only one present.
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return []
    stdout = _run_process_census(
        [shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PROCESS_CENSUS],
        _WINDOWS_PROCESS_CENSUS_TIMEOUT_SECONDS,
    )
    if stdout is None:
        return []
    samples: list[tuple[int, int, str, int | None]] = []
    for line in stdout.splitlines():
        parts = line.strip().split("|", 3)
        # An empty command field is a process this session cannot read the
        # command line of. Dropped here so it matches `ps`, where a line without
        # a command is short and gets dropped for the same reason -- the shared
        # filter would exclude it anyway, but the two censuses should not differ
        # about what they even collected.
        if len(parts) < 4 or not parts[3]:
            continue
        try:
            samples.append((int(parts[0]), int(parts[1]), parts[3], int(parts[2])))
        except ValueError:
            continue
    return samples


def _list_exomem_processes() -> list[dict[str, object]]:
    """Every OTHER exomem session process, with what it costs.

    The census is per-platform because the tools are; the filter is shared,
    because a lane that selects processes differently from the other reports a
    different thing under the same check name. `tests/test_doctor.py` pins that
    both lanes route through this one filter.
    """
    samples = _windows_process_samples() if os.name == "nt" else _posix_process_samples()
    rows: list[dict[str, object]] = []
    current_pid = os.getpid()
    for pid, rss_bytes, command, private_bytes in samples:
        if pid == current_pid:
            continue
        command_l = command.lower()
        if "exomem" not in command_l:
            continue
        if "exomem.media_worker_child" in command_l:
            continue
        if "--transport" not in command_l and "python -m exomem" not in command_l:
            continue
        rss_mb = round(rss_bytes / (1024 * 1024), 1)
        row: dict[str, object] = {
            "pid": pid,
            "rss_mb": rss_mb,
            "command": command[:180],
            **process_memory.enrich_process_memory(pid, rss_mb),
        }
        if private_bytes is not None:
            row["private_commit_mb"] = round(private_bytes / (1024 * 1024), 1)
        rows.append(row)
    return rows



#: How to run the one shared service that removes a per-process model cost.
#: Spelled out because the three conditions in `server.local_http_allowed` are
#: not guessable, and because the previous advice sent people looking for a
#: public hostname they do not need. Loopback bind means no other machine can
#: connect; an unset EXOMEM_BASE_URL is what distinguishes this from remote
#: intent; the API key is the operator saying they want it. Nobody sets a
#: bearer key by accident.
_LOOPBACK_SERVICE_HINT = (
    "For a local-only shared service, no public hostname or GitHub OAuth app is "
    "needed (#482): leave EXOMEM_BASE_URL unset, set EXOMEM_REST_API_KEY, and run "
    "`exomem --transport http --host 127.0.0.1 --port 8765`, then point each MCP "
    "client at that URL instead of launching its own stdio server."
)

def _check_runtime_processes() -> DoctorCheck | None:
    rows = _list_exomem_processes()
    if not rows:
        return None
    memory = process_memory.aggregate_memory(rows)
    count = len(rows)
    status: Status = "warn" if count > 1 else "pass"
    if memory["memory_metric"] == "physical_footprint":
        memory_message = (
            f"about {memory['memory_mb_total']} MB physical footprint total "
            f"({memory['rss_mb_total']} MB RSS compatibility total)"
        )
    elif memory["memory_metric"] == "mixed":
        memory_message = (
            f"mixed metrics: {memory['physical_footprint_mb_total']} MB physical footprint "
            f"plus {memory['rss_fallback_mb_total']} MB RSS fallback "
            f"({memory['rss_mb_total']} MB RSS compatibility total)"
        )
    else:
        memory_message = f"about {memory['rss_mb_total']} MB RSS total"
    # Windows trims an idle process's working set aggressively, so the resident
    # figure above is a reading of this instant rather than of what the process
    # costs -- one server here measured 1196 MB resident while active and 1.1 MB
    # minutes later, against a private commit that did not move. Reporting only
    # the resident number would tell a user their idle sessions are free.
    private_total = round(
        sum(float(row.get("private_commit_mb") or 0.0) for row in rows), 1
    )
    if private_total:
        memory_message += f" and {private_total} MB private commit"
    # Name a lever the reader can actually pull. The previous remedy led with
    # HTTP service mode used to be unreachable locally: it would not start
    # without EXOMEM_BASE_URL, a GitHub OAuth app and its credentials (#482), so
    # a laptop user with seven sessions and 8 GB resident was told to obtain a
    # public hostname to solve a purely local memory problem (#597). `mode
    # quiet` is one command, needs nothing external, and is exactly the policy
    # the old text gestured at: no boot preload and release models when idle.
    #
    # #482 has since shipped `server.local_http_allowed`, so the shared-service
    # remedy IS now reachable with nothing external -- bind loopback, leave
    # EXOMEM_BASE_URL unset, set EXOMEM_REST_API_KEY. Keeping the old wording
    # would leave doctor telling people to go and get a public hostname for a
    # requirement that no longer exists, which is worse than saying nothing:
    # this check exists to be acted on, and the one structural fix for a
    # per-process cost is the one it was steering people away from.
    #
    # Mode-aware, because recommending quiet to someone already in it is worse
    # than saying nothing: it reads as "you have not tried the fix" when they
    # have, and hides that the remaining cost is per-process and structural.
    from . import mode as mode_module

    current_mode = mode_module.resolve_mode()
    if current_mode == "quiet":
        remedy = (
            "Compute mode is already 'quiet' (no boot preload, models released when "
            "idle), so the remaining cost is per-process and structural: close "
            "sessions you are not using, or point every client at one shared "
            f"service. {_LOOPBACK_SERVICE_HINT}"
        )
    else:
        remedy = (
            f"Compute mode is '{current_mode}'; `exomem mode quiet` drops boot preload "
            "and releases models when idle, and is the smaller change. One shared "
            f"service removes the per-process cost entirely. {_LOOPBACK_SERVICE_HINT}"
        )
    return _check(
        "runtime.processes",
        status,
        f"Detected {count} other exomem server process(es) using {memory_message}. "
        f"Each stdio MCP client/session launches its own process. {remedy}",
        details={"count": count, "mode": current_mode, **memory, "processes": rows[:8]},
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
            f"After installing embeddings, run `{_REBUILD_VECTORS_CMD}`.",
        )
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar exists but EXOMEM_DISABLE_EMBEDDINGS is set, so the live "
            "probe was skipped.",
            "Unset EXOMEM_DISABLE_EMBEDDINGS to run the embed+search probe against it.",
        )
    backend = _resolved_embedding_backend()
    if not _vector_stack_available(backend):
        extra, _ = _embedding_requirements()
        return _check(
            "embeddings.sidecar",
            "warn",
            f"Embedding sidecar exists but the {backend} serving stack isn't installed, "
            "so it can't be probed.",
            f"Install it with `uv sync --extra {extra}` to enable hybrid search.",
        )
    from . import embeddings

    from . import model_cache

    if not _model_cached(_hf_hub_dir(), model_cache.snapshot_dirname(embeddings.MODEL_NAME)):
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
            f"Rebuild vectors: `{_REBUILD_VECTORS_CMD}`.",
        )
    if not hits:
        return _check(
            "embeddings.sidecar",
            "warn",
            "Embedding sidecar loads and the model embeds, but a probe query returned no "
            "vectors — the index is empty.",
            f"Build vectors: `{_REBUILD_VECTORS_CMD}`.",
        )
    # Readiness must be provable, not merely un-refuted: report the serving
    # backend, the vector count behind the hit, and the model fingerprint that
    # identifies the vector space. A benchmark contender is disqualified when it
    # cannot show it is serving semantically (docs/benchmark-fairness-contract.md),
    # and until now an ONNX install had no way to show that from doctor.
    from . import embedding_backend

    fingerprint = embedding_backend.fingerprint(embeddings.MODEL_NAME)
    try:
        metadata, _matrix = index.all_vectors()
        vector_count: int | None = len(metadata)
    except Exception:  # noqa: BLE001 — the probe already proved the lane serves
        vector_count = None
    counted = f"{vector_count} vector(s)" if vector_count is not None else "vectors present"
    return _check(
        "embeddings.sidecar",
        "pass",
        f"Embedding sidecar live via {backend}: embed+search over {counted} returned "
        f"{len(hits)} hit(s) ({fingerprint}).",
        details={
            "backend": backend,
            "vector_count": vector_count,
            "fingerprint": fingerprint,
            "model": embeddings.MODEL_NAME,
        },
    )


def _hf_hub_dir() -> Path:
    """The local HuggingFace hub cache directory (honors HF_HUB_CACHE / HF_HOME).

    Delegates to `model_cache` so doctor reports the directory the RUNTIME will
    actually load from — two implementations of this would drift.
    """
    from . import model_cache

    return model_cache.hub_dir()


def _model_cached(hub: Path, dirname: str) -> bool:
    """True if a model's snapshot dir exists and is non-empty — a pure directory
    check, so a caller can gate model-loading work on it WITHOUT risking a
    download (doctor never fetches)."""
    from . import model_cache

    return model_cache.snapshot_populated(hub, dirname)


def _check_model_residency() -> DoctorCheck:
    """Whether an embed is about to pay a model load, and whether it will hit the network.

    Until this check existed, the only way to know a write was about to pay ~35s
    was to read the server log after it had already paid it. `loaded` is the model
    singleton, not the module — `status.models.module_loaded` answers the narrower
    question and reads as this one. `offline_load` says whether the load resolves
    purely from the cache directory or revalidates every file against the hub.
    """
    from . import embeddings, mode, model_cache

    resident = embeddings._MODEL is not None
    cached = _model_cached(_hf_hub_dir(), model_cache.snapshot_dirname(embeddings.MODEL_NAME))
    offline = model_cache.should_load_offline(embeddings.MODEL_NAME)
    preload = mode.preload_models()
    details = {
        "model": embeddings.MODEL_NAME,
        "loaded": resident,
        "cache_dir": str(model_cache.hub_dir()),
        "bi_encoder_cached": cached,
        "offline_load": offline,
        "preload_policy": preload,
        "reap_models_when_idle": mode.reap_models_when_idle(),
    }
    if resident:
        return _check(
            "models.residency",
            "pass",
            f"{embeddings.MODEL_NAME} is resident; embeds pay no load.",
            details=details,
        )
    if not cached:
        return _check(
            "models.residency",
            "pass",
            f"{embeddings.MODEL_NAME} is not loaded and not cached; the next embed "
            "downloads it inline.",
            "Run `exomem warm` to fetch it, then `exomem mode performance` to preload "
            "it at startup instead of inside a request.",
            details=details,
        )
    where = "from the local cache" if offline else "revalidating every file against the hub"
    return _check(
        "models.residency",
        "pass",
        f"{embeddings.MODEL_NAME} is cached but not loaded; the next embed loads it {where}.",
        None
        if preload
        else "Run `exomem mode performance` (or set EXOMEM_PRELOAD_MODELS=1) to load it at "
        "startup instead of inside whichever request arrives first.",
        details=details,
    )


def _check_models_cache() -> DoctorCheck:
    """Local HF-cache presence for the three search models. Read-only: this
    inspects directories only — doctor never downloads anything."""
    from . import embeddings, model_cache

    hub = _hf_hub_dir()
    backend = _resolved_embedding_backend()

    expected = [embeddings.MODEL_NAME]
    # The reranker and CLIP are sentence-transformers models. On a lane that
    # withholds torch they can never be cached, so listing them as misses is a
    # WARN no action can clear — and `exomem warm`, the remediation, cannot
    # fetch them either.
    if _serves_reranker_and_clip(backend):
        expected.extend([embeddings.RERANKER_NAME, embeddings.CLIP_MODEL_NAME])

    # `snapshot_dirname` knows that sentence-transformers resolves a bare name
    # (CLIP) under its own org, so that layout rule lives in one place.
    missing = [
        name
        for name in expected
        if not _model_cached(hub, model_cache.snapshot_dirname(name))
    ]
    if not missing:
        return _check(
            "models.cache",
            "pass",
            f"Search models for the {backend} lane are present in the local HF cache.",
        )
    # Only claim degraded recall when the bi-encoder itself is absent — with it
    # cached and serving, finds are semantic whatever else is still downloading.
    bi_encoder_missing = embeddings.MODEL_NAME in missing
    consequence = (
        " The first server start downloads them in the background; hybrid finds are "
        "lexical-only meanwhile."
        if bi_encoder_missing
        else " Hybrid finds already run semantically on the cached bi-encoder."
    )
    return _check(
        "models.cache",
        "warn",
        f"Not yet in the local HF cache: {', '.join(missing)}.{consequence}",
        "Run `exomem warm` to pre-download them now (~1-2 GB).",
    )


def _check_tesseract(*, required: bool = True) -> DoctorCheck:
    """Report what the RUNTIME will resolve, not a narrower guess.

    Doctor checked only `EXOMEM_TESSERACT_CMD` and PATH, while
    `extract._ensure_tesseract_cmd` also probes the standard install locations.
    The UB-Mannheim Windows package installs to one of those and does not touch
    PATH, so doctor reported FAIL on a host where OCR demonstrably worked — and
    `scripts/upgrade.ps1 -Profile media` then refused a safe service restart
    with the release already staged. Sharing one resolver is what stops the two
    drifting again.
    """
    from . import extract

    configured = os.environ.get("EXOMEM_TESSERACT_CMD")
    if configured and Path(configured).exists():
        return _check("tool.tesseract", "pass", f"Tesseract configured at {configured}.")
    found = extract.resolve_tesseract_cmd()
    if found and Path(found).exists():
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


def _probe_get_authorized(url: str, token: str) -> tuple[int, object]:
    """Authenticated GET with a short timeout; returns (status, parsed-JSON-or-text).

    Module-level seam so tests fake the transport, mirroring `_probe_get`.
    """
    import httpx

    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    resp = httpx.get(url, headers=headers, timeout=5.0, follow_redirects=False)
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


_REPLICA_ORIGIN_VARS = {
    "DESKTOP_REPLICA_ID": "DESKTOP_ORIGIN",
    "LAPTOP_REPLICA_ID": "LAPTOP_ORIGIN",
}


def _check_edge_ingress_worker_fronting(base_url: str, vault_id: str) -> DoctorCheck:
    """Unauthenticated GET on a coordinator path must return the worker's 401
    shape — proves the worker (not a tunnel-direct origin) fronts the apex for
    coordinator paths (design.md Decision 3, check 1)."""
    url = f"{base_url}/v1/vaults/{urllib.parse.quote(vault_id, safe='')}/lease"
    try:
        status, body = _probe_get(url)
    except Exception as e:  # noqa: BLE001 - network diagnostic boundary
        return _check(
            "edge_ingress.worker_fronting",
            "fail",
            f"Could not reach {url}: {e}",
            "Check DNS binding, tunnel ingress hostname, and worker route coverage for "
            "the public apex.",
        )
    if status == 401 and isinstance(body, dict) and body.get("error") == "unauthorized":
        return _check(
            "edge_ingress.worker_fronting",
            "pass",
            "Public apex answers the worker's unauthenticated 401 shape on the "
            "coordinator path.",
        )
    return _check(
        "edge_ingress.worker_fronting",
        "fail",
        f"{url} answered status={status} body={body!r}, expected the worker's "
        "401 {'error': 'unauthorized'} shape.",
        "The public apex may be served tunnel-direct instead of by the HA worker. Check "
        "DNS binding, tunnel ingress hostname, and worker route coverage.",
    )


def _check_edge_ingress_provenance(base_url: str, config: LeaseConfig) -> DoctorCheck:
    """Authenticated GET /__version must succeed and its `deployed_vars` must not
    have drifted from this origin's expectations (design.md Decision 3, check 2)."""
    url = f"{base_url}/__version"
    try:
        status, body = _probe_get_authorized(url, config.token or "")
    except Exception as e:  # noqa: BLE001 - network diagnostic boundary
        return _check(
            "edge_ingress.provenance",
            "fail",
            f"Could not reach {url}: {e}",
            "Check DNS binding, tunnel ingress hostname, and worker route coverage for "
            "the public apex.",
        )
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("deployed_vars"), dict):
        return _check(
            "edge_ingress.provenance",
            "fail",
            f"{url} answered status={status}, expected an authenticated 200 with a "
            "deployed_vars payload.",
            "The public apex may not be served by the HA worker beyond /v1/*. Check "
            "DNS binding, tunnel ingress hostname, and worker route coverage.",
        )
    deployed_vars = body["deployed_vars"]
    drift: list[str] = []
    timeout_ms = deployed_vars.get("MCP_TOOL_TIMEOUT_MS")
    if not isinstance(timeout_ms, (int, float)) or isinstance(timeout_ms, bool) or timeout_ms < 60000:
        drift.append(f"MCP_TOOL_TIMEOUT_MS={timeout_ms!r} is below the 60000ms floor")
    if not deployed_vars.get("REQUIRE_COORDINATION"):
        drift.append("REQUIRE_COORDINATION is not truthy")
    replica_id = config.replica_id or ""
    replica_configured = any(
        deployed_vars.get(replica_var) == replica_id and deployed_vars.get(origin_var)
        for replica_var, origin_var in _REPLICA_ORIGIN_VARS.items()
    )
    if not replica_configured:
        drift.append(
            f"this origin's replica id {replica_id!r} is not present among the worker's "
            "replica-id vars with an origin configured"
        )
    git_sha = body.get("git_sha")
    if git_sha == "unlabeled":
        drift.append("worker was deployed without a labeled git_sha")
    if drift:
        return _check(
            "edge_ingress.provenance",
            "warn",
            f"Worker deploy provenance drift: {'; '.join(drift)}.",
            "Redeploy the worker with the deploy helper and verify MCP_TOOL_TIMEOUT_MS, "
            "REQUIRE_COORDINATION, and the replica-id/origin vars.",
            details={"git_sha": git_sha, "deployed_vars": deployed_vars},
        )
    return _check(
        "edge_ingress.provenance",
        "pass",
        f"Worker deploy provenance is current (git_sha={git_sha!r}).",
        details={"git_sha": git_sha},
    )


def _check_edge_ingress_read_routing(base_url: str, config: LeaseConfig) -> DoctorCheck:
    """Public /health/ready must agree with the coordinator's current lease holder —
    proves holder-first read routing is intact (design.md Decision 3, check 3)."""
    from .writer_lease import LeaseCoordinatorClient

    url = f"{base_url}/health/ready"
    try:
        status, body = _probe_get(url)
    except Exception as e:  # noqa: BLE001 - network diagnostic boundary
        return _check(
            "edge_ingress.read_routing",
            "fail",
            f"Could not reach {url}: {e}",
            "Check DNS binding, tunnel ingress hostname, and worker route coverage for "
            "the public apex.",
        )
    if not isinstance(body, dict):
        return _check(
            "edge_ingress.read_routing",
            "fail",
            f"{url} answered status={status} with a non-JSON body.",
        )
    reported_replica = body.get("replica_id")
    try:
        record = LeaseCoordinatorClient(config).status()
    except OpError as e:
        return _check(
            "edge_ingress.read_routing",
            "fail",
            f"Could not confirm the coordinator's current lease holder: {e}",
            "Check the writer-lease coordinator URL, credentials, and health.",
        )
    if reported_replica == record.holder:
        return _check(
            "edge_ingress.read_routing",
            "pass",
            f"Public /health/ready replica ({reported_replica!r}) matches the "
            "coordinator's current lease holder.",
        )
    return _check(
        "edge_ingress.read_routing",
        "fail",
        f"Public /health/ready reports replica {reported_replica!r}, but the "
        f"coordinator's current lease holder is {record.holder!r}.",
        "Investigate holder-first read routing at the HA edge worker.",
        details={"reported_replica": reported_replica, "lease_holder": record.holder},
    )


def _check_observability() -> DoctorCheck:
    """Log directory writability, active/rotated file sizes, JSONL
    tail-parseability, the NSSM `service.*` rotation pile, and
    metrics-snapshot freshness. Never touches the network."""
    from . import metrics
    from .logging_config import resolve_log_dir

    details: dict[str, object] = {}
    problems: list[str] = []
    warnings: list[str] = []

    log_dir = resolve_log_dir()
    details["log_dir"] = str(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe_path = log_dir / f".exomem-doctor-probe-{os.getpid()}"
        probe_path.write_text("", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        details["log_dir_writable"] = True
    except (OSError, ValueError):
        details["log_dir_writable"] = False
        problems.append("log directory is not writable")

    jsonl_names = (
        "queries.jsonl",
        "writes.jsonl",
        "reads.jsonl",
        "mutations.jsonl",
        "ledger.jsonl",
    )
    for name in jsonl_names:
        try:
            path = log_dir / name
            if name == "ledger.jsonl":
                # The ledger rotates into a content-addressed archive, not to a
                # `.1` generation, so the usual probe would report `False`
                # forever on a ledger that has rotated many times.
                archive = log_dir / "ledger-archive"
                details[f"{name}.rotated"] = archive.is_dir() and any(
                    archive.glob("ledger-*.jsonl")
                )
            else:
                details[f"{name}.rotated"] = (log_dir / f"{name}.1").exists()
            if not path.exists():
                continue
            details[f"{name}.bytes"] = path.stat().st_size
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines and lines[-1].strip():
                json.loads(lines[-1])
        except (OSError, ValueError):
            warnings.append(f"{name} tail is not parseable JSON")

    try:
        service_pile = sum(
            1
            for pattern in ("service.out.log.*", "service.err.log.*")
            for _ in log_dir.glob(pattern)
        )
    except (OSError, ValueError):
        service_pile = 0
    details["service_pile_count"] = service_pile
    if service_pile > 50:
        warnings.append(f"{service_pile} service.* rotated files have accumulated")

    try:
        from .writer_lease import get_manager

        state_dir = get_manager().config.state_dir
        interval = metrics.snapshot_interval_seconds_from_env()
        snapshot_path = metrics.snapshot_path(state_dir)
        if interval > 0 and snapshot_path.exists():
            age = time.time() - snapshot_path.stat().st_mtime
            details["metrics_snapshot_age_seconds"] = round(age, 1)
            if age > interval * 2:
                warnings.append("metrics snapshot is stale")
    except Exception:  # noqa: BLE001 - doctor must stay structured
        pass

    if problems:
        return _check(
            "observability",
            "fail",
            "; ".join(problems),
            "Ensure the log directory is writable (check permissions or EXOMEM_LOG_DIR).",
            details=details,
        )
    if warnings:
        return _check("observability", "warn", "; ".join(warnings), None, details=details)
    return _check(
        "observability", "pass", "Log directory, JSONL logs, and metrics snapshot look healthy.",
        details=details,
    )


def _check_idempotency_store() -> DoctorCheck:
    """Warn when idempotency receipts have piled up abandoned, or the oldest
    pending row has sat unresolved past the legacy grace window — either is
    a sign a mutation crashed and needs operator attention, not necessarily
    a fault by itself. Never touches the network."""
    try:
        from .writer_lease import get_manager

        store = get_manager().idempotency
        validate_runtime_state = getattr(store, "validate_runtime_state", None)
        if validate_runtime_state is not None:
            validate_runtime_state()
        summary = store.status_summary()
    except Exception as error:  # noqa: BLE001 - doctor must stay structured
        remediation = getattr(error, "remediation", None)
        return _check(
            "idempotency_store",
            "fail",
            f"Idempotency runtime is unavailable: {error}",
            remediation,
            details={"error": type(error).__name__},
        )

    abandoned = summary.get("abandoned")
    oldest_pending_age_seconds = summary.get("oldest_pending_age_seconds")
    warnings: list[str] = []
    if isinstance(abandoned, int) and abandoned > 0:
        warnings.append(f"{abandoned} abandoned idempotency receipt(s)")
    if isinstance(oldest_pending_age_seconds, (int, float)) and oldest_pending_age_seconds > 600:
        warnings.append(
            f"oldest pending idempotency receipt is {oldest_pending_age_seconds:.0f}s old"
        )
    if warnings:
        return _check("idempotency_store", "warn", "; ".join(warnings), None, details=summary)
    return _check(
        "idempotency_store", "pass", "Idempotency receipts look healthy.", details=summary
    )


def _check_edge_ingress(*, probe: bool) -> list[DoctorCheck]:
    """Doctor's `edge-ingress` section (design.md Decision 3): verifies the public
    apex is fronted by the HA edge worker rather than tunnel-direct. Skipped
    entirely when writer-lease coordination is disabled."""
    if not os.environ.get("EXOMEM_WRITER_LEASE_URL", "").strip():
        return []
    from .writer_lease import LeaseConfig

    try:
        config = LeaseConfig.from_env()
    except ValueError as e:
        return [_check(
            "edge_ingress.config",
            "fail",
            str(e),
            "Fix the writer-lease environment configuration.",
        )]

    checks: list[DoctorCheck] = []
    if config.ttl_seconds < 30:
        checks.append(_check(
            "edge_ingress.lease_ttl",
            "warn",
            f"EXOMEM_WRITER_LEASE_TTL is {config.ttl_seconds:g}s, below the supported "
            "floor of 30s.",
            "Raise EXOMEM_WRITER_LEASE_TTL to at least 30 seconds.",
        ))
    else:
        checks.append(_check(
            "edge_ingress.lease_ttl",
            "pass",
            f"EXOMEM_WRITER_LEASE_TTL is {config.ttl_seconds:g}s (>= 30s floor).",
        ))

    if not probe:
        return checks

    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        checks.append(_check(
            "edge_ingress.base_url",
            "fail",
            "EXOMEM_BASE_URL is not set; cannot probe the public ingress.",
            "Set EXOMEM_BASE_URL to the public HTTPS origin.",
        ))
        return checks

    checks.append(_check_edge_ingress_worker_fronting(base_url, config.vault_id or ""))
    checks.append(_check_edge_ingress_provenance(base_url, config))
    checks.append(_check_edge_ingress_read_routing(base_url, config))
    return checks


def doctor(
    *,
    vault: str | None = None,
    profile: Profile | None = None,
    probe: bool = False,
    replica_urls: list[str] | tuple[str, ...] | None = None,
) -> DoctorReport:
    profile = resolve_profile(profile)
    if profile not in VALID_PROFILES:
        raise ValueError(f"unknown profile: {profile!r}. Valid: {list(VALID_PROFILES)}")

    vault_root, vault_check = _resolve_vault(vault)
    lock_parity = _check_editable_lock_parity(profile)
    checks: list[DoctorCheck] = [
        _check_python(),
        _check_uv(),
        *([lock_parity] if lock_parity is not None else []),
        _check_console_scripts(),
        _check_package_import(),
        vault_check,
        *_check_schema_files(vault_root),
        _check_repo_env(),
        _check_registry(),
        _check_resource_posture(profile),
        _check_lexical(vault_root),
        _check_deferred_index_backlog(vault_root),
        _check_graph_sync_state(vault_root),
        _check_rebuild_temp_orphans(vault_root),
        _check_write_path_env_flags(vault_root),
    ]
    runtime_processes = _check_runtime_processes()
    if runtime_processes is not None:
        checks.append(runtime_processes)
    media_runtime = _check_media_runtime(vault_root)
    if media_runtime is not None:
        checks.append(media_runtime)

    if profile in ("hybrid", "standard", "media"):
        extra, requirements = _embedding_requirements()
        checks.append(_check_embeddings_disabled())
        checks.extend(
            _check_dependency(distribution, extra, import_name=import_name)
            for distribution, import_name in requirements
        )
        # The torch accelerator probes describe a runtime this install may not
        # have. Reporting "torch is not installed" on an ONNX image is noise
        # about an absent framework, not a finding about the configured one.
        if extra == "embeddings":
            checks.extend([_check_torch_cuda(), _check_torch_device()])
        checks.extend([_check_models_cache(), _check_model_residency(), _check_sqlite_vec()])
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

    # Not profile-gated: any profile can run with writer-lease coordination
    # enabled, and the section self-skips when it is not (design.md Decision 3).
    checks.extend(_check_edge_ingress(probe=probe))
    checks.append(_check_observability())
    checks.append(_check_idempotency_store())

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
