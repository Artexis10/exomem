from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from benchmark_capabilities import require_posix_interval_timers
from pydantic import ValidationError


def _matrix_entry(**changes):
    from epistemic.schema import PrivilegedEndpointMatrixEntry

    payload = {
        "driver_surface_id": "state.read",
        "provider": "fixture",
        "variant": "native",
        "disposition": "equivalent",
        "audit_scope": "read-only projected state",
        "evidence": "https://example.invalid/provider/read-surface",
        "reason": "Both rows read the documented state surface.",
        "competitor_surface": "sdk.state.read",
    }
    payload.update(changes)
    return PrivilegedEndpointMatrixEntry.model_validate(payload)


def test_endpoint_matrix_has_closed_equivalent_and_capability_gap_dispositions() -> None:
    equivalent = _matrix_entry()
    assert equivalent.disposition == "equivalent"
    assert equivalent.competitor_surface == "sdk.state.read"
    gap = _matrix_entry(
        disposition="capability_gap",
        competitor_surface=None,
        reason="The competitor exposes no documented lineage-history surface.",
    )
    assert gap.disposition == "capability_gap"
    assert gap.competitor_surface is None
    for payload in (
        {**equivalent.model_dump(), "unknown": "x"},
        {**equivalent.model_dump(), "disposition": "similar"},
        {**equivalent.model_dump(), "audit_scope": ""},
        {**equivalent.model_dump(), "evidence": ""},
        {**equivalent.model_dump(), "reason": ""},
        {**equivalent.model_dump(), "competitor_surface": None},
        {**gap.model_dump(), "competitor_surface": "must-not-exist"},
    ):
        with pytest.raises(ValidationError):
            type(equivalent).model_validate(payload)


def test_legacy_endpoint_prose_pair_is_rejected_by_scenario_schema() -> None:
    from epistemic.schema import FairnessPacket

    with pytest.raises(ValidationError, match="privileged_endpoint"):
        FairnessPacket.model_validate({
            "why_neutral": "neutral",
            "public_coverage_subtraction": "state-only",
            "mechanisms": [{"provider_role": "provider", "mechanism": "read", "verdict": "possible", "evidence": "docs"}],
            "privileged_endpoint_check": [{"driver_tool": "read", "competitor_equivalent": "read"}],
            "acceptance_predicate": "documented state is observable",
        })


def test_broker_seals_ordered_digest_bound_success_and_exception_receipts(tmp_path: Path) -> None:
    from epistemic.broker import ProviderBroker

    receipt_path = tmp_path / "receipts/invocations.v1.json"
    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={
            "state.read": lambda value: {"value": value},
            "state.fail": lambda: (_ for _ in ()).throw(RuntimeError("provider exploded")),
        },
        receipt_path=receipt_path,
    )
    result = broker.run_driver(
        """
def run(broker):
    value = broker.invoke('state.read', 'x')
    try:
        broker.invoke('state.fail')
    except Exception:
        pass
    return value
""",
        timeout_s=2.0,
    )
    assert result.value == {"value": "x"}

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [item["ordinal"] for item in payload["receipts"]] == [1, 2]
    assert [item["outcome"] for item in payload["receipts"]] == ["success", "exception"]
    assert all(item["sealed"] is True for item in payload["receipts"])
    assert payload["receipts"][1]["previous_receipt_sha256"] == payload["receipts"][0]["receipt_sha256"]
    assert payload["receipts"][0]["arguments_sha256"] == hashlib.sha256(
        b'{"args":["x"],"kwargs":{}}'
    ).hexdigest()
    ref = broker.receipt_ref()
    assert ref.path == "receipts/invocations.v1.json"
    assert ref.sha256 == hashlib.sha256(receipt_path.read_bytes()).hexdigest()


