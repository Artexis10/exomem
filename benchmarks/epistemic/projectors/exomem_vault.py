"""Read-only projector over an exomem vault directory.

Public file surfaces only: the Markdown pages and their YAML frontmatter, plus
the documented ``## Relations`` section. No database, no index, no server, no
private API — the projector reads what any text editor would show, which is the
same standard every competitor projector is held to.

The mapping and its documentation, field by field, is in
:data:`FIELD_DECLARATIONS`. Every entry cites a repository-relative
``path:line`` in exomem's own authored documentation, dereferenced by
``tests/test_epistemic_projector.py``. Two entries are deliberately
``available_via:<mechanism>`` rather than ``declared``:

- ``review_state`` — the canonical decisions file is
  ``Knowledge Base/.review-state.json``, keyed by review-item identity and
  signal fingerprint rather than by page path, so this build reads a page-level
  ``review_state`` key where a vault carries one and declares the real
  mechanism instead of pretending the frontmatter is canonical.
- ``uncertainty`` — there is no ``uncertainty`` frontmatter field by design
  (numeric confidence is an explicit non-field); unresolved knowledge is
  written under an ``## Open threads`` heading, which is what this projector
  reads.
- ``kind`` — an entity page with ``entity_type: decision`` is a documented
  settled decision surface, projected without relying on its folder name.

What is *not* projected is recorded in :data:`COMPLETENESS_NOTES` rather than
silently dropped.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ..snapshot import (
    CollectionItem,
    CollectionProjection,
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    Relation,
    StateItem,
)
from .base import Projector, module_code_line_count, module_line_count

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---\s*?(?:\r?\n|\Z)", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_RELATION_RE = re.compile(
    r"^\s*[-*+]\s+(?P<rel>[a-z][a-z0-9_]{1,60})\s+\[\[(?P<target>[^\[\]\n]+)\]\]\s*$"
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?P<text>\S.*?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\[\]\n]+)\]\]")

#: exomem page ``type`` -> neutral snapshot kind. Types the projector does not
#: recognize fall back to the folder rule in :func:`_kind_for`.
TYPE_TO_KIND: Mapping[str, str] = {
    "source": "raw_source",
    "research-note": "derived_inference",
    "insight": "claim",
    "pattern": "claim",
    "failure": "claim",
    "experiment": "hypothesis",
    "production-log": "container",
    "entity": "container",
}

#: Every branch in :func:`_kind_for` is grounded in the documented public
#: frontmatter/page-type surface. The values are immutable because this is
#: fairness evidence, not an extension point for a scenario or provider run.
KIND_MAPPING_EVIDENCE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "source": ("src/exomem/_scaffold/_Schema/references/page-types.md:17",),
        "research-note": ("src/exomem/_scaffold/_Schema/references/page-types.md:57",),
        "insight": ("src/exomem/_scaffold/_Schema/references/page-types.md:102",),
        "pattern": ("src/exomem/_scaffold/_Schema/references/page-types.md:196",),
        "failure": ("src/exomem/_scaffold/_Schema/references/page-types.md:149",),
        "experiment": ("src/exomem/_scaffold/_Schema/references/page-types.md:246",),
        "production-log": ("src/exomem/_scaffold/_Schema/references/page-types.md:364",),
        "entity": ("src/exomem/_scaffold/_Schema/references/page-types.md:461",),
        "entity:decision": (
            "src/exomem/_scaffold/_Schema/references/page-types.md:550",
            "src/exomem/_scaffold/_Schema/references/page-types.md:551",
        ),
        "sources_fallback": ("src/exomem/_scaffold/_Schema/references/page-types.md:14",),
    }
)

#: exomem page ``status`` -> neutral currency.
STATUS_TO_CURRENCY: Mapping[str, str] = {
    "draft": "yes",
    "active": "yes",
    "planned": "yes",
    "recorded": "yes",
    "edited": "yes",
    "published": "yes",
    "reflected": "yes",
    "superseded": "no",
    "archived": "no",
    "dropped": "no",
}

#: ``## Relations`` bullet type -> neutral predicate. Unmapped types are skipped
#: and counted in the completeness notes.
RELATION_TO_PREDICATE: Mapping[str, str] = {
    "supports": "supports",
    "contradicts": "contradicts",
    "supersedes": "supersedes",
    "derived_from": "derived_from",
    "evidenced_by": "evidenced_by",
    "cites": "cites",
    "depends_on": "depends_on",
    "raises_question": "raises_question",
    "answers": "answers",
    "relates_to": "relates_to",
}

#: Body predicates that also count as an evidence hop for ``StateItem.cites``.
_EVIDENCE_PREDICATES = frozenset({"cites", "derived_from", "evidenced_by"})

OPEN_THREADS_HEADING = "open threads"
RELATIONS_HEADING = "relations"

COMPLETENESS_NOTES = (
    "Public file surfaces only: Markdown pages, YAML frontmatter, and the "
    "documented '## Relations' section. Not projected: search ranking, the "
    "semantic index, governance receipts, Evidence artifact payloads, and "
    "review-queue ordering — none of which is observable from files alone."
)

#: One declaration per mapped field. Evidence is exomem's own authored
#: documentation, cited as repository-relative path:line and dereferenced by the
#: projector test suite.
FIELD_DECLARATIONS: tuple[FieldDeclaration, ...] = (
    FieldDeclaration(
        field="kind",
        status="declared",
        evidence=KIND_MAPPING_EVIDENCE["entity:decision"][1],
    ),
    FieldDeclaration(
        field="current",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/frontmatter.md:17",
    ),
    FieldDeclaration(
        field="revision_of",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/supersession.md:37",
    ),
    FieldDeclaration(
        field="prior_revision",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/supersession.md:70",
    ),
    FieldDeclaration(
        field="cites",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/frontmatter.md:59",
    ),
    FieldDeclaration(
        field="contradicts",
        status="declared",
        evidence="src/exomem/core-relations.yaml:4",
    ),
    FieldDeclaration(
        field="supports",
        status="declared",
        evidence="src/exomem/core-relations.yaml:3",
    ),
    FieldDeclaration(
        field="authored_by",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/page-types.md:14",
    ),
    FieldDeclaration(
        field="locator",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/frontmatter.md:153",
    ),
    FieldDeclaration(
        field="open_question",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/references/page-types.md:91",
    ),
    FieldDeclaration(
        field="uncertainty",
        status="available_via:open_threads_section",
        evidence="src/exomem/_scaffold/_Schema/references/frontmatter.md:144",
    ),
    FieldDeclaration(
        field="review_state",
        status="available_via:review_queue_state_file",
        evidence="docs/epistemic-inbox.md:45",
    ),
    FieldDeclaration(
        field="external_edit",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/SKILL.md:134",
    ),
    FieldDeclaration(
        field="export",
        status="declared",
        evidence="src/exomem/_scaffold/_Schema/SKILL.md:134",
    ),
    # Added for the 2026-08 no-nudge amendment (§7, sequence 2). Three of these
    # four are declared *unavailable* on purpose: the no-nudge surfaces do not
    # exist in the product yet, and that is precisely why f20-f22 are filed as
    # expected-red falsification targets. Declaring them observable to make a
    # family go green would be the exact fraud the fairness contract exists to
    # prevent; declaring them absent_by_design would be worse still, because
    # exomem has not decided they should never exist.
    FieldDeclaration(
        field="signal",
        status="available_via:review_queue_attention_mode",
        evidence="docs/epistemic-inbox.md:32",
    ),
    FieldDeclaration(
        field="dismissal",
        status="available_via:review_state_file",
        evidence="docs/epistemic-inbox.md:45",
    ),
    FieldDeclaration(
        field="due_state_counters",
        # Flipped from `unavailable` by the nag-governance slice: the emission
        # governor used to be per-process memory no projector could read, and
        # the projection file now persists the write and emission counts.
        status="available_via:due_state_file",
        evidence="docs/epistemic-inbox.md:160",
    ),
    FieldDeclaration(
        field="continuation_packet",
        status="unavailable",
        evidence="openspec/specs/agent-bootstrap-contract/spec.md:11",
    ),
)

#: The documented triage store. Decisions live here, not in note frontmatter,
#: so the dismissal projection reads it rather than inferring from pages.
REVIEW_STATE_FILE = ".review-state.json"
#: The maintained due-state projection, carrying the persisted emission ledger.
DUE_STATE_FILE = ".due-state.json"

#: ``surface name -> (projection status, why)`` for the four surfaces a quiet
#: assertion must prove absence on. Only ``review_queue`` has a file surface at
#: all, and only when the triage store exists; the rest are computed
#: server-side or do not exist, and saying so is what makes an unprojected
#: surface an *error* rather than silence a product can be credited with.
UNPROJECTABLE_SURFACES: Mapping[str, str] = {
    "audit_findings": "governance receipts are not a file surface; see completeness notes",
    "proposal_queue": "relation/compile proposals are computed server-side, not stored as files",
}

#: ``audit/attention category -> the neutral signal class it belongs to``.
#: `entity_recurrence` is the only registered category that proposes an
#: *identity* rather than a page defect, and the neutral schema's
#: entity-candidate class is the one f21 is answerable for. Everything else the
#: sweep produces is still projected, carrying its own category, so absence is
#: proven over the whole sweep rather than over a filtered view of it.
CATEGORY_SIGNAL_CLASSES: Mapping[str, str] = MappingProxyType(
    {"entity_recurrence": "entity_candidate"}
)

#: The product read paths `runtime_surfaces` adds. Declared, because a verdict
#: reached through a documented read endpoint must never be mistaken for one the
#: file surface produced.
RUNTIME_ENDPOINTS: tuple[str, ...] = (
    "exomem.audit.audit(vault)",
    "exomem.attention.attention(vault, state=all, record_surfacing=False)",
    "exomem.curation.work_item(vault, review_ref)",
    "exomem.due_state.recompute(vault)",
)

#: Why the due-state counters surface reports nothing on a vault that has none.
NO_DUE_STATE_LEDGER = (
    f"{DUE_STATE_FILE} carries no emission ledger; nothing has been counted or emitted"
)


#: The triage store records the *verb* a person used; the neutral schema names
#: the resulting *state*. Without the mapping a real vault's dismissal projects
#: as ``dismiss``, which is in none of the schema's review-state vocabularies,
#: so a genuine decision reads as no decision at all. The synthetic corpora
#: write the state directly, which is why only a real vault exposed this.
#:
#: There is no ``reopen`` row because there is no such record: reopening CLEARS
#: every record under the item id, so a stored decision can only ever be one of
#: the three below. A row for it would be a claim about the store that is false.
#:
#: ``competing`` maps to ``resolved`` rather than ``conflict`` because the
#: neutral schema treats ``conflict`` as an OPEN state, and a competing-
#: alternatives stance is the opposite: somebody decided, deliberately, that
#: both rivals stand. Projected as ``conflict`` it read as outstanding review
#: work, so a dismissal-respected assertion would see the item as still open.
#: The trade is that the projection no longer says the decision was about a
#: contradiction — which the neutral vocabulary has no closed word for, and
#: which the item's own reasons carry anyway.
ACTION_TO_REVIEW_STATE: Mapping[str, str] = MappingProxyType(
    {
        "dismiss": "dismissed",
        "snooze": "snoozed",
        "competing": "resolved",
    }
)


def _review_state_of(decision: Mapping[str, Any]) -> str:
    """The neutral review state a stored decision means."""

    raw = str(decision.get("action") or decision.get("state") or "").strip()
    return ACTION_TO_REVIEW_STATE.get(raw.casefold(), raw)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    try:
        loaded = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError:
        return {}, text[match.end() :]
    data = loaded if isinstance(loaded, dict) else {}
    return data, text[match.end() :]


def _normalize_link(raw: str) -> str:
    """Any wikilink shape -> the vault-rooted id form used for item ids."""

    value = raw.strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    value = value.split("|", 1)[0]
    value = value.split("#", 1)[0]
    value = value.strip().strip("/")
    if value.lower().endswith(".md"):
        value = value[: -len(".md")]
    return value


def _links(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = value if isinstance(value, list) else [value]
    found: list[str] = []
    for entry in raw_values:
        if not isinstance(entry, str):
            continue
        matches = _WIKILINK_RE.findall(entry)
        targets = matches if matches else [entry]
        for target in targets:
            normalized = _normalize_link(target)
            if normalized and normalized not in found:
                found.append(normalized)
    return tuple(found)


def _sections(body: str) -> dict[str, list[str]]:
    """Body lines grouped by the lowercase heading that introduces them."""

    grouped: dict[str, list[str]] = {}
    current = ""
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            current = heading.group("title").strip().casefold()
            grouped.setdefault(current, [])
            continue
        grouped.setdefault(current, []).append(line)
    return grouped


def _kind_for(page_type: str, entity_type: str, relative: str) -> str:
    if page_type.strip().casefold() == "entity" and entity_type.strip().casefold() == "decision":
        return "decision"
    mapped = TYPE_TO_KIND.get(page_type)
    if mapped is not None:
        return mapped
    return "raw_source" if "/Sources/" in f"/{relative}" else "claim"


def _authorship_for(relative: str) -> str | None:
    parts = relative.split("/")
    if "Sources" in parts:
        return "human"
    if "Notes" in parts:
        return "agent"
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(entry) for entry in value)
    return "" if value is None else str(value)


def _date_of(taken_at: str) -> dt.date:
    """The snapshot's own date. A projection never reads a clock of its own."""

    return dt.date.fromisoformat(taken_at[:10])


