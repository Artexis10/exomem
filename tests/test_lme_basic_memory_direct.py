"""§2.3-residual: the Basic Memory controlled-direct row and its owned sidecar.

These checks are hermetic.  The real row runs the pinned, unmodified
`BasicMemoryLocalProvider` under Basic Memory's own uv environment, which is
exactly why it cannot be in-process; nothing here needs that environment,
because the contract under test is the transport envelope, the exact-limit
pass-through, and the process-absence proof the new execution model owes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import textwrap
import time
from pathlib import Path

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")

ROW = "basic-memory-direct"
MODEL = "owned-subprocess-terminated-at-cleanup"


def _identity():
    from protocol.models import DatasetIdentity

    return DatasetIdentity(
        id="longmemeval", variant="mini", source="local", revision="fixture-pin",
        sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), case_count=6,
    )


def _case():
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from protocol.models import CaseHandle

    question = load_dataset(FIXTURE).questions[0]
    events = neutralize(question, _identity())
    handle = CaseHandle(
        case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text
    )
    return question, events, handle


def _context(tmp_path: Path):
    from lme.providers.base import ProviderSessionContext

    return ProviderSessionContext(
        "bm-direct-test", "session", "ns-bm", tmp_path / "work", tmp_path / "evidence"
    )


# A standalone stand-in for the pinned sidecar: same routes, same envelope, same
# ready line.  It deliberately has no Basic Memory import, so the process-owner
# contract can be proven on any machine.
_FAKE_SIDECAR = textwrap.dedent(
    """
    import json, os, sys, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    TOKEN = os.environ["MEMORYBENCH_GUEST_BEARER_TOKEN"]
    STATE = {"ingested": [], "searches": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._write(401, {"code": "unauthorized"})
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            if self.path == "/v1/ingest":
                STATE["ingested"].append(payload)
                data = {
                    "namespace": "ns",
                    "neutral_document_id": "mb-doc-%d" % len(STATE["ingested"]),
                    "readiness": {"fallback_detected": False, "document_count": len(STATE["ingested"])},
                }
            elif self.path == "/v1/search":
                STATE["searches"].append(payload)
                limit = payload["limit"]
                data = {
                    "namespace": "ns",
                    "hits": [
                        {"id": "h%d" % i, "content": "hit %d" % i, "score": 1.0 / (i + 1)}
                        for i in range(limit)
                    ],
                }
            elif self.path == "/v1/cleanup":
                data = {"namespace": "ns", "final": True, "all_absent": True}
            else:
                return self._write(404, {"code": "not_found"})
            self._write(200, {"protocol_version": 1, "request_id": payload["request_id"], "ok": True, "data": data})

        def _write(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address
    print(json.dumps({"protocol_version": 1, "event": "ready", "base_url": f"http://{host}:{port}"}), flush=True)
    server.serve_forever()
    """
).strip()


@pytest.fixture()
def fake_sidecar(tmp_path: Path) -> Path:
    script = tmp_path / "fake_sidecar.py"
    script.write_text(_FAKE_SIDECAR, encoding="utf-8")
    return script


def _provider_on(script: Path):
    """Bind a real provider to a real subprocess that is not Basic Memory."""

    import sys

    from lme.providers.basic_memory_direct import BasicMemoryDirectProvider, SidecarLaunch

    return BasicMemoryDirectProvider(
        launch=SidecarLaunch(command=(sys.executable, str(script)), env={})
    )


def test_the_row_is_registered_with_the_owned_subprocess_execution_model() -> None:
    """The honest declaration, not a borrowed in-process claim."""

    from lme.providers.registry import provider_spec, registered_provider_names

    assert ROW in registered_provider_names()
    spec = provider_spec(ROW)
    assert spec.descriptor.provider_id == ROW
    assert spec.descriptor.execution_model == MODEL
    assert "process-group" in spec.runtime_binding.required_surface_ids


def test_the_runner_admits_the_new_model_and_still_refuses_unknown_ones(tmp_path: Path) -> None:
    from lme.runner import _SUPPORTED_EXECUTION_MODELS

    assert MODEL in _SUPPORTED_EXECUTION_MODELS
    assert "in-process-no-post-return-background" in _SUPPORTED_EXECUTION_MODELS
    assert "background-capable" not in _SUPPORTED_EXECUTION_MODELS
    assert "unknown" not in _SUPPORTED_EXECUTION_MODELS


def test_the_row_constructs_offline_without_the_basic_memory_environment() -> None:
    """Registration resolves before setup; construction must not need the env."""

    from lme.providers.base import DirectProvider
    from lme.providers.registry import provider_factory

    provider = provider_factory(ROW)()
    assert isinstance(provider, DirectProvider)
    expected = {
        "setup": ["profile", "context"],
        "ingest_case": ["events", "handle"],
        "retrieve": ["question_text", "top_k", "purpose"],
        "export_state": [],
        "cleanup": [],
        "variant_id": [],
        "readiness": [],
    }
    for method, parameters in expected.items():
        assert list(inspect.signature(getattr(provider, method)).parameters) == parameters
    assert provider.variant_id() == ROW


def test_setup_refuses_clearly_when_the_pinned_basic_environment_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers.registry import provider_factory
    from membench.adapters.base import AdapterEnvironmentError

    monkeypatch.delenv("EXOMEM_BM_DIRECT_BASIC_HOME", raising=False)
    monkeypatch.setenv("EXOMEM_BM_DIRECT_BASIC_HOME", str(tmp_path / "absent"))
    provider = provider_factory(ROW)()
    with pytest.raises(AdapterEnvironmentError):
        provider.setup(None, _context(tmp_path))


def test_ingest_and_retrieve_speak_the_pinned_sidecar_envelope(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    from lme.providers.base import ProviderHit, RetrievalPurpose

    _question, events, handle = _case()
    provider = _provider_on(fake_sidecar)
    provider.setup(None, _context(tmp_path))
    try:
        inserted = provider.ingest_case(events, handle)
        assert isinstance(inserted, tuple) and inserted

        hits = provider.retrieve("what colour was the lantern", 4, RetrievalPurpose.SCORED_RETRIEVAL)
        assert [type(hit) for hit in hits] == [ProviderHit] * 4
        assert [hit.hit_id for hit in hits] == ["h0", "h1", "h2", "h3"]
        assert hits[0].score > hits[-1].score

        sent = provider.export_state()
        assert sent
    finally:
        provider.cleanup()


def test_the_exact_top_k_is_forwarded_once_and_never_widened(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    """The audit's headline defect on their side was a hardcoded wider limit."""

    from lme.providers.base import RetrievalPurpose

    _question, events, handle = _case()
    provider = _provider_on(fake_sidecar)
    provider.setup(None, _context(tmp_path))
    try:
        provider.ingest_case(events, handle)
        provider.retrieve("query", 3, RetrievalPurpose.SCORED_RETRIEVAL)
        searches = [call for call in provider.transport_log() if call["route"] == "/v1/search"]
        assert len(searches) == 1
        assert searches[0]["payload"]["limit"] == 3
    finally:
        provider.cleanup()


def test_the_boundary_still_refuses_a_gold_bearing_object(tmp_path: Path) -> None:
    from lme.providers.registry import provider_factory
    from protocol.models import CaseGold, CaseHandle

    handle = CaseHandle(case_id="c", case_ordinal=1, question_date="2026-01-01T00:00:00Z")
    gold = CaseGold(
        case_id="c", answer="violet cedar lantern", answer_session_ids=["answer_1"],
        question_type="knowledge-update", question="Which lantern?",
    )
    with pytest.raises(TypeError):
        provider_factory(ROW)().ingest_case([gold], handle)  # type: ignore[arg-type]


def test_cleanup_terminates_and_reaps_the_owned_process_group(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    """The obligation the new execution model exists to carry."""

    provider = _provider_on(fake_sidecar)
    provider.setup(None, _context(tmp_path))
    pid = provider.owned_process_id()
    assert pid is not None and _pid_alive(pid)
    assert provider.live_process_count() == 1
    assert provider.listener_bound() is True

    provider.cleanup()

    assert provider.live_process_count() == 0
    assert provider.listener_bound() is False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    assert not _pid_alive(pid), "owned sidecar survived cleanup"


def test_a_surviving_sidecar_makes_absence_unproved(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    """A provider that lies about teardown must not reach a retired work inode."""

    from lme.providers.lifecycle import CleanupUnproved, observe_cleanup
    from lme.providers.registry import provider_spec

    provider = _provider_on(fake_sidecar)
    context = _context(tmp_path)
    provider.setup(None, context)
    binding = provider_spec(ROW).runtime_binding
    try:
        # The provider holds no documents, so every other surface reads clean:
        # only the process-group surface can catch the leaked process.
        surfaces = {row["kind"]: row for row in binding.observe(context, provider)}
        assert surfaces["provider-state"] == {
            "kind": "provider-state", "remaining_record_ids": [], "backend_active": False
        }
        assert surfaces["process-group"]["remaining_count"] == 1

        with pytest.raises(CleanupUnproved):
            observe_cleanup(
                context=context, requested_provider=ROW, observed_variant=ROW,
                binding=binding, provider=provider, cleanup_called=True,
            )
    finally:
        provider.cleanup()


def test_the_process_group_surface_is_a_schema_backed_absence_fact() -> None:
    from protocol.models import ProviderCleanupObservation

    def observation(count: int, bound: bool):
        return ProviderCleanupObservation(
            run_id="r", session_id="s", requested_provider=ROW, provider_variant=ROW,
            namespace="ns", cleanup_called=True,
            required_surface_ids=["process-group"],
            observations=[{
                "kind": "process-group", "group_ref": "sidecar",
                "remaining_count": count, "listener_bound": bound,
            }],
        )

    from lme.providers.lifecycle import _absence

    assert _absence([item.model_dump(mode="json") for item in observation(0, False).observations])
    assert not _absence([item.model_dump(mode="json") for item in observation(1, False).observations])
    assert not _absence([item.model_dump(mode="json") for item in observation(0, True).observations])


def test_cleanup_evidence_carries_no_pid_port_or_token(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    from lme.providers.registry import provider_spec

    provider = _provider_on(fake_sidecar)
    context = _context(tmp_path)
    provider.setup(None, context)
    pid = provider.owned_process_id()
    port = provider.endpoint_port()
    token = provider.bearer_token()
    provider.cleanup()

    binding = provider_spec(ROW).runtime_binding
    rendered = json.dumps(binding.observe(context, provider))
    assert str(pid) not in rendered
    assert str(port) not in rendered
    assert token not in rendered


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reaped child lingers as a zombie until waited on; the owner must reap.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split(") ", 1)[1].split(" ", 1)[0] != "Z"
    except FileNotFoundError:
        return False


def test_signal_death_of_the_sidecar_is_observed_not_assumed(
    fake_sidecar: Path, tmp_path: Path
) -> None:
    """If the sidecar dies underneath us, the next call must fail loudly."""

    from lme.providers.base import RetrievalPurpose
    from lme.providers.basic_memory_direct import SidecarTransportError

    _question, events, handle = _case()
    provider = _provider_on(fake_sidecar)
    provider.setup(None, _context(tmp_path))
    try:
        provider.ingest_case(events, handle)
        os.killpg(os.getpgid(provider.owned_process_id()), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and provider.live_process_count():
            time.sleep(0.05)
        with pytest.raises(SidecarTransportError):
            provider.retrieve("query", 3, RetrievalPurpose.SCORED_RETRIEVAL)
    finally:
        provider.cleanup()
