"""A cell that proved its recall catalogue stays routable while the catalogue lags.

During a long import the cell's own writes move the live recall projection ahead
of the maintained catalogue for moments at a time. Per-request recall serves
that lag, but `/health/ready` used to answer 503 for it, and the orchestrator
pulled the only pod out of its Service. These tests drive the real probe route
over a real vault, projection and catalogue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import initialize_vault_state_offline
from fastmcp import FastMCP

from exomem import freshness, lexstore, readiness, server_assets, warmup


@pytest.fixture(autouse=True)
def _clean_process_state() -> Iterator[None]:
    readiness.reset()
    yield
    readiness.reset()
    freshness.clear()


class _Runtime:
    def __init__(self, vault: Path, page: Path) -> None:
        self.vault = vault
        self.page = page
        self.edits = 0

    def advance_projection(self) -> None:
        """A write moves the live recall projection past the published catalogue."""
        self.edits += 1
        self.page.write_text(f"# Probe\n\nedit number {self.edits}\n", encoding="utf-8")
        freshness.on_files_changed(self.vault, changed=[self.page])

    def reseed_projection(self) -> None:
        """The live projection comes back from the vault as it is now."""
        entry = (str(self.page), freshness.stat_signature(self.page))
        for scope in freshness.SCOPES:
            freshness.seed(self.vault, scope, [entry])

    def probe(self, *, times: int = 1, gap: float = 0.0) -> list[httpx.Response]:
        async def scenario() -> list[httpx.Response]:
            app = FastMCP("serve-while-lagging")
            server_assets.register_health_routes(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.http_app(transport="streamable-http")),
                base_url="http://cell.test",
            ) as client:
                answers = []
                for _ in range(times):
                    answers.append(await client.get("/health/ready"))
                    await asyncio.sleep(gap)
                return answers

        return asyncio.run(scenario())


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    """A managed process whose warm proved the catalogue exactly, once."""
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    vault = tmp_path / "vault"
    page = vault / "Knowledge Base" / "probe.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Probe\n\ninitial\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    initialize_vault_state_offline(vault, source="serve while lagging fixture")
    for scope in freshness.SCOPES:
        freshness.seed(vault, scope, [(str(page), freshness.stat_signature(page))])
    lexstore.ensure_fresh(vault)
    readiness.manage_runtime()
    readiness.begin_warm()
    assert warmup.warm_retrieval_catalog(vault)
    readiness.finish_warm()
    served = _Runtime(vault, page)
    [ready] = served.probe()
    assert ready.status_code == 200 and ready.json()["retrieval"]["state"] == "ready"
    return served


def test_a_proven_process_stays_ready_while_the_catalogue_trails_its_projection(
    runtime: _Runtime,
) -> None:
    """Catches the 503 that pulled the pod out of its Service mid-import."""
    runtime.advance_projection()

    [lagging] = runtime.probe()

    body = lagging.json()
    assert lagging.status_code == 200, body
    assert body["status"] == "ready"
    assert body["takeover_eligible"] is True
    assert body["reasons"] == []
    retrieval = body["retrieval"]
    assert retrieval["state"] == "lagging"
    assert retrieval["admitted"] is False
    assert retrieval["lagging_scopes"] and set(retrieval["lagging_scopes"]) <= set(freshness.SCOPES)
    assert isinstance(retrieval["catalogue_unchanged_seconds"], float)


def test_the_lag_age_counts_a_stuck_catalogue_and_restarts_only_when_it_publishes_again(
    runtime: _Runtime,
) -> None:
    """A busy catalogue must not read as a stuck one, nor a stuck one as busy."""
    runtime.advance_projection()
    first, second = runtime.probe(times=2, gap=0.05)
    stuck = second.json()["retrieval"]["catalogue_unchanged_seconds"]
    assert stuck > first.json()["retrieval"]["catalogue_unchanged_seconds"]

    # One probe that is not lagging says nothing about the published pair.
    freshness.invalidate(runtime.vault)
    assert runtime.probe()[0].status_code == 503
    runtime.reseed_projection()
    [resumed] = runtime.probe()
    assert resumed.json()["retrieval"]["state"] == "lagging"
    assert resumed.json()["retrieval"]["catalogue_unchanged_seconds"] > stuck

    lexstore.ensure_fresh(runtime.vault)  # the catalogue publishes a newer pair
    runtime.advance_projection()
    [after_publication] = runtime.probe()

    assert after_publication.json()["retrieval"]["state"] == "lagging"
    assert after_publication.json()["retrieval"]["catalogue_unchanged_seconds"] < stuck


@pytest.mark.parametrize("lost", ["live projection", "published projection"])
def test_a_lost_projection_is_not_ready_even_for_a_proven_process(
    runtime: _Runtime, lost: str
) -> None:
    """Catches a lagging state that hides a projection recall can no longer serve."""
    runtime.advance_projection()
    if lost == "live projection":
        freshness.invalidate(runtime.vault)
    else:
        lexstore.get_store(runtime.vault).path.unlink()

    [answer] = runtime.probe()

    assert answer.status_code == 503, answer.json()
    assert answer.json()["retrieval"]["state"] == "unavailable"


def test_a_new_warm_waits_for_a_strict_proof_however_servable_the_lag(
    runtime: _Runtime,
) -> None:
    """Catches a proof flag leaking across a worker or standby handoff."""
    runtime.advance_projection()
    assert runtime.probe()[0].json()["retrieval"]["state"] == "lagging"

    readiness.begin_warm()
    readiness.finish_required_warm()  # the warm's optional preloads still run
    [warming] = runtime.probe()
    assert warming.status_code == 503, warming.json()
    assert warming.json()["retrieval"]["state"] == "warming"

    readiness.finish_warm()  # the warm ends without a strict proof: the catalogue still lags
    [unproven] = runtime.probe()
    assert unproven.status_code == 503, unproven.json()
    assert unproven.json()["retrieval"]["state"] == "unavailable"

    lexstore.ensure_fresh(runtime.vault)
    assert warmup.warm_retrieval_catalog(runtime.vault)
    [proven] = runtime.probe()
    assert proven.status_code == 200 and proven.json()["retrieval"]["state"] == "ready"


def test_the_lag_is_served_only_once_the_whole_warm_has_finished(runtime: _Runtime) -> None:
    """Catches health saying ready while `find` refuses with the warm still running."""
    readiness.begin_warm()  # a promoted worker re-warms; its optional preloads outlast the rest
    assert warmup.warm_retrieval_catalog(runtime.vault)
    readiness.finish_required_warm()
    runtime.advance_projection()

    [during_preloads] = runtime.probe()
    readiness.finish_warm()
    [after_warm] = runtime.probe()

    assert during_preloads.status_code == 503
    assert during_preloads.json()["retrieval"]["state"] == "warming"
    assert after_warm.status_code == 200
    assert after_warm.json()["retrieval"]["state"] == "lagging"


def test_an_unmanaged_proof_does_not_make_a_later_lag_ready(runtime: _Runtime) -> None:
    """A standby's `mark_ready` runs unmanaged and must not stand in for a worker's proof."""
    readiness.unmanage_runtime()
    readiness.mark_ready("retrieval_catalog")
    runtime.advance_projection()

    [answer] = runtime.probe()

    assert answer.status_code == 503, answer.json()
    assert answer.json()["retrieval"]["state"] == "unavailable"


def test_a_lagging_answer_is_not_served_again_from_the_ready_proof_cache(
    runtime: _Runtime,
) -> None:
    """Catches a lag answer outliving the projection it described by up to the cache TTL."""
    runtime.advance_projection()
    app = FastMCP("serve-while-lagging-cache")
    server_assets.register_health_routes(app)

    async def scenario() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.http_app(transport="streamable-http")),
            base_url="http://cell.test",
        ) as client:
            await client.get("/health/ready")  # the first lagging probe revokes strict admission
            before = await client.get("/health/ready")
            freshness.invalidate(runtime.vault)  # moves no generation the cache watches
            after = await client.get("/health/ready")
            return [before, after]

    before, after = asyncio.run(scenario())

    assert before.json()["retrieval"]["state"] == "lagging"
    assert after.status_code == 503, after.json()
