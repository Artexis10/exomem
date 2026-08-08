"""Exomem native rendering: an ordered capture-op stream for the adapter.

Track B v0.1 keeps ingestion altitude equal across products: sources go in as
raw captured text (``capture_source``), so typed lifecycle facts are honestly
``degraded`` here exactly as they are for the other contenders. That was a
deliberate fairness choice, and it stands as the ``raw_source`` altitude.

What it also does — unstated until 4b.35 measured it — is make provenance and
contradiction structurally unmeasurable: a citation chain is a compiled
conclusion declaring its sources, and with nothing compiled there is no chain
to score. The fix is not to abandon the fairness choice but to add a second
declared altitude where every contender compiles, which is what the ``compiled``
altitude and this module's ``render_conclusions`` provide.
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts
from membench.schema import ScheduleOp, load_jsonl


def render_conclusions(view: CorpusView, ops: list[dict], report: FactParityReport) -> None:
    """Append the compile plan as `remember` ops, after the captures it cites.

    Ordering is load-bearing: exomem's ``remember(sources=[...])`` writes
    ``ingested_into:`` back onto each cited source, so a conclusion authored
    before its sources exist would have nothing to link to. The capture stream
    is emitted first for exactly that reason.

    Supersession is expressed with ``replace_memory`` rather than a second
    ``remember`` — a superseded conclusion must be demoted, not merely
    contradicted by a newer note.
    """

    if not view.conclusions:
        return
    seq = max((op["seq"] for op in ops), default=-1)
    for conclusion in sorted(view.conclusions, key=lambda c: c.sort_key):
        seq += 1
        ops.append(
            {
                "week": 11,
                "seq": seq,
                "op": "remember",
                "conclusion_id": conclusion.conclusion_id,
                "title": conclusion.title,
                "content": conclusion.body,
                "cites": list(conclusion.cites),
                "supersedes": conclusion.supersedes,
            }
        )
        for source_id in conclusion.cites:
            report.record(
                f"conclusion-cites:{conclusion.conclusion_id}:{source_id}",
                ParityStatus.REPRESENTED,
            )
        for other in conclusion.disputes:
            # No dispute primitive: exomem detects contradictions over the
            # corpus rather than accepting a declared one, so the edge is not
            # authored. Recorded degraded, never dropped — whether detection
            # finds the pair is precisely what the dimension measures.
            report.record(
                f"conclusion-disputes:{conclusion.conclusion_id}:{other}",
                ParityStatus.DEGRADED,
                "no authored dispute edge; contradiction detection is expected "
                "to surface the pair from the compiled corpus itself",
            )
        if conclusion.supersedes:
            report.record(
                f"conclusion-supersedes:{conclusion.conclusion_id}:{conclusion.supersedes}",
                ParityStatus.REPRESENTED,
            )


def render(
    view: CorpusView, out_dir: Path, *, altitude: str = "raw_source"
) -> FactParityReport:
    report = FactParityReport(renderer="exomem")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    schedule = load_jsonl(ScheduleOp, view.root / "schedule.jsonl")
    sources_by_id = {s.source_id: s for s in view.sources}

    ops = []
    binary_sources: set[str] = set()
    for op in schedule:
        if op.source_id and op.source_id in sources_by_id:
            source = sources_by_id[op.source_id]
            content, is_text = view.ingestable_text(source)
            if not is_text:
                binary_sources.add(source.source_id)
            ops.append(
                {
                    "week": op.week,
                    "seq": op.seq,
                    "op": "capture_source",
                    "source_id": source.source_id,
                    "title": source.title,
                    "source_type": "other",
                    "content": content,
                }
            )
    if altitude == "compiled":
        render_conclusions(view, ops, report)
    (out_dir / "capture-ops.jsonl").write_text(
        "\n".join(json.dumps(op, ensure_ascii=False, sort_keys=True) for op in ops) + "\n",
        encoding="utf-8",
    )

    for claim in view.claims:
        for assertion in claim.assertions:
            if assertion.source_id in binary_sources:
                report.record(
                    f"assert:{claim.claim_id}:{assertion.source_id}",
                    ParityStatus.DEGRADED,
                    "asserted only inside a binary artifact; media pipeline not "
                    "exercised in this profile, so the fact is not text-reachable",
                )
            else:
                report.record(
                    f"assert:{claim.claim_id}:{assertion.source_id}", ParityStatus.REPRESENTED
                )
        if claim.supersedes:
            report.record(
                f"supersedes:{claim.claim_id}:{claim.supersedes}",
                ParityStatus.DEGRADED,
                "ingested as raw source text; replace_memory supersession is a Track D "
                "workflow, not a Track B ingestion primitive",
            )
    for entity in view.entities:
        for alias in entity.aliases:
            report.record(
                f"alias:{entity.entity_id}:{alias}",
                ParityStatus.DEGRADED,
                "alias appears only in raw source text; no entity page authored at ingest",
            )

    missing = report.missing(corpus_facts(view))
    if missing:
        raise ValueError(f"exomem renderer dropped facts: {missing}")
    return report
