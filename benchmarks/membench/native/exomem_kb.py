"""Exomem native rendering: an ordered capture-op stream for the adapter.

Track B v0.1 keeps ingestion altitude equal across products: sources go in as
raw captured text (``capture_source``), so typed lifecycle facts are honestly
``degraded`` here exactly as they are for the other contenders. Compiled-note
workflows are Track D's subject, not Track B ingestion.
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts
from membench.schema import ScheduleOp, load_jsonl


def render(view: CorpusView, out_dir: Path) -> FactParityReport:
    report = FactParityReport(renderer="exomem")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    schedule = load_jsonl(ScheduleOp, view.root / "schedule.jsonl")
    sources_by_id = {s.source_id: s for s in view.sources}

    ops = []
    for op in schedule:
        if op.source_id and op.source_id in sources_by_id:
            source = sources_by_id[op.source_id]
            ops.append(
                {
                    "week": op.week,
                    "seq": op.seq,
                    "op": "capture_source",
                    "source_id": source.source_id,
                    "title": source.title,
                    "source_type": "other",
                    "content": view.source_text(source),
                }
            )
    (out_dir / "capture-ops.jsonl").write_text(
        "\n".join(json.dumps(op, ensure_ascii=False, sort_keys=True) for op in ops) + "\n",
        encoding="utf-8",
    )

    for claim in view.claims:
        for assertion in claim.assertions:
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
