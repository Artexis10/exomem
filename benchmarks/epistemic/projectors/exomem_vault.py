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

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ..snapshot import (
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
            "src/exomem/_scaffold/_Schema/references/page-types.md:542",
            "src/exomem/_scaffold/_Schema/references/page-types.md:543",
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
        status="unavailable",
        evidence="docs/product-gap-matrix.md:100",
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

#: ``surface name -> (projection status, why)`` for the four surfaces a quiet
#: assertion must prove absence on. Only ``review_queue`` has a file surface at
#: all, and only when the triage store exists; the rest are computed
#: server-side or do not exist, and saying so is what makes an unprojected
#: surface an *error* rather than silence a product can be credited with.
UNPROJECTABLE_SURFACES: Mapping[str, str] = {
    "audit_findings": "governance receipts are not a file surface; see completeness notes",
    "proposal_queue": "relation/compile proposals are computed server-side, not stored as files",
    "due_state_counters": "no due-state counters block exists in the product",
}


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


class VaultProjector(Projector):
    """Project one exomem vault directory into a neutral state snapshot."""

    name = "exomem-vault-file-projector"
    version = "0.1.0"
    author = "benchmark-harness"
    endpoints_used = ("filesystem:walk(vault)", "filesystem:read_text(*.md)")

    def __init__(self, vault_root: Path | str) -> None:
        self.vault_root = Path(vault_root)

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
        for marker in self._project_surfaces():
            items[marker.id] = marker

        return EpistemicStateSnapshot(
            provider="exomem",
            variant="native",
            phase=phase,
            taken_at=taken_at,
            items=tuple(items[key] for key in sorted(items)),
            relations=_dedupe(relations),
            declarations=FIELD_DECLARATIONS,
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

    def _project_surfaces(self) -> tuple[StateItem, ...]:
        """Project the four absence surfaces, and the triage store's dismissals.

        The honest answer for three of the four is "cannot be projected from
        files", and that answer is *load-bearing*: the anti-vacuity
        meta-predicate turns an unprojected surface into an error, so a quiet
        assertion evaluated against a real vault is blocked rather than passing.
        A projector that quietly emitted ``complete`` here would manufacture
        silence the vault never demonstrated.
        """

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

        triage_path = self.vault_root / REVIEW_STATE_FILE
        decisions: dict[str, Any] = {}
        projection = "unavailable"
        if triage_path.is_file():
            try:
                loaded = json.loads(triage_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                raw_decisions = loaded.get("decisions")
                decisions = raw_decisions if isinstance(raw_decisions, dict) else {}
                projection = "complete"
        projected.append(
            StateItem(
                id="surface-review_queue",
                kind="container",
                title="review_queue",
                text=f"{REVIEW_STATE_FILE} triage store",
                raw={"surface": "review_queue", "projection": projection},
            )
        )
        for target, decision in sorted(decisions.items()):
            if not isinstance(decision, dict):
                continue
            state = str(decision.get("action") or decision.get("state") or "").strip()
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


def _dedupe(relations: Iterable[Relation]) -> tuple[Relation, ...]:
    seen: dict[tuple[str, str, str], Relation] = {}
    for relation in relations:
        seen.setdefault((relation.subject, relation.predicate, relation.object), relation)
    return tuple(seen[key] for key in sorted(seen))
