"""Governance policy loader — strict YAML under `_Governance/`, fingerprinted.

Mirrors `access._load_config`/`access.policy_fingerprint` (`access.py:59-135`):
a missing (or file-less) policy directory yields the cached `EMPTY_POLICY`
singleton with a stable "missing" fingerprint; a present one is gated by a
cheap per-file stat signature, and a content hash — computed only when that
signature moves — is the stable identity handed to callers and used as the
membership memo key (`membership.py`). A synchronisation conflict copy anywhere
under `_Governance/` (Obsidian `(conflicted copy …)` or Syncthing
`.sync-conflict-…`) refuses the compile: the last good policy stays in
effect and the refusal is surfaced as a finding, never silently merged.

Schema v1 is strict and deliberately small (see the change's design doc,
§Risks — "selector expressiveness creep"): one YAML document per file,
`governance_version: 1`, an immutable ULID `id`. Unknown top-level FIELDS on
a recognized document are a compile error (fail-closed, the whole compile
refuses); unrecognized FILES under `_Governance/` are a warning and are
ignored (forward-compat — a newer kernel's file kinds don't break an older
one).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import held_fs, memory_refs
from ..kbdir import kb_dirname
from . import store as store_module

GOVERNANCE_DIRNAME = "_Governance"


def is_governance_path(rel_path: str) -> bool:
    """True when `rel_path` IS the policy tree or lives inside it.

    The policy tree is never vault content, for any audience including the
    owner — the answer never depends on who is asking, so this is a structural
    exclusion rather than a release decision. Callers use it to prune the tree
    from a walk AND to refuse a scan whose root points at it: pruning alone
    only ever removes it as a CHILD, and a directory is never a child of
    itself, so a scoped probe straight at `_Governance` walked it happily.
    """
    clean = str(rel_path or "").replace("\\", "/").strip("/")
    if not clean:
        return False
    folded = GOVERNANCE_DIRNAME.casefold()
    return any(part.casefold() == folded for part in clean.split("/"))
GOVERNANCE_VERSION = 1
DISCLOSURE_MIN = 0
DISCLOSURE_MAX = 6  # L0 (nothing) .. L6 (full disclosure)

# Crockford base32 (no I L O U), 26 chars — the ULID alphabet + length, not a
# full ULID timestamp/randomness validity check (format only; ids are
# user-authored in the vault's YAML, never minted by this read-only kernel).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def is_valid_document_id(value: object) -> bool:
    """Return whether ``value`` is the canonical policy document-id grammar."""
    return isinstance(value, str) and _ULID_RE.fullmatch(value) is not None


# Conflict-copy filenames, one substring per synchronisation tool the vault may
# be replicated with. Substrings only, because each tool appends its own
# timestamp/id: Obsidian Sync writes "file (conflicted copy 2024-01-01).md" and
# Syncthing writes "file.sync-conflict-20240101-120000-ABCDEFG.md". Always
# compared against a lower-cased filename (no tool guarantees case, and a stray
# capital-C sibling must still be caught as a conflict, not mistaken for a
# second, differently-named policy document).
#
# Every other walker in this codebase already filters the hyphenated form
# (`find_corpus`, `vault`, `lexstore`, `freshness`, `media_worker`); this is the
# one place where missing it is a disclosure bug rather than a stale read.
_CONFLICT_MARKERS = ("conflicted copy", ".sync-conflict-")
_DOCUMENT_KINDS = ("scopes", "rules", "grants")
_DOCUMENT_KIND_ORDER = {
    kind: index for index, kind in enumerate(_DOCUMENT_KINDS)
}


def is_conflict_copy(name: str) -> bool:
    """True when `name` is a synchronisation conflict copy, for any known tool.

    Shared by policy-document discovery, the governance file walk, and the
    receipt tree so a conflict cannot be a policy document in one walk and an
    ordinary file in another.
    """
    folded = str(name).lower()
    return any(marker in folded for marker in _CONFLICT_MARKERS)


# Distinct from `EMPTY_POLICY`'s "missing" sentinel on purpose: a refused
# compile with NO prior good compile to fall back on (a cold start) is a
# fail-closed floor, not "no governance in effect". See `_blocked`.
BLOCKED_FINGERPRINT = "blocked"

_SCOPE_SELECTOR_FIELDS = ("paths", "projects", "tags", "types", "classes", "refs")
_SCOPE_ALLOWED_FIELDS = frozenset(
    {
        "governance_version",
        "id",
        "name",
        "exclude",
        "constraint",
        "default_deny",
        *_SCOPE_SELECTOR_FIELDS,
    }
)
_SCOPE_EXCLUDE_ALLOWED_FIELDS = frozenset(_SCOPE_SELECTOR_FIELDS)
_RULE_ALLOWED_FIELDS = frozenset(
    {
        "governance_version",
        "id",
        "scope_ids",
        "audience",
        "purpose",
        "purpose_condition",
        "kind",
        "ceiling",
        "options",
    }
)
_STANDING_GRANT_ALLOWED_FIELDS = frozenset(
    {"governance_version", "id", "kind", "scope_ids", "audience", "ceiling"}
)
_RELEASE_GRANT_ALLOWED_FIELDS = frozenset(
    {
        "governance_version",
        "id",
        "kind",
        "path",
        "ref",
        "content_hash",
        "to_audience",
        "released_at",
        "why",
        "bridge_scope",
        "bridge_of",
        "options",
    }
)
_RELEASE_DEPENDENCY_FIELDS = frozenset(
    {"ref", "path", "content_hash", "restriction_signature"}
)
_RELEASE_OPTIONS_FIELDS = frozenset({"strip_provenance"})
_RULE_OPTION_STRING_FIELDS = frozenset({"notice", "constraint", "abstract", "bridge"})
_RULE_OPTION_CONTROL_FIELDS = frozenset({"suspended"})
_RULE_OPTION_FIELDS = _RULE_OPTION_STRING_FIELDS | _RULE_OPTION_CONTROL_FIELDS
_PURPOSE_CONDITIONS = frozenset({"matches", "outside"})
_RULE_KINDS = frozenset({"standing", "org_cap"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONSTRAINT_MAX_CHARS = 500


def _mapping_key_sort_key(value: Any) -> tuple[str, str, str]:
    value_type = type(value)
    return value_type.__module__, value_type.__qualname__, repr(value)


def _check_mapping_keys(
    mapping: dict[Any, Any],
    allowed: frozenset[str],
    location: str,
    findings: list[dict[str, str]],
    *,
    separator: str,
    noun: str,
) -> None:
    """Validate a strict YAML mapping without comparing heterogeneous keys."""
    non_string = sorted(
        (key for key in mapping if not isinstance(key, str)),
        key=_mapping_key_sort_key,
    )
    for key in non_string:
        findings.append(
            _finding(
                "invalid_field",
                location,
                f"{noun} keys must be strings; got {type(key).__name__}",
            )
        )
    unknown = sorted(
        key for key in mapping if isinstance(key, str) and key not in allowed
    )
    for key in sorted(unknown):
        findings.append(
            _finding(
                "unknown_field",
                f"{location}{separator}{key}",
                f"unknown {noun} {key!r}",
            )
        )


def _check_option_keys(
    options: dict[Any, Any],
    allowed: frozenset[str],
    rel: str,
    findings: list[dict[str, str]],
) -> None:
    """Reject YAML's non-string and unregistered option keys."""
    _check_mapping_keys(
        options,
        allowed,
        f"{rel}:options",
        findings,
        separator=".",
        noun="option",
    )
    if "credential_scrubber" in options:
        findings.append(
            _finding(
                "owner_migration_required",
                f"{rel}:options.credential_scrubber",
                "remove credential_scrubber through a reviewed owner migration",
            )
        )


