"""The activation-conventions registry: how THIS vault is shaped and spelled.

`working_set_index`, `working_set_state` and `working_set_resolve` used to
answer that question from constants tuned on one owner's vault: which folders
hold `resource`/`hub` anchors, which folders the index walk skips, which
frontmatter fields state a Records item's condition and observation date,
which words the lexical band and derived-name admission ignore, and how many
anchors a shared name word may name before it stops counting as contact. A
vault that spells any of these differently got a compiler that quietly found
less, with no file to edit and no finding that said why.

Load order mirrors `context_roles`, for the same reason: the shipped registry
ships in the skill scaffold (and its byte-identical plugin copy), a vault
override at `<Knowledge Base>/_Schema/activation-conventions.yaml` may add to
it or narrow it, and a broken override falls back to the shipped registry
with the failure reported rather than raising — a typo in an owner's file is
the expected failure, and refusing to activate would cost the owner every
packet to protect nothing.

One deliberate difference from `context_roles`: the digest this module
reports is taken over the EFFECTIVE conventions — the values in force after
bounds and findings are applied — never over the override file's bytes. Two
files that resolve to the same conventions must share a digest, because the
digest is what invalidates the activation sidecar and the packet cache; a
byte-for-byte digest would rebuild the catalogue on a whitespace edit that
changed nothing the compiler reads.

Folder rules are validated here without ever touching the vault's filesystem:
"a tree holding a Planning or Records collection" is, product-wide, exactly
the literal top-level `Records`/`Planning` folders under the knowledge base
(`recall_policy._is_structured_alias` draws the same line), so rejecting a
rule that names one is a string comparison, not a manifest scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REGISTRY_FILENAME = "activation-conventions.yaml"

#: Refused before parsing (design.md decision 2): an alias-expansion document
#: must not even reach the YAML parser, on a hosted tier where the file is
#: tenant-authored.
MAX_FILE_BYTES = 256 * 1024

#: Bounds, each with its reason (design.md decision 6): unbounded per-page and
#: per-turn evaluation inside a request budget. Entries past a cap are
#: ignored and reported, never silently dropped without a finding.
MAX_FOLDERS_PER_KIND = 32
MAX_TAGS_PER_KIND = 32
MAX_TYPES_PER_KIND = 32
MAX_SKIP_FOLDERS = 32
MAX_STATE_FIELDS = 24
MAX_DATE_FIELDS = 12
MAX_STOPWORDS = 2000
MAX_ENTRY_CHARS = 64

#: `rare_term_max_anchors` may only be tightened, never loosened past the
#: shipped ceiling (design.md decision 2a).
RARE_TERM_MIN = 1
RARE_TERM_MAX = 3

#: The two anchor kinds this registry governs. `entity` stays with the entity
#: registry; `collection`, `plan` and `project` come from their own manifests
#: (design.md decision 3) — none of the five is configurable here.
ANCHOR_KIND_NAMES: tuple[str, ...] = ("resource", "hub")

_ANCHOR_TOP_FIELDS = frozenset({"resource", "hub", "add_skip_folders"})
_ANCHOR_KIND_FIELDS = frozenset(
    {"add_folders", "drop_folders", "add_tags", "drop_tags", "add_types", "drop_types"}
)
_STATE_FIELDS_ALLOWED = frozenset({"prefer_state_fields", "drop_state_fields"})
_STOPWORDS_FIELDS_ALLOWED = frozenset({"add"})
_RESOLUTION_FIELDS_ALLOWED = frozenset({"rare_term_max_anchors"})
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "anchors", "state", "stopwords", "resolution"})


@dataclass(frozen=True, slots=True)
class AnchorRule:
    """One anchor kind's membership: folders (normalised path prefixes), tags
    and frontmatter `type` values (both normalised for case-insensitive
    matching, mirroring how `_page_anchor_kind` already reads a tag)."""

    folders: tuple[str, ...] = ()
    tags: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Conventions:
    """The effective conventions for one vault."""

    anchors: Mapping[str, AnchorRule]
    #: Directory basenames the index walk skips. Compared case-SENSITIVELY,
    #: exactly as `working_set_index._SKIP_DIR_NAMES` always was — this is
    #: the one field this registry does NOT normalise, because the shipped
    #: set itself is mixed-case (`_Staging`, `Templates`) and casefolding it
    #: would be a behaviour change, not a port.
    skip_folders: frozenset[str] = frozenset()
    #: Ordered, most-specific first. Literal frontmatter/Records field names,
    #: so casing is preserved rather than normalised.
    state_fields: tuple[str, ...] = ()
    date_fields: tuple[str, ...] = ()
    #: Normalised (NFKC + casefold), matching every lexical comparison key
    #: `working_set_index.normalize` / `tokens_of` already produce.
    stopwords: frozenset[str] = frozenset()
    rare_term_max_anchors: int = RARE_TERM_MAX


@dataclass(frozen=True, slots=True)
class ConventionsRegistry:
    """The effective registry for one vault, plus how it was resolved."""

    conventions: Conventions
    source: str
    conventions_hash: str
    findings: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def generation_block(self) -> dict[str, Any]:
        """What the packet's `generation` block reports about conventions."""
        block: dict[str, Any] = {
            "conventions_hash": self.conventions_hash,
            "conventions_source": self.source,
        }
        if self.findings:
            block["conventions_findings"] = [dict(finding) for finding in self.findings]
        return block


