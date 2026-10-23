"""Unit tests for the rehearsal's pure parts. The rehearsal itself is the
live test; these pin the pieces a wrong edit would silently weaken."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from cloud_rehearsal import build, platform, report, substrate, tls
from cloud_rehearsal.mcp_client import _HiddenInputs
from cloud_rehearsal.shell import wait_for


def test_wait_for_propagates_error_when_retries_are_disabled() -> None:
    calls = 0

    def probe() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("ambiguous result")

    with pytest.raises(ValueError, match="ambiguous result"):
        wait_for(probe, timeout=1, interval=0, description="a write", retry_exceptions=())
    assert calls == 1


def test_wait_for_retries_transient_error_by_default() -> None:
    calls = 0

    def probe() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("warming")
        return True

    assert wait_for(probe, timeout=1, interval=0, description="a read") is True
    assert calls == 2


@pytest.mark.parametrize("enveloped", [False, True])
def test_cited_recall_records_valid_hit_list_and_envelope(enveloped) -> None:
    """A valid recall reply must not crash before the rehearsal records its citation."""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from cloud_rehearsal import scenarios
    from cloud_rehearsal.mcp_client import ToolResult

    path = "Knowledge Base/Sources/Survey observation.md"
    hits = [{"path": path, "type": "source", "scope": "private", "title": "Recorded survey",
             "updated": "2026-01-01", "excerpt": "preserved observation"}]
    payload = {"hits": hits, "warming": {"components": []}} if enveloped else hits

    async def recall(*_args):
        return ToolResult(structured={"result": payload}, is_error=False, seconds=0.1)

    @asynccontextmanager
    async def mcp():
        yield SimpleNamespace(call=recall)

    run = report.Report(run_id="recall-shape")
    ctx = SimpleNamespace(a=SimpleNamespace(client=SimpleNamespace(mcp=mcp), phrase="preserved observation"), report=run)
    record = report.StepRecord(number=5, name="paraphrased_cited_recall")
    asyncio.run(scenarios.step_5_cited_recall(ctx, record))
    assert record.evidence["cites_the_capture"] is True
    assert record.evidence["cited_paths"] == [path]


def test_cited_recall_reports_wrapped_refusal_without_private_content() -> None:
    """A product refusal must keep its code rather than become a harness exception."""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from cloud_rehearsal import scenarios
    from cloud_rehearsal.mcp_client import ToolResult

    async def recall(*_args):
        return ToolResult(
            structured={"result": {"success": False, "error": {
                "code": "QUERY_TIMEOUT", "message": "Private source details",
            }}},
            is_error=False, seconds=0.1,
        )

    @asynccontextmanager
    async def mcp():
        yield SimpleNamespace(call=recall)

    ctx = SimpleNamespace(
        a=SimpleNamespace(client=SimpleNamespace(mcp=mcp), phrase="invented observation"),
        report=report.Report(run_id="recall-refusal"),
    )
    record = report.StepRecord(number=5, name="paraphrased_cited_recall")
    with pytest.raises(report.StepFailure, match="^ask_memory failed: QUERY_TIMEOUT$"):
        asyncio.run(scenarios.step_5_cited_recall(ctx, record))


def test_upgrade_gate_excludes_pre_release_diagnostics(monkeypatch) -> None:
    """Slow log collection must not falsely fail a timely cell upgrade."""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from cloud_rehearsal import scenarios

    clock = [0.0]
    snapshots = []
    monkeypatch.setattr(scenarios.time, "monotonic", lambda: clock[0])

    def snapshot(_ctx):
        snapshots.append(True)
        clock[0] += 8.0

    monkeypatch.setattr(scenarios, "snapshot_logs", snapshot)

    async def cell(_cell_id):
        return {"observed_image": "image-v1"}

    async def release(_body):
        clock[0] += 2.0

    async def wait_cell(_cell_id, predicate, **_kwargs):
        clock[0] += 3.0
        row = {"observed_image": "image-v2", "observed_state": "running", "ready": True}
        assert predicate(row)
        return row

    async def rollout():
        return {"last_good_image": "image-v2"}

    async def recall(*_args):
        return SimpleNamespace(text=lambda: "preserved finding")

    @asynccontextmanager
    async def mcp():
        yield SimpleNamespace(call=recall)

    run = report.Report(run_id="timing-boundary")
    ctx = SimpleNamespace(
        a=SimpleNamespace(client=SimpleNamespace(mcp=mcp), cell_id="a", phrase="preserved finding"),
        images=SimpleNamespace(cell_v1="image-v1", cell_v2="image-v2"),
        cell=cell, admin_release=release, wait_cell=wait_cell, rollout=rollout, report=run,
    )
    record = report.StepRecord(number=7, name="upgrade_and_recall")
    asyncio.run(scenarios.step_7_upgrade(ctx, record))
    assert snapshots, "the upgrade still needs its pre-replacement log snapshot"
    assert run.single["upgrade_seconds_per_cell"] == 5.0


def test_percentile_is_nearest_rank() -> None:
    assert report.percentile([], 0.95) is None
    assert report.percentile([0.1] * 19 + [5.0], 0.95) == 0.1
    assert report.percentile([0.1] * 18 + [4.0, 5.0], 0.95) == 4.0


def test_targets_are_the_ones_tasks_5_2_and_5_3_state() -> None:
    from cloud_rehearsal import images

    assert report.TARGETS == {
        "initialize_warm_p95_seconds": (0.5, False),
        "tools_list_warm_p95_seconds": (0.5, False),
        "capture_p95_seconds": (1.0, False),
        "cited_recall_p95_seconds": (1.0, False),
        "provision_seconds": (180.0, True),
        "upgrade_seconds_per_cell": (60.0, True),
    }
    tasks = (images.REPO_ROOT / "openspec/changes/adopt-exomem-cloud-plain-cells/tasks.md").read_text(encoding="utf-8")
    section = tasks[tasks.index("## 5. Local rehearsal"):tasks.index("## 6.")]
    for phrase in ("under 3 minutes", "500 ms", "at most 1 s", "under 60 s"):
        assert phrase in section, phrase


def _passing_report() -> report.Report:
    run = report.Report(run_id="x")
    run.steps = [report.StepRecord(number=n, name=str(n), status=report.PASSED) for n in range(1, 13)]
    for name in ("initialize_warm", "tools_list_warm", "capture", "cited_recall"):
        run.sample(name, 0.1)
    run.single.update({"provision_seconds": 60.0, "upgrade_seconds_per_cell": 30.0})
    run.stages["post_checks"] = {"phrases_leaked": 0, "ready_matches_pod": True}
    return run


def test_a_report_gates_the_node_only_when_everything_passed() -> None:
    assert _passing_report().to_json()["outcome"]["gates_node"] is True
    for spoil in (
        lambda r: r.single.update({"provision_seconds": 180.0}),
        lambda r: r.single.update({"upgrade_seconds_per_cell": 60.0}),
        lambda r: setattr(r, "valid", False),
        lambda r: r.defects.append({"owner": "o"}),
        lambda r: r.stages["post_checks"].update({"phrases_leaked": 1}),
        lambda r: r.stages["post_checks"].update({"ready_matches_pod": False}),
        lambda r: r.stages.update({"harness": {"status": "failed"}}),
    ):
        run = _passing_report()
        spoil(run)
        assert run.to_json()["outcome"]["gates_node"] is False


def test_capture_p95_is_reported_but_does_not_hold_the_node_gate() -> None:
    run = _passing_report()
    run.samples["capture"] = [2.0] * 20
    document = run.to_json()
    capture = document["measurements"]["capture_p95_seconds"]
    assert capture["met"] is False and capture["gating"] is False and "P4" in capture["note"]
    assert document["outcome"]["all_targets_met"] is True
    assert document["outcome"]["gates_node"] is True


def test_harness_check_fails_only_on_unknown_findings(tmp_path: Path) -> None:
    from cloud_rehearsal.__main__ import _compare_with_known_findings

    baseline = tmp_path / "known.json"
    baseline.write_text(
        json.dumps({"steps": {
            "3": {"match": "isPkceVerifier", "owner": "o"},
            "9": {"match": "x", "owner": "o"},
            "10": {"match": "A-side cross-recall is unverified", "owner": "o", "each_problem": True},
        }}),
        encoding="utf-8",
    )
    run = _passing_report()
    run.steps[2].status = report.FAILED
    run.steps[2].failure = {"message": "... isPkceVerifier refuses ..."}
    run.steps[4].status = report.BLOCKED
    run.steps[9].status = report.FAILED
    run.steps[9].failure = {"message": "tenant B recalled tenant A's note"}
    result = _compare_with_known_findings(run, baseline, None)
    # Step 5 is not listed; step 10 is listed but failed for another reason.
    assert [item["step"] for item in result["unexpected"]] == [5, 10]
    assert [item["step"] for item in result["resolved_known_findings"]] == [9]

    # A new isolation problem joined beside the known one is not hidden.
    run.steps[9].failure = {"message": "tenant B recalled tenant A's note; tenant A's cell answers 503 "
                            "CELL_NOT_READY, so the A-side cross-recall is unverified (see step 9)"}
    assert 10 in [item["step"] for item in _compare_with_known_findings(run, baseline, None)["unexpected"]]
    run.steps[9].failure = {"message": "tenant A's cell answers 503 CELL_NOT_READY, so the A-side "
                            "cross-recall is unverified (see step 9)"}
    assert 10 not in [item["step"] for item in _compare_with_known_findings(run, baseline, None)["unexpected"]]
    # Without each_problem a message's own "; " is not a separator (step 3's is prose).
    run.steps[2].failure = {"message": "the endpoint refuses it (isPkceVerifier: ...); the SDK draws ..."}
    assert 3 not in [item["step"] for item in _compare_with_known_findings(run, baseline, None)["unexpected"]]


def test_the_shipped_baseline_parses() -> None:
    shipped = Path(__file__).resolve().parents[1] / "known-findings.json"
    steps = json.loads(shipped.read_text(encoding="utf-8"))["steps"]
    assert all(int(step) in range(1, 13) and entry["match"] and entry["owner"] for step, entry in steps.items())


def test_a_cross_lane_failure_is_recorded_with_its_owner() -> None:
    record = report.failure_record(
        report.CrossLaneDefect("boom", component="c", owner="o#1", evidence={"status": 500})
    )
    assert record["cross_lane"] is True and record["owner"] == "o#1" and "traceback" not in record


def test_paddle_signature_is_paddles_scheme() -> None:
    body = b'{"a":1}'
    header = substrate.paddle_signature("secret", body)
    timestamp, digest = (part.split("=", 1)[1] for part in header.split(";"))
    assert digest == hmac.new(b"secret", timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


def test_emailed_secrets_are_seeded_as_sha256_digests_only() -> None:
    assert substrate.sha256("token") == hashlib.sha256(b"token").digest()
    assert len(substrate.paddle_id("sub")) == len("sub_") + 26


def test_the_consent_form_parser_reads_substrates_hidden_fields() -> None:
    page = (
        '<form action="/api/exomem/oauth/authorize/complete" class="mt-8" method="post">'
        '<input name="nonce" type="hidden" value="n1"/>'
        '<input name="confirmation" type="hidden" value="c1"/></form>'
    )
    parser = _HiddenInputs()
    parser.feed(page)
    assert parser.form_action == "/api/exomem/oauth/authorize/complete"
    assert parser.fields == {"nonce": "n1", "confirmation": "c1"}


def test_a_gateway_missing_required_environment_is_named_not_patched() -> None:
    container = {"env": [{"name": name} for name in platform.GATEWAY_REQUIRED_ENV if name != "EXOMEM_CLOUD_MCP_PATH"]}
    assert platform.missing_gateway_env(container) == ["EXOMEM_CLOUD_MCP_PATH"]
    assert len(container["env"]) == len(platform.GATEWAY_REQUIRED_ENV) - 1


def test_the_broken_canary_still_runs_cell_init() -> None:
    assert 'if [ "$1" = "cell-init" ]; then exec "$0-real" "$@"; fi' in build.BROKEN_SHIM


def test_the_gateway_repository_is_the_one_the_chart_schema_admits() -> None:
    schema = json.loads((platform.PLATFORM_CHART / "values.schema.json").read_text(encoding="utf-8"))
    pattern = schema["properties"]["cloudGateway"]["then"]["properties"]["image"]["pattern"]
    import re

    assert re.fullmatch(pattern, f"{build.GATEWAY_REPOSITORY}@sha256:{'a' * 64}")


def test_pki_signs_both_names(tmp_path: Path) -> None:
    pki = tls.make_pki(tmp_path)
    assert set(pki.leaves) == {tls.SUBSTRATE_HOST, tls.MCP_HOST}
    assert pki.ca_path.read_text(encoding="utf-8").startswith("-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize("path", ["Dockerfile", "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"])
def test_repository_inputs_exist(path: str) -> None:
    from cloud_rehearsal import images

    assert (images.REPO_ROOT / path).exists()


@pytest.mark.parametrize("hostname", [tls.MCP_HOST, "127.0.0.1"])
def test_pki_verifies_under_strict_x509(tmp_path: Path, hostname: str) -> None:
    import socket
    import ssl
    import threading

    pki = tls.make_pki(tmp_path, (hostname,))
    leaf = pki.leaves[hostname]
    (tmp_path / "leaf.pem").write_text(leaf.cert_pem + leaf.key_pem, encoding="utf-8")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "leaf.pem")
    client_context = ssl.create_default_context(cafile=str(pki.ca_path))
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            server_context.wrap_socket(connection, server_side=True).close()
        except ssl.SSLError:
            connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client_context.wrap_socket(raw, server_hostname=hostname) as wrapped:
            assert wrapped.version()
    thread.join(timeout=5)
    listener.close()


def test_dist_info_is_matched_by_distribution_name() -> None:
    from cloud_rehearsal.build import _dist_name

    assert _dist_name("urllib3-2.5.0.dist-info") == _dist_name("urllib3-2.2.3.dist-info")
    assert _dist_name("python_dateutil-2.9.0.dist-info") == "python-dateutil"


def test_the_token_request_observer_records_the_verifier_shape_not_the_verifier() -> None:
    import asyncio

    import httpx

    from cloud_rehearsal.mcp_client import TenantClient

    client = TenantClient.__new__(TenantClient)
    client.verifier_seen = None
    verifier = "a" * 40 + ".b~" + "c" * 40
    request = httpx.Request(
        "POST", "https://substrate.rehearsal.test/api/exomem/oauth/token",
        data={"grant_type": "authorization_code", "code_verifier": verifier},
    )
    asyncio.run(client._observe_token_request(request))
    assert client.verifier_seen == {"length": len(verifier), "uses_dot_or_tilde": True}
    assert verifier not in str(client.verifier_seen)


def test_operator_abort_is_opt_in_and_accepts_provision_only() -> None:
    """The ordinary rehearsal must not acquire the interrupted restore stage."""
    from cloud_rehearsal.__main__ import _parser

    assert _parser().parse_args(["run"]).operator_restore_abort is False
    selected = _parser().parse_args(["run", "--operator-restore-abort", "--steps", "1,2"])
    assert selected.operator_restore_abort is True and selected.steps == "1,2"


def test_operator_abort_never_qualifies_a_node_even_with_twelve_passing_steps() -> None:
    """The optional partial proof cannot accidentally become production qualification."""
    run = _passing_report()
    run.inputs["operator_restore_abort"] = True
    assert run.to_json()["outcome"]["gates_node"] is False


def test_requested_operator_abort_failure_cannot_be_a_known_finding(monkeypatch) -> None:
    """A requested restore stage must fail even when harness-check accepts known step findings."""
    import asyncio
    from types import SimpleNamespace

    from cloud_rehearsal import operator_restore_abort, scenarios
    from cloud_rehearsal.__main__ import _parser

    async def provision(_ctx, _record):
        pass

    async def abort(_ctx):
        raise report.StepFailure("the writer survived rollback")

    monkeypatch.setattr(scenarios, "STEPS", [(2, "provision", provision, ())])
    monkeypatch.setattr(scenarios, "snapshot_logs", lambda _ctx: None)
    monkeypatch.setattr(operator_restore_abort, "run_abort", abort)
    args = _parser().parse_args(["run", "--operator-restore-abort", "--harness-check"])
    ctx = SimpleNamespace(report=report.Report("abort"), cli_image=None,
                          operator_restore_abort=args.operator_restore_abort, owner_export_image=None)
    with pytest.raises(report.StageFailed):
        asyncio.run(scenarios.run_steps(ctx))
    assert ctx.report.stages["operator_restore_abort"]["status"] == "failed"


def test_owner_export_cannot_qualify_a_node() -> None:
    """The bounded C6 journey must not replace the full node acceptance workflow."""
    run = _passing_report()
    run.inputs["owner_export"] = True
    assert run.to_json()["outcome"]["gates_node"] is False


@pytest.mark.parametrize("provisioned", [False, True])
def test_requested_owner_export_must_run_and_pass(monkeypatch, provisioned) -> None:
    """Neither skipped provisioning nor a failed export may pass harness-check."""
    import asyncio
    from types import SimpleNamespace

    from cloud_rehearsal import owner_export, scenarios
    from cloud_rehearsal.__main__ import _parser

    async def provision(_ctx, _record):
        pass

    async def export(_ctx, _image):
        raise report.StepFailure("the export did not finish")

    monkeypatch.setattr(scenarios, "STEPS", [(2, "provision", provision, ())])
    monkeypatch.setattr(scenarios, "snapshot_logs", lambda _ctx: None)
    monkeypatch.setattr(owner_export, "run_export", export)
    args = _parser().parse_args(["run", "--owner-export", "--harness-check"])
    ctx = SimpleNamespace(report=report.Report("export"), cli_image=None,
                          operator_restore_abort=False,
                          owner_export_image="fixture-cloud" if args.owner_export else None)
    with pytest.raises(report.StageFailed):
        asyncio.run(scenarios.run_steps(ctx, only={2} if provisioned else set()))
    assert ctx.report.stages["owner_export"]["status"] == "failed"


def test_operator_image_build_reads_the_node_roles_x86_pin(monkeypatch, tmp_path) -> None:
    """Changing node-role pin fields must not break the x86 operator image build."""
    from types import SimpleNamespace

    import yaml

    from cloud_rehearsal import operator_restore_abort

    class ImageBuildReached(Exception):
        pass

    def command(args, **_kwargs):
        if args[:2] == ["docker", "build"]:
            raise ImageBuildReached
        return SimpleNamespace(stdout="")

    ctx = SimpleNamespace(
        stack=SimpleNamespace(workdir=tmp_path, run_id="operator-pin"),
        report=SimpleNamespace(stages={"operator_restore_abort": {}}),
    )
    monkeypatch.setattr(operator_restore_abort, "run", command)
    monkeypatch.setattr(operator_restore_abort, "observed_volume_mount", lambda *_args: "mock")
    monkeypatch.setattr(operator_restore_abort, "_labels", lambda *_args, **_kwargs: [])
    with pytest.raises(ImageBuildReached):
        operator_restore_abort._start_operator(ctx, "snapshot", "/ignored")
    defaults = yaml.safe_load((operator_restore_abort.images.REPO_ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text())
    assert ctx.report.stages["operator_restore_abort"]["operator_k3s"] == {
        "version": defaults["k3s_version"], "sha256": defaults["k3s_binaries"]["x86_64"]["sha256"],
    }


def test_operator_configuration_keeps_certificate_verification(tmp_path: Path) -> None:
    """The runner's bridge route must not replace the disposable CA with insecure TLS."""
    import yaml

    from cloud_rehearsal.operator_restore_abort import operator_kubeconfig

    source = tmp_path / "source.yaml"
    destination = tmp_path / "operator.yaml"
    document = {"clusters": [{"name": "local", "cluster": {
        "server": "https://127.0.0.1:31443", "certificate-authority-data": "disposable-ca",
    }}], "users": [{"name": "local", "user": {"client-certificate-data": "disposable-cert"}}]}
    source.write_text(yaml.safe_dump(document))
    operator_kubeconfig(source, destination, "192.0.2.7")
    observed = yaml.safe_load(destination.read_text())
    assert observed["clusters"][0]["cluster"] == {
        "server": "https://192.0.2.7:6443", "certificate-authority-data": "disposable-ca",
    }
    assert observed["users"] == document["users"]
    assert destination.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(source.read_text()) == document