@dataclass(frozen=True)
class Scope:
    """A named membership selector: any positive selector kind, minus excludes."""

    id: str
    source: str
    name: str | None = None
    constraint: str | None = None
    #: When true, an audience that no standing rule names receives NOTHING for
    #: an item in this scope, instead of full release. It inverts one default;
    #: it is not a rule and never lowers an authored ceiling (see
    #: `decisions._decide_at`). The owner is never subject to it.
    default_deny: bool = False
    paths: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    exclude_projects: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    exclude_types: tuple[str, ...] = ()
    exclude_classes: tuple[str, ...] = ()
    exclude_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    """A standing rule or org cap: audience + optional purpose + a ceiling."""

    id: str
    source: str
    scope_ids: tuple[str, ...]
    audience: str
    ceiling: int
    purpose: str | None = None
    purpose_condition: str = "matches"  # "matches" (allow) | "outside" (restrict)
    kind: str = "standing"  # "standing" | "org_cap"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StandingGrant:
    """A standing exception that can only ever raise a ceiling, never lower one."""

    id: str
    source: str
    scope_ids: tuple[str, ...]
    audience: str
    ceiling: int


@dataclass(frozen=True)
class ReleaseDependency:
    """One exact source snapshot covered by a bridge release approval."""

    ref: str
    path: str
    content_hash: str
    restriction_signature: str