_CACHE: dict[Path, tuple[str, ConventionsRegistry]] = {}
_SHIPPED: ConventionsRegistry | None = None


def _finding(code: str, field_name: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field_name, "message": message}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def conventions_payload(conventions: Conventions) -> dict[str, Any]:
    """The JSON-safe view of EFFECTIVE values the digest (and a `diff`) reads.

    Factored out of `_conventions_digest` so `schema_memory`'s `diff`
    operation can compare two registries' effective values without
    duplicating this shape.
    """
    return {
        "anchors": {
            kind: {
                "folders": sorted(rule.folders),
                "tags": sorted(rule.tags),
                "types": sorted(rule.types),
            }
            for kind, rule in sorted(conventions.anchors.items())
        },
        "skip_folders": sorted(conventions.skip_folders),
        "state_fields": list(conventions.state_fields),
        "date_fields": list(conventions.date_fields),
        "stopwords": sorted(conventions.stopwords),
        "rare_term_max_anchors": conventions.rare_term_max_anchors,
    }


def _conventions_digest(conventions: Conventions) -> str:
    """The digest of record: over EFFECTIVE values, never file bytes.

    Two override files that resolve to the same conventions therefore share a
    digest, and a whitespace-only or key-reordering edit that changes nothing
    the compiler reads does not rebuild the sidecar.
    """
    raw = json.dumps(conventions_payload(conventions), sort_keys=True, separators=(",", ":"))
    return _hash(raw)


def shipped_conventions_text() -> str:
    """The packaged registry's bytes — one source for server, skill and plugin."""
    resource = files("exomem").joinpath("_scaffold", "_Schema", REGISTRY_FILENAME)
    return resource.read_text(encoding="utf-8")


def override_path(vault_root: Path) -> Path:
    """Where a vault's own registry lives, if the owner authored one."""
    return Path(vault_root) / kb_dirname() / "_Schema" / REGISTRY_FILENAME


def clear_cache() -> None:
    """Drop the memoized registries (tests; overrides are edited out of band)."""
    global _SHIPPED
    _CACHE.clear()
    _SHIPPED = None


def shipped_conventions() -> ConventionsRegistry:
    """Parse the packaged registry once per process."""
    global _SHIPPED
    if _SHIPPED is None:
        raw = shipped_conventions_text()
        data = yaml.safe_load(raw)
        conventions, findings = _parse_shipped(data)
        if findings:
            # A broken SHIPPED registry is a build defect, not a runtime state.
            raise RuntimeError(f"packaged activation-conventions registry is invalid: {findings[0]}")
        _SHIPPED = ConventionsRegistry(
            conventions=conventions, source="shipped", conventions_hash=_conventions_digest(conventions)
        )
    return _SHIPPED


def load_conventions(
    vault_root: Path | None = None, *, proposal: Any | None = None
) -> ConventionsRegistry:
    """Return the effective registry: shipped, or shipped plus a vault override.

    `proposal=` mirrors `context_roles.load_roles`'s stateless-validation
    seam: merge a proposed override WITHOUT touching the filesystem, for
    `schema_memory`'s `validate`/`diff`/`save-conventions` operations. Unlike
    `context_roles`, this needs no digest parameter -- `conventions_hash` is
    always taken over the EFFECTIVE values (decision 5), so it is identical
    whether those values came from a file on disk or a proposal in memory.
    """
    shipped = shipped_conventions()
    if proposal is not None:
        return _merge(shipped.conventions, proposal)
    if vault_root is None:
        return shipped
    path = override_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return shipped
    except OSError:
        log.warning("activation-conventions override unreadable at %s", path, exc_info=True)
        return replace(
            shipped,
            findings=(_finding("unreadable", "conventions", "override could not be read"),),
        )
    if len(raw.encode("utf-8")) > MAX_FILE_BYTES:
        return replace(
            shipped,
            findings=(_finding("file_too_large", "conventions", f"override exceeds {MAX_FILE_BYTES} bytes"),),
        )
    file_digest = _hash(raw)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == file_digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        registry = replace(
            shipped, findings=(_finding("invalid_yaml", "conventions", str(exc)),)
        )
    else:
        registry = _merge(shipped.conventions, data)
    _CACHE[path] = (file_digest, registry)
    return registry