@pytest.mark.parametrize("requested", [False, True])
def test_abort_only_renders_the_shipped_break_glass_binding(monkeypatch, tmp_path, requested) -> None:
    """The optional operator must mint its own identity without breaking ordinary observer exec."""
    from types import SimpleNamespace

    import yaml

    documents = list(yaml.safe_load_all((platform.PLATFORM_CHART / "templates/operator-access.yaml").read_text()))
    binding = next(doc for doc in documents if doc["metadata"]["name"] == "exomem-break-glass")

    def render(_stack, _values, **kwargs):
        return [("templates/cloud-storage-class.yaml", {"kind": "StorageClass", "metadata": {"name": "test-storage"}}),
                *[("templates/operator-access.yaml", doc) for doc in documents
                  if "templates/operator-access.yaml" in kwargs["show_only"]]]

    monkeypatch.setattr(platform, "render_chart", render)
    config = platform.PlatformConfig("controller", "gateway", "cell", "0-1", "https://example.test", "/mcp", "source",
                                     operator_restore_abort=requested)
    stack = SimpleNamespace(postgres=SimpleNamespace(ip="192.0.2.3"), workdir=tmp_path,
                            object_store=SimpleNamespace(endpoint=lambda **_kwargs: "https://192.0.2.4"))
    rendered = platform._render(stack, config).rendered
    assert rendered[1:] == ([binding] if requested else [])


