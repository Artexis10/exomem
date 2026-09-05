"""D3 reports/latency.py + R3: no cross-provider latency COMPARISON is
publishable from this host (design.md #10, 4b.40) -- every provider's own
number still renders, individually labelled "indicative"; two or more
distinct providers additionally carry the withheld marker, but never lose a
provider's own row (operational-quality-bench/spec.md:30-41: "latency on
such hosts renders per-provider as indicative-only"), and per the "Latency
column refused" scenario's other half, no table column or header is ever
SHARED across providers -- each gets its own heading and its own table.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from protocol.models import MemoryBenchLatency


def _host_unvalidated() -> MemoryBenchLatency:
    return MemoryBenchLatency(publishable=False, reason="host_unvalidated")


def test_zero_providers_render_nothing() -> None:
    from reports.latency import render_indicative_latency

    assert render_indicative_latency([]) == ""


def test_a_single_provider_renders_with_the_word_indicative() -> None:
    from reports.latency import ProviderLatency, render_indicative_latency

    rendered = render_indicative_latency(
        [ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=12.5)]
    )
    assert "indicative" in rendered
    assert "12.5" in rendered
    assert "exomem" in rendered
    assert "aggregate" not in rendered.lower()


def test_r3_two_providers_never_share_a_latency_ms_column() -> None:
    """R3 + H1: operational-quality-bench/spec.md's "Latency column refused"
    scenario, BOTH halves -- "no cross-provider latency column renders AND
    each latency cell carries the indicative-only label". Neither provider's
    own figure is dropped, but no table row-set (no shared `latency_ms`
    header) is ever populated by more than one provider."""

    from reports.latency import WITHHELD_LATENCY, ProviderLatency, render_indicative_latency

    rendered = render_indicative_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=12.5),
            ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=930.0),
        ]
    )
    assert WITHHELD_LATENCY in rendered
    assert "### exomem" in rendered
    assert "### basic-memory" in rendered
    assert "indicative (host_unvalidated)" in rendered

    # H1's core assertion: the `latency_ms` header is never shared -- each
    # provider owns its own table, so the header line appears once PER
    # provider, never once for both.
    assert rendered.count("| latency_ms | disposition |") == 2

    # And structurally: each provider's own number lives ONLY inside its own
    # block, never reachable by reading down a column that also carries the
    # other provider's rows.
    exomem_block = rendered.split("### exomem", 1)[1].split("### basic-memory", 1)[0]
    basic_memory_block = rendered.split("### basic-memory", 1)[1]
    assert "12.5" in exomem_block and "930" not in exomem_block
    assert "930" in basic_memory_block and "12.5" not in basic_memory_block


def test_three_providers_get_exactly_one_withheld_marker_and_three_own_tables() -> None:
    from reports.latency import WITHHELD_LATENCY, ProviderLatency, render_indicative_latency

    rendered = render_indicative_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
            ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=2.0),
            ProviderLatency(provider="supermemory", latency=_host_unvalidated(), value_ms=3.0),
        ]
    )
    assert rendered.count(WITHHELD_LATENCY) == 1
    assert rendered.count("indicative (host_unvalidated)") == 3
    # One own table per provider -- the header is never shared, so it
    # appears exactly as many times as there are distinct providers.
    assert rendered.count("| latency_ms | disposition |") == 3
    for provider in ("exomem", "basic-memory", "supermemory"):
        assert f"### {provider}" in rendered


def test_repeated_observations_from_one_provider_are_not_treated_as_cross_provider() -> None:
    """Two samples from the SAME provider are still one provider: only the
    identity set matters, not the observation count -- both rows land in
    that ONE provider's own table, no withheld marker."""

    from reports.latency import WITHHELD_LATENCY, ProviderLatency, render_indicative_latency

    rendered = render_indicative_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=2.0),
        ]
    )
    assert "indicative" in rendered
    assert WITHHELD_LATENCY not in rendered
    assert rendered.count("### exomem") == 1
    assert rendered.count("| latency_ms | disposition |") == 1
    assert "| 1 | indicative (host_unvalidated) |" in rendered
    assert "| 2 | indicative (host_unvalidated) |" in rendered


def test_a_missing_value_ms_renders_as_not_available() -> None:
    """reports/consolidate.py's own memorybench-export.v1.json source never
    carries a millisecond figure (see ProviderLatency's docstring) -- the
    cell must say so honestly rather than fabricate a number."""

    from reports.latency import ProviderLatency, render_indicative_latency

    rendered = render_indicative_latency(
        [ProviderLatency(provider="exomem", latency=_host_unvalidated())]
    )
    assert "### exomem" in rendered
    assert "| n/a | indicative (host_unvalidated) |" in rendered


def test_f2_the_publishable_flag_is_load_bearing_not_decorative() -> None:
    """F2: a number renders WITHOUT the indicative label only if
    `latency.publishable` is True. Unreachable via the real model today
    (`MemoryBenchLatency.publishable` is pinned `Literal[False]` -- pydantic
    refuses to construct one with `publishable=True`), so this exercises the
    branch with a duck-typed stand-in carrying the same `.publishable` /
    `.reason` shape, proving the code actually reads the flag rather than
    hardcoding "indicative" for every row."""

    from pydantic import ValidationError
    from reports.latency import ProviderLatency, render_indicative_latency

    with pytest.raises(ValidationError):  # sanity: the real model truly forbids this
        MemoryBenchLatency(publishable=True, reason="host_unvalidated")

    validated_stand_in = SimpleNamespace(publishable=True, reason="host_validated")
    rendered = render_indicative_latency(
        [ProviderLatency(provider="exomem", latency=validated_stand_in, value_ms=5.0)]  # type: ignore[arg-type]
    )
    assert "indicative" not in rendered
    assert "| 5 | host_validated |" in rendered


def test_withheld_latency_pins_against_the_real_membench_constant() -> None:
    """reports/ deliberately does not import membench.reporting at module
    scope (it transitively reaches membench.judge.backends' lazily-imported
    httpx, see test_reports_import_closure.py); this pins the duplicated
    string against the value membench actually renders so drift is caught."""

    from membench.reporting import WITHHELD_LATENCY as membench_withheld
    from reports.latency import WITHHELD_LATENCY as reports_withheld

    assert reports_withheld == membench_withheld


def test_the_module_never_touches_a_real_socket() -> None:
    """RM8-style offline proof, scoped to this module: nothing in it must
    require a network round trip to compute a refusal."""

    original = socket.socket.connect

    def _fail_if_called(self, address):  # type: ignore[no-untyped-def]
        raise AssertionError(f"reports.latency touched the network: connect({address!r})")

    socket.socket.connect = _fail_if_called
    try:
        from reports.latency import ProviderLatency, render_indicative_latency

        render_indicative_latency(
            [
                ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
                ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=2.0),
            ]
        )
    finally:
        socket.socket.connect = original
