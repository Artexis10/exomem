"""Cross-provider latency refusal (design.md #10; 4b.40 standing policy).

No cross-provider latency COMPARISON is publishable from this host: the GPU
is unusable here, so nothing on it can stand next to another provider's
measured latency as if the two were on equal footing. That is narrower than
"hide the numbers": per operational-quality-bench/spec.md's own scenario,
"latency on such hosts renders per-provider as indicative-only" -- every
provider's own figure always renders, individually labelled. What refuses is
a COMPARATIVE construct: two or more providers add the withheld marker
alongside their (still-rendered) individual rows, never a replacement for
them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from protocol.models import MemoryBenchLatency

#: Pinned against benchmarks/membench/reporting.py::WITHHELD_LATENCY by
#: tests/test_reports_latency.py::test_withheld_latency_pins_against_the_real_membench_constant.
#: reports/ deliberately does not import membench.reporting at module scope:
#: it transitively reaches `from membench.judge.backends import
#: parse_judge_verdict`, and backends.py:486 has its own lazily-imported
#: `import httpx` ("lazy: offline flows must not require the dependency") --
#: exactly the kind of module tests/test_reports_import_closure.py refuses to
#: let benchmarks/reports/ reach. Duplicating the literal string here, pinned
#: by a test, keeps the closure clean without a runtime import edge.
WITHHELD_LATENCY = "withheld: transport asymmetry (4b.40)"


@dataclass(frozen=True)
class ProviderLatency:
    """One provider's own latency observation.

    `value_ms` is optional: the one real on-disk source this lane reads today
    (memorybench-export.v1.json's run-level `latency` object -- see
    reports/consolidate.py::_latency_observation) carries only the
    publishable/reason flag, never a millisecond figure -- membench's actual
    numbers live in its own per-run view (benchmarks/membench/reporting.py's
    `_RunView.latencies`), a lane this package does not read. `value_ms`
    exists so a caller that DOES have a number (a future membench-backed
    source) can supply one; absent, the cell renders `n/a`.
    """

    provider: str
    latency: MemoryBenchLatency
    value_ms: float | None = None


def _row(item: ProviderLatency) -> str:
    value = "n/a" if item.value_ms is None else f"{item.value_ms:g}"
    # Load-bearing on the flag, not decorative: MemoryBenchLatency.publishable
    # is pinned Literal[False] today (memorybench/export.py:1220 always emits
    # `{"publishable": False, "reason": "host_unvalidated"}"), so this branch
    # is unreached by any artifact this lane can currently read -- but the
    # code must still ask, not assume, since a future validated host would
    # flip it and a number should then render bare, not mislabelled.
    if item.latency.publishable:
        disposition = str(item.latency.reason)
    else:
        disposition = f"indicative ({item.latency.reason})"
    return f"| {item.provider} | {value} | {disposition} |"


def assert_no_cross_provider_latency(observations: Sequence[ProviderLatency]) -> str:
    """Render one indicative-only row per provider; never a comparative column.

    Zero observations render nothing. Every provider's own number always
    renders -- refusing to publish it at all would be losing real data 4b.40
    never asked to hide. Two or more DISTINCT providers additionally append
    membench's own withheld marker (never replacing the per-provider rows)
    because nothing on this host may present two providers' numbers as
    directly comparable.
    """

    if not observations:
        return ""
    lines = [
        "## Latency",
        "",
        "| provider | latency_ms | disposition |",
        "| --- | --- | --- |",
        *(_row(item) for item in observations),
    ]
    if len({item.provider for item in observations}) > 1:
        lines.extend(["", WITHHELD_LATENCY])
    lines.append("")
    return "\n".join(lines)


__all__ = ["ProviderLatency", "WITHHELD_LATENCY", "assert_no_cross_provider_latency"]