def test_broker_exposes_no_provider_visible_capabilities_to_the_driver(tmp_path: Path) -> None:
    from epistemic.broker import ProviderBroker

    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "invocations.json",
        credentials={"api_key": "secret"}, sockets={"endpoint": object()},
        sdk_clients={"client": object()}, cli_commands={"cli": ("tool",)},
        filesystem_roots={"root": tmp_path},
    )
    assert broker.run_driver(
        "def run(broker): return broker.invoke('state.read')", timeout_s=2.0
    ).value == "ok"
    assert not hasattr(broker, "invoke")
    for leaked in ("credentials", "sockets", "sdk_clients", "cli_commands", "filesystem_roots"):
        assert not hasattr(broker, leaked)


def test_receipt_is_reread_and_tamper_unsealed_or_undeclared_calls_refuse(tmp_path: Path) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, audit_invocation_receipts

    path = tmp_path / "receipts.json"
    broker = ProviderBroker(
        provider="fixture", variant="native",
        surfaces={"state.read": lambda: "ok", "secret.admin": lambda: "bad"},
        receipt_path=path,
    )
    driver_result = broker.run_driver(
        "def run(broker): return broker.invoke('state.read')", timeout_s=2.0
    )
    audit = audit_invocation_receipts(
        broker=broker, run_root=tmp_path, driver_result=driver_result,
        matrix=(_matrix_entry(),), provider="fixture", variant="native",
    )
    assert audit.comparable is True

    original = path.read_bytes()
    payload = json.loads(original)
    payload["receipts"][0]["sealed"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BrokerContractError, match="digest|unsealed|changed"):
        audit_invocation_receipts(
            broker=broker, run_root=tmp_path, driver_result=driver_result,
            matrix=(_matrix_entry(),),
            provider="fixture", variant="native",
        )

    undeclared_broker = ProviderBroker(
        provider="fixture", variant="native",
        surfaces={"secret.admin": lambda: "bad"},
        receipt_path=tmp_path / "undeclared.json",
    )
    undeclared = undeclared_broker.run_driver(
        "def run(broker): return broker.invoke('secret.admin')", timeout_s=2.0
    )
    with pytest.raises(BrokerContractError, match="secret.admin"):
        audit_invocation_receipts(
            broker=undeclared_broker, run_root=tmp_path, driver_result=undeclared,
            matrix=(_matrix_entry(),),
            provider="fixture", variant="native",
        )


def test_missing_or_extra_declared_surface_refuses_exact_inventory(tmp_path: Path) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, audit_invocation_receipts

    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "receipts.json",
    )
    # The log is authenticated, but the declared equivalent surface was never invoked.
    driver_result = broker.run_driver("def run(_broker): return None", timeout_s=2.0)
    with pytest.raises(BrokerContractError, match="state.read"):
        audit_invocation_receipts(
            broker=broker, run_root=tmp_path, driver_result=driver_result,
            matrix=(_matrix_entry(),),
            provider="fixture", variant="native",
        )


def test_capability_gap_is_named_noncomparability_never_score_or_unsupported(tmp_path: Path) -> None:
    from epistemic.broker import ProviderBroker, audit_invocation_receipts
    from epistemic.schema import PrivilegedEndpointMatrixEntry

    gap = PrivilegedEndpointMatrixEntry(
        driver_surface_id="history.read", provider="fixture", variant="native",
        disposition="capability_gap", audit_scope="documented public history APIs",
        evidence="https://example.invalid/provider/no-history",
        reason="No competitor-authored history surface exists.", competitor_surface=None,
    )
    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={},
        receipt_path=tmp_path / "receipts.json",
    )
    driver_result = broker.run_driver("def run(_broker): return None", timeout_s=2.0)
    audit = audit_invocation_receipts(
        broker=broker, run_root=tmp_path, driver_result=driver_result,
        matrix=(gap,), provider="fixture", variant="native",
    )
    assert audit.comparable is False
    assert audit.exclusions == (
        "fixture/native: capability_gap for history.read — No competitor-authored history surface exists.",
    )
    assert "unsupported" not in audit.model_dump_json()
    assert "score" not in audit.model_dump_json()


