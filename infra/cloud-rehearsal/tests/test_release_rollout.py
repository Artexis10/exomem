"""The release mode's own decisions and wiring.

Each test guards something a wrong edit would otherwise show only after the mode's long run on
a disposable cluster: the flag, the deploy token, the chart values the proofs depend on, the
broken target's marker, and the contracts with cellctl's window parser and the workflow script.
The run itself needs a K3s cluster and is exercised by the `release` workflow mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from cellctl.capacity import LocalCapacityObservation, LogicalVolumeRecord, compute_local_capacity
from cellctl.manifests import BACKUP_PATHS
from cellctl.storage_config import GIB
from cellctl.upgrade_window import parse_window
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from cloud_rehearsal import build, platform, release_rollout, substrate
from cloud_rehearsal.__main__ import _parser, main
from cloud_rehearsal.mcp_client import read_refusal_body, refusal_text
from cloud_rehearsal.release_rollout import (
    CELL_GIB,
    HOLD_BUDGET_SECONDS,
    LOCAL,
    MIN_MINUTES_LEFT,
    POOL_GIB,
    SLOTS_AT_REST,
    deploy_settings,
    deploy_transport,
    lists_start_times,
    load_cloud_deploy,
    pause_alert_lines,
    poll_failures,
    require_admission_room,
    seconds_until_quiet_hour,
    summarize_calls,
    window_annotations,
)


def test_the_release_flag_selects_the_mode_and_runs_alone(capsys) -> None:
    assert _parser().parse_args(["run"]).release is False
    assert _parser().parse_args(["run", "--release"]).release is True
    with pytest.raises(SystemExit):
        main(["run", "--release", "--large-import"])
    assert "--release runs alone; remove --large-import" in capsys.readouterr().err


def test_the_deploy_token_reaches_substrate_apart_from_the_admin_token() -> None:
    """The workflow's principal must exist in Substrate, and must never be the admin token."""

    secrets = substrate.SubstrateSecrets()
    stack = SimpleNamespace(postgres=SimpleNamespace(dsn=lambda *_args, **_kwargs: "postgresql://unused"))
    env = substrate._app_environment(stack, secrets, "registry.test/cell")
    assert env["EXOMEM_DEPLOY_TOKEN"] == secrets.deploy_token
    assert env["EXOMEM_ADMIN_TOKEN"] == secrets.admin_token
    assert secrets.deploy_token != secrets.admin_token


def _render_values(monkeypatch, tmp_path, **config) -> dict:
    values: dict = {}

    def render(_stack, rendered_values, **_kwargs):
        values.update(rendered_values)
        return [("templates/cloud-storage-class.yaml", {"kind": "StorageClass", "metadata": {"name": "test-storage"}})]

    monkeypatch.setattr(platform, "render_chart", render)
    stack = SimpleNamespace(postgres=SimpleNamespace(ip="192.0.2.3"), workdir=tmp_path, k3s=SimpleNamespace(ip="192.0.2.5"),
                            object_store=SimpleNamespace(endpoint=lambda **_kwargs: "https://192.0.2.4"))
    platform._render(stack, platform.PlatformConfig("controller", "gateway", "cell", "0-1", "https://example.test", "/mcp",
                                                    "source", **config))
    return values


def test_the_release_render_turns_the_gateway_hold_on_and_shortens_the_idle_rule(monkeypatch, tmp_path) -> None:
    """The chart's hold budget is 0, which turns the hold off and would make the switch polls prove nothing."""

    release = _render_values(monkeypatch, tmp_path, upgrade_idle_minutes=release_rollout.IDLE_MINUTES,
                             upgrade_force_minutes=release_rollout.FORCE_MINUTES,
                             gateway_hold_budget_seconds=HOLD_BUDGET_SECONDS)
    assert release["cloudGateway"]["hold"]["budgetSeconds"] > 0
    assert release["cellctl"]["upgradeWindow"]["idleMinutes"] == 1
    ordinary = _render_values(monkeypatch, tmp_path)
    assert "hold" not in ordinary["cloudGateway"]
    assert "idleMinutes" not in ordinary["cellctl"]["upgradeWindow"]


