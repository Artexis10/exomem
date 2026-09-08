"""Resolve the durable external placement of vocabulary authority artifacts."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

ENV_VOCABULARY_AUTHORITY_DIR = "EXOMEM_VOCABULARY_AUTHORITY_DIR"
MARKER_SUFFIX = ".vocabulary-authority.activation.json"
DATABASE_SUFFIX = ".vocabulary-authority.sqlite"
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class VocabularyAuthorityPlacementUnavailable(RuntimeError):
    """The configured authority directory is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class AuthorityArtifactPaths:
    marker_path: Path
    database_path: Path

    def __iter__(self):
        yield self.marker_path
        yield self.database_path


def _paths(parent: Path, logical_vault_id: str) -> AuthorityArtifactPaths:
    if not isinstance(logical_vault_id, str) or not logical_vault_id:
        raise VocabularyAuthorityPlacementUnavailable
    token = hashlib.sha256(logical_vault_id.encode("utf-8")).hexdigest()
    return AuthorityArtifactPaths(
        parent / f"{token}{MARKER_SUFFIX}",
        parent / f"{token}{DATABASE_SUFFIX}",
    )


def artifact_family(paths: AuthorityArtifactPaths | tuple[Path, Path]) -> tuple[Path, ...]:
    """Return the marker, database and every SQLite sidecar identity."""

    marker, database = paths
    return (
        marker,
        database,
        *(
            database.with_name(f"{database.name}{suffix}")
            for suffix in SQLITE_SIDECAR_SUFFIXES
        ),
    )


def contains_authority_artifacts(directory: Path) -> bool:
    """Return whether a directory retains any canonical authority artifact."""

    suffixes = (
        MARKER_SUFFIX,
        DATABASE_SUFFIX,
        *(f"{DATABASE_SUFFIX}{suffix}" for suffix in SQLITE_SIDECAR_SUFFIXES),
    )
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                for suffix in suffixes:
                    stem = entry.name.removesuffix(suffix)
                    if (
                        stem != entry.name
                        and len(stem) == 64
                        and all(character in "0123456789abcdef" for character in stem)
                    ):
                        return True
        return False
    except OSError:
        raise VocabularyAuthorityPlacementUnavailable from None


def _validated_directory(raw: str, vault_root: Path | None) -> Path:
    if not raw or "\x00" in raw:
        raise VocabularyAuthorityPlacementUnavailable
    supplied = Path(raw)
    if not supplied.is_absolute():
        raise VocabularyAuthorityPlacementUnavailable
    candidate = Path(os.path.abspath(supplied))
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise VocabularyAuthorityPlacementUnavailable from None
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
        raise VocabularyAuthorityPlacementUnavailable

    from .governance import authorization_custody

    if not authorization_custody._private_parent_is_safe(candidate):  # noqa: SLF001
        raise VocabularyAuthorityPlacementUnavailable
    if vault_root is not None:
        from . import state_paths

        vault = Path(vault_root).resolve(strict=False)
        state = state_paths.vault_state_dir(vault).resolve(strict=False)
        if authorization_custody._path_is_within(candidate, vault) or (  # noqa: SLF001
            authorization_custody._path_is_within(candidate, state)  # noqa: SLF001
        ):
            raise VocabularyAuthorityPlacementUnavailable
    return candidate


def validated_authority_directory(vault_root: Path) -> Path:
    """Return the explicitly configured, existing private authority directory."""

    raw = os.environ.get(ENV_VOCABULARY_AUTHORITY_DIR)
    if raw is None:
        raise VocabularyAuthorityPlacementUnavailable
    return _validated_directory(raw, Path(vault_root))


def resolve_authority_artifact_paths(
    control_path: Path,
    logical_vault_id: str,
    *,
    vault_root: Path | None = None,
) -> AuthorityArtifactPaths:
    """Resolve one authority family without creating or moving storage."""

    control = Path(control_path)
    legacy = _paths(control.parent, logical_vault_id)
    raw = os.environ.get(ENV_VOCABULARY_AUTHORITY_DIR)
    if raw is None:
        return legacy
    candidate = _validated_directory(raw, vault_root)

    configured = _paths(candidate, logical_vault_id)
    if candidate != legacy.marker_path.parent and any(
        os.path.lexists(path) for path in artifact_family(legacy)
    ):
        raise VocabularyAuthorityPlacementUnavailable
    return configured