def test_mixed_equivalent_and_gap_inventory_still_requires_equivalent_receipt(
    tmp_path: Path,
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, audit_invocation_receipts

    gap = _matrix_entry(
        driver_surface_id="history.read",
        disposition="capability_gap",
        competitor_surface=None,
        reason="No documented history surface exists.",
    )
    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={},
        receipt_path=tmp_path / "receipts.json",
    )
    driver_result = broker.run_driver("def run(_broker): return None", timeout_s=2.0)
    with pytest.raises(BrokerContractError, match="receipt|state.read"):
        audit_invocation_receipts(
            broker=broker,
            run_root=tmp_path,
            driver_result=driver_result,
            matrix=(_matrix_entry(), gap),
            provider="fixture",
            variant="native",
        )


def test_source_import_conformance_rejects_direct_provider_capability_bypass() -> None:
    from epistemic.broker import BrokerContractError, validate_driver_source

    source = """
import socket
class Driver:
    def run(self, broker):
        return socket.create_connection((\"127.0.0.1\", 9999))
"""
    with pytest.raises(BrokerContractError, match="socket|bypass"):
        validate_driver_source(source, filename="synthetic_driver.py")


def test_synthetic_runtime_conformance_rejects_direct_open_as_defense_in_depth(tmp_path: Path) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, runtime_capability_isolation

    broker = ProviderBroker(
        provider="fixture", variant="native",
        surfaces={"file.read": lambda path: Path(path).read_text(encoding="utf-8")},
        receipt_path=tmp_path / "receipts.json",
    )
    target = tmp_path / "state.txt"
    target.write_text("state", encoding="utf-8")
    with runtime_capability_isolation():
        with pytest.raises(BrokerContractError, match="broker"):
            target.open("r", encoding="utf-8")
    assert not hasattr(broker, "invoke")


def test_real_bwrap_sandbox_exposes_only_serializable_broker_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from epistemic.broker import ProviderBroker

    secret = "parent-provider-secret-7de5"
    provider_root = tmp_path / "provider-private"
    provider_root.mkdir()
    (provider_root / "state.txt").write_text(secret, encoding="utf-8")
    provider_cli = tmp_path / "provider-private-cli"
    provider_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provider_cli.chmod(0o700)
    monkeypatch.setenv("PROVIDER_SECRET", secret)

    receipt_path = tmp_path / "receipts/invocations.v1.json"
    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: {"public": "ok"}},
        receipt_path=receipt_path,
        credentials={"api_key": secret},
        sockets={"loopback": object()},
        sdk_clients={"client": object()},
        cli_commands={"cli": (str(provider_cli),)},
        filesystem_roots={"root": provider_root},
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        driver = f"""
builtins_module = __import__('builtins')
io_module = __import__('io')
os_module = __import__('os')
socket_module = __import__('socket')
subprocess_module = __import__('subprocess')
aliased_open = builtins_module.open
reflective_open = getattr(io_module, 'open')
aliased_listdir = os_module.listdir
aliased_connect = getattr(socket_module, 'create_connection')
aliased_cli = getattr(subprocess_module, 'run')

def denied(operation):
    try:
        operation()
    except Exception:
        return True
    return False

def run(broker):
    proxy_view = repr((type(broker), dir(broker), getattr(broker, '__dict__', None)))
    return {{
        'builtins_open_denied': denied(lambda: aliased_open({str(provider_root / 'state.txt')!r})),
        'io_open_denied': denied(lambda: reflective_open({str(provider_root / 'state.txt')!r})),
        'listdir_denied': denied(lambda: aliased_listdir({str(provider_root)!r})),
        'network_denied': denied(lambda: aliased_connect(('127.0.0.1', {port}), timeout=0.2)),
        'provider_cli_denied': denied(lambda: aliased_cli([{str(provider_cli)!r}], check=False)),
        'environment_scrubbed': os_module.environ.get('PROVIDER_SECRET') is None,
        'parent_broker_absent': '_ProviderBroker__' not in proxy_view,
        'declared_result': broker.invoke('state.read'),
    }}
"""
        result = broker.run_driver(driver, timeout_s=3.0)

    assert result.value == {
        "builtins_open_denied": True,
        "io_open_denied": True,
        "listdir_denied": True,
        "network_denied": True,
        "provider_cli_denied": True,
        "environment_scrubbed": True,
        "parent_broker_absent": True,
        "declared_result": {"public": "ok"},
    }
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [(row["driver_surface_id"], row["outcome"]) for row in receipt["receipts"]] == [
        ("state.read", "success")
    ]
    assert secret not in receipt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["missing", "spawn"])
