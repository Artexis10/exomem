"""recall_parity: a paired recall comparison between two Exomem MCP endpoints.

The instrument behind one decision: whether daily use can move from a personal
server to a Cloud cell. Four steps, each a subcommand of ``python -m
recall_parity``: ``sample`` real queries into a tuning and a held-out set,
``capture`` both arms through the MCP client over HTTP, write a blinded judge
``packet``, and ``score`` the judgements into a paired nDCG@10 difference with
a bootstrap interval and per-arm latency.

Lives outside ``src/`` and is never shipped. The queries it handles are
private: every artifact goes under one operator-chosen directory outside the
repository (or under the git-ignored ``benchmarks/private/``), and nothing in
this package or its tests contains a real query. Import side effects are
forbidden here so lean tests stay fast.
"""

from __future__ import annotations

__version__ = "0.1.0"
