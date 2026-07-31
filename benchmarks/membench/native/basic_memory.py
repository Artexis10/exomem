"""Basic Memory native rendering: frontmatter + observations + [[relations]].

Grammar per the product's documented note format: YAML frontmatter with
title/type/permalink/tags, ``- [category] fact #tag`` observation lines, and
``- relation_type [[Target]]`` relation lines. Supersession and aliases have
no typed primitive there; they are recorded degraded/unsupported, never
silently dropped.
"""

from __future__ import annotations

from pathlib import Path

from membench.ids import slugify
from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts


def _claim_sentence(view: CorpusView, claim_id: str) -> tuple[str, str]:
    claim = next(c for c in view.claims if c.claim_id == claim_id)
    entity = view.entities_by_id()[claim.subject]
    value = claim.object.value + (f" {claim.object.unit}" if claim.object.unit else "")
    return entity.canonical_name, f"{claim.predicate.replace('_', ' ')} is {value}"


def render(view: CorpusView, out_dir: Path) -> FactParityReport:
    report = FactParityReport(renderer="basic-memory")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entities_by_id = view.entities_by_id()

    for entity in view.entities:
        note = [
            "---",
            f"title: {entity.canonical_name}",
            f"type: {entity.kind}",
            f"permalink: {slugify(entity.canonical_name)}",
            f"tags: [{entity.domain}]",
            "---",
            "",
            f"# {entity.canonical_name}",
            "",
        ]
        (out_dir / f"{slugify(entity.canonical_name)}.md").write_text(
            "\n".join(note) + "\n", encoding="utf-8"
        )

    claims_by_source: dict[str, list[str]] = {}
    for claim in view.claims:
        for assertion in claim.assertions:
            claims_by_source.setdefault(assertion.source_id, []).append(claim.claim_id)

    for source in view.sources:
        body = view.source_text(source)
        lines = [
            "---",
            f"title: {source.title}",
            "type: note",
            f"permalink: {slugify(source.title)}",
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
            report.record(f"assert:{claim_id}:{source.source_id}", ParityStatus.REPRESENTED)
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

    missing = report.missing(corpus_facts(view))
    if missing:
        raise ValueError(f"basic-memory renderer dropped facts: {missing}")
    _ = entities_by_id
    return report
