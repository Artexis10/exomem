"""D3 reports/latency.py + R3: no cross-provider latency COMPARISON is
publishable from this host (design.md #10, 4b.40) -- every provider's own
number still renders, individually labelled "indicative"; two or more
distinct providers additionally carry the withheld marker, but never lose a
provider's own row (operational-quality-bench/spec.md:30-41: "latency on
such hosts renders per-provider as indicative-only").
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from protocol.models import MemoryBenchLatency


def _host_unvalidated() -> MemoryBenchLatency:
    return MemoryBenchLatency(publishable=False, reason="host_unvalidated")


def test_zero_providers_render_nothing() -> None:
    from reports.latency import assert_no_cross_provider_latency

    assert assert_no_cross_provider_latency([]) == ""


def test_a_single_provider_renders_with_the_word_indicative() -> None:
    from reports.latency import ProviderLatency, assert_no_cross_provider_latency

    rendered = assert_no_cross_provider_latency(
        [ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=12.5)]
    )
    assert "indicative" in rendered
    assert "12.5" in rendered
    assert "exomem" in rendered
    assert "aggregate" not in rendered.lower()


def test_r3_two_providers_get_a_withheld_marker_but_keep_their_own_rows() -> None:
    """R3 + F1: a run whose export carries host_unvalidated latency for two
    providers must never render a COMPARATIVE column -- but per spec.md's
    own scenario ("latency on such hosts renders per-provider as
    indicative-only"), neither provider's own figure is dropped."""

    from reports.latency import WITHHELD_LATENCY, ProviderLatency, assert_no_cross_provider_latency

    rendered = assert_no_cross_provider_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=12.5),
            ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=930.0),
        ]
    )
    assert WITHHELD_LATENCY in rendered
    assert "| exomem | 12.5 | indicative (host_unvalidated) |" in rendered
    assert "| basic-memory | 930 | indicative (host_unvalidated) |" in rendered
    # No comparative construct: the two never sit inside one juxtaposed cell.
    assert "12.5 | 930" not in rendered
    assert "exomem | basic-memory" not in rendered


def test_three_providers_get_exactly_one_withheld_marker_and_three_rows() -> None:
    from reports.latency import WITHHELD_LATENCY, ProviderLatency, assert_no_cross_provider_latency

    rendered = assert_no_cross_provider_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
            ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=2.0),
            ProviderLatency(provider="supermemory", latency=_host_unvalidated(), value_ms=3.0),
        ]
    )
    assert rendered.count(WITHHELD_LATENCY) == 1
    assert rendered.count("indicative (host_unvalidated)") == 3
    for provider in ("exomem", "basic-memory", "supermemory"):
        assert f"| {provider} |" in rendered


def test_repeated_observations_from_one_provider_are_not_treated_as_cross_provider() -> None:
    """Two samples from the SAME provider are still one provider: only the
    identity set matters, not the observation count."""

    from reports.latency import WITHHELD_LATENCY, ProviderLatency, assert_no_cross_provider_latency

    rendered = assert_no_cross_provider_latency(
        [
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
            ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=2.0),
        ]
    )
    assert "indicative" in rendered
    assert WITHHELD_LATENCY not in rendered


def test_a_missing_value_ms_renders_as_not_available() -> None:
    """reports/consolidate.py's own memorybench-export.v1.json source never
    carries a millisecond figure (see ProviderLatency's docstring) -- the
    cell must say so honestly rather than fabricate a number."""

    from reports.latency import ProviderLatency, assert_no_cross_provider_latency

    rendered = assert_no_cross_provider_latency(
        [ProviderLatency(provider="exomem", latency=_host_unvalidated())]
    )
    assert "| exomem | n/a | indicative (host_unvalidated) |" in rendered


def test_f2_the_publishable_flag_is_load_bearing_not_decorative() -> None:
    """F2: a number renders WITHOUT the indicative label only if
    `latency.publishable` is True. Unreachable via the real model today
    (`MemoryBenchLatency.publishable` is pinned `Literal[False]` -- pydantic
    refuses to construct one with `publishable=True`), so this exercises the
    branch with a duck-typed stand-in carrying the same `.publishable` /
    `.reason` shape, proving the code actually reads the flag rather than
    hardcoding "indicative" for every row."""

    from pydantic import ValidationError
    from reports.latency import ProviderLatency, assert_no_cross_provider_latency

    with pytest.raises(ValidationError):  # sanity: the real model truly forbids this
        MemoryBenchLatency(publishable=True, reason="host_unvalidated")

    validated_stand_in = SimpleNamespace(publishable=True, reason="host_validated")
    rendered = assert_no_cross_provider_latency(
        [ProviderLatency(provider="exomem", latency=validated_stand_in, value_ms=5.0)]  # type: ignore[arg-type]
    )
    assert "indicative" not in rendered
    assert "| exomem | 5 | host_validated |" in rendered


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
        from reports.latency import ProviderLatency, assert_no_cross_provider_latency

        assert_no_cross_provider_latency(
            [
                ProviderLatency(provider="exomem", latency=_host_unvalidated(), value_ms=1.0),
                ProviderLatency(provider="basic-memory", latency=_host_unvalidated(), value_ms=2.0),
            ]
        )
    finally:
        socket.socket.connect = original