def test_bwrap_absence_or_namespace_spawn_failure_is_pre_provider_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import epistemic.broker as broker_module

    called = False

    def provider_call():
        nonlocal called
        called = True
        return "not allowed"

    broker = broker_module.ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": provider_call},
        receipt_path=tmp_path / "receipts.json",
    )
    if failure == "missing":
        monkeypatch.setattr(broker_module.shutil, "which", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr(
            broker_module.subprocess,
            "Popen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("namespace denied")),
        )

    with pytest.raises(broker_module.BrokerContractError, match="bwrap|sandbox|namespace"):
        broker.run_driver("def run(broker): return broker.invoke('state.read')")
    assert called is False
    assert not (tmp_path / "receipts.json").exists()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            b'{"protocol":"epistemic-driver-ipc.v1","protocol":"confused","type":"complete"}\n',
            id="duplicate-key",
        ),
        pytest.param(
            b'{"protocol":"epistemic-driver-ipc.v1","type":"complete","result":NaN}\n',
            id="nan-result",
        ),
        pytest.param(b'{not-json}\n', id="not-json"),
        pytest.param(
            b'{"protocol":"wrong","type":"complete","result":null}\n',
            id="wrong-protocol",
        ),
        pytest.param(
            b'{"protocol":"epistemic-driver-ipc.v1","type":"unknown","result":null}\n',
            id="unknown-type",
        ),
        pytest.param((b'[' * 40) + b'0' + (b']' * 40) + b'\n', id="deeply-nested"),
        # Unnamed, this 64KiB payload becomes the test id verbatim, and pytest
        # exports the node id as PYTEST_CURRENT_TEST -- past the 32767-char cap
        # Windows puts on an environment variable, so the case errors in setup
        # and teardown without ever running.
        pytest.param(b'x' * (64 * 1024 + 1), id="oversized"),
    ],
)
def test_driver_ipc_rejects_malformed_confused_deep_or_oversized_messages(
    tmp_path: Path, raw: bytes
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker

    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "receipts.json",
    )
    source = (
        "def run(_broker):\n"
        f"    data = {raw!r}\n"
        "    __import__('os').write(1, data)\n"
        "    __import__('os')._exit(0)\n"
    )
    with pytest.raises(BrokerContractError, match="IPC|protocol|message|sandbox"):
        broker.run_driver(source, timeout_s=2.0)
    assert not (tmp_path / "receipts.json").exists()


@pytest.mark.parametrize(
    "source",
    [
        "def run(broker): return broker.invoke('undeclared.surface')",
        "def run(_broker): __import__('os')._exit(7)",
        "def run(_broker):\n    while True: pass",
        (
            "def run(_broker):\n"
            "    __import__('os').write(2, b'x' * (64 * 1024 + 1))\n"
            "    return 'not accepted'\n"
        ),
    ],
)
def test_driver_unknown_surface_abnormal_exit_deadline_or_stderr_overflow_refuses(
    tmp_path: Path, source: str
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker

    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "receipts.json",
    )
    with pytest.raises(BrokerContractError, match="surface|exit|deadline|stderr|sandbox"):
        broker.run_driver(source, timeout_s=0.3)


def test_broker_receipt_ref_path_is_canonical_relative(tmp_path: Path) -> None:
    from epistemic.broker import InvocationReceiptRef

    for path in ("/abs.json", "../escape.json", "a/../../b.json", "a\\b.json", "./x.json"):
        with pytest.raises(ValidationError):
            InvocationReceiptRef(path=path, sha256="a" * 64)