def _entity_candidate_identity(item: Any) -> str | None:
    """The recurring identity one attention row is about, or ``None``.

    Read from the row's own `entity_recurrence` reason rather than from its
    anchor path: the anchor is whichever page sorts smallest, and two identities
    routinely share one.
    """

    for reason in item.reasons or ():
        if reason.get("category") != "entity_recurrence":
            continue
        identity = str((reason.get("meta") or {}).get("identity") or "").strip()
        if identity:
            return identity
    return None


def _audit_finding_items(report: Any) -> tuple[StateItem, ...]:
    """One sweep of every registered audit category, projected whole.

    Every finding is projected, not only the signal-bearing ones: absence is
    proven over what the sweep produced rather than over a filtered view of it,
    and a reader can see which categories ran.
    """

    findings = sorted(report.findings, key=lambda row: (row.category, row.path, row.detail))
    projected: list[StateItem] = [
        StateItem(
            id="surface-audit_findings",
            kind="container",
            title="audit_findings",
            text="one read-only audit sweep over every registered category",
            raw={
                "surface": "audit_findings",
                "projection": "complete",
                "findings": str(len(findings)),
                "categories": ",".join(sorted(report.summary)),
            },
        )
    ]
    for index, finding in enumerate(findings):
        meta = finding.meta or {}
        identity = str(meta.get("identity") or "").strip()
        raw = {
            "surface": "audit_findings",
            "category": finding.category,
            "targets": identity or finding.path,
        }
        signal_class = CATEGORY_SIGNAL_CLASSES.get(finding.category)
        if signal_class is not None:
            if not identity:
                # Never silence. A signal-bearing finding that carries no
                # identity would otherwise project as an ordinary page defect,
                # and a quiet assertion would then pass over a candidate the
                # runtime really did raise.
                raise ValueError(
                    f"{finding.category} finding on {finding.path} carries no identity; "
                    "a signal-bearing finding cannot be projected without its subject"
                )
            raw["signal_class"] = signal_class
            raw["identity"] = identity
            raw["candidate_state"] = str(meta.get("candidate_state") or "")
            projected.append(_identity_item(identity, finding))
        projected.append(
            StateItem(
                id=f"audit-{index:03d}-{finding.category}",
                kind="container",
                title=finding.category,
                text=finding.detail,
                raw=raw,
            )
        )
    return tuple(projected)


