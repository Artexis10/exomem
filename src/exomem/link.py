"""The `link` MCP tool: create a registered typed entity under Entities/.

Name is **Title Case**, not slugified — entities are named after the thing
they are (e.g., `Ada Lovelace.md`, `Agentic RAG.md`, `pgvector.md`).

v1 is create-only. If the entity file already exists, this raises
`ENTITY_EXISTS` — use `replace` to supersede an existing entity.

Sub-folder index maintenance (e.g. categorizing concepts by domain in
`Entities/Concepts/index.md`) is deferred — handled by audit follow-up.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import (
    entity_candidates,
    indexes,
    memory_refs,
    origin_bindings,
    provenance,
    semantic_writes,
    tag_variants,
    temporal,
    vocabulary_resolution,
)
from .entity_types import (
    ENTITY_WRITER_OPTIONAL_FRONTMATTER,
    MAX_FACET_TEXT_CHARS,
    MAX_FACET_VALUES,
    EntityTypeDefinition,
    EntityTypeRegistry,
    load_entity_types,
)
from .kbdir import kb_prefix
from .vault import (
    InvalidSlugError,
    PlannedWrite,
    WikilinkResolver,
    canonical_vault_rel,
    escape_wikilinks_for_log,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    plan_log_writes,
    read_guarded_text,
    render_wikilink_target,
    resolve_filename_slug,
    writer_link_visibility,
    yaml_scalar,
)

log = logging.getLogger(__name__)


DECISION_STATUS_VALUES = ("proposed", "accepted", "superseded")


@dataclass
class LinkResult:
    path: str  # vault-relative
    ref: str
    warnings: list[str]
    creation: dict | None = None
    # The filename slug actually written, after truncation/normalisation.
    # See NoteResult.slug — callers must link by this, not by re-slugging.
    # Declared last so the positional LinkResult(...) construction stays valid.
    slug: str = ""
    # The explicit shared-name decision this creation committed under, if any.
    identity_decision: dict | None = None
    # How the requested entity type resolved and where it projects.
    vocabulary_resolution: dict[str, str] | None = None

    def as_dict(self) -> dict:
        value: dict[str, object] = {
            "path": self.path,
            "ref": self.ref,
            "warnings": self.warnings,
        }
        if self.slug:
            value["slug"] = self.slug
        if self.creation is not None:
            value["creation"] = self.creation
        if self.identity_decision is not None:
            value["identity_decision"] = self.identity_decision
        if self.vocabulary_resolution is not None:
            value["vocabulary_resolution"] = self.vocabulary_resolution
        return value


@dataclass
class LinkError(Exception):
    code: str
    missing: list[str]
    reason: str
    candidates: list[dict[str, str]] | None = None
    # Present when the refusal can be overridden by an explicit `distinct`
    # decision bound to exactly these candidates.
    candidate_fingerprint: str | None = None

    def as_dict(self) -> dict:
        value: dict[str, object] = {
            "code": self.code,
            "missing": self.missing,
            "reason": self.reason,
        }
        if self.candidates:
            value["candidates"] = self.candidates
        if self.candidate_fingerprint is not None:
            value["candidate_fingerprint"] = self.candidate_fingerprint
        return value


IDENTITY_DECISION_OUTCOMES = ("distinct",)
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class IdentityPreparation:
    """A shared-name decision point. Nothing was written.

    The requested name already denotes active entities of other types. The
    active agent decides from its own context whether the new identity is
    distinct (and re-submits with an `identity_decision` bound to the
    fingerprint), reuses a candidate, or abstains.
    """

    evidence: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "mutated": False,
            "identity_preparation": self.evidence,
            "identity_decision": "required",
        }


def _identity_decision(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"outcome", "candidate_fingerprint"}
        or value.get("outcome") not in IDENTITY_DECISION_OUTCOMES
        or not isinstance(value.get("candidate_fingerprint"), str)
        or not _FINGERPRINT_RE.fullmatch(value["candidate_fingerprint"])
    ):
        raise LinkError(
            "INVALID_IDENTITY_DECISION",
            ["identity_decision"],
            "identity_decision must be {outcome: 'distinct', candidate_fingerprint: "
            "<the 64-hex fingerprint a preparation or refusal returned>}",
        )
    return {"outcome": value["outcome"], "candidate_fingerprint": value["candidate_fingerprint"]}


def _facet_error(reason: str) -> LinkError:
    return LinkError("INVALID_ENTITY_FACET", ["facets"], reason)


def _validated_facets(
    registry: EntityTypeRegistry,
    definition: EntityTypeDefinition,
    facets: object,
) -> list[tuple[str, str, list[str], bool]]:
    """Check `facets` against the type's declarations; wikilinks stay raw.

    Returns `(name, value_kind, values, multi)` in declaration order. Only
    declared facets are accepted, so a facet can never set a core writer field.
    """
    if facets is None:
        return []
    if not isinstance(facets, dict) or any(not isinstance(key, str) for key in facets):
        raise _facet_error("facets must be an object of declared facet names")
    declared = {facet.name: facet for facet in registry.facets_for(definition.id)}
    undeclared = sorted(set(facets) - set(declared))
    if undeclared:
        raise LinkError(
            "ENTITY_FACET_UNDECLARED",
            ["facets"],
            f"entity_type {definition.id!r} declares no facet {undeclared}; "
            f"declared: {sorted(declared)}. Declare it in the registry's `facets` first.",
        )
    out: list[tuple[str, str, list[str], bool]] = []
    for name, facet in declared.items():
        if name not in facets:
            continue
        raw = facets[name]
        if facet.cardinality == "single":
            if not isinstance(raw, str):
                raise _facet_error(f"facet {name!r} takes one string value")
            values = [raw]
        else:
            if (
                not isinstance(raw, list)
                or not 0 < len(raw) <= MAX_FACET_VALUES
                or any(not isinstance(item, str) for item in raw)
            ):
                raise _facet_error(
                    f"facet {name!r} takes a list of 1-{MAX_FACET_VALUES} string values"
                )
            values = list(raw)
        cleaned: list[str] = []
        for item in values:
            value = item.strip()
            if not value or len(value) > MAX_FACET_TEXT_CHARS or "\n" in value or "\r" in value:
                raise _facet_error(
                    f"facet {name!r} values are single-line, 1-{MAX_FACET_TEXT_CHARS} characters"
                )
            if facet.value == "date":
                try:
                    parsed = dt.date.fromisoformat(value)
                except ValueError:
                    parsed = None
                if parsed is None or parsed.isoformat() != value:
                    raise _facet_error(f"facet {name!r} takes YYYY-MM-DD dates")
            elif facet.value == "wikilink":
                value = value.removeprefix("[[").removesuffix("]]").strip()
                if not value or "[" in value or "]" in value:
                    raise _facet_error(f"facet {name!r} takes wikilink targets")
            if value not in cleaned:
                cleaned.append(value)
        out.append((name, facet.value, cleaned, facet.cardinality == "multi"))
    return out


def _entity_exists_reason(vault_root: Path, rel_entity: str) -> str:
    """The occupied-destination refusal, naming no path to a restricted writer.

    For a writer other than the owner the destination may be occupied by a
    page it may not see; the refusal is then the same whatever occupies it.
    """
    from .governance import egress

    if egress.caller_restricted(vault_root):
        return (
            "an entity page already exists at this name's path. Entities are "
            "create-only via `link`; use `replace` to supersede."
        )
    return (
        f"{rel_entity!r} already exists. Entities are create-only via `link`; "
        "use `replace` to supersede."
    )


# ---------------- validation ----------------


@dataclass
class _Err:
    code: str
    missing: list[str]
    reason: str


def _validate(
    *, entity_type: str, name: str, summary: str, decision_status: str | None
) -> _Err | None:
    missing: list[str] = []
    reasons: list[str] = []
    if not name or not name.strip():
        missing.append("name")
        reasons.append("name is empty")
    if not summary or not summary.strip():
        missing.append("summary")
        reasons.append("summary is empty")
    if entity_type == "decision" and decision_status is not None:
        if decision_status not in DECISION_STATUS_VALUES:
            return _Err(
                code="INVALID_LINK",
                missing=["decision_status"],
                reason=(
                    f"decision_status {decision_status!r} not valid. "
                    f"Valid: {list(DECISION_STATUS_VALUES)}"
                ),
            )
    if missing:
        return _Err(code="INVALID_LINK", missing=missing, reason="; ".join(reasons))
    return None


# ---------------- name + path sanitization ----------------


_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str) -> str:
    """Strip filesystem-reserved chars from an entity name while preserving
    Title Case and spaces (which Obsidian filenames allow on Windows)."""
    cleaned = _INVALID_NAME_CHARS.sub("", name.strip())
    # Collapse repeated whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Unnamed"


# ---------------- render ----------------


def _render_entity(
    *,
    entity_type: str,
    name: str,
    summary: str,
    why_in_kb: str | None,
    date_iso: str,
    tags: list[str],
    connections: list[str],
    affiliation: str | None,
    relationship: str | None,
    domain: str | None,
    language: str | None,
    repo: str | None,
    license: str | None,
    used_in: list[str] | None,
    decided: str | None,
    project: str | None,
    decision_status: str | None,
    exomem_id: str,
    definition: EntityTypeDefinition,
    facets: list[tuple[str, list[str], bool]] | None = None,
    aliases: list[str] | None = None,
    origin_metadata: str | None = None,
) -> str:
    lines = ["---"]
    lines.append("type: entity")
    lines.append(f"exomem_id: {exomem_id}")
    lines.append(f"title: {yaml_scalar(name)}")
    lines.append(f"entity_type: {entity_type}")
    lines.append("status: active")
    lines.append(f"created: {date_iso}")
    lines.append(f"updated: {date_iso}")
    if aliases:
        lines.append("aliases: [" + ", ".join(yaml_scalar(alias) for alias in aliases) + "]")

    optional_values = _entity_writer_optional_values(
        affiliation=affiliation,
        relationship=relationship,
        domain=domain,
        language=language,
        repo=repo,
        license=license,
        used_in=used_in,
        decided=decided,
        project=project,
        decision_status=decision_status,
    )
    for field in definition.optional_frontmatter:
        value = optional_values.get(field)
        if not value:
            continue
        if isinstance(value, list):
            lines.append(f"{field}: [" + ", ".join(value) + "]")
        else:
            lines.append(f"{field}: {value}")
    # Declared facets, already validated and normalized: (name, values, multi).
    for field, values, multi in facets or ():
        if multi:
            lines.append(f"{field}: [" + ", ".join(yaml_scalar(v) for v in values) + "]")
        else:
            lines.append(f"{field}: {yaml_scalar(values[0])}")

    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    else:
        lines.append("tags: []")
    lines.append("---")
    lines.append("")
    if origin_metadata is not None:
        lines.extend([origin_metadata, ""])
    lines.append(f"# {name}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(summary.strip())
    if why_in_kb and why_in_kb.strip():
        lines.append("")
        lines.append("## Why in the KB")
        lines.append("")
        lines.append(why_in_kb.strip())
    if connections:
        lines.append("")
        lines.append("## Relations")
        lines.append("")
        for c in connections:
            lines.append(f"- relates_to [[{c}]]")
    lines.append("")
    return "\n".join(lines)


def _entity_writer_optional_values(
    *,
    affiliation: str | None = None,
    relationship: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
) -> dict[str, str | list[str] | None]:
    """Map the entity writer's supported optional values from one field registry."""
    values: tuple[str | list[str] | None, ...] = (
        affiliation,
        relationship,
        domain,
        language,
        repo,
        license,
        used_in,
        decided,
        project,
        decision_status,
    )
    return dict(
        zip(
            ENTITY_WRITER_OPTIONAL_FRONTMATTER,
            values,
            strict=True,
        )
    )


