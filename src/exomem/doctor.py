"""Local install preflight checks for exomem.

`doctor` is deliberately CLI-only and read-only: it inspects the host, Python
environment, vault path, optional dependency imports, and environment variables.
It never initializes a vault, writes `.env`, starts services, downloads models,
or mutates the embedding sidecar.

The one network exception is explicit opt-in: `--probe` (remote profile only)
performs three read-only GETs to verify the live connector endpoints — local
/mcp expects 401, OAuth discovery expects 200, and the bare
oauth-protected-resource path expects 200 (the claude.ai registration gate).
Without the flag, doctor performs zero network calls.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["pass", "warn", "fail"]
Profile = Literal["lean", "hybrid", "media", "remote"]
VALID_PROFILES: tuple[Profile, ...] = ("lean", "hybrid", "media", "remote")

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DoctorCheck:
    id: str
    status: Status
    message: str
    remediation: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
        }


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
) -> DoctorCheck:
    return DoctorCheck(id=id_, status=status, message=message, remediation=remediation)


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


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
    skill = path / "Knowledge Base" / "_Schema" / "SKILL.md"
    if not skill.exists():
        return path, _check(
            "vault.path",
            "fail",
            f"{path} does not contain Knowledge Base/_Schema/SKILL.md.",
            "Pass the vault root, not the Knowledge Base folder. For a new vault, run "
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
        "copy .env.example to .env and fill it in.",
    )


def _check_schema_files(vault_root: Path | None) -> list[DoctorCheck]:
    if vault_root is None:
        return []
    kb = vault_root / "Knowledge Base"
    checks: list[DoctorCheck] = []
    required = [
        ("vault.schema", kb / "_Schema" / "SKILL.md", "Knowledge Base schema contract"),
        ("vault.index", kb / "index.md", "Knowledge Base/index.md"),
        ("vault.log", kb / "log.md", "Knowledge Base/log.md"),
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
            "torch is not installed, so CUDA availability cannot be checked.",
            "Install embeddings with `uv sync --extra embeddings`.",
        )
    try:
        import torch

        available = bool(torch.cuda.is_available())
        if available:
            name = torch.cuda.get_device_name(0)
            arches = ", ".join(torch.cuda.get_arch_list())
            return _check("torch.cuda", "pass", f"CUDA visible to torch: {name} ({arches}).")
        return _check(
            "torch.cuda",
            "warn",
            "torch imports but CUDA is not available; embeddings/media will run on CPU.",
            "This is supported. On NVIDIA GPU hosts, verify the uv torch source and driver.",
        )
    except Exception as e:  # noqa: BLE001
        return _check(
            "torch.cuda",
            "warn",
            f"torch imports failed during CUDA probe: {e}",
            "Re-run `uv sync --extra embeddings`; on GPU hosts, verify the CUDA wheel/driver.",
        )


def _check_embedding_sidecar(vault_root: Path | None) -> DoctorCheck | None:
    if vault_root is None:
        return None
    sidecar = vault_root / "Knowledge Base" / ".embeddings.sqlite"
    if sidecar.exists():
        return _check("embeddings.sidecar", "pass", "Embedding sidecar exists.")
    return _check(
        "embeddings.sidecar",
        "warn",
        "Embedding sidecar is missing; hybrid search will degrade until vectors are built.",
        "After installing embeddings, run `kb reconcile` or `kb audit_fix --rebuild-embeddings true`.",
    )


def _check_models_cache() -> DoctorCheck:
    """Local HF-cache presence for the three search models. Read-only: this
    inspects directories only — doctor never downloads anything."""
    from . import embeddings

    if os.environ.get("HF_HUB_CACHE"):
        hub = Path(os.environ["HF_HUB_CACHE"])
    elif os.environ.get("HF_HOME"):
        hub = Path(os.environ["HF_HOME"]) / "hub"
    else:
        hub = Path.home() / ".cache" / "huggingface" / "hub"

    expected = {
        embeddings.MODEL_NAME: "models--" + embeddings.MODEL_NAME.replace("/", "--"),
        embeddings.RERANKER_NAME: "models--" + embeddings.RERANKER_NAME.replace("/", "--"),
        # sentence-transformers resolves bare names under its org.
        embeddings.CLIP_MODEL_NAME: "models--sentence-transformers--" + embeddings.CLIP_MODEL_NAME,
    }

    def _cached(dirname: str) -> bool:
        snapshots = hub / dirname / "snapshots"
        try:
            return snapshots.is_dir() and any(snapshots.iterdir())
        except OSError:
            return False

    missing = [name for name, dirname in expected.items() if not _cached(dirname)]
    if not missing:
        return _check("models.cache", "pass", "Search models are present in the local HF cache.")
    return _check(
        "models.cache",
        "warn",
        f"Not yet in the local HF cache: {', '.join(missing)}. The first server "
        "start downloads them in the background; hybrid finds are lexical-only meanwhile.",
        "Run `exomem warm` to pre-download them now (~1-2 GB).",
    )


def _check_tesseract() -> DoctorCheck:
    configured = os.environ.get("EXOMEM_TESSERACT_CMD")
    if configured and Path(configured).exists():
        return _check("tool.tesseract", "pass", f"Tesseract configured at {configured}.")
    found = shutil.which("tesseract")
    if found:
        return _check("tool.tesseract", "pass", f"Tesseract found at {found}.")
    return _check(
        "tool.tesseract",
        "fail",
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


def doctor(*, vault: str | None = None, profile: Profile = "lean", probe: bool = False) -> DoctorReport:
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
    ]

    if profile in ("hybrid", "media"):
        checks.extend([
            _check_embeddings_disabled(),
            _check_dependency("sentence-transformers", "embeddings", import_name="sentence_transformers"),
            _check_dependency("torch", "embeddings"),
            _check_dependency("pillow", "embeddings", import_name="PIL"),
            _check_torch_cuda(),
            _check_models_cache(),
        ])
        sidecar = _check_embedding_sidecar(vault_root)
        if sidecar is not None:
            checks.append(sidecar)

    if profile == "media":
        checks.extend([
            _check_dependency("faster-whisper", "media", import_name="faster_whisper"),
            _check_dependency("pytesseract", "media"),
            _check_dependency("pymupdf", "media", import_name="fitz"),
            _check_dependency("markitdown", "media"),
            _check_tesseract(),
        ])

    if profile == "remote":
        checks.extend(_check_remote_env())
        # Opt-in live-endpoint verification (three read-only GETs). The
        # default stays fully offline — doctor never touches the network
        # unless --probe is passed explicitly.
        if probe:
            checks.extend(_check_remote_probes())

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