def _identity_item(identity: str, finding: Any) -> StateItem:
    """The recurring identity itself, carrying the counts the runtime measured.

    The distinct-*origin* count is what rides `source_count`, never the page or
    occurrence count: f21's acceptance predicate says "distinct sources, never
    occurrence counts", and a corpus that repeats one name on one page is the
    case the whole family exists to keep quiet.

    An identity reaches the snapshot only once the audit has produced a finding
    for it, because that is the only place the runtime names one. A subject with
    no finding therefore evaluates `unsupported` rather than `pass` — not a
    silence anything is credited with, and not the twin's proof either: the
    twin's silence is established by the absence meta-predicate over four
    completely projected surfaces, which needs no item at all.
    """

    meta = finding.meta or {}
    facets = int(meta.get("facet_count") or 0)
    return StateItem(
        id=identity,
        kind="container",
        title=str(meta.get("candidate") or identity),
        text=finding.detail,
        current="yes",
        raw={
            "source_count": str(int(meta.get("origin_count") or 0)),
            "page_count": str(int(meta.get("page_count") or 0)),
            "facet_count": str(facets),
            "reusable_facts": "yes" if facets else "no",
            "candidate_state": str(meta.get("candidate_state") or ""),
        },
    )


def _review_queue_items(report: Any) -> tuple[StateItem, ...]:
    """Every registered review queue at ``state="all"``, default union included.

    `entity_recurrence` is registered but deliberately outside the default
    union until f21's own acknowledgment gate admits it, so a projection that
    read only the default surface would report a candidate as absent while the
    agent's explicit read returns it. Both are read, and each row records which
    of the two carried it.
    """

    from exomem.attention import DEFAULT_ATTENTION_CATEGORIES

    default = frozenset(DEFAULT_ATTENTION_CATEGORIES)
    rows = sorted(report.items, key=lambda row: (str(row.item_id), row.path))
    projected: list[StateItem] = [
        StateItem(
            id="surface-review_queue",
            kind="container",
            title="review_queue",
            text="every registered attention category, read at state=all",
            raw={
                "surface": "review_queue",
                "projection": "complete",
                "items": str(len(rows)),
            },
        )
    ]
    for row in rows:
        identity = _entity_candidate_identity(row)
        # Delivery describes the REASON the row's signal came from, never the
        # fused row. The runtime fuses every finding that shares an anchor page
        # into one item, so an `entity_recurrence` reason routinely rides a row
        # whose other reason IS in the default union — and reading the label off
        # the fused row's categories reported the candidate as delivered by the
        # unfiltered daily read, which design D9 says it is not, and which the
        # default read demonstrably does not do.
        categories = (
            ("entity_recurrence",)
            if identity is not None
            else tuple(str(reason.get("category") or "") for reason in row.reasons or ())
        )
        raw = {
            "surface": "review_queue",
            "categories": " ".join(row.categories),
            "signal_categories": " ".join(categories),
            "targets": identity or row.path,
            "delivery": "default" if default.intersection(categories) else "explicit_only",
        }
        if identity is not None:
            raw["signal_class"] = CATEGORY_SIGNAL_CLASSES["entity_recurrence"]
            raw["identity"] = identity
        projected.append(
            StateItem(
                id=f"review-{row.item_id}",
                kind="container",
                title=row.path,
                text=row.reasons[0]["detail"] if row.reasons else "",
                review_state=row.state or None,
                raw=raw,
            )
        )
    return tuple(projected)