def test_requested_operator_abort_cannot_be_silently_skipped() -> None:
    """A subset without provisioning must not report success for an unexecuted requested stage."""
    import asyncio
    from types import SimpleNamespace

    from cloud_rehearsal import scenarios

    ctx = SimpleNamespace(report=report.Report("no-provision"), operator_restore_abort=True)
    with pytest.raises(report.StageFailed):
        asyncio.run(scenarios.run_steps(ctx, only=set()))
    assert ctx.report.stages["operator_restore_abort"]["failure"]["message"].endswith("provisioning at step 2")


def test_operator_observer_mounts_only_the_selected_pvc(monkeypatch) -> None:
    """Observing a PVC must not expose the entire K3s volume in the operator container."""
    from types import SimpleNamespace

    from cloud_rehearsal import operator_restore_abort

    stack = SimpleNamespace(run_id="example", k3s=SimpleNamespace(container="exo-rehearsal-k3s-example"))
    observed = [{"Name": "/exo-rehearsal-k3s-example", "Mounts": [
        {"Destination": "/var/lib/rancher/k3s", "Type": "volume", "Name": "whole-node"},
        {"Destination": "/var/lib/rancher/k3s/storage", "Type": "volume", "Name": "storage"},
    ]}]
    monkeypatch.setattr(operator_restore_abort, "run", lambda _command: SimpleNamespace(stdout=json.dumps(observed)))
    mount = operator_restore_abort.observed_volume_mount(stack, "/var/lib/rancher/k3s/storage/selected-pvc")
    assert mount == "type=volume,source=storage,target=/observed-cell-volume,readonly,volume-subpath=selected-pvc"