def _scenario(entry):
    from epistemic.schema import (
        Expectation, FairnessMechanism, FairnessPacket, Scenario, ScenarioOp, ScenarioPhase,
    )

    return Scenario(
        scenario_id="broker-scenario", family_id="f08", kind="corpus", public_coverage="none",
        phases=(ScenarioPhase(
            phase_id="p1", ops=(ScenarioOp(op="snapshot", ref="s1"),),
            expect=(Expectation(**{"assert": "open_question_queryable"}),),
        ),),
        fairness=FairnessPacket(
            why_neutral="Open questions must remain queryable.",
            public_coverage_subtraction="none",
            mechanisms=(FairnessMechanism(
                provider_role="fixture", mechanism="typed record", verdict="possible", evidence="docs",
            ),),
            privileged_endpoint_matrix=(entry,),
            acceptance_predicate="A documented query returns the open question.",
        ),
    )


def _snapshot():
    from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem

    return EpistemicStateSnapshot(
        provider="fixture", variant="native", phase="p1", taken_at="2026-08-11T00:00:00Z",
        items=(StateItem(id="q", kind="open_question", title="question", current="yes"),),
        declarations=(FieldDeclaration(
            field="open_question", status="declared", evidence="https://example.invalid/questions",
        ),),
        projector=ProjectorMeta(
            name="fixture", version="1", author="test", endpoints_used=("broker:state.read",), loc=1,
        ),
    )


def test_runner_rereads_sealed_receipt_before_any_assertion(tmp_path: Path, monkeypatch) -> None:
    from epistemic.broker import ProviderBroker
    from epistemic.runner import run_scenario

    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "receipts.json",
    )
    driver_result = broker.run_driver(
        "def run(broker): return broker.invoke('state.read')", timeout_s=2.0
    )
    ref = driver_result.receipt_ref
    (tmp_path / ref.path).write_bytes((tmp_path / ref.path).read_bytes() + b"tampered")
    called = False

    def assertion(_context):
        nonlocal called
        called = True

    monkeypatch.setattr("epistemic.runner.resolve", lambda _name: assertion)
    with pytest.raises(Exception, match="digest|changed"):
        run_scenario(
            _scenario(_matrix_entry()), snapshots={"s1": _snapshot()},
            run_root=tmp_path, broker=broker, sandbox_result=driver_result,
            provider="fixture", variant="native",
        )
    assert called is False


def test_runner_turns_capability_gap_into_named_noncomparability_without_assertions(tmp_path: Path) -> None:
    from epistemic.broker import ProviderBroker
    from epistemic.runner import run_scenario

    gap = _matrix_entry(
        disposition="capability_gap", competitor_surface=None,
        reason="No documented open-question query surface.",
    )
    broker = ProviderBroker(
        provider="fixture", variant="native", surfaces={},
        receipt_path=tmp_path / "receipts.json",
    )
    driver_result = broker.run_driver("def run(_broker): return None", timeout_s=2.0)
    result = run_scenario(
        _scenario(gap), snapshots={"s1": _snapshot()}, run_root=tmp_path,
        broker=broker, sandbox_result=driver_result,
        provider="fixture", variant="native",
    )
    assert result.assertions == ()
    assert result.comparability_exclusions == (
        "fixture/native: capability_gap for state.read — No documented open-question query surface.",
    )


def test_comparative_runner_refuses_missing_bound_audit_context() -> None:
    from epistemic.runner import RunnerBindingError, run_scenario

    with pytest.raises(RunnerBindingError, match="audit|receipt|provider"):
        run_scenario(
            _scenario(_matrix_entry()),
            snapshots={"s1": _snapshot()},
        )


# ---------------------------------------------------------------------------
# Independent final recheck: authenticated sandbox execution and audit shape.
# ---------------------------------------------------------------------------


def test_recheck3_direct_parent_invoke_cannot_mint_comparable_receipts(
    tmp_path: Path,
) -> None:
    from epistemic.broker import ProviderBroker

    path = tmp_path / "receipts.json"
    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "ok"},
        receipt_path=path,
    )

    assert not hasattr(broker, "invoke")
    assert not path.exists()