def test_the_broken_target_leaves_its_marker_in_every_backed_up_path(tmp_path) -> None:
    """If the marker did not land in the backed-up paths, a restore that removes it would prove nothing."""

    for path in BACKUP_PATHS:
        (tmp_path / path.lstrip("/")).mkdir(parents=True)
    shim = build.BROKEN_SHIM.replace("exec sleep 2147483647", "true")
    for path in BACKUP_PATHS:
        shim = shim.replace(path, str(tmp_path / path.lstrip("/")))
    subprocess.run(["sh", "-c", shim, "shim", "serve"], check=True, timeout=30)
    for path in BACKUP_PATHS:
        assert (tmp_path / path.lstrip("/") / build.BROKEN_MARKER).exists()


def test_cellctl_parses_and_keeps_a_hand_set_window_for_the_day() -> None:
    """cellctl recomputes a window it cannot parse, or a window stamped for another day."""

    day = dt.date(2026, 3, 9)
    window, stamped = (item.split("=", 1)[1] for item in window_annotations(7, day))
    assert parse_window(window) is not None and parse_window(window).hour == 7
    assert stamped == day.isoformat()


@pytest.mark.parametrize(
    ("clock", "waits"),
    [("2026-03-09T10:00:00", False), (f"2026-03-09T10:{60 - MIN_MINUTES_LEFT:02d}:00", False),
     (f"2026-03-09T10:{60 - MIN_MINUTES_LEFT + 1:02d}:00", True), ("2026-03-09T10:59:30", True)],
)
def test_a_window_opens_only_with_enough_of_the_hour_left_for_two_attempts(clock, waits) -> None:
    now = dt.datetime.fromisoformat(clock).replace(tzinfo=dt.UTC)
    wait = seconds_until_quiet_hour(now)
    assert (wait > 0) is waits
    if waits:
        assert (now + dt.timedelta(seconds=wait)).minute < 1


def _calls(*outcomes: tuple[float, str | None]):
    return summarize_calls([release_rollout.Call(seconds, error) for seconds, error in outcomes])


def test_a_poll_that_errors_names_whether_the_hold_ran_out_or_never_held() -> None:
    assert poll_failures("B", _calls((0.2, None), (3.0, None))) == []
    assert "never polled" in poll_failures("B", _calls())[0]
    early = poll_failures("B", _calls((0.2, None), (1.5, "connection reset")))
    late = poll_failures("B", _calls((HOLD_BUDGET_SECONDS + 1, "timed out")))
    assert "before" in early[0] and "after waiting out" in late[0]


def test_the_rehearsal_runs_the_workflows_own_script_and_its_paused_stop(capsys) -> None:
    """The proof of the workflow's paused path is only as good as the script it runs: the real script, no copy."""

    deploy = load_cloud_deploy()
    settings = deploy_settings(deploy, "deploy-token")
    assert settings.substrate_url.startswith("https://")
    view = {"paused": True, "error_code": "UPGRADE_READINESS_TIMEOUT", "held_cell_id": "cell-123"}
    with pytest.raises(deploy.Stop) as stopped:
        deploy.refuse_paused(view)
    assert deploy.finish(stopped.value) == 1
    assert capsys.readouterr().out.strip() == "cloud-deploy: ROLLOUT_PAUSED UPGRADE_READINESS_TIMEOUT"


def test_the_workflows_client_reports_a_network_failure() -> None:
    """The script's client reports an OSError as SUBSTRATE_UNREACHABLE; an httpx error would escape it as a crash."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    deploy = load_cloud_deploy()
    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        substrate_client = deploy.Substrate(deploy_settings(deploy, "deploy-token"), deploy_transport(client))
        with pytest.raises(deploy.Stop) as stopped:
            substrate_client.rollout()
    assert stopped.value.code == "SUBSTRATE_UNREACHABLE"


def test_a_failed_proof_fails_the_stage_and_blocks_only_what_needs_it(monkeypatch) -> None:
    """The run's exit code reads the stage status: a failed proof must not leave it passed, and the poll check must still run."""
    import asyncio

    from cloud_rehearsal import report as report_module

    async def passes(_rollout, _record):
        pass

    async def fails(_rollout, _record):
        raise report_module.StepFailure("the canary did not roll")

    async def poll_check(_rollout, _record):
        pass

    monkeypatch.setattr(release_rollout, "PROOFS", [(13, passes, ()), (14, fails, (13,)), (15, passes, (14,)), (23, poll_check, ())])
    ctx = SimpleNamespace(report=report_module.Report("release"))
    asyncio.run(release_rollout.run_release_rollout(ctx))
    steps = {step.number: step for step in ctx.report.steps}
    assert [steps[number].status for number in (13, 14, 15, 23)] == ["passed", "failed", "blocked", "passed"]
    assert "step(s) [14]" in steps[15].failure["message"]
    assert ctx.report.stages["release_rollout"]["status"] == "failed"


