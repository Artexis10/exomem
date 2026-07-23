"""Cheap, non-secret CLI and managed-service install identity.

This module is intentionally dependency-light.  ``exomem --version`` must work
in a lean uv-tool environment without importing optional model/media packages,
and it must be useful when that lean command fronts a separately managed full
service installation.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