def test_recheck3_run_driver_returns_live_nonserializable_authenticated_result(
    tmp_path: Path,
) -> None:
    from epistemic.broker import ProviderBroker

    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: {"state": "ok"}},
        receipt_path=tmp_path / "receipts.json",
    )
    source = "def run(broker): return broker.invoke('state.read')"
    result = broker.run_driver(source, timeout_s=2.0)

    assert result.value == {"state": "ok"}
    assert result.receipt_ref == result.attestation.receipt_ref
    assert result.attestation.provider == "fixture"
    assert result.attestation.variant == "native"
    assert result.attestation.driver_source_sha256 == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()
    assert len(result.attestation.worker_sha256) == 64
    assert len(result.attestation.bwrap_binary_sha256) == 64
    assert len(result.attestation.bwrap_profile_sha256) == 64
    assert result.attestation.session_id
    with pytest.raises(TypeError):
        json.dumps(result)


@pytest.mark.parametrize(
    "substitution",
    ["other_broker", "provider", "variant", "receipt_ref", "driver_attestation"],
)
def test_recheck3_live_attestation_rejects_every_cross_binding_substitution(
    tmp_path: Path, substitution: str
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, audit_invocation_receipts

    first = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "first"},
        receipt_path=tmp_path / "first/receipts.json",
    )
    second = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "second"},
        receipt_path=tmp_path / "second/receipts.json",
    )
    first_result = first.run_driver(
        "def run(broker): return broker.invoke('state.read')", timeout_s=2.0
    )
    second_result = second.run_driver(
        "def run(broker):\n    value = broker.invoke('state.read')\n    return value",
        timeout_s=2.0,
    )
    audit_broker = first
    candidate = first_result
    provider = "fixture"
    variant = "native"
    if substitution == "other_broker":
        candidate = second_result
    elif substitution == "provider":
        provider = "other-provider"
    elif substitution == "variant":
        variant = "other-variant"
    elif substitution == "receipt_ref":
        candidate = replace(first_result, receipt_ref=second_result.receipt_ref)
    else:
        candidate = replace(first_result, attestation=second_result.attestation)

    with pytest.raises(BrokerContractError, match="attestation|session|provider|variant|receipt|driver"):
        audit_invocation_receipts(
            broker=audit_broker,
            run_root=tmp_path / "first",
            driver_result=candidate,
            matrix=(_matrix_entry(),),
            provider=provider,
            variant=variant,
        )


def test_recheck3_hand_authored_digest_chain_without_live_attestation_is_refused(
    tmp_path: Path,
) -> None:
    from epistemic.broker import BrokerContractError, InvocationReceiptRef, audit_invocation_receipts

    unsigned = {
        "ordinal": 1,
        "provider": "fixture",
        "variant": "native",
        "driver_surface_id": "state.read",
        "arguments_sha256": hashlib.sha256(b'{"args":[],"kwargs":{}}').hexdigest(),
        "outcome": "success",
        "result_sha256": hashlib.sha256(b'"ok"').hexdigest(),
        "exception_type": None,
        "previous_receipt_sha256": None,
        "sealed": True,
    }
    unsigned["receipt_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "artifact_type": "driver-invocation-receipts.v1",
        "schema_version": 1,
        "provider": "fixture",
        "variant": "native",
        "receipts": [unsigned],
    }
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    ref = InvocationReceiptRef(
        path="receipts.json", sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )

    with pytest.raises((BrokerContractError, TypeError), match="attestation|driver_result|broker"):
        audit_invocation_receipts(
            run_root=tmp_path,
            receipt_ref=ref,
            matrix=(_matrix_entry(),),
            provider="fixture",
            variant="native",
        )


def test_recheck3_runner_requires_typed_attested_result_and_rejects_raw_ref(
    tmp_path: Path,
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker
    from epistemic.runner import RunnerBindingError, run_scenario

    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "ok"},
        receipt_path=tmp_path / "receipts.json",
    )
    driver_result = broker.run_driver(
        "def run(broker): return broker.invoke('state.read')", timeout_s=2.0
    )
    raw_ref = broker.receipt_ref()

    with pytest.raises((TypeError, RunnerBindingError, BrokerContractError)):
        run_scenario(
            _scenario(_matrix_entry()),
            snapshots={"s1": _snapshot()},
            run_root=tmp_path,
            invocation_receipt_ref=raw_ref,
            provider="fixture",
            variant="native",
        )

    accepted = run_scenario(
        _scenario(_matrix_entry()),
        snapshots={"s1": _snapshot()},
        run_root=tmp_path,
        broker=broker,
        sandbox_result=driver_result,
        provider="fixture",
        variant="native",
    )
    assert accepted.assertions[0].result.outcome == "pass"