def save_conventions(vault_root: Path, proposal: Any, *, expected_hash: str) -> dict[str, Any]:
    """Save one reviewed, complete activation-conventions override document.

    The proposal is the raw override document -- the same override grammar as
    the file on disk -- never a delta. `expected_hash` is unconditionally
    required (design.md decision 7's `save-relations` pattern) and is checked
    against the CURRENT effective registry's `conventions_hash`, which is
    always defined (the shipped registry has one even with no override file).

    Callers are expected to have already rejected a proposal with any
    finding (`op_schema_memory` does, before calling this); the check here
    is defence in depth, matching `context_roles.save_roles`.
    """
    current = load_conventions(vault_root)
    if current.conventions_hash != expected_hash:
        raise ValueError(
            "STALE_ACTIVATION_CONVENTIONS_REGISTRY: expected_hash does not match current hash"
        )
    candidate = load_conventions(proposal=proposal)
    if candidate.findings:
        raise ValueError(
            f"INVALID_ACTIVATION_CONVENTIONS_REGISTRY: {[dict(item) for item in candidate.findings]!r}"
        )
    path = override_path(vault_root)
    rendered = yaml.safe_dump(proposal, sort_keys=True, allow_unicode=True)
    from . import vault as vault_module

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=path, content=rendered)], vault_root=Path(vault_root)
    )
    _CACHE.pop(path, None)
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": candidate.conventions_hash,
        "previous_hash": current.conventions_hash,
        "created": current.source == "shipped",
    }


# --------------------------------------------------------------------------- #
# Parsing: shipped
# --------------------------------------------------------------------------- #


def _shipped_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _shipped_anchor_rule(raw: Any) -> AnchorRule:
    if not isinstance(raw, Mapping):
        return AnchorRule()
    from .working_set_index import normalize

    folders = tuple(
        dict.fromkeys(
            "/".join(normalize(segment) for segment in folder.split("/"))
            for folder in _shipped_strings(raw.get("folders"))
        )
    )
    tags = frozenset(normalize(tag) for tag in _shipped_strings(raw.get("tags")))
    types = frozenset(normalize(item) for item in _shipped_strings(raw.get("types")))
    return AnchorRule(folders=folders, tags=tags, types=types)


def _parse_shipped(data: Any) -> tuple[Conventions, tuple[dict[str, str], ...]]:
    findings: list[dict[str, str]] = []
    if not isinstance(data, Mapping):
        return Conventions(anchors={}), (_finding("invalid_registry", "conventions", "must be a mapping"),)
    if data.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}"))
    anchors_raw = data.get("anchors")
    anchors_raw = anchors_raw if isinstance(anchors_raw, Mapping) else {}
    anchors = {
        kind: _shipped_anchor_rule(anchors_raw.get(kind)) for kind in ANCHOR_KIND_NAMES
    }
    skip_folders = frozenset(
        entry.strip() for entry in _shipped_strings(anchors_raw.get("skip_folders"))
    )
    state_raw = data.get("state")
    state_raw = state_raw if isinstance(state_raw, Mapping) else {}
    state_fields = tuple(dict.fromkeys(_shipped_strings(state_raw.get("state_fields"))))
    date_fields = tuple(dict.fromkeys(_shipped_strings(state_raw.get("date_fields"))))
    from .working_set_index import normalize

    stopwords = frozenset(normalize(word) for word in _shipped_strings(data.get("stopwords")))
    resolution_raw = data.get("resolution")
    resolution_raw = resolution_raw if isinstance(resolution_raw, Mapping) else {}
    rare_term_max_anchors = resolution_raw.get("rare_term_max_anchors")
    if not isinstance(rare_term_max_anchors, int) or isinstance(rare_term_max_anchors, bool):
        findings.append(_finding("invalid_threshold", "resolution.rare_term_max_anchors", "must be an integer"))
        rare_term_max_anchors = RARE_TERM_MAX
    conventions = Conventions(
        anchors=anchors,
        skip_folders=skip_folders,
        state_fields=state_fields,
        date_fields=date_fields,
        stopwords=stopwords,
        rare_term_max_anchors=int(rare_term_max_anchors),
    )
    return conventions, tuple(findings)


