"""The evaluation that decides the activation packet's default: arms A, M and P.

Runs the frozen protocol of ``openspec/changes/compile-context-maps-and-profiles/
design.md`` ("## Evaluation"): real headless sessions over per-arm state built
from a vault copy, a blind judge pass, and the decision statistic with its
power step. The stages and their order are in ``__main__``.
"""