def test_recheck3_parent_reply_write_is_nonblocking_deadline_bound_and_reaped(
    tmp_path: Path,
) -> None:
    """The outer timeout is test cleanup only; success requires the broker's own deadline."""

    script = textwrap.dedent(
        """
        import json
        import sys
        import time
        from pathlib import Path
        import epistemic.broker as broker_module

        seen = []
        real_popen = broker_module.subprocess.Popen
        def tracking_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            seen.append(process)
            return process
        broker_module.subprocess.Popen = tracking_popen

        root = Path(sys.argv[1])
        broker = broker_module.ProviderBroker(
            provider="fixture",
            variant="native",
            surfaces={"state.read": lambda: "x" * 60000},
            receipt_path=root / "receipts.json",
        )
        driver = '''
        def run(_broker):
            import json
            import os
            request = {"protocol":"epistemic-driver-ipc.v1","type":"invoke","id":1,"surface":"state.read","args":[],"kwargs":{}}
            os.write(1, (json.dumps(request, separators=(",", ":")) + "\\\\n").encode())
            while True:
                pass
        '''
        started = time.monotonic()
        try:
            broker.run_driver(driver, timeout_s=0.25)
        except broker_module.BrokerContractError:
            elapsed = time.monotonic() - started
            assert elapsed < 1.5, elapsed
            assert seen and seen[0].poll() is not None
            print("broker-deadline-reaped", round(elapsed, 3))
        else:
            raise AssertionError("driver unexpectedly completed")
        """
    )
    try:
        repo_root = Path(__file__).resolve().parents[1]
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(
            (str(repo_root / "src"), str(repo_root / "benchmarks"))
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            check=False,
            capture_output=True,
            env=child_env,
            text=True,
            timeout=4.0,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"broker blocked in a parent stdin write past its own deadline: {exc}")
    assert completed.returncode == 0, completed.stderr
    assert "broker-deadline-reaped" in completed.stdout


def test_recheck3_endpoint_audit_accepts_repeats_and_interleaving_in_actual_order(
    tmp_path: Path,
) -> None:
    from epistemic.broker import ProviderBroker, audit_invocation_receipts

    query = _matrix_entry(
        driver_surface_id="state.query",
        competitor_surface="sdk.state.query",
    )
    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "read", "state.query": lambda: "query"},
        receipt_path=tmp_path / "receipts.json",
    )
    result = broker.run_driver(
        """
def run(broker):
    return [broker.invoke('state.query'), broker.invoke('state.read'), broker.invoke('state.query')]
""",
        timeout_s=2.0,
    )
    audit = audit_invocation_receipts(
        broker=broker,
        run_root=tmp_path,
        driver_result=result,
        matrix=(_matrix_entry(), query),
        provider="fixture",
        variant="native",
    )
    assert audit.comparable is True
    assert audit.invoked_surfaces == ("state.query", "state.read", "state.query")


@pytest.mark.parametrize("mode", ["missing", "undeclared_gap"])
def test_recheck3_endpoint_audit_requires_each_equivalent_and_rejects_gap_calls(
    tmp_path: Path, mode: str
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker, audit_invocation_receipts

    second = _matrix_entry(
        driver_surface_id="state.query" if mode == "missing" else "history.read",
        disposition="equivalent" if mode == "missing" else "capability_gap",
        competitor_surface="sdk.state.query" if mode == "missing" else None,
        reason=(
            "Both rows query documented state."
            if mode == "missing"
            else "No documented history surface exists."
        ),
    )
    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: "read", second.driver_surface_id: lambda: "other"},
        receipt_path=tmp_path / "receipts.json",
    )
    source = (
        "def run(broker): return broker.invoke('state.read')"
        if mode == "missing"
        else "def run(broker): return [broker.invoke('state.read'), broker.invoke('history.read')]"
    )
    result = broker.run_driver(source, timeout_s=2.0)
    with pytest.raises(BrokerContractError, match="state.query|history.read|undeclared|missing|gap"):
        audit_invocation_receipts(
            broker=broker,
            run_root=tmp_path,
            driver_result=result,
            matrix=(_matrix_entry(), second),
            provider="fixture",
            variant="native",
        )