def test_the_pool_publishes_a_slot_for_each_cell_with_the_canarys_attempt_held() -> None:
    """The first run found no slot for the second cell: cellctl publishes what the pool holds beyond its backup
    reserve, so a pool or a cell size that leaves less than three slots stops the mode at its first redemption."""
    cells = {"a": CELL_GIB, "b": CELL_GIB, "c": CELL_GIB}
    # The three cells, then the canary's attempt: a snapshot and its clone, each at the cell's size.
    volumes = tuple(LogicalVolumeRecord("node", CELL_GIB * GIB, LOCAL.device_class, True, False) for _ in range(5))
    free = POOL_GIB * GIB - sum(volume.size for volume in volumes)
    observation = LocalCapacityObservation(free_bytes={"node": free}, volumes=volumes,
                                           cell_nodes={cell_id: "node" for cell_id in cells})
    published = compute_local_capacity(observation, local=LOCAL, cell_sizes=cells)["node"].cell_slots
    assert published == SLOTS_AT_REST
    assert published - len(cells) >= 1


def test_the_stage_stops_before_a_redemption_that_substrate_would_refuse() -> None:
    import asyncio

    from cloud_rehearsal import report as report_module

    async def fetchrow(_sql):
        return {"published_slots": 0, "live_cells": 1, "cell_gib": '{"abc": 10}'}

    ctx = SimpleNamespace(fetchrow=fetchrow)
    record = report_module.StepRecord(13, "provision_disposable_cells")
    with pytest.raises(report_module.StepFailure) as stopped:
        asyncio.run(require_admission_room(ctx, record, "B and C", room=2, timeout=0))
    assert "published 0 slot(s) for 1 live cell(s)" in str(stopped.value)
    assert record.evidence["admission"]["B and C"] == {"published_slots": 0, "live_cells": 1, "cell_gib": {"abc": 10}, "room": -1}


def test_admission_with_room_records_the_published_slots_and_goes_on() -> None:
    import asyncio

    from cloud_rehearsal import report as report_module

    async def fetchrow(_sql):
        return {"published_slots": SLOTS_AT_REST, "live_cells": 1, "cell_gib": '{"abc": 4}'}

    record = report_module.StepRecord(13, "provision_disposable_cells")
    asyncio.run(require_admission_room(SimpleNamespace(fetchrow=fetchrow), record, "B and C", room=2, timeout=0))
    assert record.evidence["admission"]["B and C"]["published_slots"] == SLOTS_AT_REST


def test_a_step_that_never_provisioned_blocks_the_poll_check_and_every_proof_after_it() -> None:
    """The first run read "switch A was never polled" as a finding of its own, though step 13 had failed and no
    poller had started. With the real proof table, the poll check is blocked and names the step that failed."""
    import asyncio

    from cloud_rehearsal import report as report_module

    ctx = SimpleNamespace(report=report_module.Report("release"), a=SimpleNamespace(client=None, cell_id=None))
    asyncio.run(release_rollout.run_release_rollout(ctx))
    steps = {step.number: step for step in ctx.report.steps}
    assert steps[13].status == "failed"
    assert {steps[number].status for number in steps if number != 13} == {"blocked"}
    assert "the failed step is [13]" in steps[23].failure["message"]
    assert ctx.report.stages["release_rollout"]["status"] == "failed"


