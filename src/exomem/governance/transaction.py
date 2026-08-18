"""Dependency-light primitives shared by governance mutation and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


class GovernanceError(RuntimeError):
    """Stable governance refusal."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class GovernanceCrash(RuntimeError):
    """Test seam representing a process crash at a durable boundary."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


def canonical_documents(documents: Any) -> dict[str, str]:
    if not isinstance(documents, Mapping) or not documents:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "documents must be a mapping")
    canonical: dict[str, str] = {}
    for raw_path, raw_content in sorted(documents.items(), key=lambda item: str(item[0])):
        rel = str(raw_path).replace("\\", "/").strip("/")
        parts = Path(rel).parts
        if (
            Path(rel).is_absolute()
            or not parts
            or parts[0] not in {"scopes", "rules", "grants"}
            or any(part in {"", ".", ".."} for part in parts)
            or not rel.endswith(".yaml")
        ):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy path")
        if not isinstance(raw_content, str):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be text")
        try:
            parsed = yaml.safe_load(raw_content)
        except yaml.YAMLError as exc:
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy YAML") from exc
        if not isinstance(parsed, dict):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be a mapping")
        canonical[rel] = raw_content.rstrip() + "\n"
    return canonical


def component(kind: str, key: str, value: Mapping[str, Any], *, status: str) -> dict[str, Any]:
    normalized = dict(value)
    return {
        "component_kind": kind,
        "component_key": key,
        "value_json": canonical_json(normalized),
        "value_hash": digest(normalized),
        "status": status,
    }


def composite(phase: str, components: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "kind": item["component_kind"],
            "key": item["component_key"],
            "value_hash": item["value_hash"],
            "status": item["status"],
        }
        for item in sorted(
            components, key=lambda row: (row["component_kind"], row["component_key"])
        )
    ]
    return digest({"domain": f"governance-composite/{phase}/v1", "components": normalized})


def archive_value(rel: str, prior_bytes: bytes | None, prior_hash: str) -> dict[str, str]:
    return {
        "path_hash": hashlib.sha256(rel.encode()).hexdigest(),
        "prior_hash": prior_hash,
        "bytes_hash": "absent" if prior_bytes is None else hashlib.sha256(prior_bytes).hexdigest(),
    }


def policy_target(governance_root: Path, relative: str) -> Path:
    """Prove a policy target stays beneath its real, non-symlinked root."""
    root = Path(governance_root)
    rel = Path(relative)
    if (
        not isinstance(relative, str)
        or rel.is_absolute()
        or not rel.parts
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        raise GovernanceError("INVALID_GOVERNANCE_TARGET", "policy target is not canonical")
    try:
        if root.resolve(strict=False) != root.absolute():
            raise GovernanceError("INVALID_GOVERNANCE_TARGET", "governance root is symlinked")
        current = root
        for index, part in enumerate(rel.parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise GovernanceError("INVALID_GOVERNANCE_TARGET", "policy target is symlinked")
            if index < len(rel.parts) - 1 and not stat.S_ISDIR(mode):
                raise GovernanceError("INVALID_GOVERNANCE_TARGET", "policy parent is not a directory")
    except GovernanceError:
        raise
    except (OSError, RuntimeError) as exc:
        raise GovernanceError("INVALID_GOVERNANCE_TARGET", "policy target cannot be resolved") from exc
    return root.joinpath(*rel.parts)


def fsync_directory(path: Path) -> None:
    """Flush one directory entry so a completed rename survives a crash.

    Windows has no CRT route to this. `os.open` on a directory raises
    `PermissionError: [Errno 13]` there, so the POSIX idiom below did not
    degrade on Windows -- it failed every call, and with it every governance
    commit that reached `durable_json`. That is 76 of the 82 permission
    failures on the `windows-latest` shard, and a product defect rather than
    a test artifact: the governance layer could not commit on Windows at all.

    `governance.lifecycle` and `governance.receipts` already flush a raw
    directory handle through `mutation_lock`; this module, its `durable_json`,
    `governance.recovery` and `governance.tool` all shared the unported copy.
    Both branches raise `OSError` on failure, so the contract is unchanged.
    """
    if os.name == "nt":
        from .. import mutation_lock

        mutation_lock._windows_flush_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def authorization_row(**fields: Any) -> dict[str, Any]:
    """Versioned canonical projection for authorization-bearing sidecar rows."""
    if "projection_version" in fields:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROJECTION",
            "projection_version is reserved by the authorization projection",
        )
    return {"projection_version": 1, **fields}
