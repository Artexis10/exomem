"""Cheap, non-secret CLI and managed-service install identity.

This module is intentionally dependency-light.  ``exomem --version`` must work
in a lean uv-tool environment without importing optional model/media packages,
and it must be useful when that lean command fronts a separately managed full
service installation.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

_MANIFEST_ENV = "EXOMEM_MANAGED_INSTALL_MANIFEST"
_VALID_PROFILES = frozenset({"lean", "hybrid", "standard", "media"})
_VALID_ROUTES = frozenset({"direct", "service"})


def _package_version() -> str:
    try:
        return version("exomem")
    except Exception:  # noqa: BLE001 - version reporting must not crash
        return "unknown"


def _install_source() -> str:
    """Classify the distribution without resolving git or importing extras."""
    try:
        dist = distribution("exomem")
    except Exception:  # noqa: BLE001 - metadata may be partially installed
        return "unknown"
    try:
        raw = dist.read_text("direct_url.json")
        if raw and json.loads(raw).get("dir_info", {}).get("editable"):
            return "editable"
    except Exception:  # noqa: BLE001 - malformed metadata is non-fatal
        return "unknown"
    return "wheel"


def editable_project_root_status() -> tuple[Path | None, str | None]:
    """Return a validated editable root or the reason editable metadata is unusable."""
    try:
        raw = distribution("exomem").read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - missing metadata cannot establish an editable root
        return None, "installation metadata is unavailable"
    if not raw:
        return None, None
    try:
        direct_url = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return None, "direct_url.json is malformed"
    if not isinstance(direct_url, dict):
        return None, "direct_url.json is malformed"
    dir_info = direct_url.get("dir_info")
    if dir_info is not None and not isinstance(dir_info, dict):
        return None, "direct_url.json has malformed dir_info"
    if not isinstance(dir_info, dict) or not dir_info.get("editable"):
        return None, None
    try:
        parsed = urlsplit(direct_url.get("url", ""))
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            return None, "editable direct_url.json does not name a local file URL"
        root = Path(url2pathname(unquote(parsed.path))).resolve()
        required = (root / "pyproject.toml", root / "uv.lock")
        if not root.is_dir() or not all(path.is_file() for path in required):
            return None, "editable project root, pyproject.toml, or uv.lock is unavailable"
        return root, None
    except Exception:  # noqa: BLE001 - metadata is an untrusted diagnostic input
        return None, "editable direct_url.json is invalid"


def editable_project_root() -> Path | None:
    """Return the validated local project root recorded by an editable install."""
    return editable_project_root_status()[0]


def managed_manifest_path() -> Path:
    override = os.environ.get(_MANIFEST_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA", "").strip()
        if root:
            return Path(root) / "Exomem" / "managed-install.json"
        return Path.home() / "AppData" / "Local" / "Exomem" / "managed-install.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Exomem" / "managed-install.json"
    root = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(root) if root else Path.home() / ".config") / "exomem" / "managed-install.json"


def _manifest() -> tuple[dict[str, Any], str]:
    path = managed_manifest_path()
    if not path.is_file():
        return {}, "absent"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, "invalid"
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return {}, "unsupported"
    return value, "ready"


def configured_local_profile() -> str | None:
    manifest, status = _manifest()
    value = _safe_string(manifest.get("cli_profile")) if status == "ready" else None
    return value if value in _VALID_PROFILES else None


def persist_local_profile(profile: str) -> Path:
    """Atomically merge a selected local profile into a schema-v1 manifest."""
    if profile not in _VALID_PROFILES:
        raise ValueError(f"unknown local profile: {profile!r}")
    path = managed_manifest_path()
    manifest, status = _manifest()
    if status not in {"absent", "ready"}:
        raise ValueError(f"cannot update {path}: existing managed-install manifest is {status}")
    data = dict(manifest) if status == "ready" else {"schema_version": 1}
    data["cli_profile"] = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
    os.replace(temporary, path)
    return path


def _safe_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _safe_service_target(value: Any) -> str | None:
    target = _safe_string(value)
    if target is None:
        return None
    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
        _ = parsed.port  # Validate a present port before exposing the original value.
    except ValueError:
        return None
    if (
        parsed.geturl() != target
        or parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return target


def report() -> dict[str, Any]:
    """Return the stable local report, whitelisting every manifest field.

    Unknown manifest fields are ignored so a future installer can never make
    this surface echo credentials, vault locations, or user-authored content.
    """
    package_version = _package_version()
    manifest, status = _manifest()
    service_version = _safe_string(manifest.get("service_version"))
    service_profile = _safe_string(manifest.get("service_profile"))
    if service_profile not in _VALID_PROFILES:
        service_profile = None
    target = _safe_service_target(manifest.get("service_target"))
    local_profile = os.environ.get("EXOMEM_PROFILE", "").strip()
    if local_profile not in _VALID_PROFILES:
        local_profile = _safe_string(manifest.get("cli_profile")) or "lean"
    if local_profile not in _VALID_PROFILES:
        local_profile = "lean"
    route = _safe_string(manifest.get("cli_route")) or "direct"
    if route not in _VALID_ROUTES:
        route = "direct"
    return {
        "version": package_version,
        "python_executable": sys.executable,
        "install_source": _install_source(),
        "local_profile": local_profile,
        "managed_service_version": service_version,
        "managed_service_profile": service_profile,
        "managed_service_target": target,
        "effective_route": route,
        "version_match": (
            package_version == service_version if service_version is not None else None
        ),
        "manifest_status": status,
    }


def print_version(*, as_json: bool) -> int:
    identity = report()
    if as_json:
        print(json.dumps(identity, ensure_ascii=False))
    else:
        print(f"exomem {identity['version']}")
    return 0
