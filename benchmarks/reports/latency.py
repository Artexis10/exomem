"""Cross-provider latency refusal (design.md #10; 4b.40 standing policy).

No cross-provider latency COMPARISON is publishable from this host: the GPU
is unusable here, so nothing on it can stand next to another provider's
measured latency as if the two were on equal footing. That is narrower than
"hide the numbers": per operational-quality-bench/spec.md's own scenario,
"latency on such hosts renders per-provider as indicative-only" -- every
provider's own figure always renders, individually labelled. What refuses is
a COMPARATIVE construct: with two or more distinct providers, no table cell
or column may ever be shared across providers -- each gets its OWN one-row
table under its OWN heading, so a reader cannot read two providers' numbers
down one column even though both remain visible -- plus the withheld marker
in place of any comparative construct.
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


def _disposition(item: ProviderLatency) -> str:
    # Load-bearing on the flag, not decorative: MemoryBenchLatency.publishable
    # is pinned Literal[False] today (memorybench/export.py:1220 always emits
    # `{"publishable": False, "reason": "host_unvalidated"}"), so this branch
    # is unreached by any artifact this lane can currently read -- but the
    # code must still ask, not assume, since a future validated host would
    # flip it and a number should then render bare, not mislabelled.
    if item.latency.publishable:
        return str(item.latency.reason)
    return f"indicative ({item.latency.reason})"


def _provider_block(provider: str, items: Sequence[ProviderLatency]) -> str:
    """A single provider's OWN heading and OWN one-row-per-observation table.

    No column here is ever shared with another provider's block: the
    provider name lives in the heading, not in a row of a table other
    providers also populate, so nothing can be read down a shared column.
    """

    lines = [
        f"### {provider}",
        "",
        "| latency_ms | disposition |",
        "| --- | --- |",
    ]
    for item in items:
        value = "n/a" if item.value_ms is None else f"{item.value_ms:g}"
        lines.append(f"| {value} | {_disposition(item)} |")
    return "\n".join(lines)


def render_indicative_latency(observations: Sequence[ProviderLatency]) -> str:
    """Render one single-provider latency block per distinct provider.

    Zero observations render nothing. Every provider's own number always
    renders -- refusing to publish it at all would be losing real data 4b.40
    never asked to hide -- but with two or more DISTINCT providers, no value
    column ever spans providers: each provider gets its own heading and its
    own one-row(s) table (operational-quality-bench/spec.md's "Latency
    column refused" scenario, both halves -- no cross-provider column AND
    each cell individually indicative-only), plus membench's own withheld
    marker in place of any comparative construct.
    """

    if not observations:
        return ""
    by_provider: dict[str, list[ProviderLatency]] = {}
    for item in observations:
        by_provider.setdefault(item.provider, []).append(item)

    sections = ["## Latency", ""]
    sections.append(
        "\n\n".join(_provider_block(provider, items) for provider, items in by_provider.items())
    )
    if len(by_provider) > 1:
        sections.extend(["", WITHHELD_LATENCY])
    sections.append("")
    return "\n".join(sections)


__all__ = ["ProviderLatency", "WITHHELD_LATENCY", "render_indicative_latency"]