@pytest.mark.parametrize("path", ["/var/lib/rancher/k3s/server", "/var/lib/rancher/k3s/storage", "/var/lib/rancher/k3s/storage/pvc/../server"])
def test_operator_observer_rejects_a_mount_beyond_one_pvc(path) -> None:
    """A wider or traversing mount must fail before Docker can expose node state."""
    from types import SimpleNamespace

    from cloud_rehearsal.operator_restore_abort import observed_volume_mount

    with pytest.raises((ValueError, report.StepFailure)):
        observed_volume_mount(SimpleNamespace(), path)


@pytest.mark.parametrize("cited", [False, True])
def test_restore_recall_requires_a_hit_not_a_query_echo(monkeypatch, cited) -> None:
    """An echoed query cannot prove that the post-snapshot write survived rollback."""
    from cloud_rehearsal import operator_restore_abort

    phrase = "After snapshot example"
    body = {"result": {"query": phrase, "hits": [{"title": phrase}] if cited else []}}
    monkeypatch.setattr(operator_restore_abort, "_runtime", lambda *_args: {"result": body, "is_error": False})
    if cited:
        assert operator_restore_abort._mcp(None, phrase, "recall")["phrase_recalled"] is True
    else:
        with pytest.raises(report.StepFailure, match="recall lost a governed write"):
            operator_restore_abort._mcp(None, phrase, "recall")


@pytest.mark.parametrize("copies", [0, 1, 2])
def test_final_kernel_witness_uses_retained_stdout_and_rejects_loss_or_duplicates(copies) -> None:
    """A stopped container's discarded tmpfs must not lose or fabricate the recovery witness."""
    from cloud_rehearsal.operator_restore_abort import observer_record
    from cloud_rehearsal.restore_abort_observer import OBSERVER_SCHEMA

    final = {"schema": OBSERVER_SCHEMA, "bash_exit_code": 143, "errors": [], "events": []}
    stdout = "run started\n" + json.dumps({"unrelated": "output"}) + "\n" + (json.dumps(final) + "\n") * copies
    if copies == 1:
        assert observer_record(stdout) == final
    else:
        with pytest.raises(report.StepFailure, match="expected one final kernel witness"):
            observer_record(stdout)
