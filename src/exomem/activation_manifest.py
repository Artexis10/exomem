"""Portable, immutable boundary for semantic-contract activation.

The manifest records which compiled pages existed when a vault first acquired
the semantic contract.  It is governed vault state: create-once, portable, and
independent from rebuildable indexes and review decisions.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from . import access, activation
from . import find as find_module
from .kbdir import kb_dirname
from .memory_refs import ID_FIELD, normalize_id
from .vault import (
    PlannedWrite,
    VaultLockError,
    VaultLockTimeout,
    batch_atomic_write,
    content_hash,
    parse_frontmatter,
    vault_creation_lock,
)

SCHEMA_VERSION = 1
CONTRACT_VERSION = 1
_MANIFEST_NAME = "semantic-activation.yaml"
_PAGE_KEYS = frozenset(
    {"identity_kind", "identity", "path_at_activation", "source_hash"}
)
_ROOT_KEYS = frozenset({"schema_version", "contract_version", "pages"})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()


@dataclass
class ActivationManifestError(ValueError):
    code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class ActivationPage:
    identity_kind: str
    identity: str
    path_at_activation: str
    source_hash: str


@dataclass(frozen=True)
class ActivationManifest:
    schema_version: int
    contract_version: int
    pages: tuple[ActivationPage, ...]


@dataclass(frozen=True, slots=True)
class ActivationBoundaryPlan:
    """Pure activation boundary selected for one prospective evaluation."""

    manifest: ActivationManifest
    install_required: bool


@dataclass(frozen=True, slots=True)
class ActivationCandidate:
    rel_path: str
    source_hash: str
    normalized_id: str | None


@dataclass(frozen=True, slots=True)
class ActivationCensus:
    """Immutable eligible-page snapshot reusable by activation consumers."""

    candidates: tuple[ActivationCandidate, ...]
    by_path: Mapping[str, ActivationCandidate] = field(init=False, repr=False)
    paths_by_id: Mapping[str, tuple[str, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        raw_candidates = tuple(self.candidates)
        for index, candidate in enumerate(raw_candidates):
            _validate_candidate(candidate, index=index)
        ordered = tuple(sorted(raw_candidates, key=lambda item: item.rel_path))
        by_path: dict[str, ActivationCandidate] = {}
        id_paths: dict[str, list[str]] = {}
        for candidate in ordered:
            if candidate.rel_path in by_path:
                raise ActivationManifestError(
                    "ACTIVATION_CENSUS_DUPLICATE_PATH",
                    f"duplicate activation census path: {candidate.rel_path}",
                )
            by_path[candidate.rel_path] = candidate
            if candidate.normalized_id is not None:
                id_paths.setdefault(candidate.normalized_id, []).append(
                    candidate.rel_path
                )
        object.__setattr__(self, "candidates", ordered)
        object.__setattr__(self, "by_path", MappingProxyType(by_path))
        object.__setattr__(
            self,
            "paths_by_id",
            MappingProxyType(
                {key: tuple(paths) for key, paths in sorted(id_paths.items())}
            ),
        )

    @classmethod
    def from_candidates(
        cls, candidates: Iterable[ActivationCandidate]
    ) -> ActivationCensus:
        return cls(tuple(candidates))

    def unique_path_for_id(self, normalized_id: str) -> str | None:
        paths = self.paths_by_id.get(normalized_id, ())
        return paths[0] if len(paths) == 1 else None


def _validate_candidate(candidate: ActivationCandidate, *, index: int) -> None:
    if not isinstance(candidate, ActivationCandidate):
        raise ActivationManifestError(
            "ACTIVATION_CENSUS_INVALID",
            f"activation census candidate {index} has an unsupported type",
        )
    try:
        _validate_rel_path(
            candidate.rel_path,
            path=Path("<activation-census>"),
            index=index,
        )
    except ActivationManifestError as error:
        raise ActivationManifestError(
            "ACTIVATION_CENSUS_INVALID",
            f"invalid activation census candidate {index}: {error.reason}",
        ) from error
    if not isinstance(candidate.source_hash, str) or not _HASH_RE.fullmatch(
        candidate.source_hash
    ):
        raise ActivationManifestError(
            "ACTIVATION_CENSUS_INVALID",
            f"activation census candidate {index} source_hash must be a lowercase full SHA-256",
        )
    if candidate.normalized_id is not None and (
        not isinstance(candidate.normalized_id, str)
        or normalize_id(candidate.normalized_id) != candidate.normalized_id
    ):
        raise ActivationManifestError(
            "ACTIVATION_CENSUS_INVALID",
            f"activation census candidate {index} normalized_id must be a normalized UUID",
        )


def manifest_path(vault_root: Path) -> Path:
    """Return the governed activation-manifest path for ``vault_root``."""
    return Path(vault_root) / kb_dirname() / "_Schema" / _MANIFEST_NAME


def load_manifest(vault_root: Path) -> ActivationManifest | None:
    """Load and validate an existing manifest without changing its bytes."""
    path = manifest_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ActivationManifestError(
            "ACTIVATION_MANIFEST_UNREADABLE",
            f"could not read {path}: {error}",
        ) from error
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ActivationManifestError(
            "ACTIVATION_MANIFEST_INVALID_YAML",
            f"{path} is not valid YAML: {error}",
        ) from error
    return _validate_manifest(value, path=path)


def ensure_manifest(
    vault_root: Path, *, census: ActivationCensus | None = None
) -> ActivationManifest:
    """Return the existing manifest or atomically establish the boundary once."""
    vault_root = Path(vault_root)
    existing = load_manifest(vault_root)
    if existing is not None:
        return existing

    candidate = (
        _snapshot(vault_root)
        if census is None
        else _snapshot(vault_root, census=census)
    )
    path = manifest_path(vault_root)
    with _creation_lock(path):
        winner = load_manifest(vault_root)
        if winner is not None:
            return winner
        path.parent.mkdir(parents=True, exist_ok=True)
        batch_atomic_write(
            [PlannedWrite(path=path, content=_serialize(candidate))],
            vault_root=vault_root,
        )
        written = load_manifest(vault_root)
        if written is None:  # pragma: no cover - atomic writer guarantees destination
            raise ActivationManifestError(
                "ACTIVATION_MANIFEST_WRITE_FAILED",
                f"activation manifest was not present after writing {path}",
            )
        return written


def is_grandfathered(
    vault_root: Path,
    path: Path | str,
    *,
    source_hash: str | None = None,
    exomem_id: object = _UNSET,
    manifest: ActivationManifest | None = None,
    census: ActivationCensus | None = None,
) -> bool:
    """Return whether the current page belongs to the activation baseline."""
    vault_root = Path(vault_root)
    loaded = manifest if manifest is not None else load_manifest(vault_root)
    if loaded is None:
        return False
    rel_path, absolute = _normalize_page_path(vault_root, path)
    if rel_path is None:
        return False
    id_supplied = exomem_id is not _UNSET
    normalized_id = normalize_id(exomem_id) if id_supplied else None
    current_hash = source_hash
    census_candidate = census.by_path.get(rel_path) if census is not None else None
    if census_candidate is not None:
        if current_hash is None:
            current_hash = census_candidate.source_hash
        if not id_supplied:
            normalized_id = census_candidate.normalized_id
    if current_hash is None or (not id_supplied and census_candidate is None):
        try:
            raw = absolute.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if current_hash is None:
            current_hash = content_hash(raw)
        if not id_supplied:
            frontmatter, _, _ = parse_frontmatter(raw)
            normalized_id = normalize_id(frontmatter.get(ID_FIELD))

    if normalized_id is not None and any(
        page.identity_kind == "exomem_id" and page.identity == normalized_id
        for page in loaded.pages
    ):
        try:
            observed = census if census is not None else build_census(vault_root)
        except ActivationManifestError:
            return False
        return observed.unique_path_for_id(normalized_id) == rel_path
    return bool(
        current_hash
        and any(
            page.identity_kind == "path_source_hash"
            and page.identity == rel_path
            and page.path_at_activation == rel_path
            and page.source_hash == current_hash
            for page in loaded.pages
        )
    )


def build_census(vault_root: Path) -> ActivationCensus:
    """Walk and read the eligible compiled corpus exactly once."""
    return ActivationCensus.from_candidates(_eligible_candidates(Path(vault_root)))


def snapshot_from_census(census: ActivationCensus) -> ActivationManifest:
    """Build the immutable activation snapshot without filesystem access."""
    if not isinstance(census, ActivationCensus):
        raise ActivationManifestError(
            "ACTIVATION_CENSUS_INVALID",
            "activation snapshot requires an immutable census",
        )
    candidates = census.candidates
    counts = Counter(
        candidate.normalized_id
        for candidate in candidates
        if candidate.normalized_id is not None
    )
    pages = []
    for candidate in candidates:
        stable = (
            candidate.normalized_id is not None
            and counts[candidate.normalized_id] == 1
        )
        pages.append(
            ActivationPage(
                identity_kind="exomem_id" if stable else "path_source_hash",
                identity=(
                    candidate.normalized_id if stable else candidate.rel_path
                ),
                path_at_activation=candidate.rel_path,
                source_hash=candidate.source_hash,
            )
        )
    return ActivationManifest(
        schema_version=SCHEMA_VERSION,
        contract_version=CONTRACT_VERSION,
        pages=tuple(pages),
    )


def plan_activation_boundary(
    census: ActivationCensus,
    *,
    manifest: ActivationManifest | None,
) -> ActivationBoundaryPlan:
    """Select an observed or prospective boundary without installing it."""
    if manifest is not None:
        return ActivationBoundaryPlan(manifest, False)
    return ActivationBoundaryPlan(snapshot_from_census(census), True)


def _snapshot(
    vault_root: Path, *, census: ActivationCensus | None = None
) -> ActivationManifest:
    observed = census if census is not None else build_census(vault_root)
    return snapshot_from_census(observed)


def _eligible_candidates(vault_root: Path) -> list[ActivationCandidate]:
    kb = vault_root / kb_dirname()
    candidates: list[ActivationCandidate] = []
    if kb.is_dir():
        paths = sorted(
            find_module._walk_md(kb),
            key=lambda item: item.relative_to(vault_root).as_posix(),
        )
        for path in paths:
            rel_path = path.relative_to(vault_root).as_posix()
            if access.access_tier(vault_root, rel_path) != access.TIER_READ_WRITE:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ActivationManifestError(
                    "ACTIVATION_MANIFEST_PAGE_UNREADABLE",
                    f"could not read activation candidate {path}: {error}",
                ) from error
            frontmatter, body, _ = parse_frontmatter(raw)
            page = find_module.ParsedPage(
                path=path,
                rel_path=rel_path,
                frontmatter=frontmatter,
                body=body,
                title="",
                mtime=0.0,
            )
            if not activation.is_eligible_compiled_page(vault_root, page):
                continue
            candidates.append(
                ActivationCandidate(
                    rel_path=rel_path,
                    source_hash=content_hash(raw),
                    normalized_id=normalize_id(frontmatter.get(ID_FIELD)),
                )
            )
    return candidates


def _serialize(manifest: ActivationManifest) -> str:
    value = {
        "schema_version": manifest.schema_version,
        "contract_version": manifest.contract_version,
        "pages": [
            {
                "identity_kind": page.identity_kind,
                "identity": page.identity,
                "path_at_activation": page.path_at_activation,
                "source_hash": page.source_hash,
            }
            for page in manifest.pages
        ],
    }
    return yaml.safe_dump(
        value,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _validate_manifest(value: Any, *, path: Path) -> ActivationManifest:
    if not isinstance(value, dict):
        _invalid(path, "root must be a mapping")
    if set(value) != _ROOT_KEYS:
        _invalid(path, "root must contain exactly schema_version, contract_version, and pages")
    schema_version = value["schema_version"]
    contract_version = value["contract_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        _invalid(path, "schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise ActivationManifestError(
            "ACTIVATION_MANIFEST_UNSUPPORTED_SCHEMA",
            f"unsupported activation manifest schema_version: {schema_version!r}",
        )
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        _invalid(path, "contract_version must be an integer")
    if contract_version != CONTRACT_VERSION:
        raise ActivationManifestError(
            "ACTIVATION_MANIFEST_UNSUPPORTED_CONTRACT",
            f"unsupported semantic contract_version: {contract_version!r}",
        )
    raw_pages = value["pages"]
    if not isinstance(raw_pages, list):
        _invalid(path, "pages must be a list")

    pages: list[ActivationPage] = []
    seen_paths: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    previous_path: str | None = None
    for index, raw_page in enumerate(raw_pages):
        if not isinstance(raw_page, dict) or set(raw_page) != _PAGE_KEYS:
            _invalid(path, f"pages[{index}] must contain exactly the four page fields")
        if not all(isinstance(raw_page[key], str) for key in _PAGE_KEYS):
            _invalid(path, f"pages[{index}] fields must be strings")
        kind = raw_page["identity_kind"]
        identity = raw_page["identity"]
        rel_path = raw_page["path_at_activation"]
        source_hash = raw_page["source_hash"]
        _validate_rel_path(rel_path, path=path, index=index)
        if kind == "exomem_id":
            normalized = normalize_id(identity)
            if normalized is None or normalized != identity:
                _invalid(path, f"pages[{index}].identity must be a normalized UUID")
        elif kind == "path_source_hash":
            if identity != rel_path:
                _invalid(path, f"pages[{index}] path identity must equal path_at_activation")
        else:
            _invalid(path, f"pages[{index}].identity_kind is unsupported")
        if not _HASH_RE.fullmatch(source_hash):
            _invalid(path, f"pages[{index}].source_hash must be a lowercase full SHA-256")
        if rel_path in seen_paths:
            _invalid(path, f"duplicate activation path: {rel_path}")
        identity_key = (kind, identity)
        if identity_key in seen_identities:
            _invalid(path, f"duplicate activation identity: {kind}:{identity}")
        if previous_path is not None and rel_path < previous_path:
            _invalid(path, "pages must be sorted by path_at_activation")
        seen_paths.add(rel_path)
        seen_identities.add(identity_key)
        previous_path = rel_path
        pages.append(ActivationPage(kind, identity, rel_path, source_hash))
    return ActivationManifest(schema_version, contract_version, tuple(pages))


def _validate_rel_path(value: str, *, path: Path, index: int) -> None:
    posix = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or posix.as_posix() != value
        or posix.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or not posix.parts
        or posix.parts[0] != kb_dirname()
        or posix.suffix.lower() != ".md"
    ):
        _invalid(path, f"pages[{index}].path_at_activation must be a safe KB Markdown path")


def _invalid(path: Path, reason: str) -> None:
    raise ActivationManifestError(
        "ACTIVATION_MANIFEST_INVALID",
        f"invalid activation manifest {path}: {reason}",
    )


def _normalize_page_path(vault_root: Path, path: Path | str) -> tuple[str | None, Path]:
    candidate = Path(path)
    absolute = candidate if candidate.is_absolute() else vault_root / candidate
    try:
        rel_path = absolute.resolve().relative_to(vault_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None, absolute
    return rel_path, absolute


@contextmanager
def _creation_lock(path: Path) -> Iterator[None]:
    try:
        with vault_creation_lock(path.parents[2], "activation-manifest"):
            yield
    except VaultLockTimeout as error:
        raise ActivationManifestError(
            "ACTIVATION_MANIFEST_LOCK_TIMEOUT",
            "timed out acquiring the activation-manifest creation lock",
        ) from error
    except VaultLockError as error:
        raise ActivationManifestError(
            error.code,
            "could not safely acquire the activation-manifest creation lock",
        ) from error