def test_only_the_line_where_the_pause_alert_fires_for_the_canary_counts() -> None:
    delivery_failure = "ERROR cellctl: cloud-rollout-paused alert delivery failed; retrying in a minute: 404"
    other_cell = "WARNING cellctl: cloud-rollout-paused: bbbbbbbbbbbbbbbb"
    fires = "WARNING cellctl: cloud-rollout-paused: aaaaaaaaaaaaaaaa"
    log = "\n".join([delivery_failure, other_cell, fires])
    assert pause_alert_lines(delivery_failure + "\n" + other_cell, "aaaaaaaaaaaaaaaa") == 0
    assert pause_alert_lines(log, "aaaaaaaaaaaaaaaa") == 1


def test_the_key_rotation_listing_passes_only_with_a_start_time_for_every_cell() -> None:
    """A pod that is still Pending lists `<none>` as its start, and a cell missing from the listing leaves the
    runbook's wait for a restart with nothing to read."""
    cells = ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
    listing = "NAMESPACE              STARTED\nexo-cell-aaaaaaaaaaaaaaaa   2026-11-15T10:00:00Z\nexo-cell-bbbbbbbbbbbbbbbb   {}\n"
    assert lists_start_times(listing.format("2026-11-15T10:01:00Z"), cells) is None
    assert "exo-cell-bbbbbbbbbbbbbbbb" in lists_start_times(listing.format("<none>"), cells)
    assert "exo-cell-bbbbbbbbbbbbbbbb" in lists_start_times(listing.split("exo-cell-bbbb")[0], cells)


class _Session:
    """A session that answers its script in order and repeats the last answer: a result, or an error to raise."""

    def __init__(self, *script) -> None:
        self.script = list(script)

    async def call(self, _name, _arguments):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(is_error=False, error_code=None)


class _Connector:
    """A tenant's connector: each `mcp()` opens the next scripted session, and `opened` counts them."""

    def __init__(self, *sessions: _Session) -> None:
        self.sessions = list(sessions)
        self.opened = 0

    @contextlib.asynccontextmanager
    async def mcp(self):
        session = self.sessions[min(self.opened, len(self.sessions) - 1)]
        self.opened += 1
        yield session


def _poll(connector: _Connector, calls: int) -> dict:
    async def run() -> dict:
        poller = release_rollout.SwitchPoller("A", SimpleNamespace(client=connector), interval=0.001)
        poller.start()
        while len(poller.calls) < calls:
            await asyncio.sleep(0.005)
        return await poller.stop()

    return asyncio.run(run())


def test_a_poller_reuses_its_session_across_calls() -> None:
    """The second run answered 429 to almost half of its polls: a session per call sent three requests from the
    harness's address each time, over the gateway's 120 a minute. A session kept open sends a request per call."""
    connector = _Connector(_Session("answers"))
    summary = _poll(connector, calls=8)
    assert connector.opened == 1
    assert summary["sessions"] == 1
    assert summary["errors"] == 0


def test_a_poller_opens_another_session_when_the_server_says_it_is_gone_and_repeats_the_call() -> None:
    gone = McpError(ErrorData(code=32600, message="Session terminated"))
    connector = _Connector(_Session(gone), _Session("answers"))
    summary = _poll(connector, calls=3)
    assert summary["sessions"] == 2
    assert summary["errors"] == 0


def test_a_refused_call_names_the_gateways_error_code_and_the_next_poll_opens_a_new_session() -> None:
    async def refused(hooks: list) -> httpx.HTTPStatusError:
        # The body arrives as a stream, as the SDK's streamed POST receives it.
        def answer(_request):
            return httpx.Response(429, stream=httpx.ByteStream(b'{"error": "RATE_LIMITED"}'))

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), event_hooks={"response": hooks}) as client:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                async with client.stream("POST", "https://mcp.rehearsal.test/mcp") as response:
                    response.raise_for_status()
        return raised.value

    assert refusal_text(asyncio.run(refused([]))) == "HTTP 429 <body not read>"
    error = asyncio.run(refused([read_refusal_body]))
    assert refusal_text(error) == "HTTP 429 RATE_LIMITED"

    connector = _Connector(_Session(error), _Session("answers"))
    summary = _poll(connector, calls=3)
    assert summary["sessions"] == 2
    assert summary["error_kinds"] == {"HTTP 429 RATE_LIMITED": 1}
