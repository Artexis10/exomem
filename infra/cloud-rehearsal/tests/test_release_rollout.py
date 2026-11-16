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
from cloud_rehearsal.mcp_client import ToolResult, read_refusal_body, refusal_text
from cloud_rehearsal.release_rollout import (
    CELL_GIB,
    HOLD_BUDGET_SECONDS,
    LOCAL,
    MIN_MINUTES_LEFT,
    POOL_GIB,
    SLOTS_AT_REST,
    WriteLoop,
    deploy_settings,
    deploy_transport,
    lists_start_times,
    load_cloud_deploy,
    missing_writes,
    parse_marker_probe,
    pause_alert_lines,
    poll_failures,
    require_admission_room,
    seconds_until_next_day,
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


def _http_error(status: int, body: bytes) -> httpx.HTTPStatusError:
    """The error the SDK raises on a refused streamed POST, with the body read as the harness's response hook reads it."""

    async def refused() -> httpx.HTTPStatusError:
        def answer(_request):
            return httpx.Response(status, stream=httpx.ByteStream(body))

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), event_hooks={"response": [read_refusal_body]}) as client:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                async with client.stream("POST", "https://mcp.rehearsal.test/mcp") as response:
                    response.raise_for_status()
        return raised.value

    return asyncio.run(refused())


def test_a_poll_error_keeps_its_time_and_the_start_of_a_plain_text_502() -> None:
    """The error code alone calls a proxy's plain-text answer "<non-JSON body>", which hid the "Bad Gateway" behind a
    failed step 23. The sample must name the time, to line up with the edge's log, and the start of the body."""

    body = b"Bad Gateway" + b" and more" * 40
    summary = _poll(_Connector(_Session(_http_error(502, body)), _Session("answers")), calls=3)
    [sample] = summary["error_samples"]
    assert sample["error"] == "HTTP 502 <non-JSON body>"
    assert sample["response_body"] == body[:120].decode()
    assert dt.datetime.fromisoformat(sample["at"]).utcoffset() == dt.timedelta(0)
    assert "Bad Gateway" in poll_failures("C", summary)[0]


class _Writer:
    """A session whose writes answer its script in order and repeat the last answer."""

    def __init__(self, *script: ToolResult) -> None:
        self.script = list(script)
        self.calls = 0

    async def governed_write(self, arguments):
        self.calls += 1
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


ACKNOWLEDGED = ToolResult({"status": "created"}, False, 0.0)
REFUSED = ToolResult({"error": {"code": "CELL_STOPPED"}}, True, 0.0)


def _write_loop(monkeypatch, session: _Writer, hold_after_calls: int) -> WriteLoop:
    """A loop on the canary whose row shows the upgrade hold once `session` has been called `hold_after_calls` times."""
    monkeypatch.setattr(release_rollout, "WRITE_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(release_rollout, "WRITE_WATCH_SECONDS", 0.005)
    held_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)  # after every write this test acknowledges

    async def cell(_cell_id):
        return {"hold_kind": "upgrade", "hold_started_at": held_at} if session.calls >= hold_after_calls else {"hold_kind": None}

    tenant = SimpleNamespace(cell_id="aaaa", phrase="ultramarine-1234abcd-heron", client=_Connector(session))
    return WriteLoop(SimpleNamespace(cell=cell), tenant)


def test_the_write_loop_never_counts_a_refused_write_and_still_records_the_stop_after_it(monkeypatch) -> None:
    """Step 21 looks for every counted write in the restored canary: a write the cell refused is not in it, and the
    stop is what the last acknowledgement is measured against, whichever way the loop ended."""

    async def run() -> dict:
        loop = _write_loop(monkeypatch, _Writer(ACKNOWLEDGED, ACKNOWLEDGED, ACKNOWLEDGED, REFUSED), hold_after_calls=4)
        loop.start()
        while loop.hold_seen_at is None:
            await asyncio.sleep(0.005)
        assert [write.token for write in loop.acknowledged] == [f"ultramarine-1234abcd-heron-w000{n}" for n in (1, 2, 3)]
        return await loop.finish()

    summary = asyncio.run(run())
    assert summary["acknowledged_writes"] == 3
    assert summary["ended_by"] == "a write the cell refused: CELL_STOPPED"
    assert summary["seconds_from_last_acknowledgement_to_hold_started"] > 0


