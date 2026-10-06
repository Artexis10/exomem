"""What an idle cell's health probes cost, and what that saving may not cost.

The kubelet asks a cloud cell for `/health/ready` every 5 s and `/health` every
10 s, and on 0.108.0 that probe traffic alone kept an idle cell at 33-47
millicores. Each readiness probe re-ran the whole proof: a coordination thread,
a SQLite catalogue open and a log-directory write. Each liveness refresh
re-read the package metadata.

A ready proof now answers probes for up to `READINESS_SUCCESS_TTL_SECONDS`. A
not-ready proof is never reused, a cached answer says how old its proof is,
and any admission change this process makes, or a standby promotion, drops
the cached answer. Install provenance is read once per process: it describes
the code that is running, which cannot change under it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from fastmcp import FastMCP

from exomem import deploy_provenance, readiness, server_assets, service_standby
from exomem import runtime_readiness as runtime_readiness_module


@pytest.fixture(autouse=True)
def _clean_process_state():
    readiness.reset()
    service_standby.reset_for_tests()
    yield
    readiness.reset()
    service_standby.reset_for_tests()


def _probe_app():
    app = FastMCP("probe-cost")
    server_assets.register_health_routes(app)
    return app.http_app(transport="streamable-http")


async def _get_each(asgi, paths: list[str], *, gap: float = 0.0) -> list[httpx.Response]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://cell.local"
    ) as client:
        responses = []
        for path in paths:
            responses.append(await client.get(path))
            await asyncio.sleep(gap)
        return responses


# -- /health/ready -----------------------------------------------------------


def test_a_ready_proof_answers_the_next_probe_and_says_how_old_it_is(monkeypatch) -> None:
    proofs: list[str] = []

    def ready(**_kwargs):
        proofs.append("proof")
        return {"status": "ready", "service": "exomem"}

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", ready)
    asgi = _probe_app()

    first, second = asyncio.run(_get_each(asgi, ["/health/ready"] * 2, gap=0.05))

    assert [first.status_code, second.status_code] == [200, 200]
    assert proofs == ["proof"], "every readiness probe re-ran the whole proof"
    # A reused answer must not pass itself off as a fresh measurement.
    assert first.json()["proof_age_seconds"] == 0.0
    assert second.json()["proof_age_seconds"] >= 0.05


def test_a_not_ready_proof_is_never_reused_so_readiness_lands_on_the_next_probe(
    monkeypatch,
) -> None:
    """A roll gate waits for the new pod to turn Ready; a cached 503 would hold it."""
    answers = iter(["not_ready", "ready"])
    proofs: list[str] = []

    def proof(**_kwargs):
        proofs.append("proof")
        return {"status": next(answers), "service": "exomem"}

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", proof)
    asgi = _probe_app()

    statuses = [r.status_code for r in asyncio.run(_get_each(asgi, ["/health/ready"] * 2))]

    assert statuses == [503, 200]
    assert proofs == ["proof", "proof"]


def test_a_ready_proof_older_than_the_ttl_is_proved_again(monkeypatch) -> None:
    monkeypatch.setattr(server_assets, "READINESS_SUCCESS_TTL_SECONDS", 0.1)
    proofs: list[str] = []

    def ready(**_kwargs):
        proofs.append("proof")
        return {"status": "ready", "service": "exomem"}

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", ready)
    asgi = _probe_app()

    asyncio.run(_get_each(asgi, ["/health/ready"] * 2, gap=0.15))

    assert proofs == ["proof", "proof"]


def test_a_revocation_this_process_already_made_shows_on_the_next_probe(
    monkeypatch,
) -> None:
    """`find` and the lexical store revoke admission in process; the cache must not hide it."""
    readiness.mark_ready("retrieval_catalog")

    def proof(**_kwargs):
        admitted = readiness.is_ready("retrieval_catalog")
        return {"status": "ready" if admitted else "not_ready", "service": "exomem"}

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", proof)
    asgi = _probe_app()

    async def scenario() -> list[int]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url="http://cell.local"
        ) as client:
            before = await client.get("/health/ready")
            readiness.mark_unready("retrieval_catalog")
            after = await client.get("/health/ready")
            return [before.status_code, after.status_code]

    assert asyncio.run(scenario()) == [200, 503]


def test_a_standby_proves_every_probe_so_the_supervisor_sees_cutover_land(
    monkeypatch,
) -> None:
    """The supervisor polls a standby's `cutover_ready` every 0.1 s to start the handoff."""
    service_standby.enter_standby()
    proofs: list[str] = []

    def proof(**_kwargs):
        proofs.append("proof")
        return {
            "status": "ready",
            "service": "exomem",
            "cutover": {
                "standby": service_standby.in_standby(),
                "cutover_ready": len(proofs) > 1,
            },
        }

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", proof)
    asgi = _probe_app()

    first, second = asyncio.run(_get_each(asgi, ["/health/ready"] * 2))

    assert first.json()["cutover"]["cutover_ready"] is False
    assert second.json()["cutover"]["cutover_ready"] is True
    assert proofs == ["proof", "proof"]


