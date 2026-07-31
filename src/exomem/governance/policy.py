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
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import memory_refs
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
_PURPOSE_CONDITIONS = frozenset({"matches", "outside"})
_RULE_KINDS = frozenset({"standing", "org_cap"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONSTRAINT_MAX_CHARS = 500


@dataclass(frozen=True)
class Scope:
    """A named membership selector: any positive selector kind, minus excludes."""

    id: str
    source: str
    name: str | None = None
    constraint: str | None = None
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


def _is_operational_state(root: Path, path: Path) -> bool:
    """Receipt evidence is governed state, never a policy input or warning."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return bool(parts) and (
        parts[0] in {"events", "deletion-tombstones", "archives"}
        or parts == (".policy-mutation.pending.json",)
    )


def _iter_policy_files(root: Path) -> list[tuple[str, Path]]:
    """`(kind, path)` for every recognized `*.yaml` under scopes/rules/grants."""
    out: list[tuple[str, Path]] = []
    for kind in ("scopes", "rules", "grants"):
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


def _marker_generation(vault_root: Path) -> tuple[str, bool]:
    marker = governance_root(vault_root) / ".policy-mutation.pending.json"
    try:
        stat_result = marker.lstat()
        if not stat.S_ISREG(stat_result.st_mode):
            return f"invalid:{stat_result.st_mode}", True
        raw = marker.read_bytes()
    except FileNotFoundError:
        return "absent", False
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}", True
    digest = hashlib.sha256(raw).hexdigest()
    return f"{stat_result.st_ino}:{stat_result.st_size}:{digest}", True


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
    root: Path, files: list[tuple[str, Path]]
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

    recognized = {path for _kind, path in files}
    for p in _iter_all_files(root):
        if p in recognized or is_conflict_copy(p.name):
            continue
        findings.append(
            _finding(
                "unknown_file",
                p.relative_to(root).as_posix(),
                "not a recognized governance document; ignored",
                severity="warning",
            )
        )

    for kind, path in files:
        rel = path.relative_to(root).as_posix()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            findings.append(_finding("read_error", rel, str(error)))
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


def compile_prospective(
    vault_root: Path, documents: dict[str, str | None]
) -> Policy:
    """Compile the complete policy produced by overlaying proposed documents.

    This deliberately uses the same strict parser as the live loader.  The
    temporary tree contains policy inputs only, so operational receipts and
    mutation markers cannot accidentally become proposal warnings.
    """
    live_root = governance_root(Path(vault_root))
    with tempfile.TemporaryDirectory(prefix="exomem-governance-") as temporary:
        root = Path(temporary)
        for _kind, source in _iter_policy_files(live_root):
            rel = source.relative_to(live_root).as_posix()
            if rel in documents:
                continue
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        for rel, content in documents.items():
            if content is None:
                continue
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        files = _iter_policy_files(root)
        findings, scopes, rules, grants, release_grants = _compile(root, files)
        fingerprint = _content_fingerprint(root, files)
    if any(finding["severity"] == "error" for finding in findings):
        return _blocked(tuple(findings))
    return Policy(
        fingerprint=fingerprint,
        scopes=scopes,
        rules=rules,
        grants=grants,
        release_grants=release_grants,
        findings=tuple(findings),
    )


def _check_common(
    data: dict[str, Any], rel: str, allowed: frozenset[str]
) -> tuple[list[dict[str, str]], str | None]:
    findings: list[dict[str, str]] = []
    for key in sorted(set(data) - allowed):
        findings.append(_finding("unknown_field", f"{rel}:{key}", f"unknown field {key!r}"))
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


def _parse_scope(data: dict[str, Any], rel: str) -> tuple[Scope | None, list[dict[str, str]]]:
    findings, doc_id = _check_common(data, rel, _SCOPE_ALLOWED_FIELDS)

    exclude = data.get("exclude") or {}
    if not isinstance(exclude, dict):
        findings.append(_finding("invalid_field", f"{rel}:exclude", "exclude must be a mapping"))
        exclude = {}
    else:
        for key in sorted(set(exclude) - _SCOPE_EXCLUDE_ALLOWED_FIELDS):
            findings.append(
                _finding("unknown_field", f"{rel}:exclude.{key}", f"unknown exclude field {key!r}")
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

    if doc_id is None:
        return None, findings

    scope = Scope(
        id=doc_id,
        source=rel,
        name=name,
        constraint=constraint,
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
    if purpose_condition not in _PURPOSE_CONDITIONS:
        findings.append(
            _finding(
                "invalid_field",
                f"{rel}:purpose_condition",
                f"must be one of {sorted(_PURPOSE_CONDITIONS)}",
            )
        )
        purpose_condition = "matches"

    kind = data.get("kind", "standing")
    if kind not in _RULE_KINDS:
        findings.append(_finding("invalid_field", f"{rel}:kind", f"must be one of {sorted(_RULE_KINDS)}"))
        kind = "standing"

    options = data.get("options", {})
    if not isinstance(options, dict):
        findings.append(_finding("invalid_field", f"{rel}:options", "options must be a mapping"))
        options = {}

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

    to_audience = required_text("to_audience")
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
            for key in sorted(set(raw) - _RELEASE_DEPENDENCY_FIELDS):
                findings.append(_finding("unknown_field", f"{location}.{key}", f"unknown dependency field {key!r}"))
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
        for key in sorted(set(options) - _RELEASE_OPTIONS_FIELDS):
            findings.append(_finding("unknown_field", f"{rel}:options.{key}", f"unknown option {key!r}"))
        values = options.get("strip_provenance")
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
            findings.append(_finding("missing_field", f"{rel}:options.strip_provenance", "strip_provenance must be a non-empty list of refs or paths"))
        else:
            strip = tuple(dict.fromkeys(item.strip() for item in values))
            identities = {item for dep in dependencies for item in (dep.ref, dep.path)}
            if any(item not in identities for item in strip):
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
