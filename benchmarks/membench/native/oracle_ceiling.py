"""Identity 'renderer' for the oracle ceiling: no translation, full parity.

Every other renderer rewrites the neutral corpus into a product's grammar, and
its parity report is the honest accounting of what that translation lost. The
ceiling has no grammar to translate into — it reads the canonical corpus
directly — so its parity report is trivially complete, and saying so explicitly
is the point rather than a formality.

That matters for reading a ceiling run. If the ceiling under-scores a
dimension, this report rules out translation loss as the cause: nothing was
dropped on the way in, so the gap is in the scorer, the corpus, or the shared
answerer. A ceiling with an unexamined renderer would leave that ambiguous,
which is exactly the confusion the per-contender parity reports exist to end.

Registering it is not a formality either. `_NATIVE_RENDERERS` hands any
unregistered provider an EMPTY directory, so an unregistered ceiling would
retrieve nothing and read as a catastrophic result while measuring only the
omission — the defect found on 2026-08-05, when the map held `exomem-local`
alone.
"""

from __future__ import annotations

from pathlib import Path

from membench.native import CorpusView, FactParityReport, ParityStatus, corpus_facts


def render(view: CorpusView, out_dir: Path) -> FactParityReport:
    # The directory is created but stays empty: the adapter reads `corpus_dir`,
    # not `native_dir`. Creating it keeps the runner's contract uniform.
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    report = FactParityReport(renderer="oracle-ceiling")
    for fact_id in corpus_facts(view):
        report.record(fact_id, ParityStatus.REPRESENTED)
    return report