class _Activation:
    def release(self) -> None:
        pass


def test_a_proof_taken_across_a_promotion_is_not_served_after_it(
    monkeypatch, tmp_path: Path
) -> None:
    """The supervisor promotes a standby, then waits for its readiness.

    A proof that began before promotion measured the standby's lease and
    catalogue state. It may finish after the flip and read `standby: false`,
    which would make it look like a promoted worker's proof.
    """
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    entered = threading.Event()
    finish = threading.Event()
    proofs: list[str] = []

    def proof(**_kwargs):
        proofs.append("proof")
        if len(proofs) == 1:
            entered.set()
            finish.wait(5.0)
        return {
            "status": "ready",
            "service": "exomem",
            "cutover": {"standby": service_standby.in_standby()},
        }

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", proof)
    asgi = _probe_app()

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url="http://cell.local"
        ) as client:
            straddling = asyncio.create_task(client.get("/health/ready"))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            service_standby.promote(tmp_path, migrated=False)
            finish.set()
            assert (await straddling).json()["cutover"]["standby"] is False
            await client.get("/health/ready")

    asyncio.run(scenario())

    assert proofs == ["proof", "proof"]


def test_a_cached_answer_still_counts_this_probe_in_the_traffic_block(
    monkeypatch,
) -> None:
    """`doctor --probe` reads these counters to spot an origin that only sees probes."""

    def ready(*, traffic=None, **_kwargs):
        return {"status": "ready", "service": "exomem", "traffic": dict(traffic or {})}

    monkeypatch.setattr(runtime_readiness_module, "runtime_readiness", ready)
    asgi = _probe_app()

    responses = asyncio.run(_get_each(asgi, ["/health/ready"] * 3))

    counts = [r.json()["traffic"]["health_probe_count"] for r in responses]
    assert counts[1] == counts[0] + 1
    assert counts[2] == counts[0] + 2


# -- /health -----------------------------------------------------------------


def test_health_reports_the_provenance_of_the_code_that_is_running(monkeypatch) -> None:
    """An in-place upgrade rewrites the package metadata under a running worker.

    Re-reading it made `/health` claim the new release while the old code
    still served, and cost a metadata walk on every liveness refresh.
    """
    versions = iter(f"v{n}" for n in range(1, 100))
    reads: list[str] = []

    def provenance(**_kwargs):
        reads.append("read")
        return {"version": next(versions)}

    monkeypatch.setattr(deploy_provenance, "provenance", provenance)
    monkeypatch.setattr(server_assets, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.0)
    asgi = _probe_app()

    responses = asyncio.run(_get_each(asgi, ["/health"] * 6, gap=0.02))

    assert [r.json()["version"] for r in responses] == ["v1"] * 6
    assert reads == ["read"]


def test_health_reads_provenance_again_after_a_failed_first_read(monkeypatch) -> None:
    """A transient metadata failure at start must not pin `version: unknown` for life."""
    outcomes = iter([RuntimeError("metadata unavailable")])

    def provenance(**_kwargs):
        failure = next(outcomes, None)
        if failure is not None:
            raise failure
        return {"version": "v1"}

    monkeypatch.setattr(deploy_provenance, "provenance", provenance)
    monkeypatch.setattr(server_assets, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.0)
    asgi = _probe_app()

    responses = asyncio.run(_get_each(asgi, ["/health"] * 4, gap=0.05))

    assert responses[0].json()["version"] == "unknown"
    assert responses[-1].json()["version"] == "v1"