# ---------------- helpers ----------------


def _normalize_connections(
    connections: list[str] | None,
    *,
    vault_root: Path,
    resolver: WikilinkResolver,
) -> tuple[list[str], list[str]]:
    """Canonicalize each connection wikilink to full vault-rooted form.

    Returns (canonical_connections, warnings). Same fall-through behaviour
    as `note._normalize_sources`: unresolved targets pass through with a
    warning so forward refs aren't blocked.
    """
    if not connections:
        return [], []
    visible = writer_link_visibility(vault_root)
    out: list[str] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for c in connections:
        c = (c or "").strip()
        if not c:
            continue
        canonical, warning = normalize_wikilink(
            c, vault_root, resolver=resolver, strict=False, visible=visible
        )
        if warning:
            warnings.append(warning)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out, warnings


def _clean_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        norm = str(t).strip().lower().replace(" ", "-").replace("_", "-")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out



#: Capture-time aliases share the learned-name bounds: at most this many per
#: page, each at most `MAX_ALIAS_CHARS` code points.
MAX_ALIASES = 8
MAX_ALIAS_CHARS = 64


def _clean_aliases(name: str, aliases: list[str] | None) -> list[str]:
    """The owner's alternate names for a new entity, in the order given.

    Another spelling of the name, in any script: a Japanese user's name for a
    page titled in English is what lets a Japanese turn reach it. Each is
    stripped; a repeat, or the name itself, by identity key (NFKC, casefold,
    collapsed spaces) or by the activation index's `normalize` is dropped. An
    empty one, one spanning lines, one over `MAX_ALIAS_CHARS`, or more than
    `MAX_ALIASES` refuses the whole write.
    """
    if not aliases:
        return []
    from .working_set_index import normalize

    out: list[str] = []
    seen = {entity_candidates.identity_key(name), normalize(name)}
    for raw in aliases:
        alias = str(raw).strip()
        if not alias or "\n" in alias or "\r" in alias or len(alias) > MAX_ALIAS_CHARS:
            raise LinkError(
                "INVALID_LINK",
                ["aliases"],
                f"alias {raw!r} must be one line of 1-{MAX_ALIAS_CHARS} characters",
            )
        keys = {entity_candidates.identity_key(alias), normalize(alias)}
        if keys & seen:
            continue
        seen |= keys
        out.append(alias)
    if len(out) > MAX_ALIASES:
        raise LinkError(
            "INVALID_LINK", ["aliases"], f"at most {MAX_ALIASES} aliases per entity"
        )
    return out

