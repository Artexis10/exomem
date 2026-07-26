"""Governance policy loader — strict YAML under `_Governance/`, fingerprinted.

Mirrors `access._load_config`/`access.policy_fingerprint` (`access.py:59-135`):
a missing (or file-less) policy directory yields the cached `EMPTY_POLICY`
singleton with a stable "missing" fingerprint; a present one is gated by a
cheap per-file stat signature, and a content hash — computed only when that
signature moves — is the stable identity handed to callers and used as the
membership memo key (`membership.py`). A `(conflicted copy)` sibling anywhere
under `_Governance/` refuses the compile: the last good policy stays in
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
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..kbdir import kb_dirname

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
# Substring only (no trailing ")") — Obsidian Sync typically inserts a
# timestamp before the closing paren, e.g. "file (conflicted copy 2024-01-01).md".
# Always compared against a lower-cased filename (Obsidian doesn't guarantee
# case, and a stray capital-C sibling must still be caught as a conflict, not
# mistaken for a second, differently-named policy document).
_CONFLICTED_MARKER = "(conflicted copy"

# Distinct from `EMPTY_POLICY`'s "missing" sentinel on purpose: a refused
# compile with NO prior good compile to fall back on (a cold start) is a
# fail-closed floor, not "no governance in effect". See `_blocked`.
BLOCKED_FINGERPRINT = "blocked"

_SCOPE_SELECTOR_FIELDS = ("paths", "projects", "tags", "types", "classes", "refs")
_SCOPE_ALLOWED_FIELDS = frozenset(
    {"governance_version", "id", "name", "exclude", *_SCOPE_SELECTOR_FIELDS}
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
_GRANT_ALLOWED_FIELDS = frozenset(
    {"governance_version", "id", "scope_ids", "audience", "ceiling"}
)
_PURPOSE_CONDITIONS = frozenset({"matches", "outside"})
_RULE_KINDS = frozenset({"standing", "org_cap"})


@dataclass(frozen=True)
class Scope:
    """A named membership selector: any positive selector kind, minus excludes."""

    id: str
    source: str
    name: str | None = None
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
class Policy:
    fingerprint: str
    scopes: dict[str, Scope] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    grants: tuple[StandingGrant, ...] = ()
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
    return bool(parts) and parts[0] in {"events", "deletion-tombstones"}


def _iter_policy_files(root: Path) -> list[tuple[str, Path]]:
    """`(kind, path)` for every recognized `*.yaml` under scopes/rules/grants."""
    out: list[tuple[str, Path]] = []
    for kind in ("scopes", "rules", "grants"):
        sub = root / kind
        if not sub.is_dir():
            continue
        for p in sorted(sub.glob("*.yaml")):
            if p.is_file() and _CONFLICTED_MARKER not in p.name.lower():
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


def load(vault_root: Path) -> Policy:
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
    conflicts = [p for p in _iter_all_files(root) if _CONFLICTED_MARKER in p.name.lower()]

    if not files and not conflicts:
        _CACHE.pop(key, None)
        return EMPTY_POLICY

    if conflicts:
        conflict_findings = tuple(
            _finding(
                "conflicted_copy",
                p.relative_to(root).as_posix(),
                "an Obsidian conflicted-copy sibling is present; resolve it before "
                "policy changes take effect",
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

    findings, scopes, rules, grants = _compile(root, files)
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
        findings=tuple(findings),
    )
    _CACHE[key] = (signature, compiled)
    return compiled


def _compile(
    root: Path, files: list[tuple[str, Path]]
) -> tuple[list[dict[str, str]], dict[str, Scope], tuple[Rule, ...], tuple[StandingGrant, ...]]:
    findings: list[dict[str, str]] = []
    scopes: dict[str, Scope] = {}
    rules: list[Rule] = []
    grants: list[StandingGrant] = []

    recognized = {path for _kind, path in files}
    for p in _iter_all_files(root):
        if p in recognized or _CONFLICTED_MARKER in p.name.lower():
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
                rules.append(rule)
        else:
            grant, doc_findings = _parse_grant(data, rel)
            findings.extend(doc_findings)
            if grant is not None:
                grants.append(grant)

    return findings, scopes, tuple(rules), tuple(grants)


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
    if not isinstance(raw_id, str) or not _ULID_RE.match(raw_id):
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

    if doc_id is None:
        return None, findings

    scope = Scope(
        id=doc_id,
        source=rel,
        name=name,
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
) -> tuple[StandingGrant | None, list[dict[str, str]]]:
    findings, doc_id = _check_common(data, rel, _GRANT_ALLOWED_FIELDS)

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
        return None, findings

    grant = StandingGrant(
        id=doc_id, source=rel, scope_ids=scope_ids, audience=audience, ceiling=ceiling
    )
    return grant, findings
