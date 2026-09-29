"""Real-byte admission and cost probe for the `pair.relation` instrument.

The lean suite collects this file but skips it. The dedicated frozen-verifier CI
job sets `EXOMEM_RUN_REAL_NLI=1` after downloading the pin's exact declared files.
"""

from __future__ import annotations

import os
import resource
import time

import pytest

from exomem import claims, sensing, sensing_nli

pytestmark = [
    pytest.mark.nli,
    # One CPU model load plus the fixture set is about 50 s on one thread.
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        os.environ.get("EXOMEM_RUN_REAL_NLI") != "1",
        reason="real instrument gate requires EXOMEM_RUN_REAL_NLI=1",
    ),
]


def test_the_exact_pin_admits_every_relation_fixture_on_cpu() -> None:
    pin = claims.VERIFIER_PINS[0]
    instrument = sensing_nli.admit()
    identity = instrument.identity
    assert identity.model == pin.model_name
    assert identity.revision == pin.model_revision
    assert identity.weights_sha256 == pin.weights_sha256
    assert identity.placement == "local-cpu" and identity.runtime.endswith("-cpu")
    green, detail, results = sensing_nli.judge_fixtures(instrument, identity.fixture_set)
    assert green, detail
    assert len(results) == len(sensing.RELATION_FIXTURES[identity.fixture_set])


def test_the_instrument_abstains_instead_of_truncating() -> None:
    instrument = sensing_nli.admit()
    long_text = " ".join(["The pump runs cool under a steady load."] * 200)
    [(ab, ba, reason)] = instrument.judge([(long_text, "The pump runs hot.")])
    assert (ab, ba, reason) == (None, None, "input_too_long")


def test_cost_per_judgement_stays_inside_the_hourly_budget() -> None:
    instrument = sensing_nli.admit()
    pairs = [
        (item.text_a, item.text_b)
        for item in sensing.RELATION_FIXTURES["relation-v1-multilingual"]
    ]
    started_cpu, started_wall = time.process_time(), time.monotonic()
    instrument.judge(pairs)
    cpu_per_judgement = (time.process_time() - started_cpu) / len(pairs)
    wall_per_judgement = (time.monotonic() - started_wall) / len(pairs)
    peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(
        f"cpu/judgement={cpu_per_judgement:.3f}s wall/judgement={wall_per_judgement:.3f}s "
        f"peak_rss={peak_rss_mib:.0f}MiB"
    )
    # Measured at 1.1 CPU-s per judgement on one thread (design.md, "Measured"):
    # the CPU budget, not the judgement budget, binds on a CPU host. This bound
    # only catches a pathological regression (a lost thread cap, a batch of 1).
    assert cpu_per_judgement < 5.0


def test_a_recorded_green_verdict_skips_the_fixture_run(tmp_path, monkeypatch) -> None:
    evidence = tmp_path / "admission.json"
    sensing_nli._ADMITTED.clear()
    sensing_nli._LOADED.clear()
    sensing_nli.admit(evidence_path=evidence)
    assert evidence.is_file()
    sensing_nli._ADMITTED.clear()
    sensing_nli._LOADED.clear()

    def refuse(*_args, **_kwargs):
        raise AssertionError("the fixture set ran again for an identity already admitted")

    monkeypatch.setattr(sensing_nli, "judge_fixtures", refuse)
    instrument = sensing_nli.admit(evidence_path=evidence)
    assert instrument.identity.model == claims.VERIFIER_PINS[0].model_name