def _activity_summary(
    *,
    rel_entity_no_ext: str,
    name: str,
    entity_type: str,
    domain: str | None,
    project: str | None,
) -> str:
    path_part = rel_entity_no_ext.replace(kb_prefix(), "")
    modifier_parts: list[str] = [entity_type]
    if entity_type == "concept" and domain:
        modifier_parts.append(domain)
    if entity_type == "decision" and project:
        modifier_parts.append(project)
    modifier = ", ".join(modifier_parts)
    return (
        f"`{path_part}` ({modifier}, mobile via exomem) — \"{name}\""
    )


def _log_entry_body(
    *,
    entity_type: str,
    name: str,
    domain: str | None,
    project: str | None,
    decision_status: str | None,
    tags: list[str],
) -> str:
    parts: list[str] = []
    parts.append(
        f"Mobile link via exomem. entity_type={entity_type}. \"{name}\"."
    )
    if entity_type == "concept" and domain:
        parts.append(f"domain={domain}.")
    if entity_type == "decision":
        if project:
            parts.append(f"project={project}.")
        if decision_status:
            parts.append(f"decision_status={decision_status}.")
    if tags:
        parts.append(f"tags: {tags}.")
    return " ".join(parts)


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_path: str, body: str
) -> str:
    """Insert `## [<date>] link | <kb-relative-path>` after the `---` separator."""
    title = rel_path.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] link | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    if new_entry in text:
        return text
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def link(
    vault_root: Path,
    *,
    entity_type: str,
    name: str,
    slug: str | None = None,
    summary: str,
    why_in_kb: str | None = None,
    tags: list[str] | None = None,
    connections: list[str] | None = None,
    affiliation: str | None = None,
    relationship: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    repo: str | None = None,
    license: str | None = None,
    used_in: list[str] | None = None,
    decided: str | None = None,
    project: str | None = None,
    decision_status: str | None = None,
    identity_decision: dict | None = None,
    facets: dict | None = None,
    aliases: list[str] | None = None,
    today: dt.date | None = None,
    validate_only: bool = False,
) -> LinkResult | IdentityPreparation:
    """Create an entity through detached structural preflight.

    A name that already denotes active entities only of OTHER types returns a
    non-mutating `IdentityPreparation`; a same-type match stays ENTITY_EXISTS
    or ENTITY_AMBIGUOUS. Either commits only with an explicit `distinct`
    `identity_decision` bound to the current candidate fingerprint.
    """
    registry = load_entity_types(vault_root)
    definition = registry.resolve(entity_type)
    if definition is None:
        raise LinkError(
            "ENTITY_TYPE_UNKNOWN",
            ["entity_type"],
            f"entity_type {entity_type!r} is not active. Active ids: {list(registry.active_ids)}",
        )
    requested_type = entity_type
    entity_type = definition.id
    slug_warnings: list[str] = []
    filename_slug: str | None = None
    if slug is not None:
        try:
            filename_slug, slug_warnings = resolve_filename_slug(name, slug)
        except InvalidSlugError as error:
            raise LinkError("INVALID_SLUG", ["slug"], str(error)) from error
    origin_block = None
    if isinstance(summary, str):
        try:
            summary, origin_block = origin_bindings.extract_origin_metadata(summary)
        except provenance.OriginError as error:
            raise LinkError(error.code, [], error.reason) from error
    err = _validate(
        entity_type=entity_type,
        name=name,
        summary=summary,
        decision_status=decision_status,
    )
    if err is not None:
        raise LinkError(err.code, err.missing, err.reason)
    declared_facets = _validated_facets(registry, definition, facets)
    from . import find as find_module
    from . import project_keys as project_keys_module

    try:
        key_plan = project_keys_module.plan_project_keys(
            vault_root,
            [project] if entity_type == "decision" and project else [],
        )
    except project_keys_module.ProjectKeyTypoError as error:
        raise LinkError("PROJECT_KEY_TYPO", ["project"], str(error)) from error
    except ValueError as error:
        raise LinkError("INVALID_LINK", ["project"], str(error)) from error
    now = today or temporal.now()
    date_iso = temporal.render_date(now)
    stamp_iso = temporal.stamp(now)
    identity = memory_refs.new_id()
    display_name = name.strip()
    decision = _identity_decision(identity_decision)
    identity_resolution = entity_candidates.resolve_entity_candidate(
        vault_root, name=display_name
    )
    candidates = list(identity_resolution["candidates"])
    fingerprint = entity_candidates.candidate_fingerprint(
        name=display_name, entity_type=entity_type, resolution=identity_resolution
    )
    aliases_clean = _clean_aliases(display_name, aliases)
    # An alias is a name the page answers to: one any other page already
    # answers to would make a turn naming it resolve both. One lookup for all
    # of them, read as the resolver reads names (`claimed_names`).
    claimed = entity_candidates.claimed_names(vault_root, aliases_clean) if aliases_clean else {}
    # One decision covers the title and every claimed alias in this write: its
    # fingerprint binds the union of their claimants.
    if claimed:
        fingerprint = entity_candidates.claim_set_fingerprint(
            vault_root, list(claimed), title_resolution=identity_resolution
        )
    decision_covers = (
        decision is not None
        and decision["candidate_fingerprint"] == fingerprint
        and (identity_resolution["status"] != "no_match" or bool(claimed))
    )
    accepted_decision: dict | None = None
    if decision is not None and not decision_covers:
        raise LinkError(
            "STALE_IDENTITY_DECISION",
            ["identity_decision"],
            "what this name and its aliases resolve to changed since the decision; "
            "decide again against the returned candidates",
            candidates,
            None if identity_resolution["status"] == "no_match" and not claimed else fingerprint,
        )
    if decision_covers:
        distinct_from = [str(item.get("ref") or item["path"]) for item in candidates]
        for found in claimed.values():
            distinct_from.extend(path for path in found if path not in distinct_from)
        accepted_decision = {**decision, "distinct_from": distinct_from}
    elif identity_resolution["status"] != "no_match":
        same_type = identity_resolution["omitted_candidate_count"] or any(
            item["entity_type"] == entity_type for item in candidates
        )
        if not same_type:
            return IdentityPreparation(
                {
                    "name": display_name,
                    "entity_type": entity_type,
                    "candidates": candidates,
                    "omitted_candidate_count": identity_resolution["omitted_candidate_count"],
                    "candidate_fingerprint": fingerprint,
                    "outcomes": list(IDENTITY_DECISION_OUTCOMES),
                }
            )
        if identity_resolution["status"] == "match":
            raise LinkError(
                "ENTITY_EXISTS",
                ["name"],
                "an active entity already has this exact title or alias; update or link "
                "it instead, or pass identity_decision {outcome: distinct} with this "
                "candidate_fingerprint if it is a different identity",
                candidates,
                fingerprint,
            )
        raise LinkError(
            "ENTITY_AMBIGUOUS",
            ["name"],
            "the exact title or alias matches multiple active entities; reconcile the "
            "identity first, or pass identity_decision {outcome: distinct} with this "
            "candidate_fingerprint if it is a different identity",
            candidates,
            fingerprint,
        )
    if claimed and not decision_covers:
        alias = next(iter(claimed))
        raise LinkError(
            "ENTITY_EXISTS",
            ["aliases"],
            f"another page already answers to the alias {alias!r}; pass "
            "identity_decision {outcome: distinct} with this candidate_fingerprint if "
            "it is a different identity",
            [{"alias": name, "path": path} for name, found in claimed.items() for path in found],
            fingerprint,
        )
    folder = kb_root(vault_root) / "Entities" / definition.folder
    entity_path = folder / f"{filename_slug or _sanitize_name(name)}.md"
    # Re-spell the destination to the real on-disk casing *before* it is bound
    # into the draft token: creating into an existing but differently-cased
    # entity folder would otherwise record the minted spelling as the page's
    # identity path.
    rel_entity = canonical_vault_rel(
        vault_root, entity_path.relative_to(vault_root).as_posix()
    )
    if entity_path.exists():
        reason = _entity_exists_reason(vault_root, rel_entity)
        if accepted_decision is not None:
            reason += " A distinct identity needs its own `slug`."
        raise LinkError("ENTITY_EXISTS", ["name"], reason)
    type_resolution = vocabulary_resolution.entity_type_record(
        requested_type,
        definition,
        fingerprint=registry.fingerprint,
        destination=rel_entity.rsplit("/", 2)[-2],
    )
    rel_entity_no_ext = rel_entity.removesuffix(".md")
    resolver = find_module.writer_resolver_snapshot(vault_root)
    resolver.add_pending(rel_entity_no_ext, title=display_name)
    connections_norm, connection_warnings = _normalize_connections(
        connections, vault_root=vault_root, resolver=resolver
    )
    facet_values: list[tuple[str, list[str], bool]] = []
    for facet_name, value_kind, values, multi in declared_facets:
        if value_kind == "wikilink":
            targets, target_warnings = _normalize_connections(
                values, vault_root=vault_root, resolver=resolver
            )
            connection_warnings.extend(target_warnings)
            values = [f"[[{render_wikilink_target(item, vault_root)}]]" for item in targets]
        facet_values.append((facet_name, values, multi))
    summary_clean, summary_warnings = normalize_body_wikilinks(
        summary, vault_root, resolver=resolver
    )
    why_clean: str | None = None
    why_warnings: list[str] = []
    if why_in_kb:
        why_clean, why_warnings = normalize_body_wikilinks(
            why_in_kb, vault_root, resolver=resolver
        )
    source = _render_entity(
        entity_type=entity_type,
        name=display_name,
        summary=summary_clean,
        why_in_kb=why_clean,
        date_iso=stamp_iso,
        tags=_clean_tags(tags),
        connections=[
            render_wikilink_target(item, vault_root) for item in connections_norm
        ],
        affiliation=affiliation,
        relationship=relationship,
        domain=domain,
        language=language,
        repo=repo,
        license=license,
        used_in=used_in,
        decided=decided,
        project=project,
        decision_status=decision_status,
        exomem_id=identity,
        definition=definition,
        facets=facet_values,
        aliases=aliases_clean,
        origin_metadata=origin_block,
    )
    registrations = tuple(
        semantic_writes.DraftRegistration(item.key, item.category, item.folder)
        for item in key_plan.introductions
    )
    token = semantic_writes.DraftToken(
        "link",
        "create",
        rel_entity,
        date_iso,
        registrations,
        render_stamp=stamp_iso,
    ).encode()
    try:
        preflight = semantic_writes.preflight_creation(
            vault_root,
            path=rel_entity,
            source=source,
            operation="create",
            writer="link",
            draft_id=identity,
            draft_token=token,
            registrations=registrations,
        )
    except semantic_writes.SemanticWriteError as error:
        raise LinkError(error.code, [], error.reason) from error
    warnings = (
        list(slug_warnings)
        + list(connection_warnings)
        + list(summary_warnings)
        + list(why_warnings)
        + tag_variants.advise_authored(vault_root, _clean_tags(tags))
    )
    auxiliary: list[PlannedWrite] = list(key_plan.writes)
    derived_auxiliaries: list[tuple[str, PlannedWrite]] = []
    kb = kb_root(vault_root)
    activity = _activity_summary(
        rel_entity_no_ext=rel_entity_no_ext,
        name=display_name,
        entity_type=entity_type,
        domain=domain,
        project=project,
    )
    top_index = kb / "index.md"
    if top_index.is_file():
        top_text, top_guard = read_guarded_text(vault_root, top_index)
        new_top, _ = indexes._prepend_recent_activity(
            top_text, date_iso=date_iso, summary=activity
        )
        sub_writes, counted_top = indexes.compute_subindex_writes(
            vault_root,
            top_index_text=new_top,
            pending_paths=[rel_entity_no_ext],
            include_unchanged=True,
        )
        index_writes = [PlannedWrite(top_index, counted_top or new_top, guard=top_guard), *sub_writes]
        auxiliary.extend(index_writes)
        derived_auxiliaries.extend(("index", write) for write in index_writes)
    else:
        warnings.append(f"{kb_prefix()}index.md missing; skipped Recent activity bump")
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=stamp_iso,
            op="link",
            rel_path_no_ext=rel_entity_no_ext,
            body=_log_entry_body(
                entity_type=entity_type,
                name=display_name,
                domain=domain,
                project=project,
                decision_status=decision_status,
                tags=_clean_tags(tags),
            ),
            operation_token=token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise LinkError(
            "LOG_PLAN_CONFLICT", [], "entity log update could not be planned safely"
        ) from error
    auxiliary.extend(log_plan.writes)
    derived_auxiliaries.extend(("operation-log", write) for write in log_plan.writes)
    if log_plan.warning is not None:
        warnings.append(log_plan.warning)
    if log_plan.rotation_note is not None:
        warnings.append(log_plan.rotation_note)
    if validate_only:
        return LinkResult(
            rel_entity,
            memory_refs.memory_ref(identity),
            warnings,
            preflight.as_dict(),
            slug=filename_slug or "",
            identity_decision=accepted_decision,
            vocabulary_resolution=type_resolution,
        )
    committed = semantic_writes.commit_creation(
        vault_root,
        preflight=preflight,
        auxiliary_writes=tuple(auxiliary),
        derived_auxiliary_writes=tuple(derived_auxiliaries),
        operation="create",
    )
    return LinkResult(
        rel_entity,
        memory_refs.memory_ref(identity),
        warnings,
        committed.as_dict(),
        slug=filename_slug or "",
        identity_decision=accepted_decision,
        vocabulary_resolution=type_resolution,
    )
