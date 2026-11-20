"""Content-free discovery for the composed Cloud cell and its service contract."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, capabilities

DISCOVERY_PATH = "/.well-known/exomem-cell"
# Concurrent discovery reads take one store lock in turn, each in milliseconds. This wait
# lets them queue; a store a capture holds longer reports an unknown recall space.
RECALL_SPACE_WAIT_SECONDS = 0.5


def software_identity(package_root: Path | None = None) -> dict:
    """Fingerprint package files once at composition, without an install registry."""
    root = package_root or Path(__file__).parent
    digest = hashlib.sha256()
    files = 0
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            # Python's interpreter cache and bytecode formats do not identify package source.
            if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo") or not path.is_file():
                continue
            digest.update(relative.as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            files += 1
        if not files:
            raise FileNotFoundError("package files are unavailable")
    except (OSError, UnicodeError):
        return {"state": "unknown", "version": __version__, "sha256": None,
                "scope": "package-files-at-server-composition"}
    return {"state": "observed", "version": __version__, "sha256": digest.hexdigest(),
            "scope": "package-files-at-server-composition"}


def service_contract(surface: capabilities.ActiveSurfaceDescriptor) -> dict:
    """Read service features from their command declarations, never a second allowlist."""
    from . import capture_inventory, passage_evidence, source_capture

    features = sorted({feature for command in surface.contributing_commands
                       for feature in command.mcp_meta.get(capabilities.SERVICE_CAPABILITIES_META_KEY, ())})
    limits = {}
    # These are the service protocol's feature names, not a content vocabulary.
    if "declared_passages" in features:
        limits.update(passages_per_capture=source_capture.MAX_PASSAGES,
                      passage_key_utf8_bytes=source_capture.MAX_PASSAGE_KEY_BYTES)
    if "passage_results" in features:
        limits["passage_results"] = passage_evidence.MAX_RESULTS
    if "captured_source_inventory" in features:
        limits["captured_source_inventory_page"] = capture_inventory.MAX_PAGE_SIZE
    return {"operations": features, "limits": limits, "surface_sha256": surface.fingerprint}


def register_identity_route(mcp, *, cell_id: str, vault_root: Path,
                            surface: capabilities.ActiveSurfaceDescriptor) -> None:
    """Register the credential-free route only from Cloud composition."""
    software = software_identity()

    @mcp.custom_route(DISCOVERY_PATH, methods=["GET"])
    async def identity(request: Request) -> JSONResponse:  # noqa: ARG001
        space = await asyncio.to_thread(recall_space_state, vault_root)
        return JSONResponse({"protocol_version": 1, "cell_id": cell_id, "software": software,
                             **service_contract(surface), "recall_space": space},
                            headers={"Cache-Control": "no-store"})


def recall_space_state(vault_root: Path) -> dict:
    """The published recall space; a store that stays busy is unknown, never unpublished."""
    from .embedding_index import RecallSpaceUnread, read_published_identity

    # These states belong to this discovery response schema.
    try:
        space = read_published_identity(vault_root, wait_seconds=RECALL_SPACE_WAIT_SECONDS)
    except RecallSpaceUnread:
        return {"state": "unknown", "id": None}
    if space is None:
        return {"state": "unavailable", "id": None}
    return {"state": "observed", "id": space.identity_digest}
