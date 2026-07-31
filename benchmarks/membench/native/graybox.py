"""Gray Box native rendering: a raw capture stream (inbox altitude).

Without the LLM ``organize`` pass Gray Box holds captures as immutable inbox
markdown; the benchmark profile searches ``search_all`` over that raw text,
so typed facts are honestly degraded to raw-text representation.
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts


def render(view: CorpusView, out_dir: Path) -> FactParityReport:
    report = FactParityReport(renderer="graybox")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    captures = [
        {
            "week": source.recorded_week,
            "source_id": source.source_id,
            "text": f"{source.title}\n\n{view.source_text(source)}",
        }
        for source in view.sources
    ]
    (out_dir / "captures.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False, sort_keys=True) for c in captures) + "\n",
        encoding="utf-8",
    )
    for fact_id in corpus_facts(view):
        report.record(
            fact_id,
            ParityStatus.DEGRADED,
            "raw inbox capture; typed extraction requires the LLM organize pass, "
            "which the deterministic profile does not run",
        )
    return report
