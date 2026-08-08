"""Basic Memory native rendering: frontmatter + observations + [[relations]].

Grammar per the product's documented note format: YAML frontmatter with
title/type/permalink/tags, ``- [category] fact #tag`` observation lines, and
``- relation_type [[Target]]`` relation lines. Supersession and aliases have
no typed primitive there; they are recorded degraded/unsupported, never
silently dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

from membench.ids import slugify
from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts

_ASCII_ALNUM = re.compile(r"[a-z0-9]")


def _identity_stem(name: str, identifier: str) -> str:
    """Filename/permalink stem that stays unique for non-Latin names.

    ``slugify`` drops every non-ASCII character, so a fully non-Latin name
    (the cross-lingual family) collapses to its shared fallback stem: all such
    notes would overwrite one another and share one permalink — the identity
    key in this product's grammar — silently losing facts. Falling back to the
    record's own ASCII id keeps identity intact; Latin names are unaffected.
    """

    if _ASCII_ALNUM.search(name.lower()):
        return slugify(name)
    return slugify(identifier)


def _claim_sentence(view: CorpusView, claim_id: str) -> tuple[str, str]:
    claim = next(c for c in view.claims if c.claim_id == claim_id)
    entity = view.entities_by_id()[claim.subject]
    value = claim.object.value + (f" {claim.object.unit}" if claim.object.unit else "")
    return entity.canonical_name, f"{claim.predicate.replace('_', ' ')} is {value}"


def _conclusion_stem(conclusion) -> str:
    """Permalink stem for a compiled conclusion.

    Uses the conclusion id rather than the title: two conclusions can share a
    title whenever two entities share a canonical name (task 4b.32 counts 18
    such names in seed-1), and a colliding permalink is identity loss in this
    product's grammar — one note silently overwriting another.
    """

    return f"{_identity_stem(conclusion.title, conclusion.conclusion_id)}-{conclusion.conclusion_id[-8:].lower()}"


def render_conclusions(view: CorpusView, out_dir: Path, report: FactParityReport) -> None:
    """Write the compile plan as Basic Memory notes.

    Grammar fit, stated honestly per relation kind:

    - ``cites`` and ``disputes`` are typed relation lines, which is a native
      fit — this product has arbitrary relation types.
    - ``supersedes`` is written as a relation too, but recorded DEGRADED: the
      product has no lifecycle primitive, so the older note is not demoted or
      hidden the way a supersession-aware store would. Matching how the
      existing renderer already treats claim supersession.
    """

    if not view.conclusions:
        return
    by_id = {c.conclusion_id: c for c in view.conclusions}
    source_stems = {
        source.source_id: f"{slugify(source.title)}-{source.source_id[-8:].lower()}"
        for source in view.sources
    }

    for conclusion in view.conclusions:
        stem = _conclusion_stem(conclusion)
        lines = [
            "---",
            f"title: {conclusion.title}",
            "type: conclusion",
            f"permalink: {stem}",
            "tags: [conclusion]",
            "---",
            "",
            f"# {conclusion.title}",
            "",
            "## Observations",
            f"- [conclusion] {conclusion.body} #conclusion",
            "",
            "## Relations",
        ]
        for source_id in conclusion.cites:
            target = source_stems.get(source_id)
            if target is None:
                continue
            lines.append(f"- cites [[{target}]]")
            report.record(
                f"conclusion-cites:{conclusion.conclusion_id}:{source_id}",
                ParityStatus.REPRESENTED,
            )
        for other in conclusion.disputes:
            partner = by_id.get(other)
            if partner is None:
                continue
            lines.append(f"- disputes [[{_conclusion_stem(partner)}]]")
            report.record(
                f"conclusion-disputes:{conclusion.conclusion_id}:{other}",
                ParityStatus.REPRESENTED,
            )
        if conclusion.supersedes:
            partner = by_id.get(conclusion.supersedes)
            if partner is not None:
                lines.append(f"- supersedes [[{_conclusion_stem(partner)}]]")
            report.record(
                f"conclusion-supersedes:{conclusion.conclusion_id}:{conclusion.supersedes}",
                ParityStatus.DEGRADED,
                "relation written, but no lifecycle primitive: the superseded "
                "note is not demoted or hidden",
            )
        (out_dir / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def render(
    view: CorpusView, out_dir: Path, *, altitude: str = "raw_source"
) -> FactParityReport:
    report = FactParityReport(renderer="basic-memory")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entities_by_id = view.entities_by_id()

    for entity in view.entities:
        stem = _identity_stem(entity.canonical_name, entity.entity_id)
        note = [
            "---",
            f"title: {entity.canonical_name}",
            f"type: {entity.kind}",
            f"permalink: {stem}",
            f"tags: [{entity.domain}]",
            "---",
            "",
            f"# {entity.canonical_name}",
            "",
        ]
        (out_dir / f"{stem}.md").write_text("\n".join(note) + "\n", encoding="utf-8")

    claims_by_source: dict[str, list[str]] = {}
    for claim in view.claims:
        for assertion in claim.assertions:
            claims_by_source.setdefault(assertion.source_id, []).append(claim.claim_id)

    binary_sources: set[str] = set()
    for source in view.sources:
        body, is_text = view.ingestable_text(source)
        if not is_text:
            binary_sources.add(source.source_id)
        lines = [
            "---",
            f"title: {source.title}",
            "type: note",
            f"permalink: {_identity_stem(source.title, source.source_id)}",
            f"tags: [{source.authority.value}]",
            "---",
            "",
            body.rstrip(),
            "",
            "## Observations",
        ]
        related: dict[str, None] = {}
        for claim_id in claims_by_source.get(source.source_id, []):
            subject, sentence = _claim_sentence(view, claim_id)
            lines.append(f"- [fact] {subject} {sentence} #bench")
            related.setdefault(subject)
            if source.source_id in binary_sources:
                report.record(
                    f"assert:{claim_id}:{source.source_id}",
                    ParityStatus.DEGRADED,
                    "source is a binary artifact; the observation line restates the "
                    "fact but the original content is not text-ingested",
                )
            else:
                report.record(
                    f"assert:{claim_id}:{source.source_id}", ParityStatus.REPRESENTED
                )
        if related:
            lines.append("")
            lines.append("## Relations")
            lines.extend(f"- relates_to [[{name}]]" for name in related)
        (out_dir / f"{slugify(source.title)}-{source.source_id[-8:].lower()}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    for claim in view.claims:
        if claim.supersedes:
            report.record(
                f"supersedes:{claim.claim_id}:{claim.supersedes}",
                ParityStatus.DEGRADED,
                "no supersession primitive; update rides as prose in the newer note",
            )
    for entity in view.entities:
        for alias in entity.aliases:
            report.record(
                f"alias:{entity.entity_id}:{alias}",
                ParityStatus.UNSUPPORTED,
                "no alias field in the note frontmatter grammar",
            )

    if altitude == "compiled":
        render_conclusions(view, out_dir, report)

    missing = report.missing(corpus_facts(view))
    if missing:
        raise ValueError(f"basic-memory renderer dropped facts: {missing}")
    _ = entities_by_id
    return report
