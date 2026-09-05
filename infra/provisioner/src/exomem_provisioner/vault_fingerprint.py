"""Offline, content-free canonical vault preservation proof."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CLASSIFICATION_VERSION = 1
VAULT_ROOT = Path("/var/lib/exomem/vault")
TERMINATION_LOG = Path("/dev/termination-log")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_MAX_FILES = 100_000
_MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
_REBUILDABLE_SQLITE_NAMES = {
    ".embeddings.sqlite",
    ".clip.sqlite",
    ".lexical.sqlite",
    ".graph.sqlite",
    ".claims.sqlite",
    ".references.sqlite",
    ".refs.sqlite",
    ".freshness.sqlite",
    ".deferred-index.sqlite",
    ".deferred_index.sqlite",
    ".media-jobs.sqlite",
    ".media_jobs.sqlite",
    ".idempotency.sqlite",
}
_RESERVED_EXACT = {
    *(
        f"{name}{suffix}"
        for name in (
            ".governance.sqlite",
            ".embeddings.sqlite",
            ".clip.sqlite",
            ".lexical.sqlite",
            ".graph.sqlite",
            ".claims.sqlite",
            ".references.sqlite",
            ".refs.sqlite",
            ".freshness.sqlite",
            ".deferred-index.sqlite",
            ".deferred_index.sqlite",
            ".media-jobs.sqlite",
            ".media_jobs.sqlite",
            ".idempotency.sqlite",
        )
        for suffix in ("", "-wal", "-shm", "-journal")
    ),
    ".deferred-index.json",
    ".media-jobs.json",
    ".media-worker.lock",
    ".idempotency.json",
    ".idempotency.jsonl",
    ".voice_profiles.json",
    ".graph-sync.json",
    ".graph-sync-floor.json",
    ".review-state.json",
    ".due-state.json",
}
_RESERVED_TREES = {
    "_governance",
    "_consolidation",
    ".graph-commit-receipts",
    ".graph-coordination",
    ".authorization-projections",
}
_RESERVED_ROOT_PATTERNS = (
    re.compile(r"^\.\.review-state\.json\.[a-z0-9_]{8}\.tmp$"),
    re.compile(r"^\.\.due-state\.json\.[a-z0-9_]{8}\.tmp$"),
    re.compile(r"^\.lexical\.sqlite\.rebuild-[0-9a-f]{32}\.tmp(?:-(?:wal|shm|journal))?$"),
    re.compile(r"^\.lexical\.sqlite(?:-(?:wal|shm))?\.quarantine-[0-9a-f]{32}$"),
    re.compile(r"^\.graph-rebuild-[0-9a-f]{64}-[0-9a-f]{24}\.sqlite(?:-(?:wal|shm|journal))?$"),
)
_RESERVED_TREE_ROOT_PATTERNS = (re.compile(r"^\.graph-reset-[0-9a-f]{24}$"),)
_RESERVED_COMPONENT_TREE_PATTERNS = (re.compile(r"^\.exomem-batch-[0-9a-f]{32}$"),)
_RESERVED_LEAF_PATTERNS = (re.compile(r"^\.exomem-held-publish-[0-9a-f]{32}$"),)


class FingerprintError(ValueError):
    """Stable internal failure whose details never cross the Job boundary."""


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: str
    size: int
    sha256: str
    classification: str


def _error(message: str) -> None:
    raise FingerprintError(message)


def _normalized(path: str) -> str:
    if not path or "\x00" in path or "\\" in path:
        _error("unsafe path")
    normalized = unicodedata.normalize("NFC", path)
    candidate = PurePosixPath(normalized)
    if (
        candidate.is_absolute()
        or path.startswith("/")
        or _WINDOWS_DRIVE.match(path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        _error("unsafe path")
    return candidate.as_posix()


def _is_registered_internal_state(lowered: tuple[str, ...], kb_dir: str) -> bool:
    if not lowered or lowered[0] != kb_dir.casefold():
        return False
    parts = lowered[1:]
    if not parts:
        return False
    leaf_path = "/".join(parts)
    if leaf_path in _RESERVED_EXACT:
        return True
    if any(leaf_path == tree or leaf_path.startswith(f"{tree}/") for tree in _RESERVED_TREES):
        return True
    if len(parts) == 1 and any(pattern.fullmatch(parts[0]) for pattern in _RESERVED_ROOT_PATTERNS):
        return True
    if any(pattern.fullmatch(parts[0]) for pattern in _RESERVED_TREE_ROOT_PATTERNS):
        return True
    if any(pattern.fullmatch(parts[-1]) for pattern in _RESERVED_LEAF_PATTERNS):
        return True
    return any(
        pattern.fullmatch(part) for part in parts for pattern in _RESERVED_COMPONENT_TREE_PATTERNS
    )


def _classification(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    lowered = tuple(part.casefold() for part in parts)
    basename = lowered[-1] if lowered else ""
    kb_dir = os.environ.get("EXOMEM_KB_DIRNAME", "").strip().strip("/") or "Knowledge Base"
    if path == f"{kb_dir}/.review-state.json":
        return "portable-derived"
    if re.fullmatch(rf"{re.escape(kb_dir)}/\.graph-commit-receipts/[0-9a-f]{{24}}\.json", path):
        return "portable-derived"
    if _is_registered_internal_state(lowered, kb_dir):
        return None
    if lowered and lowered[0] in {"logs", ".logs", "runtime-logs"}:
        return None
    names = set(lowered)
    secret_stems = {
        "service-credential",
        "oauth-token",
        "session-token",
        "encryption-key",
        "master-key",
    }
    if (
        basename == ".env"
        or basename.startswith(".env.")
        or basename.endswith((".pem", ".key", ".p12", ".pfx", ".jwk"))
        or bool(names & {"credentials", ".credentials", "secrets", ".secrets"})
        or any(
            basename == stem
            or basename in {f"{stem}.json", f"{stem}.yaml", f"{stem}.yml", f"{stem}.txt"}
            for stem in secret_stems
        )
    ):
        return None
    if (
        basename.endswith((".tmp", ".partial", ".lock", ".lck", ".swp", ".bak", "~"))
        or basename.startswith(".~")
        or basename in {"lock", "mutation.guard", ".ds_store", "thumbs.db"}
    ):
        return None
    if (
        any(part.endswith(".frames") for part in lowered)
        or bool(
            names
            & {
                ".models",
                "models",
                ".model-cache",
                ".voice-models",
                ".voice-profiles",
            }
        )
        or basename in {".voice_profiles.json", ".voice-profiles.json"}
    ):
        return None
    if any(
        basename == name or basename in {f"{name}-wal", f"{name}-shm"}
        for name in _REBUILDABLE_SQLITE_NAMES
    ) or basename in {
        ".idempotency.json",
        ".idempotency.jsonl",
        ".media-jobs.json",
        ".deferred-index.json",
    }:
        return None
    if (
        basename
        in {
            ".exomem-hosted-cell.json",
            "hosted-lifecycle-state.json",
            "hosted-security.sqlite",
            "hosted-security.sqlite-wal",
            "hosted-security.sqlite-shm",
            "writer-leases.sqlite",
            "writer-leases.sqlite-wal",
            "writer-leases.sqlite-shm",
        }
        or (lowered and lowered[0] in {"hosted-init-operations", "restore-journal", "tmp"})
        or (
            basename.startswith("idempotency-")
            and basename.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm"))
        )
    ):
        return None
    if len(lowered) >= 2 and lowered[0] == ".exomem" and lowered[1] == "schema":
        return "canonical"
    if any(part.startswith(".") for part in parts):
        return None
    return "canonical"


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read_regular(path: Path) -> tuple[int, str]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise FingerprintError("source changed") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _error("unsafe source entry")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FingerprintError("source changed") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            _error("source changed")
        initial = _signature(opened)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        if _signature(os.fstat(descriptor)) != initial or size != initial[2]:
            _error("source changed")
    finally:
        os.close(descriptor)
    if size > _MAX_FILE_BYTES:
        _error("resource limit exceeded")
    return size, digest.hexdigest()


def _enumerate(vault_root: Path) -> list[_Snapshot]:
    try:
        root = vault_root.lstat()
    except OSError as exc:
        raise FingerprintError("vault unavailable") from exc
    if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
        _error("vault unavailable")
    snapshots: list[_Snapshot] = []
    total_bytes = 0
    for current, directory_names, file_names in os.walk(
        vault_root, topdown=True, followlinks=False
    ):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        for name in directory_names:
            value = current_path / name
            try:
                information = value.lstat()
            except OSError as exc:
                raise FingerprintError("source changed") from exc
            if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
                _error("unsafe source entry")
        for name in file_names:
            source = current_path / name
            relative = source.relative_to(vault_root).as_posix()
            normalized = _normalized(relative)
            if normalized != relative:
                _error("unsafe path")
            classification = _classification(normalized)
            if classification is None:
                try:
                    information = source.lstat()
                except OSError as exc:
                    raise FingerprintError("source changed") from exc
                if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
                    _error("unsafe source entry")
                continue
            size, digest = _read_regular(source)
            total_bytes += size
            snapshots.append(_Snapshot(normalized, size, digest, classification))
            if len(snapshots) > _MAX_FILES or total_bytes > _MAX_TOTAL_BYTES:
                _error("resource limit exceeded")
    snapshots.sort(key=lambda item: item.path)
    folded: set[str] = set()
    directories: dict[str, str] = {}
    for snapshot in snapshots:
        key = unicodedata.normalize("NFC", snapshot.path).casefold()
        if key in folded:
            _error("case collision")
        folded.add(key)
        parts = PurePosixPath(snapshot.path).parts
        for index in range(1, len(parts)):
            spelling = "/".join(parts[:index])
            directory_key = unicodedata.normalize("NFC", spelling).casefold()
            if directories.setdefault(directory_key, spelling) != spelling:
                _error("case collision")
    return snapshots


def _records(vault_root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": item.path,
            "size": item.size,
            "sha256": item.sha256,
            "classification": item.classification,
        }
        for item in _enumerate(vault_root)
    ]


def canonical_vault_fingerprint(vault_root: Path) -> str:
    """Hash canonical/portable files twice and fail if the quiesced vault changes."""

    first = _records(vault_root)
    if _records(vault_root) != first:
        _error("source changed")
    payload = {
        "artifact": "exomem-hosted-canonical-vault",
        "schema_version": 1,
        "classification_version": CLASSIFICATION_VERSION,
        "files": first,
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def run(*, vault_root: Path, output_path: Path) -> int:
    """Write one bounded termination record without exposing vault metadata."""

    try:
        digest = canonical_vault_fingerprint(vault_root)
    except (OSError, FingerprintError):
        try:
            _write(
                output_path,
                {
                    "artifact": "exomem-hosted-vault-fingerprint",
                    "schemaVersion": 1,
                    "error": "vault-fingerprint-failed",
                },
            )
        except OSError:
            pass
        return 1
    try:
        _write(
            output_path,
            {
                "artifact": "exomem-hosted-vault-fingerprint",
                "schemaVersion": 1,
                "sha256": digest,
            },
        )
    except OSError:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the fixed no-argument fingerprint command."""

    if list(sys.argv[1:] if argv is None else argv):
        return 2
    return run(vault_root=VAULT_ROOT, output_path=TERMINATION_LOG)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