def test_the_write_loop_ends_at_the_canarys_stop_when_no_write_was_refused(monkeypatch) -> None:
    """A gateway that holds a write through the stop acknowledges it late: the watch, not a refusal, must end the loop
    and give step 21 the instant that the last acknowledgement is measured against."""

    async def run() -> dict:
        session = _Writer(ACKNOWLEDGED)
        loop = _write_loop(monkeypatch, session, hold_after_calls=3)
        loop.start()
        while loop.hold_seen_at is None:
            await asyncio.sleep(0.005)
        calls_at_the_stop = session.calls
        await asyncio.sleep(0.05)
        assert session.calls <= calls_at_the_stop + 1
        return await loop.finish()

    summary = asyncio.run(run())
    assert summary["ended_by"] == "the canary's stop for its attempt"
    assert summary["acknowledged_writes"] >= 3
    assert summary["seconds_from_last_acknowledgement_to_hold_started"] > 0


def test_a_found_write_needs_no_second_search_and_the_last_write_is_looked_for_by_itself() -> None:
    class Searcher:
        def __init__(self, shown: dict[str, str]) -> None:
            self.shown, self.queries = shown, []

        async def call(self, _name, arguments):
            self.queries.append(arguments["query"])
            return SimpleNamespace(text=lambda: self.shown.get(arguments["query"], ""))

    tokens = ["heron-w0001", "heron-w0002", "heron-w0003"]
    # The shared search lists w0001 only; w0002 is found by itself; the last write is lost.
    session = Searcher({"heron": "heron-w0001", "heron-w0002": "heron-w0002"})
    missing = asyncio.run(missing_writes(session, "heron", tokens, last_timeout=0))
    assert missing == ["heron-w0003"]
    assert session.queries == ["heron-w0003", "heron", "heron-w0002"]


def test_the_marker_probe_is_read_as_a_json_object_or_not_at_all() -> None:
    assert parse_marker_probe('{"/data": true, "/data/host": false}') == {"/data": True, "/data/host": False}
    assert parse_marker_probe("Traceback (most recent call last)") is None
    assert parse_marker_probe("[1, 2]") is None


@pytest.mark.parametrize(
    ("clock", "waits"),
    [("2026-11-15T21:00:00", False), ("2026-11-15T23:15:00", False), ("2026-11-15T23:16:00", True)],
)
def test_the_steps_that_hold_cached_windows_wait_past_midnight_when_the_day_is_nearly_over(clock, waits) -> None:
    now = dt.datetime.fromisoformat(clock).replace(tzinfo=dt.UTC)
    wait = seconds_until_next_day(now)
    assert (wait > 0) is waits
    if waits:
        assert (now + dt.timedelta(seconds=wait)).date() == now.date() + dt.timedelta(days=1)


@pytest.mark.parametrize(
    ("status", "body", "passes"),
    [
        (403, {"error": {"code": "OPERATOR_SCOPE_DENIED"}}, True),
        (401, {"error": {"code": "OPERATOR_UNAUTHORIZED"}}, False),
        (403, {"error": {"code": "SOMETHING_ELSE"}}, False),
        (200, {"success": True}, False),
    ],
)
def test_only_a_403_with_the_scope_code_proves_the_deploy_token_cannot_clear_the_pause(status, body, passes) -> None:
    from cloud_rehearsal import report as report_module

    async def deploy_rollout():
        return {"paused": True, "cell_image": "image@sha256:1"}

    async def release_request(_token, _payload):
        return httpx.Response(status, json=body)

    ctx = SimpleNamespace(
        deploy_rollout=deploy_rollout, release_request=release_request,
        images=SimpleNamespace(cell_v2="image@sha256:2"),
        substrate=SimpleNamespace(secrets=SimpleNamespace(deploy_token="deploy-token")))
    record = report_module.StepRecord(22, "deploy_token_cannot_clear_the_pause")
    proof = release_rollout.Rollout(ctx).deploy_token_cannot_clear_the_pause(record)
    if passes:
        asyncio.run(proof)
    else:
        with pytest.raises(report_module.StepFailure, match="not 403 OPERATOR_SCOPE_DENIED"):
            asyncio.run(proof)
