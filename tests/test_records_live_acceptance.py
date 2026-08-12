from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "records_live_acceptance.py"
_SPEC = importlib.util.spec_from_file_location("records_live_acceptance", _SCRIPT)
assert _SPEC and _SPEC.loader
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac(value: str) -> str:
    return hmac.new(b"pairing-key", value.encode("utf-8"), hashlib.sha256).hexdigest()


def _evidence() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "deployment": {"sha256": _digest("deployment")},
        "release": {"package": "exomem", "version": "9.9.9"},
        "surface": {"mcp_digest": _digest("surface")},
        "run": {
            "nonce": "run-20260812-a1b2c3d4",
            "timestamp": "2026-08-12T10:00:00Z",
            "expires_at": "2026-08-12T11:00:00Z",
        },
        "vault": {"purpose": "records-live-acceptance", "reset_epoch": "reset-42"},
        "identity": {
            "principal_hmac_sha256": _hmac("principal"),
            "audience_hmac_sha256": _hmac("audience"),
        },
        "client_contract": {
            "client": "transport",
            "client_version": "1.0.0",
            "model_version": "deterministic",
            "system_contract_version": "records-v2",
        },
        "actions": [
            {"action": "describe", "outcome": "completed"},
            {"action": "validate", "outcome": "completed"},
            {"action": "create", "outcome": "committed"},
        ],
        "mutations": [
            {
                "action": "create",
                "request_id": "request-create-1",
                "receipt_id": "receipt-create-1",
                "terminal_outcome": "committed",
                "before_readback_sha256": _digest("before"),
                "after_readback_sha256": _digest("after"),
            }
        ],
        "restart": {"outcome": "completed", "readback_sha256": _digest("restart")},
        "prompt_cases": [
            {"id": "existing-collection", "sha256": _digest("case-a")},
            {"id": "no-collection", "sha256": _digest("case-b")},
        ],
        "graph_availability": {"proof_digest": _digest("graph")},
    }


def _expected() -> live.RecordsEvidenceExpectation:
    return live.RecordsEvidenceExpectation(
        deployment_sha256=_digest("deployment"),
        package="exomem",
        release_version="9.9.9",
        surface_digest=_digest("surface"),
        vault_purpose="records-live-acceptance",
        reset_epoch="reset-42",
        principal_hmac_sha256=_hmac("principal"),
        audience_hmac_sha256=_hmac("audience"),
        client_contract=("transport", "1.0.0", "deterministic", "records-v2"),
        required_actions=frozenset({"describe", "validate", "create"}),
        required_prompt_cases={
            "existing-collection": _digest("case-a"),
            "no-collection": _digest("case-b"),
        },
        graph_proof_digest=_digest("graph"),
    )


def test_closed_evidence_accepts_complete_content_free_facts() -> None:
    rendered = live.validate_records_live_evidence(
        _evidence(), expected=_expected(), now="2026-08-12T10:30:00Z"
    )

    assert rendered == live.canonical_json(_evidence())
    assert "secret-value" not in rendered.decode("utf-8")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.pop("run"), "missing fields"),
        (lambda item: item.__setitem__("prose", "trust me"), "unknown fields"),
        (lambda item: item["run"].__setitem__("expires_at", "2026-08-12T09:00:00Z"), "expired"),
        (lambda item: item["vault"].__setitem__("reset_epoch", "reset-old"), "reset"),
        (lambda item: item["client_contract"].__setitem__("model_version", "other"), "client contract"),
        (lambda item: item["surface"].__setitem__("mcp_digest", _digest("old")), "surface"),
        (lambda item: item["mutations"][0].__setitem__("after_readback_sha256", _digest("before")), "readback"),
        (lambda item: item["actions"].pop(), "required actions"),
        (lambda item: item["prompt_cases"].pop(), "prompt cases"),
    ],
)
def test_closed_evidence_refuses_invalid_or_incomplete_facts(mutate: Any, match: str) -> None:
    evidence = json.loads(json.dumps(_evidence()))
    mutate(evidence)

    with pytest.raises(live.RecordsEvidenceError, match=match):
        live.validate_records_live_evidence(
            evidence, expected=_expected(), now="2026-08-12T10:30:00Z"
        )


def test_acceptance_ledger_replays_exact_evidence_only() -> None:
    facts = _evidence()
    envelope = {
        "facts": facts,
        "operator_signature": hmac.new(
            b"operator-secret", live.canonical_json(facts), hashlib.sha256
        ).hexdigest(),
    }
    ledger = live.RecordsAcceptanceLedger()

    first = ledger.accept(
        candidate_digest=_digest("candidate"),
        envelope=envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    )
    replay = ledger.accept(
        candidate_digest=_digest("candidate"),
        envelope=envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    )

    assert first == replay
    assert first["accepted"] is True
    with pytest.raises(live.RecordsEvidenceError, match="different candidate"):
        ledger.accept(
            candidate_digest=_digest("candidate-2"),
            envelope=envelope,
            expected=_expected(),
            operator_secret="operator-secret",
            now="2026-08-12T10:30:00Z",
        )


def test_operator_signed_envelope_requires_operator_held_signature() -> None:
    facts = _evidence()
    envelope = {
        "facts": facts,
        "operator_signature": hmac.new(
            b"operator-secret", live.canonical_json(facts), hashlib.sha256
        ).hexdigest(),
    }

    assert live.validate_operator_signed_records_evidence(
        envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    ) == live.canonical_json(envelope)
    with pytest.raises(live.RecordsEvidenceError, match="signature"):
        live.validate_operator_signed_records_evidence(
            {"facts": facts},
            expected=_expected(),
            operator_secret="operator-secret",
            now="2026-08-12T10:30:00Z",
        )


def test_runner_resets_then_records_only_content_free_transport_facts() -> None:
    transport = _Transport()
    runner = live.RecordsLiveAcceptanceRunner(
        transport,
        timeout_seconds=2.0,
        expected_actions=("describe", "create"),
        reset_purpose="records-live-acceptance",
    )

    facts = runner.run()

    assert transport.calls == ["reset", "describe", "readback", "create", "readback"]
    assert facts["vault"] == {"purpose": "records-live-acceptance", "reset_epoch": "reset-42"}
    assert facts["mutations"][0]["request_id"] == "request-create-1"
    assert "secret-value" not in live.canonical_json(facts).decode("utf-8")


class _Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reset(self, *, purpose: str, timeout_seconds: float) -> dict[str, str]:
        assert purpose == "records-live-acceptance"
        assert timeout_seconds == 2.0
        self.calls.append("reset")
        return {"reset_epoch": "reset-42"}

    def invoke(self, *, action: str, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        self.calls.append(action)
        if action == "describe":
            return {"outcome": "completed"}
        return {
            "outcome": "committed",
            "request_id": "request-create-1",
            "receipt_id": "receipt-create-1",
        }

    def readback(self, *, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        self.calls.append("readback")
        return {"private": "secret-value", "public": {"state": len(self.calls)}}
