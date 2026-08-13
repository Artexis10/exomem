from __future__ import annotations


def test_canaries_are_deterministic_and_isolation_is_classified() -> None:
    from protocol.canary import canary_for, evaluate_probes

    assert canary_for("secret", "case-a", "presence") == canary_for("secret", "case-a", "presence")
    assert canary_for("secret", "case-a", "presence") != canary_for("secret", "case-b", "presence")
    assert evaluate_probes({"presence": True, "cross_case": False, "never_ingested": False}) == "isolated"
    assert evaluate_probes({"presence": True, "cross_case": True, "never_ingested": False}) == "contaminated"
    assert evaluate_probes({"presence": False}) == "unverifiable"