# ---------------------------------------------------------------------------
# Final surgical review: one absolute deadline owns trusted surface callbacks.
# ---------------------------------------------------------------------------


def test_finalreview_whole_driver_deadline_interrupts_surface_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_posix_interval_timers()
    import epistemic.broker as broker_module

    children = []
    real_popen = broker_module.subprocess.Popen

    def tracking_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(broker_module.subprocess, "Popen", tracking_popen)
    broker = broker_module.ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": lambda: time.sleep(3.0)},
        receipt_path=tmp_path / "receipts.json",
    )
    started = time.monotonic()
    with pytest.raises(broker_module.BrokerContractError, match="deadline|timeout"):
        broker.run_driver(
            "def run(broker): return broker.invoke('state.read')", timeout_s=0.2
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, elapsed
    assert children and children[0].poll() is not None
    receipt = json.loads((tmp_path / "receipts.json").read_text(encoding="utf-8"))
    assert [(item["outcome"], item["exception_type"]) for item in receipt["receipts"]] == [
        ("exception", "BrokerSurfaceTimeout")
    ]


def test_finalreview_surface_timer_restores_handler_and_refuses_conflicting_timer(
    tmp_path: Path,
) -> None:
    require_posix_interval_timers()
    from epistemic.broker import BrokerContractError, ProviderBroker

    original_handler = signal.getsignal(signal.SIGALRM)

    def existing_handler(_signum, _frame):
        raise AssertionError("restored handler must not fire")

    signal.signal(signal.SIGALRM, existing_handler)
    try:
        broker = ProviderBroker(
            provider="fixture",
            variant="native",
            surfaces={"state.read": lambda: "ok"},
            receipt_path=tmp_path / "success.json",
        )
        assert broker.run_driver(
            "def run(broker): return broker.invoke('state.read')", timeout_s=1.0
        ).value == "ok"
        assert signal.getsignal(signal.SIGALRM) is existing_handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

        signal.setitimer(signal.ITIMER_REAL, 5.0)
        called = False

        def provider_call():
            nonlocal called
            called = True
            return "must-not-run"

        conflicting = ProviderBroker(
            provider="fixture",
            variant="native",
            surfaces={"state.read": provider_call},
            receipt_path=tmp_path / "conflict.json",
        )
        with pytest.raises(BrokerContractError, match="timer|deadline|ownership"):
            conflicting.run_driver(
                "def run(broker): return broker.invoke('state.read')", timeout_s=1.0
            )
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert remaining > 0.0
        assert interval == 0.0
        assert signal.getsignal(signal.SIGALRM) is existing_handler
        assert called is False
        assert not (tmp_path / "conflict.json").exists()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, original_handler)


def test_finalreview_non_main_thread_refuses_before_provider_surface(
    tmp_path: Path,
) -> None:
    from epistemic.broker import BrokerContractError, ProviderBroker

    called = False

    def provider_call():
        nonlocal called
        called = True
        return "must-not-run"

    broker = ProviderBroker(
        provider="fixture",
        variant="native",
        surfaces={"state.read": provider_call},
        receipt_path=tmp_path / "receipts.json",
    )
    outcome: list[object] = []

    def invoke_from_thread() -> None:
        try:
            outcome.append(
                broker.run_driver(
                    "def run(broker): return broker.invoke('state.read')", timeout_s=1.0
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    thread = threading.Thread(target=invoke_from_thread)
    thread.start()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], BrokerContractError)
    assert "main thread" in str(outcome[0]).lower()
    assert called is False
    assert not (tmp_path / "receipts.json").exists()
