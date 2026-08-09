"""Neutral 'renderer': the generated corpus itself; every fact represented."""

from __future__ import annotations

from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts


def render(view: CorpusView) -> FactParityReport:
    report = FactParityReport(renderer="neutral")
    for fact_id in corpus_facts(view):
        report.record(fact_id, ParityStatus.REPRESENTED)
    return report