class VaultProjector(Projector):
    """Project one exomem vault directory into a neutral state snapshot."""

    name = "exomem-vault-file-projector"
    #: 0.2.0 adds the `collections` section. Additive: every field a 0.1.0
    #: snapshot carried is unchanged, and a vault with no collection serialises
    #: byte-for-byte as it did. 0.3.0 adds `CollectionProjection.storage_source`,
    #: read from the manifest's own `storage.source`. Also additive and
    #: default-empty, but the output schema moved, and a snapshot's provenance is
    #: only worth anything if the version tracks what the projector can emit.
    #: 0.4.0 adds the opt-in `runtime_surfaces` projection, which emits signal
    #: and surface items the file-only build cannot produce at all.
    version = "0.4.0"
    author = "benchmark-harness"
    endpoints_used = ("filesystem:walk(vault)", "filesystem:read_text(*.md)")

    def __init__(self, vault_root: Path | str, *, runtime_surfaces: bool = False) -> None:
        self.vault_root = Path(vault_root)
        #: Read the four absence surfaces through the product's documented read
        #: paths instead of from files. Off by default, and the default is the
        #: fair-comparison build: from files alone three of the four surfaces
        #: cannot be projected at all, so every quiet assertion is blocked.
        self.runtime_surfaces = runtime_surfaces
        if runtime_surfaces:
            self.endpoints_used = (*type(self).endpoints_used, *RUNTIME_ENDPOINTS)

    # -- reading -----------------------------------------------------------

    def _pages(self) -> tuple[tuple[str, dict[str, Any], str], ...]:
        pages: list[tuple[str, dict[str, Any], str]] = []
        for path in sorted(self.vault_root.rglob("*.md")):
            if not path.is_file() or path.name == "index.md":
                continue
            relative = path.relative_to(self.vault_root).as_posix()
            frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            pages.append((relative, frontmatter, body))
        return tuple(pages)

    # -- projection --------------------------------------------------------

    def project(self, *, phase: str, taken_at: str) -> EpistemicStateSnapshot:
        pages = self._pages()
        items: dict[str, StateItem] = {}
        relations: list[Relation] = []
        successor_of: dict[str, str] = {}

        for relative, frontmatter, body in pages:
            item_id = relative[: -len(".md")] if relative.endswith(".md") else relative
            sections = _sections(body)
            page_type = str(frontmatter.get("type") or "").strip()
            entity_type = str(frontmatter.get("entity_type") or "")
            status = str(frontmatter.get("status") or "").strip().casefold()

            cites = list(_links(frontmatter.get("sources")))
            body_relations: list[tuple[str, str]] = []
            for line in sections.get(RELATIONS_HEADING, []):
                match = _RELATION_RE.match(line)
                if match is None:
                    continue
                predicate = RELATION_TO_PREDICATE.get(match.group("rel"))
                if predicate is None:
                    continue
                target = _normalize_link(match.group("target"))
                if target:
                    body_relations.append((predicate, target))

            for predicate, target in body_relations:
                relations.append(Relation(subject=item_id, predicate=predicate, object=target))
                if predicate in _EVIDENCE_PREDICATES and target not in cites:
                    cites.append(target)

            for target in _links(frontmatter.get("sources")):
                relations.append(Relation(subject=item_id, predicate="cites", object=target))
            for target in _links(frontmatter.get("ingested_into")):
                relations.append(
                    Relation(subject=target, predicate="derived_from", object=item_id)
                )

            supersedes = _links(frontmatter.get("supersedes"))
            superseded_by = _links(frontmatter.get("superseded_by"))
            for target in supersedes:
                successor_of[item_id] = target
                relations.append(Relation(subject=item_id, predicate="supersedes", object=target))
            for target in superseded_by:
                successor_of.setdefault(target, item_id)
                relations.append(Relation(subject=target, predicate="supersedes", object=item_id))

            contradicts = tuple(
                target for predicate, target in body_relations if predicate == "contradicts"
            )
            supports = tuple(
                target for predicate, target in body_relations if predicate == "supports"
            )

            open_threads = [
                match.group("text")
                for match in (
                    _BULLET_RE.match(line) for line in sections.get(OPEN_THREADS_HEADING, [])
                )
                if match is not None
            ]
            declared_uncertainty = str(frontmatter.get("uncertainty") or "").strip()
            uncertainty = declared_uncertainty or ("; ".join(open_threads) or None)

            retired_reason = None
            if status == "superseded":
                target = superseded_by[0] if superseded_by else "an unnamed successor"
                retired_reason = f"superseded by {target}"
            elif status in {"archived", "dropped"}:
                retired_reason = f"status {status}"

            raw = {
                key: _as_text(frontmatter.get(key))
                for key in (
                    "type",
                    "entity_type",
                    "status",
                    "project",
                    "source_type",
                    "tags",
                    "review_state",
                )
                if frontmatter.get(key) is not None
            }

            items[item_id] = StateItem(
                id=item_id,
                kind=_kind_for(page_type, entity_type, relative),
                title=str(frontmatter.get("title") or "").strip() or item_id.rsplit("/", 1)[-1],
                text="\n".join(line for line in body.splitlines()).strip(),
                current=STATUS_TO_CURRENCY.get(status, "undeclared"),
                retired_reason=retired_reason,
                cites=tuple(cites),
                supports=supports,
                contradicts=contradicts,
                review_state=str(frontmatter.get("review_state") or "").strip() or None,
                authored_by=_authorship_for(relative),
                uncertainty=uncertainty,
                locator=relative,
                locator_kind="file",
                observed_at=_as_text(frontmatter.get("updated")) or None,
                raw=raw,
            )

            for index, thread in enumerate(open_threads, start=1):
                question_id = f"{item_id}#open-thread-{index}"
                items[question_id] = StateItem(
                    id=question_id,
                    kind="open_question",
                    title=thread[:120],
                    text=thread,
                    current=STATUS_TO_CURRENCY.get(status, "undeclared"),
                    cites=(item_id,),
                    authored_by=_authorship_for(relative),
                    uncertainty=thread,
                    locator=f"{relative}#open-thread-{index}",
                    locator_kind="file",
                    observed_at=_as_text(frontmatter.get("updated")) or None,
                    raw={"section": "Open threads"},
                )
                relations.append(
                    Relation(subject=item_id, predicate="raises_question", object=question_id)
                )

        self._apply_revision_chains(items, successor_of)
        for marker in self._project_surfaces(taken_at):
            items[marker.id] = marker

        collections = self._project_collections(pages)
        return EpistemicStateSnapshot(
            provider="exomem",
            variant="native",
            phase=phase,
            taken_at=taken_at,
            items=tuple(items[key] for key in sorted(items)),
            relations=_dedupe(relations),
            declarations=FIELD_DECLARATIONS,
            collections=collections,
            projector=ProjectorMeta(
                name=self.name,
                version=self.version,
                author=self.author,
                endpoints_used=self.endpoints_used,
                loc=module_line_count(VaultProjector),
                loc_code=module_code_line_count(VaultProjector),
            ),
            completeness_notes=COMPLETENESS_NOTES,
        )

    def _project_surfaces(self, taken_at: str) -> tuple[StateItem, ...]:
        """Project the four absence surfaces, and the triage store's dismissals.

        The honest answer for three of the four is "cannot be projected from
        files", and that answer is *load-bearing*: the anti-vacuity
        meta-predicate turns an unprojected surface into an error, so a quiet
        assertion evaluated against a real vault is blocked rather than passing.
        A projector that quietly emitted ``complete`` here would manufacture
        silence the vault never demonstrated.

        ``runtime_surfaces`` does not widen the file surface — it swaps to a
        different, declared one. See :meth:`_project_runtime_surfaces`.
        """

        if self.runtime_surfaces:
            return self._project_runtime_surfaces(taken_at)

        projected: list[StateItem] = []
        for surface, reason in sorted(UNPROJECTABLE_SURFACES.items()):
            projected.append(
                StateItem(
                    id=f"surface-{surface}",
                    kind="container",
                    title=surface,
                    text=reason,
                    raw={"surface": surface, "projection": "unavailable", "reason": reason},
                )
            )

        projected.append(self._project_due_state_counters())

        projection, decisions = self._triage_decisions()
        projected.append(
            StateItem(
                id="surface-review_queue",
                kind="container",
                title="review_queue",
                text=f"{REVIEW_STATE_FILE} triage store",
                raw={"surface": "review_queue", "projection": projection},
            )
        )
        projected.extend(self._dismissal_items(decisions))
        return tuple(projected)

    def _triage_decisions(self) -> tuple[str, dict[str, Any]]:
        """``(projection status, stored decisions)`` from the documented store."""

        triage_path = self._state_file(REVIEW_STATE_FILE)
        decisions: dict[str, Any] = {}
        projection = "unavailable"
        if triage_path is not None:
            loaded = _read_json(triage_path)
            if isinstance(loaded, dict):
                # Schema 2 sections the store; schema 1 was flat. The synthetic
                # corpora write `decisions`. Read all three rather than pinning
                # one, so a projection is not silently empty after a migration.
                for key in ("records", "decisions"):
                    raw_decisions = loaded.get(key)
                    if isinstance(raw_decisions, dict) and raw_decisions:
                        decisions = raw_decisions
                        break
                projection = "complete"
        return projection, decisions

    @staticmethod
    def _dismissal_items(decisions: Mapping[str, Any]) -> tuple[StateItem, ...]:
        projected: list[StateItem] = []
        for target, decision in sorted(decisions.items()):
            if not isinstance(decision, dict):
                continue
            state = _review_state_of(decision)
            fingerprint = str(decision.get("fingerprint") or "").strip()
            if not state or not fingerprint:
                continue
            projected.append(
                StateItem(
                    id=f"dismissal-{target}",
                    kind="container",
                    title=target,
                    text=str(decision.get("why") or ""),
                    review_state=state,
                    raw={
                        "surface": "review_queue",
                        "targets": target,
                        "fingerprint": fingerprint,
                    },
                )
            )
        return tuple(projected)

    # -- the same four surfaces, read from the product's own read paths -----

    def _project_runtime_surfaces(self, taken_at: str) -> tuple[StateItem, ...]:
        """All four absence surfaces, each one actually enumerated.

        The file projection above is the fair-comparison build and it stays the
        default: it blocks every quiet assertion, which is the honest verdict
        for a surface nobody can read from files. This mode reads the *other*
        representation the acceptance predicate already admits — "a documented
        list/read endpoint" (PREREGISTRATION.md §4) — and names each endpoint in
        ``endpoints_used`` so a verdict can never be mistaken for one the file
        surface produced.

        Nothing here writes *vault* state. The audit sweep and the due-state
        recomputation are read-only over the Knowledge Base, the attention read
        passes ``record_surfacing=False`` so projecting a queue never counts as
        having shown it to anybody, and the curation read assembles a work item
        without authoring a plan. ``taken_at`` supplies the date, so the
        projection has no clock of its own.

        It is not inert on disk, and saying otherwise would be a claim the
        filesystem contradicts: reading through the product materialises its
        derived caches — ``.refs.sqlite`` and its siblings — under
        ``EXOMEM_STATE_ROOT``. That is machine-local derived state outside the
        vault, it changes no projected value, and a caller must still point
        ``EXOMEM_STATE_ROOT`` somewhere disposable before projecting.
        """

        from exomem import attention as attention_module
        from exomem import audit as audit_module

        today = _date_of(taken_at)
        audit_report = audit_module.audit(self.vault_root, today=today)
        review_report = attention_module.attention(
            self.vault_root,
            categories=list(attention_module.ATTENTION_CATEGORIES),
            limit=0,
            state="all",
            today=today,
            record_surfacing=False,
        )
        return (
            *_audit_finding_items(audit_report),
            *_review_queue_items(review_report),
            *self._project_proposal_queue(review_report),
            *self._project_runtime_due_state(today),
            *self._dismissal_items(self._triage_decisions()[1]),
        )

    def _project_proposal_queue(self, review_report: Any) -> tuple[StateItem, ...]:
        """Stored governed curation plans, plus every candidate that can become one.

        A proposal queue that only listed already-authored plans would report
        silence on a vault where the agent has not run yet, which is exactly the
        silence a quiet assertion must not be credited with. So both halves are
        enumerated: the plans on disk, and the work item each open entity
        candidate resolves to through the governed curation lane.

        A candidate whose work item refuses to bind is never projected as
        absent. The refusal is recorded and the surface stops reporting
        ``complete``, so the anti-vacuity meta-predicate blocks every quiet
        assertion over the snapshot instead of passing one on a queue nobody
        could read. Swallowing the refusal and continuing would leave the marker
        saying ``complete`` over a queue that lost its entries — the exact cheat
        this predicate exists to catch, one level up.
        """

        from exomem import curation as curation_module

        stored = sorted(self.vault_root.rglob("_Governance/curation/runs/*/plan.json"))
        projected: list[StateItem] = [
            StateItem(
                id=f"proposal-plan-{path.parent.name}",
                kind="container",
                title=path.parent.name,
                text="stored governed curation plan",
                raw={"surface": "proposal_queue", "run_id": path.parent.name},
            )
            for path in stored
        ]
        refusals: list[str] = []
        for item in sorted(review_report.items, key=lambda row: str(row.item_id)):
            identity = _entity_candidate_identity(item)
            if identity is None or not item.ref:
                continue
            try:
                work = curation_module.work_item(self.vault_root, review_ref=item.ref)
            except Exception as error:  # noqa: BLE001 - recorded, never swallowed
                refusals.append(f"{item.item_id} ({type(error).__name__}: {error})")
                continue
            binding = work.get("entity_candidate") or {}
            projected.append(
                StateItem(
                    id=f"proposal-{item.item_id}",
                    kind="container",
                    title=str(binding.get("candidate_state") or ""),
                    text=(
                        "governed curation work item for one recurring identity; "
                        f"allowed step kinds: {work.get('allowed_candidate_step_kinds')}"
                    ),
                    review_state=item.state or None,
                    raw={
                        "surface": "proposal_queue",
                        "signal_class": CATEGORY_SIGNAL_CLASSES["entity_recurrence"],
                        "targets": identity,
                        "identity": identity,
                        "candidate_state": str(binding.get("candidate_state") or ""),
                        "signal_version": str(binding.get("signal_version") or ""),
                    },
                )
            )
        projected.insert(
            0,
            StateItem(
                id="surface-proposal_queue",
                kind="container",
                title="proposal_queue",
                text="stored curation plans and the work item each open candidate binds",
                raw={
                    "surface": "proposal_queue",
                    "projection": "complete" if not refusals else "refused",
                    "stored_plans": str(len(stored)),
                    "candidate_work_items": str(len(projected) - len(stored)),
                    "refusals": str(len(refusals)),
                    "reason": "; ".join(sorted(refusals)[:8]),
                },
            ),
        )
        return tuple(projected)

    def _project_runtime_due_state(self, today: Any) -> tuple[StateItem, ...]:
        """The due-state projection itself, recomputed from canonical state.

        Projected rather than asserted. ``entity_recurrence`` is not one of the
        due-state projection categories, so a recurring identity cannot reach
        this surface — but reading that fact off a constant would be a claim
        about the code, and what a quiet assertion needs is the surface's own
        answer. So the marker names the categories the projection actually
        carried and every entry is projected with the page it is about.
        """

        from exomem import due_state as due_state_module

        projection = due_state_module.recompute(self.vault_root, today=today)
        categories = projection.get("categories") or {}
        projected: list[StateItem] = []
        for category in sorted(categories):
            for path, buckets in sorted((categories[category] or {}).items()):
                for bucket in sorted(buckets or {}):
                    for index, entry in enumerate(buckets[bucket] or []):
                        projected.append(
                            StateItem(
                                id=f"due-{category}-{path}-{bucket}-{index}",
                                kind="container",
                                title=category,
                                text=str(entry.get("detail") or ""),
                                raw={
                                    "surface": "due_state_counters",
                                    "category": category,
                                    "bucket": bucket,
                                    "targets": path,
                                },
                            )
                        )
        marker = {
            "surface": "due_state_counters",
            "projection": "complete",
            "categories": ",".join(sorted(categories)),
            "entries": str(len(projected)),
        }
        ledger = self._due_state_ledger()
        if ledger is not None:
            marker.update(ledger)
        projected.insert(
            0,
            StateItem(
                id="surface-due_state_counters",
                kind="container",
                title="due_state_counters",
                text="due-state projection recomputed from canonical state",
                raw=marker,
            ),
        )
        return tuple(projected)

    def _due_state_ledger(self) -> dict[str, str] | None:
        """The persisted emission ledger, when the vault has one."""

        path = self._state_file(DUE_STATE_FILE)
        payload = _read_json(path) if path is not None else None
        ledger = payload.get("emission") if isinstance(payload, dict) else None
        if not isinstance(ledger, dict):
            return None
        return {
            "writes": str(int(ledger.get("writes") or 0)),
            "emissions": str(int(ledger.get("emissions") or 0)),
            "due_total": str(int(ledger.get("due_total") or 0)),
        }

    def _project_collections(
        self, pages: tuple[tuple[str, dict[str, Any], str], ...]
    ) -> tuple[CollectionProjection, ...]:
        """Structured collections, read from the same page walk as everything else.

        Deliberately file-level, like the rest of this projector: a manifest
        declares `item_schema.natural_key`, an item file declares its own key and
        values, and nothing here calls into the product to interpret either. The
        natural-key VALUES are what the acceptance journey compares across
        providers — an item is "the same deliverable" because its declared key
        says so, not because two systems happened to spell a title alike.
        """
        manifests: dict[str, tuple[str, dict[str, Any]]] = {}
        for relative, frontmatter, _ in pages:
            if str(frontmatter.get("type") or "").strip() != "collection":
                continue
            collection_id = str(frontmatter.get("exomem_id") or "").strip()
            if collection_id:
                manifests[collection_id] = (relative, frontmatter)
        if not manifests:
            return ()
        grouped: dict[str, list[CollectionItem]] = {}
        for relative, frontmatter, _ in pages:
            page_type = str(frontmatter.get("type") or "").strip()
            if page_type not in {"record", "plan"}:
                continue
            collection_id = str(frontmatter.get("collection_id") or "").strip()
            manifest = manifests.get(collection_id)
            if manifest is None:
                continue
            key = str(
                frontmatter.get("record_id") or frontmatter.get("plan_id") or ""
            ).strip()
            if not key:
                continue
            natural_key = _natural_key_names(manifest[1])
            grouped.setdefault(collection_id, []).append(
                CollectionItem(
                    key=key,
                    natural_key={
                        name: _as_text(frontmatter.get(name))
                        for name in natural_key
                        if frontmatter.get(name) is not None
                    },
                    lifecycle=str(frontmatter.get("lifecycle") or "").strip() or None,
                    status=str(frontmatter.get("status") or "").strip() or None,
                )
            )
        projected: list[CollectionProjection] = []
        for collection_id, (relative, frontmatter) in sorted(manifests.items()):
            schema_version = frontmatter.get("schema_version")
            storage = frontmatter.get("storage")
            projected.append(
                CollectionProjection(
                    id=collection_id,
                    storage_source=(
                        str(storage.get("source") or "").strip()
                        if isinstance(storage, Mapping)
                        else ""
                    ),
                    profile=str(frontmatter.get("semantic_profile") or "").strip()
                    or "unknown",
                    manifest=relative,
                    title=str(frontmatter.get("title") or "").strip(),
                    schema_version=(
                        schema_version if isinstance(schema_version, int) and schema_version >= 1 else 1
                    ),
                    natural_key=_natural_key_names(frontmatter),
                    items=tuple(sorted(grouped.get(collection_id, []), key=lambda item: item.key)),
                )
            )
        return tuple(projected)

    def _state_file(self, filename: str) -> Path | None:
        """Resolve one registered portable-derived file through product placement."""

        from exomem import reserved_paths, state_paths

        classification = reserved_paths.classify_logical(filename)
        if classification.descriptor_id not in {"review-state", "due-state"}:
            raise ValueError(f"unregistered projector state file {filename!r}")
        candidate = state_paths.vault_state_dir(self.vault_root) / filename
        return candidate if candidate.is_file() else None

    def _project_due_state_counters(self) -> StateItem:
        """The persisted emission ledger, or an honest absence.

        All three are read, never guessed. `writes` is how many governed writes
        the projection absorbed and `emissions` how many due-state blocks were
        actually delivered; both are CUMULATIVE over the vault's life, so the
        counter-repetition assertion compares them across a snapshot pair rather
        than as a ratio on this one. A projector that guessed either would
        decide the family's verdict.

        `due_total` is the size of the last block a caller was HANDED — one
        definition, one writer, recorded where a block is marked emitted and
        nowhere else. It is informational and gates nothing: it persists past
        the delivery it describes, so it cannot say whether any particular batch
        delivered anything. It is projected because it is real and cheap, not
        because an assertion depends on it — one did, and that is exactly how a
        batch that delivered nothing came to inherit an earlier batch's `pass`.
        """
        path = self._state_file(DUE_STATE_FILE)
        payload = _read_json(path) if path is not None else None
        ledger = payload.get("emission") if isinstance(payload, dict) else None
        if not isinstance(ledger, dict):
            return StateItem(
                id="surface-due_state_counters",
                kind="container",
                title="due_state_counters",
                text=NO_DUE_STATE_LEDGER,
                raw={
                    "surface": "due_state_counters",
                    "projection": "unavailable",
                    "reason": NO_DUE_STATE_LEDGER,
                },
            )
        return StateItem(
            id="surface-due_state_counters",
            kind="container",
            title="due_state_counters",
            text=f"{DUE_STATE_FILE} emission ledger",
            raw={
                "surface": "due_state_counters",
                "projection": "complete",
                "writes": str(int(ledger.get("writes") or 0)),
                "emissions": str(int(ledger.get("emissions") or 0)),
                "due_total": str(int(ledger.get("due_total") or 0)),
            },
        )

    def declarations(self) -> tuple[FieldDeclaration, ...]:
        return FIELD_DECLARATIONS

    # -- lineage -----------------------------------------------------------

    @staticmethod
    def _apply_revision_chains(
        items: dict[str, StateItem], successor_of: Mapping[str, str]
    ) -> None:
        """Stamp ``revision_of``, chain id, and index from supersession edges."""

        for successor, predecessor in successor_of.items():
            if successor in items:
                items[successor] = items[successor].model_copy(
                    update={"revision_of": predecessor}
                )

        for item_id in list(items):
            root = item_id
            seen = {item_id}
            depth = 0
            while True:
                parent = items[root].revision_of if root in items else None
                if not parent or parent in seen:
                    break
                seen.add(parent)
                root = parent
                depth += 1
            if depth == 0 and not any(
                other.revision_of == item_id for other in items.values()
            ):
                continue
            items[item_id] = items[item_id].model_copy(
                update={"revision_chain_id": f"chain:{root}", "revision_index": depth}
            )


def _natural_key_names(frontmatter: Mapping[str, Any]) -> tuple[str, ...]:
    schema = frontmatter.get("item_schema")
    names = schema.get("natural_key") if isinstance(schema, Mapping) else None
    if not isinstance(names, list):
        return ()
    return tuple(str(name) for name in names if isinstance(name, str) and name)


def _dedupe(relations: Iterable[Relation]) -> tuple[Relation, ...]:
    seen: dict[tuple[str, str, str], Relation] = {}
    for relation in relations:
        seen.setdefault((relation.subject, relation.predicate, relation.object), relation)
    return tuple(seen[key] for key in sorted(seen))