@dataclass(frozen=True)
class ReleaseGrant:
    """Exact-item approval gate; never participates in the scope lattice."""

    id: str
    source: str
    path: str
    ref: str
    content_hash: str
    to_audience: str
    released_at: str
    why: str
    bridge_scope: str
    bridge_of: tuple[ReleaseDependency, ...]
    strip_provenance: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    fingerprint: str
    scopes: dict[str, Scope] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    grants: tuple[StandingGrant, ...] = ()
    release_grants: tuple[ReleaseGrant, ...] = ()
    findings: tuple[dict[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        """True for the no-`_Governance/` (or file-less) singleton — the fast path."""
        return self.fingerprint == "missing"

    @property
    def blocked(self) -> bool:
        """True for the cold-start fail-closed floor (see `_blocked`): a
        refused compile (conflict or compile-error findings) with no prior
        good policy for this vault to fall back on.

        Never conflated with `.empty`: `EMPTY_POLICY` means "no governance
        configured, fully open"; `.blocked` means "governance IS configured,
        but the current state can't be trusted, so refuse rather than open."
        Every caller that consults `scopes`/`rules`/`grants` must check
        `pol.blocked` immediately after `pol.empty`, the same way it already
        checks `pol.empty` first (see `governance.decide_paths`).
        """
        return self.fingerprint == BLOCKED_FINGERPRINT

    @property
    def conflicted(self) -> bool:
        """True when a synchronisation conflict copy is present under `_Governance/`.

        Deliberately NOT folded into `.blocked`. A conflict on a warm vault must
        keep SERVING the last good policy — flooring every read to L0 because a
        sync tool dropped a sibling file would be worse than the defect. What it
        must not do is let policy be AUTHORED, because the author cannot see
        which of the two documents will win. The authoring gate and guarded
        prospective snapshot both refuse every supported conflict-copy shape.

        Before conflict detection was widened, a conflict alongside its original
        produced a `duplicate_id` error that refused the mutation by accident.
        Filtering the copy at discovery removed that refusal, so this restores
        it deliberately and for the right reason.
        """
        return any(f.get("code") == "conflicted_copy" for f in self.findings)


@dataclass(frozen=True)
class AuthoringFileIdentity:
    """One no-follow regular-file identity captured from the workspace."""

    path: str
    identity: held_fs.StableIdentity
    sha256: str


@dataclass(frozen=True)
class AuthoringSnapshot:
    """Stable source state that a prospective policy was compiled from."""

    documents: tuple[tuple[str, bytes], ...]
    source_fingerprint: str
    conflict_set_digest: str
    guard_generation: str
    file_identities: tuple[AuthoringFileIdentity, ...]
    governance_root_identity: held_fs.StableIdentity | None


@dataclass(frozen=True)
class ProspectiveCompile:
    """A compiled target bound to the exact live authoring snapshot."""

    snapshot: AuthoringSnapshot
    target_documents: tuple[tuple[str, bytes], ...]
    policy: Policy


@dataclass(frozen=True)
class _AuthoringTreeProbe:
    root_identity: held_fs.StableIdentity | None
    documents: tuple[tuple[str, bytes], ...]
    file_identities: tuple[AuthoringFileIdentity, ...]
    directory_identities: tuple[tuple[str, held_fs.StableIdentity], ...]
    conflict_paths: tuple[str, ...]


EMPTY_POLICY = Policy(fingerprint="missing")


def _blocked(findings: tuple[dict[str, str], ...]) -> Policy:
    """Build the cold-start fail-closed floor: a refused compile with no
    prior good policy to serve instead. `scopes`/`rules`/`grants` stay empty
    by construction — callers must branch on `.blocked` before consulting
    them, not rely on their emptiness meaning "nothing to enforce"."""
    return Policy(fingerprint=BLOCKED_FINGERPRINT, findings=findings)

_Signature = tuple[tuple[str, int, int, int, int, int], ...]
# Keyed by governance-root path, one entry per distinct vault this process has
# loaded — unbounded by design, matching `access.py`'s `_CACHE` convention:
# it grows with the number of vaults a process touches, not with file count
# or call volume, so an LRU would trade real correctness for imaginary memory
# pressure. Not a defect; revisit only if that convention itself changes.
_CACHE: dict[str, tuple[_Signature, Policy]] = {}
_LAST_GOOD: dict[str, Policy] = {}


def governance_root(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / GOVERNANCE_DIRNAME


def _finding(code: str, path: str, detail: str, *, severity: str = "error") -> dict[str, str]:
    return {"code": code, "path": path, "span": path, "severity": severity, "detail": detail}


def _iter_all_files(root: Path):
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and not _is_operational_state(root, p):
            yield p


def has_conflict_copy(vault_root: Path) -> bool:
    """Filesystem-only conflict probe for the authoring gate.

    Deliberately does NOT go through `load()`: `op_govern_memory` guarantees a
    rejected operation creates no sidecar, policy directory, receipt or marker,
    and `load()` opens the governance sidecar through the guard probe. Reading
    the directory listing keeps the gate free of that side effect.
    """
    return any(is_conflict_copy(p.name) for p in _iter_all_files(governance_root(Path(vault_root))))


def _is_operational_state(root: Path, path: Path) -> bool:
    """Receipt evidence is governed state, never a policy input or warning."""
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return _is_operational_relative(relative)


def _is_operational_relative(relative: str) -> bool:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    return bool(parts) and (
        parts[0] in {"events", "deletion-tombstones", "archives"}
        or parts == (".policy-mutation.pending.json",)
    )


def _document_kind(relative: str) -> str | None:
    parts = relative.replace("\\", "/").split("/")
    if (
        len(parts) == 2
        and parts[0] in _DOCUMENT_KIND_ORDER
        and parts[1].endswith(".yaml")
        and not is_conflict_copy(parts[1])
    ):
        return parts[0]
    return None


def _document_fingerprint(documents: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    recognized = [
        (kind, relative, raw)
        for relative, raw in documents.items()
        if (kind := _document_kind(relative)) is not None
    ]
    for _kind, relative, raw in sorted(
        recognized, key=lambda item: (_DOCUMENT_KIND_ORDER[item[0]], item[1])
    ):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _path_set_digest(domain: bytes, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256(domain + b"\0")
    for path in paths:
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _clear_guard_generation(
    vault_root: Path,
    *,
    expected_pending_event_id: str | None = None,
) -> str | None:
    probe = store_module.guard_generation_probe(vault_root)
    marker_generation, marker_present, marker_event_id = _marker_probe(vault_root)
    event_ids = tuple(str(value) for value in probe.get("event_ids", ()))
    if expected_pending_event_id is None:
        if probe.get("state") != "clear" or marker_present:
            return None
    else:
        if (
            probe.get("state") != "pending"
            or event_ids != (expected_pending_event_id,)
        ):
            return None
        if marker_present and marker_event_id != expected_pending_event_id:
            return None
    generation = str(probe.get("generation", ""))
    return _path_set_digest(
        b"exomem.governance-authoring-guard.v1",
        (str(probe.get("state")), generation, marker_generation, *event_ids),
    )


def _authoring_snapshot_barrier(_phase: str, _path: str | None = None) -> None:
    """Deterministic test seam around the three live workspace probes."""


def _probe_authoring_tree(vault_root: Path) -> _AuthoringTreeProbe | None:
    base = f"{kb_dirname()}/{GOVERNANCE_DIRNAME}"
    acquired = held_fs.acquire(vault_root)
    if not acquired.ok:
        return None
    try:
        with acquired.require() as filesystem:
            root_result = filesystem.parent(base)
            if not root_result.ok:
                if root_result.error is not None and root_result.error.code == "MISSING":
                    return _AuthoringTreeProbe(None, (), (), (), ())
                return None
            with root_result.require() as root:
                records_result = filesystem.enumerate(root)
                if not records_result.ok:
                    return None
                records = tuple(
                    record
                    for record in records_result.require()
                    if not _is_operational_relative(record.relative_path)
                )
                directories = tuple(
                    (record.relative_path, record.identity)
                    for record in records
                    if record.identity.kind == "directory"
                )
                documents: list[tuple[str, bytes]] = []
                identities: list[AuthoringFileIdentity] = []
                for record in records:
                    if record.identity.kind == "directory":
                        continue
                    if record.identity.kind != "file" or record.identity.link_count != 1:
                        return None
                    relative = record.relative_path
                    path = Path(relative)
                    parent_relative = Path(base, path.parent).as_posix()
                    parent_result = filesystem.parent(parent_relative)
                    if not parent_result.ok:
                        return None
                    with parent_result.require() as parent:
                        file_result = filesystem.file(parent, path.name)
                        if not file_result.ok:
                            return None
                        with file_result.require() as file:
                            if file.identity != record.identity:
                                return None
                            raw_result = filesystem.read(file)
                            if not raw_result.ok:
                                return None
                            raw = raw_result.require()
                        if not filesystem.validate_directory(parent).ok:
                            return None
                    documents.append((relative, raw))
                    identities.append(
                        AuthoringFileIdentity(
                            path=relative,
                            identity=record.identity,
                            sha256=hashlib.sha256(raw).hexdigest(),
                        )
                    )
                if not filesystem.validate_directory(root).ok:
                    return None
                conflicts = tuple(
                    relative for relative, _raw in documents if is_conflict_copy(Path(relative).name)
                )
                return _AuthoringTreeProbe(
                    root.identity,
                    tuple(documents),
                    tuple(identities),
                    directories,
                    conflicts,
                )
    except held_fs.HeldFsError:
        return None


def _iter_policy_files(root: Path) -> list[tuple[str, Path]]:
    """`(kind, path)` for every recognized `*.yaml` under scopes/rules/grants."""
    out: list[tuple[str, Path]] = []
    for kind in _DOCUMENT_KINDS:
        sub = root / kind
        if not sub.is_dir():
            continue
        for p in sorted(sub.glob("*.yaml")):
            if p.is_file() and not is_conflict_copy(p.name):
                out.append((kind, p))
    return out


def _signature(root: Path) -> _Signature:
    entries = []
    for p in _iter_all_files(root):
        try:
            stat = p.stat()
        except OSError:
            continue
        entries.append(
            (
                p.relative_to(root).as_posix(),
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
                stat.st_dev,
                stat.st_ino,
            )
        )
    return tuple(entries)


def _content_fingerprint(root: Path, files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for _kind, path in files:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _load_unguarded(vault_root: Path) -> Policy:
    """Load (or reuse the cached compile of) the vault's governance policy.

    No `_Governance/` directory, and no recognized policy files AND no
    conflicted-copy siblings, is the same case: the cached `EMPTY_POLICY`
    singleton — every downstream caller's first line is
    `if pol.empty: return <no-op>` (design D2).

    A refused compile (a conflicted-copy sibling, or a document-level error
    finding) is handled two ways depending on whether a prior good compile
    already exists for this vault:

    - If one does, it stays in effect — returned as-is with the refusal's
      findings attached ("the last good policy remains in effect", D3).
    - If none exists yet (a cold start: the very first `load()` for this
      vault already sees a conflict or an error), there is no good state to
      fall back on. That is NOT `EMPTY_POLICY` — silently resolving a
      refused cold compile to "no governance, fully open" is exactly the
      fail-open bug this distinction exists to prevent. It is the distinct
      `.blocked` fail-closed floor instead (see `_blocked`).
    """
    root = governance_root(Path(vault_root))
    key = str(root)
    if not root.is_dir():
        _CACHE.pop(key, None)
        return EMPTY_POLICY

    signature = _signature(root)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    files = _iter_policy_files(root)
    conflicts = [p for p in _iter_all_files(root) if is_conflict_copy(p.name)]

    if not files and not conflicts:
        _CACHE.pop(key, None)
        return EMPTY_POLICY

    if conflicts:
        conflict_findings = tuple(
            _finding(
                "conflicted_copy",
                p.relative_to(root).as_posix(),
                "a synchronisation conflict-copy sibling is present; resolve it "
                "before policy changes take effect",
            )
            for p in conflicts
        )
        if cached is not None:
            # Deliberately do not touch `_CACHE`: the last good compile stays
            # exactly as it was for the next non-conflicted load.
            return dataclasses.replace(cached[1], findings=conflict_findings)
        return _blocked(conflict_findings)

    fingerprint = _content_fingerprint(root, files)
    if cached is not None and cached[1].fingerprint == fingerprint:
        # Content unchanged (e.g. a bare touch) — keep the parsed policy, just
        # refresh the cheap signature so the next call short-circuits again.
        _CACHE[key] = (signature, cached[1])
        return cached[1]

    findings, scopes, rules, grants, release_grants = _compile(root, files)
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        if cached is not None:
            return dataclasses.replace(cached[1], findings=tuple(findings))
        return _blocked(tuple(findings))

    compiled = Policy(
        fingerprint=fingerprint,
        scopes=scopes,
        rules=rules,
        grants=grants,
        release_grants=release_grants,
        findings=tuple(findings),
    )
    _CACHE[key] = (signature, compiled)
    return compiled


def _marker_probe(vault_root: Path) -> tuple[str, bool, str | None]:
    marker = governance_root(vault_root) / ".policy-mutation.pending.json"
    try:
        stat_result = marker.lstat()
        if not stat.S_ISREG(stat_result.st_mode):
            return f"invalid:{stat_result.st_mode}", True, None
        raw = marker.read_bytes()
    except FileNotFoundError:
        return "absent", False, None
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}", True, None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw)
        event_id = value.get("event_id") if isinstance(value, dict) else None
    except (UnicodeError, json.JSONDecodeError):
        event_id = None
    return (
        f"{stat_result.st_ino}:{stat_result.st_size}:{digest}",
        True,
        event_id if isinstance(event_id, str) else None,
    )


def _marker_generation(vault_root: Path) -> tuple[str, bool]:
    generation, present, _event_id = _marker_probe(vault_root)
    return generation, present


def _guarded_policy(vault_root: Path, code: str, detail: str) -> Policy:
    key = str(governance_root(vault_root))
    finding = _finding(code, ".policy-mutation.pending.json", detail)
    last_good = _LAST_GOOD.get(key)
    if last_good is None:
        return _blocked((finding,))
    return dataclasses.replace(last_good, findings=(*last_good.findings, finding))


def load(vault_root: Path) -> Policy:
    """Load policy behind a non-creating seqlock-style authoring guard."""
    vault_root = Path(vault_root)
    key = str(governance_root(vault_root))
    for _attempt in range(3):
        before = store_module.guard_generation_probe(vault_root)
        marker_before, marker_present = _marker_generation(vault_root)
        if before["state"] == "blocked":
            return _blocked(
                (
                    _finding(
                        "governance_sidecar_blocked",
                        ".policy-mutation.pending.json",
                        "the governance sidecar is locked, corrupt, structurally unknown, or unsupported",
                    ),
                )
            )
        if before["state"] == "pending":
            return _guarded_policy(
                vault_root,
                "governance_mutation_pending",
                "a receipted governance mutation is pending activation",
            )
        if marker_present:
            return _blocked(
                (
                    _finding(
                        "governance_orphan_marker",
                        ".policy-mutation.pending.json",
                        "a governance marker exists without a pending journal",
                    ),
                )
            )

        loaded = _load_unguarded(vault_root)
        after = store_module.guard_generation_probe(vault_root)
        marker_after, marker_after_present = _marker_generation(vault_root)
        if (
            before["state"] == after["state"] == "clear"
            and before["generation"] == after["generation"]
            and marker_before == marker_after
            and not marker_after_present
        ):
            # Retain only a compile that PRODUCED GOVERNANCE — neither the
            # empty open singleton nor the blocked floor. `_LAST_GOOD` is
            # defined as "the last policy worth falling back to", and an open
            # policy is never worth falling back to: `_guarded_policy` replaces
            # only `findings`, so retaining `EMPTY_POLICY` here would hand back
            # a policy that still fingerprints as "missing" and therefore takes
            # every caller's fully-open fast path while a mutation is pending.
            # With the cache unable to hold one, the existing `last_good is
            # None` branch already reaches `_blocked` correctly.
            if (
                not loaded.empty
                and not loaded.blocked
                and not any(
                    finding.get("severity") == "error" for finding in loaded.findings
                )
            ):
                _LAST_GOOD[key] = loaded
            return loaded
    return _blocked(
        (
            _finding(
                "governance_guard_changed",
                ".policy-mutation.pending.json",
                "governance guard generation changed during policy compilation",
            ),
        )
    )


def _compile(
    root: Path, _files: list[tuple[str, Path]]
) -> tuple[
    list[dict[str, str]],
    dict[str, Scope],
    tuple[Rule, ...],
    tuple[StandingGrant, ...],
    tuple[ReleaseGrant, ...],
]:
    documents: dict[str, bytes] = {}
    findings: list[dict[str, str]] = []
    for path in _iter_all_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            documents[relative] = path.read_bytes()
        except OSError as error:
            findings.append(_finding("read_error", relative, str(error)))
    compiled = _compile_document_bytes(documents)
    return (
        [*findings, *compiled[0]],
        compiled[1],
        compiled[2],
        compiled[3],
        compiled[4],
    )


def _compile_document_bytes(
    documents: Mapping[str, bytes],
) -> tuple[
    list[dict[str, str]],
    dict[str, Scope],
    tuple[Rule, ...],
    tuple[StandingGrant, ...],
    tuple[ReleaseGrant, ...],
]:
    findings: list[dict[str, str]] = []
    scopes: dict[str, Scope] = {}
    rules: list[Rule] = []
    grants: list[StandingGrant] = []
    release_grants: list[ReleaseGrant] = []
    rule_ids: set[str] = set()
    grant_ids: set[str] = set()

    recognized: list[tuple[str, str, bytes]] = []
    for relative, raw in sorted(documents.items()):
        kind = _document_kind(relative)
        if kind is not None:
            recognized.append((kind, relative, raw))
            continue
        if is_conflict_copy(Path(relative).name):
            continue
        findings.append(
            _finding(
                "unknown_file",
                relative,
                "not a recognized governance document; ignored",
                severity="warning",
            )
        )

    recognized.sort(key=lambda item: (_DOCUMENT_KIND_ORDER[item[0]], item[1]))

    for kind, rel, encoded in recognized:
        try:
            raw = encoded.decode("utf-8")
        except UnicodeError as error:
            findings.append(_finding("invalid_yaml", rel, str(error)))
            continue
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            findings.append(_finding("invalid_yaml", rel, str(error)))
            continue
        if not isinstance(data, dict):
            findings.append(_finding("invalid_document", rel, "must be a YAML mapping"))
            continue

        if kind == "scopes":
            scope, doc_findings = _parse_scope(data, rel)
            findings.extend(doc_findings)
            if scope is not None:
                if scope.id in scopes:
                    findings.append(
                        _finding("duplicate_id", rel, f"scope id {scope.id!r} already defined")
                    )
                else:
                    scopes[scope.id] = scope
        elif kind == "rules":
            rule, doc_findings = _parse_rule(data, rel)
            findings.extend(doc_findings)
            if rule is not None:
                if rule.id in rule_ids:
                    findings.append(
                        _finding("duplicate_id", rel, f"rule id {rule.id!r} already defined")
                    )
                else:
                    rule_ids.add(rule.id)
                    rules.append(rule)
        else:
            grant, release_grant, doc_findings = _parse_grant(data, rel)
            findings.extend(doc_findings)
            document = grant if grant is not None else release_grant
            if document is not None:
                if document.id in grant_ids:
                    findings.append(
                        _finding("duplicate_id", rel, f"grant id {document.id!r} already defined")
                    )
                else:
                    grant_ids.add(document.id)
                    if grant is not None:
                        grants.append(grant)
                    else:
                        release_grants.append(release_grant)  # type: ignore[arg-type]

    for document in (*rules, *grants):
        for scope_id in document.scope_ids:
            if scope_id not in scopes:
                findings.append(
                    _finding(
                        "unknown_scope",
                        f"{document.source}:scope_ids",
                        f"scope id {scope_id!r} is not defined",
                    )
                )

    return findings, scopes, tuple(rules), tuple(grants), tuple(release_grants)


def compile_documents(documents: Mapping[str, bytes]) -> Policy:
    """Compile only the supplied immutable workspace bytes."""

    pinned: dict[str, bytes] = {}
    for relative, raw in documents.items():
        if not isinstance(relative, str) or not isinstance(raw, bytes):
            raise TypeError("policy documents must map relative string paths to bytes")
        pinned[relative.replace("\\", "/")] = raw
    findings, scopes, rules, grants, release_grants = _compile_document_bytes(pinned)
    if any(finding["severity"] == "error" for finding in findings):
        return _blocked(tuple(findings))
    return Policy(
        fingerprint=_document_fingerprint(pinned),
        scopes=scopes,
        rules=rules,
        grants=grants,
        release_grants=release_grants,
        findings=tuple(findings),
    )


def compile_prospective(
    vault_root: Path,
    documents: dict[str, str | None],
    *,
    _expected_pending_event_id: str | None = None,
) -> ProspectiveCompile | None:
    """Compile an overlay only after a stable no-follow workspace acquisition."""

    vault_root = Path(vault_root)
    guard_before = _clear_guard_generation(
        vault_root,
        expected_pending_event_id=_expected_pending_event_id,
    )
    if guard_before is None:
        return None
    before = _probe_authoring_tree(vault_root)
    if before is None:
        return None

    _authoring_snapshot_barrier("after_before")
    read = _probe_authoring_tree(vault_root)
    if read is None:
        return None

    _authoring_snapshot_barrier("after_read")
    after = _probe_authoring_tree(vault_root)
    guard_after = _clear_guard_generation(
        vault_root,
        expected_pending_event_id=_expected_pending_event_id,
    )
    if (
        after is None
        or before.conflict_paths
        or read.conflict_paths
        or after.conflict_paths
        or guard_after is None
        or guard_before != guard_after
        or before != read
        or read != after
    ):
        return None

    current_documents = dict(read.documents)
    snapshot = AuthoringSnapshot(
        documents=read.documents,
        source_fingerprint=_document_fingerprint(current_documents),
        conflict_set_digest=_path_set_digest(
            b"exomem.governance-conflict-set.v1", read.conflict_paths
        ),
        guard_generation=guard_before,
        file_identities=read.file_identities,
        governance_root_identity=read.root_identity,
    )
    target_documents = dict(current_documents)
    for relative, content in documents.items():
        normalized = relative.replace("\\", "/")
        if content is None:
            target_documents.pop(normalized, None)
        else:
            target_documents[normalized] = content.encode("utf-8")
    target = tuple(sorted(target_documents.items()))
    return ProspectiveCompile(
        snapshot=snapshot,
        target_documents=target,
        policy=compile_documents(target_documents),
    )


def _check_common(
    data: dict[Any, Any], rel: str, allowed: frozenset[str]
) -> tuple[list[dict[str, str]], str | None]:
    findings: list[dict[str, str]] = []
    _check_mapping_keys(
        data,
        allowed,
        rel,
        findings,
        separator=":",
        noun="field",
    )
    version = data.get("governance_version")
    if version != GOVERNANCE_VERSION:
        findings.append(
            _finding(
                "invalid_version",
                f"{rel}:governance_version",
                f"must be {GOVERNANCE_VERSION}, got {version!r}",
            )
        )
    raw_id = data.get("id")
    doc_id: str | None = None
    if not is_valid_document_id(raw_id):
        findings.append(
            _finding("invalid_id", f"{rel}:id", "id must be a 26-character Crockford-base32 ULID")
        )
    else:
        doc_id = raw_id
    return findings, doc_id


def _as_str_tuple(
    value: Any, rel: str, field_name: str, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        findings.append(
            _finding("invalid_field", f"{rel}:{field_name}", f"{field_name} must be a list of strings")
        )
        return ()
    return tuple(value)


def _valid_constraint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > _CONSTRAINT_MAX_CHARS or "\n" in text or "\r" in text:
        return False
    folded = text.casefold()
    forbidden = ("[[", "]]", "exomem://", "knowledge base/", ".md", "\\")
    return not any(marker in folded for marker in forbidden)


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _valid_release_time(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _reject_reserved_audience(
    value: str | None,
    rel: str,
    field_name: str,
    findings: list[dict[str, str]],
) -> str | None:
    """Keep authored policy out of the process-reserved NUL namespace."""
    if value is not None and "\x00" in value:
        findings.append(
            _finding(
                "invalid_field",
                f"{rel}:{field_name}",
                f"{field_name} must not contain NUL; the NUL prefix is reserved",
            )
        )
        return None
    return value


def _parse_scope(data: dict[str, Any], rel: str) -> tuple[Scope | None, list[dict[str, str]]]:
    findings, doc_id = _check_common(data, rel, _SCOPE_ALLOWED_FIELDS)

    exclude = data.get("exclude") or {}
    if not isinstance(exclude, dict):
        findings.append(_finding("invalid_field", f"{rel}:exclude", "exclude must be a mapping"))
        exclude = {}
    else:
        _check_mapping_keys(
            exclude,
            _SCOPE_EXCLUDE_ALLOWED_FIELDS,
            f"{rel}:exclude",
            findings,
            separator=".",
            noun="exclude field",
        )

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        findings.append(_finding("invalid_field", f"{rel}:name", "name must be a string"))
        name = None

    constraint = data.get("constraint")
    if constraint is not None and not _valid_constraint(constraint):
        findings.append(
            _finding(
                "invalid_constraint",
                f"{rel}:constraint",
                "constraint must be a single provenance-free string of at most 500 characters",
            )
        )
        constraint = None

    # Presence-checked rather than `.get() is not None`: `default_deny:` with
    # the value forgotten parses as YAML null, and for a confidentiality
    # control the permissive reading of a typo is the whole failure mode this
    # field exists to close. Any non-boolean is an ERROR, which refuses the
    # compile — the scope is never quietly left open.
    default_deny = False
    if "default_deny" in data:
        raw_default_deny = data["default_deny"]
        if isinstance(raw_default_deny, bool):
            default_deny = raw_default_deny
        else:
            findings.append(
                _finding(
                    "invalid_field",
                    f"{rel}:default_deny",
                    "default_deny must be a boolean",
                )
            )

    if doc_id is None:
        return None, findings

    scope = Scope(
        id=doc_id,
        source=rel,
        name=name,
        constraint=constraint,
        default_deny=default_deny,
        paths=_as_str_tuple(data.get("paths"), rel, "paths", findings),
        projects=_as_str_tuple(data.get("projects"), rel, "projects", findings),
        tags=_as_str_tuple(data.get("tags"), rel, "tags", findings),
        types=_as_str_tuple(data.get("types"), rel, "types", findings),
        classes=_as_str_tuple(data.get("classes"), rel, "classes", findings),
        refs=_as_str_tuple(data.get("refs"), rel, "refs", findings),
        exclude_paths=_as_str_tuple(exclude.get("paths"), rel, "exclude.paths", findings),
        exclude_projects=_as_str_tuple(exclude.get("projects"), rel, "exclude.projects", findings),
        exclude_tags=_as_str_tuple(exclude.get("tags"), rel, "exclude.tags", findings),
        exclude_types=_as_str_tuple(exclude.get("types"), rel, "exclude.types", findings),
        exclude_classes=_as_str_tuple(exclude.get("classes"), rel, "exclude.classes", findings),
        exclude_refs=_as_str_tuple(exclude.get("refs"), rel, "exclude.refs", findings),
    )
    # Authoring foot-gun, reported rather than guessed at. A binary carries no
    # frontmatter, so `tags`/`types`/`classes`/`projects` cannot select one —
    # only `paths` and `refs` can. A scope built purely from frontmatter
    # selectors therefore withholds the tagged `.md` while `board-call.mp4`
    # beside it stays at full disclosure, which is a surprise in a control that
    # fails closed everywhere else.
    #
    # A WARNING, not an error: the semantics are deliberately unchanged.
    # Inferring membership for an item we cannot read would be guessing, and
    # refusing the compile would break working policies. The author is the one
    # who knows whether media is in scope, so tell them.
    if not scope.paths and not scope.refs and (
        scope.projects or scope.tags or scope.types or scope.classes
    ):
        findings.append(
            _finding(
                "SCOPE_CANNOT_SELECT_MEDIA",
                rel,
                "this scope cannot select non-markdown items; add a `paths` "
                "selector to cover media",
                severity="warning",
            )
        )
    return scope, findings


def _parse_rule(data: dict[str, Any], rel: str) -> tuple[Rule | None, list[dict[str, str]]]:
    findings, doc_id = _check_common(data, rel, _RULE_ALLOWED_FIELDS)

    scope_ids = _as_str_tuple(data.get("scope_ids"), rel, "scope_ids", findings)
    if not scope_ids:
        findings.append(
            _finding("missing_field", f"{rel}:scope_ids", "scope_ids must be a non-empty list of strings")
        )

    audience = data.get("audience")
    if not isinstance(audience, str) or not audience.strip():
        findings.append(_finding("missing_field", f"{rel}:audience", "audience is required"))
        audience = None
    audience = _reject_reserved_audience(audience, rel, "audience", findings)

    ceiling = data.get("ceiling")
    if (
        not isinstance(ceiling, int)
        or isinstance(ceiling, bool)
        or not (DISCLOSURE_MIN <= ceiling <= DISCLOSURE_MAX)
    ):
        findings.append(
            _finding(
                "invalid_ceiling",
                f"{rel}:ceiling",
                f"ceiling must be an integer between {DISCLOSURE_MIN} and {DISCLOSURE_MAX}",
            )
        )
        ceiling = None

    purpose = data.get("purpose")
    if purpose is not None and not isinstance(purpose, str):
        findings.append(_finding("invalid_field", f"{rel}:purpose", "purpose must be a string"))
        purpose = None

    purpose_condition = data.get("purpose_condition", "matches")
    if (
        type(purpose_condition) is not str
        or purpose_condition not in _PURPOSE_CONDITIONS
    ):
        findings.append(
            _finding(
                "invalid_field",
                f"{rel}:purpose_condition",
                f"must be one of {sorted(_PURPOSE_CONDITIONS)}",
            )
        )
        purpose_condition = "matches"

    kind = data.get("kind", "standing")
    if type(kind) is not str or kind not in _RULE_KINDS:
        findings.append(_finding("invalid_field", f"{rel}:kind", f"must be one of {sorted(_RULE_KINDS)}"))
        kind = "standing"

    options = data.get("options", {})
    if not isinstance(options, dict):
        findings.append(_finding("invalid_field", f"{rel}:options", "options must be a mapping"))
        options = {}
    else:
        _check_option_keys(options, _RULE_OPTION_FIELDS, rel, findings)
        for key in _RULE_OPTION_STRING_FIELDS:
            if key in options and not _valid_constraint(options[key]):
                findings.append(
                    _finding(
                        "invalid_field",
                        f"{rel}:options.{key}",
                        f"{key} must be a bounded provenance-free string",
                    )
                )
        if "suspended" in options and not isinstance(options["suspended"], bool):
            findings.append(
                _finding(
                    "invalid_field",
                    f"{rel}:options.suspended",
                    "suspended must be a boolean",
                )
            )

    if doc_id is None or not scope_ids or audience is None or ceiling is None:
        return None, findings

    rule = Rule(
        id=doc_id,
        source=rel,
        scope_ids=scope_ids,
        audience=audience,
        ceiling=ceiling,
        purpose=purpose,
        purpose_condition=purpose_condition,
        kind=kind,
        options=dict(options),
    )
    return rule, findings


def _parse_grant(
    data: dict[str, Any], rel: str
) -> tuple[StandingGrant | None, ReleaseGrant | None, list[dict[str, str]]]:
    kind = data.get("kind", "standing")
    if kind == "release":
        release, findings = _parse_release_grant(data, rel)
        return None, release, findings
    findings, doc_id = _check_common(data, rel, _STANDING_GRANT_ALLOWED_FIELDS)
    if kind != "standing":
        findings.append(
            _finding(
                "invalid_field",
                f"{rel}:kind",
                "grant kind must be 'standing' or 'release'",
            )
        )

    scope_ids = _as_str_tuple(data.get("scope_ids"), rel, "scope_ids", findings)
    if not scope_ids:
        findings.append(
            _finding("missing_field", f"{rel}:scope_ids", "scope_ids must be a non-empty list of strings")
        )

    audience = data.get("audience")
    if not isinstance(audience, str) or not audience.strip():
        findings.append(_finding("missing_field", f"{rel}:audience", "audience is required"))
        audience = None
    audience = _reject_reserved_audience(audience, rel, "audience", findings)

    ceiling = data.get("ceiling")
    if (
        not isinstance(ceiling, int)
        or isinstance(ceiling, bool)
        or not (DISCLOSURE_MIN <= ceiling <= DISCLOSURE_MAX)
    ):
        findings.append(
            _finding(
                "invalid_ceiling",
                f"{rel}:ceiling",
                f"ceiling must be an integer between {DISCLOSURE_MIN} and {DISCLOSURE_MAX}",
            )
        )
        ceiling = None

    if doc_id is None or not scope_ids or audience is None or ceiling is None:
        return None, None, findings

    grant = StandingGrant(
        id=doc_id, source=rel, scope_ids=scope_ids, audience=audience, ceiling=ceiling
    )
    return grant, None, findings


def _parse_release_grant(
    data: dict[str, Any], rel: str
) -> tuple[ReleaseGrant | None, list[dict[str, str]]]:
    findings, doc_id = _check_common(data, rel, _RELEASE_GRANT_ALLOWED_FIELDS)

    def required_text(name: str) -> str | None:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            findings.append(_finding("missing_field", f"{rel}:{name}", f"{name} is required"))
            return None
        return value.strip()

    path = required_text("path")
    if path is not None and not _valid_relative_path(path):
        findings.append(_finding("invalid_field", f"{rel}:path", "path must be canonical and vault-relative"))
        path = None

    ref = required_text("ref")
    if ref is not None and memory_refs.parse_memory_ref(ref) is None:
        findings.append(_finding("invalid_field", f"{rel}:ref", "ref must be a stable memory reference"))
        ref = None

    content_hash = required_text("content_hash")
    if content_hash is not None and _SHA256_RE.fullmatch(content_hash) is None:
        findings.append(_finding("invalid_field", f"{rel}:content_hash", "content_hash must be lowercase SHA-256"))
        content_hash = None

    to_audience = _reject_reserved_audience(
        required_text("to_audience"), rel, "to_audience", findings
    )
    released_at = required_text("released_at")
    if released_at is not None and not _valid_release_time(released_at):
        findings.append(_finding("invalid_field", f"{rel}:released_at", "released_at must be an ISO-8601 timestamp"))
        released_at = None
    why = required_text("why")
    bridge_scope = required_text("bridge_scope")
    if bridge_scope is not None and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", bridge_scope) is None:
        findings.append(_finding("invalid_field", f"{rel}:bridge_scope", "bridge_scope must be a lowercase slug"))
        bridge_scope = None

    raw_dependencies = data.get("bridge_of")
    dependencies: list[ReleaseDependency] = []
    if not isinstance(raw_dependencies, list) or not raw_dependencies:
        findings.append(_finding("missing_field", f"{rel}:bridge_of", "bridge_of must be a non-empty list"))
    else:
        seen_refs: set[str] = set()
        for index, raw in enumerate(raw_dependencies):
            location = f"{rel}:bridge_of[{index}]"
            if not isinstance(raw, dict):
                findings.append(_finding("invalid_field", location, "dependency must be a mapping"))
                continue
            _check_mapping_keys(
                raw,
                _RELEASE_DEPENDENCY_FIELDS,
                location,
                findings,
                separator=".",
                noun="dependency field",
            )
            dep_ref = raw.get("ref")
            dep_path = raw.get("path")
            dep_hash = raw.get("content_hash")
            dep_signature = raw.get("restriction_signature")
            valid = True
            if not isinstance(dep_ref, str) or memory_refs.parse_memory_ref(dep_ref) is None:
                findings.append(_finding("invalid_field", f"{location}.ref", "dependency ref must be stable"))
                valid = False
            if not _valid_relative_path(dep_path):
                findings.append(_finding("invalid_field", f"{location}.path", "dependency path must be canonical"))
                valid = False
            if not isinstance(dep_hash, str) or _SHA256_RE.fullmatch(dep_hash) is None:
                findings.append(_finding("invalid_field", f"{location}.content_hash", "dependency hash must be lowercase SHA-256"))
                valid = False
            if not isinstance(dep_signature, str) or _SHA256_RE.fullmatch(dep_signature) is None:
                findings.append(_finding("invalid_field", f"{location}.restriction_signature", "restriction signature must be lowercase SHA-256"))
                valid = False
            if isinstance(dep_ref, str) and dep_ref in seen_refs:
                findings.append(_finding("duplicate_reference", f"{location}.ref", "dependency refs must be unique"))
                valid = False
            if valid:
                seen_refs.add(dep_ref)
                dependencies.append(
                    ReleaseDependency(
                        ref=dep_ref,
                        path=str(dep_path),
                        content_hash=str(dep_hash),
                        restriction_signature=str(dep_signature),
                    )
                )

    options = data.get("options")
    strip: tuple[str, ...] = ()
    if not isinstance(options, dict):
        findings.append(_finding("missing_field", f"{rel}:options", "options must be a mapping"))
    else:
        _check_option_keys(options, _RELEASE_OPTIONS_FIELDS, rel, findings)
        values = options.get("strip_provenance")
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
            findings.append(_finding("missing_field", f"{rel}:options.strip_provenance", "strip_provenance must be a non-empty list of refs or paths"))
        else:
            strip = tuple(dict.fromkeys(item.strip() for item in values))
            identities = {item for dep in dependencies for item in (dep.ref, dep.path)}
            if any(
                item not in identities
                or (
                    memory_refs.parse_memory_ref(item) is None
                    and not _valid_relative_path(item)
                )
                for item in strip
            ):
                findings.append(_finding("invalid_field", f"{rel}:options.strip_provenance", "strip targets must name approved dependencies"))
            for dep in dependencies:
                if dep.ref not in strip and dep.path not in strip:
                    findings.append(_finding("missing_field", f"{rel}:options.strip_provenance", "every dependency needs a strip target"))

    required = (
        doc_id,
        path,
        ref,
        content_hash,
        to_audience,
        released_at,
        why,
        bridge_scope,
    )
    if any(value is None for value in required) or not dependencies or not strip:
        return None, findings
    return (
        ReleaseGrant(
            id=str(doc_id),
            source=rel,
            path=str(path),
            ref=str(ref),
            content_hash=str(content_hash),
            to_audience=str(to_audience),
            released_at=str(released_at),
            why=str(why),
            bridge_scope=str(bridge_scope),
            bridge_of=tuple(sorted(dependencies, key=lambda item: (item.ref, item.path))),
            strip_provenance=strip,
        ),
        findings,
    )