# --------------------------------------------------------------------------- #
# Parsing: override merge
# --------------------------------------------------------------------------- #


def _cap_tuple(
    items: Sequence[str], *, field_name: str, cap: int, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    """Apply one cap, ONCE, at the level the spec's scenario names: the
    EFFECTIVE combined list (shipped survivors plus additions), never the
    incoming `add_*` list on its own — capping both would silently lose more
    entries than the cap describes and report two overlapping findings for
    one event."""
    deduped = tuple(dict.fromkeys(items))
    if len(deduped) > cap:
        findings.append(
            _finding("cap_exceeded", field_name, f"{len(deduped) - cap} entry(ies) past the {cap} cap ignored")
        )
    return deduped[:cap]


def _validated_strings(
    value: object,
    *,
    field_name: str,
    findings: list[dict[str, str]],
    normalize_fn: Any = None,
) -> tuple[str, ...]:
    """Strip, drop empties and over-length entries (with a finding each),
    dedupe. No cap here — the caller caps once, after combining with the
    shipped survivors (see `_cap_tuple`)."""
    if not isinstance(value, (list, tuple)):
        return ()
    accepted: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_ENTRY_CHARS:
            findings.append(
                _finding("entry_too_long", field_name, f"entry exceeds {MAX_ENTRY_CHARS} characters: {text!r}")
            )
            continue
        accepted.append(normalize_fn(text) if normalize_fn else text)
    return tuple(dict.fromkeys(accepted))


def _validate_folder_rule(raw: str) -> tuple[str | None, str | None]:
    """`(normalised_rule, rejection_code)` — exactly one side is not `None`."""
    from . import vault
    from .working_set_index import normalize

    text = raw.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None, "rule_absolute"
    segments = text.split("/")
    if any(segment == "" for segment in segments):
        return None, "rule_malformed"
    if any(segment == ".." for segment in segments):
        return None, "rule_parent_segment"
    if any(segment.startswith(".") or segment.startswith("_") for segment in segments):
        return None, "rule_hidden_segment"
    head = segments[0]
    if head.casefold() == kb_dirname().casefold():
        return None, "rule_kb_prefixed"
    if head.casefold() == "entities":
        return None, "rule_entity_folder"
    if head.casefold() in {"records", "planning"}:
        return None, "rule_structured_tree"
    if vault.in_append_only_tree(text) is not None:
        return None, "rule_append_only"
    normalized = "/".join(normalize(segment) for segment in segments)
    return normalized, None


def _validated_folder_rules(
    value: object, *, field_name: str, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    """Validate shape and char length (with a finding each rejection), dedupe.
    No cap here — see `_cap_tuple`."""
    if not isinstance(value, (list, tuple)):
        return ()
    accepted: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_ENTRY_CHARS:
            findings.append(
                _finding("entry_too_long", field_name, f"entry exceeds {MAX_ENTRY_CHARS} characters: {text!r}")
            )
            continue
        normalized, code = _validate_folder_rule(text)
        if code is not None:
            findings.append(_finding(code, field_name, f"rejected folder rule: {text!r}"))
            continue
        accepted.append(normalized)  # type: ignore[arg-type]
    return tuple(dict.fromkeys(accepted))


def _merge_anchor_rule(
    shipped_rule: AnchorRule, raw: Any, *, kind: str, findings: list[dict[str, str]]
) -> AnchorRule:
    if not isinstance(raw, Mapping):
        findings.append(_finding("invalid_anchor_kind", f"anchors.{kind}", "must be a mapping"))
        return shipped_rule
    unknown = sorted(set(raw) - _ANCHOR_KIND_FIELDS)
    for key in unknown:
        findings.append(_finding("unknown_field", f"anchors.{kind}.{key}", "unknown override field"))

    from .working_set_index import normalize

    dropped_folders = {
        seg
        for entry in raw.get("drop_folders") or ()
        if isinstance(entry, str)
        for seg in ("/".join(normalize(part) for part in entry.strip().split("/")),)
    }
    surviving_folders = tuple(f for f in shipped_rule.folders if f not in dropped_folders)
    added_folders = _validated_folder_rules(
        raw.get("add_folders"), field_name=f"anchors.{kind}.add_folders", findings=findings
    )
    folders = _cap_tuple(
        (*surviving_folders, *added_folders),
        field_name=f"anchors.{kind}.folders",
        cap=MAX_FOLDERS_PER_KIND,
        findings=findings,
    )

    dropped_tags = {normalize(t) for t in raw.get("drop_tags") or () if isinstance(t, str)}
    surviving_tags = tuple(t for t in shipped_rule.tags if t not in dropped_tags)
    added_tags = _validated_strings(
        raw.get("add_tags"), field_name=f"anchors.{kind}.add_tags", findings=findings, normalize_fn=normalize
    )
    tags = frozenset(
        _cap_tuple(
            (*surviving_tags, *added_tags),
            field_name=f"anchors.{kind}.tags",
            cap=MAX_TAGS_PER_KIND,
            findings=findings,
        )
    )

    dropped_types = {normalize(t) for t in raw.get("drop_types") or () if isinstance(t, str)}
    surviving_types = tuple(t for t in shipped_rule.types if t not in dropped_types)
    added_types = _validated_strings(
        raw.get("add_types"), field_name=f"anchors.{kind}.add_types", findings=findings, normalize_fn=normalize
    )
    types = frozenset(
        _cap_tuple(
            (*surviving_types, *added_types),
            field_name=f"anchors.{kind}.types",
            cap=MAX_TYPES_PER_KIND,
            findings=findings,
        )
    )

    return AnchorRule(folders=folders, tags=tags, types=types)


def _merge_anchors(
    shipped: Conventions, raw: Any, findings: list[dict[str, str]]
) -> tuple[dict[str, AnchorRule], frozenset[str]]:
    if raw is None:
        return dict(shipped.anchors), shipped.skip_folders
    if not isinstance(raw, Mapping):
        findings.append(_finding("invalid_anchors", "anchors", "must be a mapping"))
        return dict(shipped.anchors), shipped.skip_folders
    unknown = sorted(set(raw) - _ANCHOR_TOP_FIELDS)
    for key in unknown:
        findings.append(_finding("unknown_field", f"anchors.{key}", "unknown override field"))
    anchors = {
        kind: _merge_anchor_rule(shipped.anchors.get(kind, AnchorRule()), raw.get(kind), kind=kind, findings=findings)
        if kind in raw
        else shipped.anchors.get(kind, AnchorRule())
        for kind in ANCHOR_KIND_NAMES
    }
    added_skip = _validated_strings(
        raw.get("add_skip_folders"), field_name="anchors.add_skip_folders", findings=findings
    )
    skip_folders = frozenset(
        _cap_tuple(
            (*sorted(shipped.skip_folders), *added_skip),
            field_name="anchors.skip_folders",
            cap=MAX_SKIP_FOLDERS,
            findings=findings,
        )
    )
    return anchors, skip_folders


def _merge_state(
    shipped: Conventions, raw: Any, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    if raw is None:
        return shipped.state_fields
    if not isinstance(raw, Mapping):
        findings.append(_finding("invalid_state", "state", "must be a mapping"))
        return shipped.state_fields
    unknown = sorted(set(raw) - _STATE_FIELDS_ALLOWED)
    for key in unknown:
        findings.append(_finding("unknown_field", f"state.{key}", "unknown override field"))
    dropped = set(_shipped_strings(raw.get("drop_state_fields")))
    base = tuple(field_name for field_name in shipped.state_fields if field_name not in dropped)
    preferred = _validated_strings(
        raw.get("prefer_state_fields"), field_name="state.prefer_state_fields", findings=findings
    )
    return _cap_tuple(
        (*preferred, *base), field_name="state.state_fields", cap=MAX_STATE_FIELDS, findings=findings
    )


def _merge_stopwords(
    shipped: Conventions, raw: Any, findings: list[dict[str, str]]
) -> frozenset[str]:
    if raw is None:
        return shipped.stopwords
    if not isinstance(raw, Mapping):
        findings.append(_finding("invalid_stopwords", "stopwords", "must be a mapping"))
        return shipped.stopwords
    if "drop" in raw:
        findings.append(
            _finding("stopword_drop_refused", "stopwords.drop", "stopwords are add-only; drop is ignored")
        )
    unknown = sorted(set(raw) - _STOPWORDS_FIELDS_ALLOWED - {"drop"})
    for key in unknown:
        findings.append(_finding("unknown_field", f"stopwords.{key}", "unknown override field"))
    from .working_set_index import normalize

    added = _validated_strings(
        raw.get("add"), field_name="stopwords.add", findings=findings, normalize_fn=normalize
    )
    return frozenset(
        _cap_tuple(
            (*sorted(shipped.stopwords), *added), field_name="stopwords", cap=MAX_STOPWORDS, findings=findings
        )
    )


def _merge_resolution(
    shipped: Conventions, raw: Any, findings: list[dict[str, str]]
) -> int:
    if raw is None:
        return shipped.rare_term_max_anchors
    if not isinstance(raw, Mapping):
        findings.append(_finding("invalid_resolution", "resolution", "must be a mapping"))
        return shipped.rare_term_max_anchors
    unknown = sorted(set(raw) - _RESOLUTION_FIELDS_ALLOWED)
    for key in unknown:
        findings.append(_finding("unknown_field", f"resolution.{key}", "unknown resolution key"))
    if "rare_term_max_anchors" not in raw:
        return shipped.rare_term_max_anchors
    value = raw.get("rare_term_max_anchors")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not (RARE_TERM_MIN <= value <= RARE_TERM_MAX)
    ):
        findings.append(
            _finding(
                "invalid_threshold",
                "resolution.rare_term_max_anchors",
                f"rejected value {value!r}; must be an integer {RARE_TERM_MIN}..{RARE_TERM_MAX}",
            )
        )
        return shipped.rare_term_max_anchors
    return int(value)


def _merge(shipped: Conventions, data: Any) -> ConventionsRegistry:
    """Apply a vault override: add or narrow, never remove past the shipped floor."""
    findings: list[dict[str, str]] = []
    if not isinstance(data, Mapping):
        conventions = shipped
        findings.append(_finding("invalid_registry", "conventions", "override must be a mapping"))
        return ConventionsRegistry(
            conventions=conventions,
            source="shipped",
            conventions_hash=_conventions_digest(conventions),
            findings=tuple(findings),
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}"))
    unknown = sorted(set(data) - _TOP_LEVEL_FIELDS)
    for key in unknown:
        findings.append(_finding("unknown_field", key, "unknown top-level field"))

    anchors, skip_folders = _merge_anchors(shipped, data.get("anchors"), findings)
    state_fields = _merge_state(shipped, data.get("state"), findings)
    stopwords = _merge_stopwords(shipped, data.get("stopwords"), findings)
    rare_term_max_anchors = _merge_resolution(shipped, data.get("resolution"), findings)

    conventions = Conventions(
        anchors=anchors,
        skip_folders=skip_folders,
        state_fields=state_fields,
        date_fields=shipped.date_fields,
        stopwords=stopwords,
        rare_term_max_anchors=rare_term_max_anchors,
    )
    source = "vault"
    return ConventionsRegistry(
        conventions=conventions,
        source=source,
        conventions_hash=_conventions_digest(conventions),
        findings=tuple(findings),
    )


def _folder_prefix_matches(rule_folders: Sequence[str], directory_segments: Sequence[str]) -> bool:
    """True when `directory_segments` (already normalised) fall inside any of
    `rule_folders` (each an already-normalised, `/`-joined path prefix)."""
    for rule in rule_folders:
        rule_segments = rule.split("/") if rule else []
        if not rule_segments:
            continue
        if list(directory_segments[: len(rule_segments)]) == rule_segments:
            return True
    return False


def anchor_membership(
    conventions: Conventions,
    *,
    kind: str,
    directory_segments: Sequence[str],
    tags: Sequence[str],
    type_value: str,
) -> bool:
    """Whether a walked page's folder/tags/type satisfy `kind`'s rule.

    `directory_segments` is the page's KB-relative CONTAINING directory, one
    entry per path segment, already normalised by the caller
    (`working_set_index.normalize`, segment by segment) — a folder rule is a
    prefix of at most three segments, so a page arbitrarily deep under a
    matched folder still qualifies. `tags` and `type_value` must likewise
    already be normalised by the caller, matching how the rule's own
    `tags`/`types` were normalised at load time.
    """
    rule = conventions.anchors.get(kind)
    if rule is None:
        return False
    if rule.folders and _folder_prefix_matches(rule.folders, directory_segments):
        return True
    if rule.tags and set(tags) & rule.tags:
        return True
    if rule.types and type_value in rule.types:
        return True
    return False
