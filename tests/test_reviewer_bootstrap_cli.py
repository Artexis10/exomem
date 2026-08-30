"""The promotion harness CLI must define every option its dispatcher reads.

`--openai-redirect` was documented in `--openai-connector`'s help text and in
`chatgpt_cimd_identity`'s docstring, and read by `main()`, but never added to the
parser. `run` therefore raised `AttributeError` before doing any work -- and `run`
is the command that spends a one-shot promotion window, so the failure could only
be discovered at the one moment it was most expensive.

These tests read the parser and the dispatcher rather than a hand-kept list, so a
future option that is consumed but not declared fails here instead of live.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest
from benchmark_capabilities import has_posix_file_modes

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "reviewer_bootstrap.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reviewer_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _main_function() -> ast.FunctionDef:
    tree = ast.parse(SCRIPT.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("scripts/reviewer_bootstrap.py has no main()")


def _attributes_read_from_args(main: ast.FunctionDef) -> set[str]:
    return {
        node.attr
        for node in ast.walk(main)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    }


def _declared_destinations(main: ast.FunctionDef) -> set[str]:
    declared: set[str] = set()
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        dest = next(
            (kw.value.value for kw in node.keywords if kw.arg == "dest"),
            None,
        )
        if isinstance(dest, str):
            declared.add(dest)
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        long = next((f for f in flags if f.startswith("--")), None)
        if long is not None:
            declared.add(long[2:].replace("-", "_"))
        elif flags:
            declared.add(flags[0].replace("-", "_"))
    return declared


def test_every_option_main_reads_is_declared() -> None:
    main = _main_function()
    missing = _attributes_read_from_args(main) - _declared_destinations(main)
    assert not missing, (
        "main() reads argparse attributes that no add_argument() declares: "
        f"{sorted(missing)}. The command would raise AttributeError at runtime."
    )


@pytest.mark.parametrize(
    "argv",
    [
        [
            "preflight",
            "--candidate-id",
            "c",
            "--state-dir",
            "/tmp/s",
            "--profile",
            "hosted-alpha-agent-v1",
        ],
        [
            "prepare",
            "--candidate-id",
            "c",
            "--state-dir",
            "/tmp/s",
            "--email",
            "a@b.c",
            "--profile",
            "hosted-alpha-agent-v1",
        ],
        [
            "run",
            "--candidate-id",
            "c",
            "--state-dir",
            "/tmp/s",
            "--profile",
            "hosted-alpha-agent-v1",
            "--token",
            "t",
            "--openai-connector",
            "6UNqc_HaufBZ",
        ],
    ],
    ids=("preflight", "prepare", "run"),
)
def test_each_command_parses_and_exposes_every_attribute(argv: list[str], monkeypatch) -> None:
    module = _load_module()
    read = _attributes_read_from_args(_main_function())

    captured: dict[str, object] = {}

    class _StopAfterParse(Exception):
        pass

    original = module.ControlPlane

    class _Probe(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            raise _StopAfterParse

    monkeypatch.setattr(module, "ControlPlane", _Probe)
    monkeypatch.setattr(sys, "argv", ["reviewer_bootstrap.py", *argv])
    monkeypatch.setenv("EXOMEM_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("EXOMEM_ADMIN_TOKEN", "token")

    real_parse = module.argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        namespace = real_parse(self, *a, **k)
        captured["ns"] = namespace
        return namespace

    monkeypatch.setattr(module.argparse.ArgumentParser, "parse_args", _capture)

    with pytest.raises(_StopAfterParse):
        module.main()

    namespace = captured["ns"]
    for attribute in sorted(read):
        assert hasattr(namespace, attribute), (
            f"`{' '.join(argv[:1])}` parses without an `args.{attribute}`, which main() reads"
        )


def test_openai_redirect_is_declared_and_documented() -> None:
    assert "openai_redirect" in _declared_destinations(_main_function())

    help_text = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--openai-redirect" in help_text, (
        "the operator is told to use this flag when the connector document is "
        "unreadable from their network; it has to appear in --help"
    )


# --- OpenAI lock attachment -------------------------------------------------
#
# Every contract candidate is created with `openai_package_lock` NULL --
# `storeExomemAgentContractCandidate` sets it to null deliberately, because the
# OpenAI artifact is not part of the checked Exomem release. `create-stage` for
# platform `openai` joins on `candidate.openai_package_lock->>'artifact_sha256'`,
# so until the locks are attached the OpenAI sibling stage matches nothing.
#
# `run` builds the Claude sibling first, which means that failure lands after the
# invite is spent and the <=30 minute authority clock has started. These tests pin
# the attachment to `prepare`, and pin the signature encoding, because a wrong
# encoding is indistinguishable from a wrong secret in the server's reply.

OPENAI_PACKAGE_LOCK = {
    "platform": "openai",
    "artifact_sha256": "b9" * 32,
    "registered_app_id_sha256": "b0" * 32,
    "schema_version": 1,
    "platform_schema_version": "1.0.0",
    "plugin_id": "exomem-hosted",
    "plugin_version": "0.1.0",
    "endpoint": "https://example.invalid/api/exomem/mcp/v1",
    "profile": "hosted-alpha-agent-v1",
    "command_surface_sha256": "ed" * 32,
    "schema_contract_sha256": "47" * 32,
    "definition_sha256": "be" * 32,
    "skills_sha256": "e2" * 32,
    "compatibility_sha256": "54" * 32,
    "oauth_discovery_sha256": "10" * 32,
}
OPENAI_ARCHIVE_LOCK = {
    "platform": "openai",
    "archive_sha256": "d8" * 32,
    "registered_app_id_sha256": "b0" * 32,
}


def _locks() -> dict:
    return {
        "claude_package": "9d" * 32,
        "claude_archive": "0d" * 32,
        "openai_package": OPENAI_PACKAGE_LOCK["artifact_sha256"],
        "openai_archive": OPENAI_ARCHIVE_LOCK["archive_sha256"],
        "openai_registered_app": OPENAI_PACKAGE_LOCK["registered_app_id_sha256"],
        "compatibility": OPENAI_PACKAGE_LOCK["compatibility_sha256"],
        "contract": OPENAI_PACKAGE_LOCK["schema_contract_sha256"],
        "command_surface": OPENAI_PACKAGE_LOCK["command_surface_sha256"],
        "plugin_version": "0.1.0",
        "profile": OPENAI_PACKAGE_LOCK["profile"],
        "fixture_version": "v2",
        "fixture_digest": "ff" * 32,
        "fixture": {
            "fixture_version": "v2",
            "payload_sha256": "ff" * 32,
            "payload": {"notes": [{"key": "fixture-note"}], "absent_notes": [{}]},
        },
        "openai_package_lock": OPENAI_PACKAGE_LOCK,
        "openai_archive_lock": OPENAI_ARCHIVE_LOCK,
    }


class _RecordingControlPlane:
    """Records calls and answers from a canned table keyed by label."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []
        self.base_url = "https://example.invalid"

    def call(self, method, path, *, label, body=None, **_):
        self.calls.append({"method": method, "path": path, "label": label, "body": body})
        return self.responses.get(label, (200, {}))


def test_canonical_json_sorts_keys_recursively_and_omits_whitespace() -> None:
    module = _load_module()
    assert module.canonical_json({"b": 1, "a": [3, {"d": None, "c": True}]}) == (
        '{"a":[3,{"c":true,"d":null}],"b":1}'
    )


def test_attach_signs_the_payload_the_control_plane_will_verify(monkeypatch) -> None:
    """The HMAC is over the control plane's `canonical()` of the unsigned payload.

    Reproduced here rather than deferred to the server: a mismatched encoding and
    a wrong secret produce the same rejection, and the operator cannot tell them
    apart from the reply.
    """
    module = _load_module()
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_KEY_ID", "key-1")
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET", "s3cret")
    cp = _RecordingControlPlane({"prepare-attach-openai-locks": (200, {"attached": True})})

    module.attach_openai_locks(cp, "cand-1", _locks())

    body = cp.calls[0]["body"]
    assert body["action"] == "attach-openai-locks"
    assert body["packageLock"] == OPENAI_PACKAGE_LOCK
    assert body["archiveLock"] == OPENAI_ARCHIVE_LOCK
    expected = hmac.new(
        b"s3cret",
        module.canonical_json(
            {
                "candidateId": "cand-1",
                "packageLock": OPENAI_PACKAGE_LOCK,
                "archiveLock": OPENAI_ARCHIVE_LOCK,
                "operatorKeyId": "key-1",
            }
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert body["operatorSignature"] == expected


def test_attach_refuses_without_the_operator_signing_key(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.delenv("EXOMEM_HOSTED_CONTRACT_IMPORT_KEY_ID", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET", raising=False)
    cp = _RecordingControlPlane({})

    with pytest.raises(SystemExit) as raised:
        module.attach_openai_locks(cp, "cand-1", _locks())

    assert "CONTRACT_IMPORT" in str(raised.value)
    assert cp.calls == [], "no request may be sent without a signing key"


def test_attach_surfaces_a_rejected_attachment(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_KEY_ID", "key-1")
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET", "s3cret")
    cp = _RecordingControlPlane({"prepare-attach-openai-locks": (400, {"code": "INVALID"})})

    with pytest.raises(SystemExit):
        module.attach_openai_locks(cp, "cand-1", _locks())


def test_attach_refuses_an_unproven_already_attached_response(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_KEY_ID", "key-1")
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET", "s3cret")
    cp = _RecordingControlPlane({"prepare-attach-openai-locks": (200, {"attached": False})})

    with pytest.raises(SystemExit) as raised:
        module.attach_openai_locks(cp, "cand-1", _locks())

    assert "could not prove" in str(raised.value)


@pytest.mark.parametrize(
    "field",
    ("command_surface_sha256", "schema_contract_sha256", "compatibility_sha256"),
)
def test_load_locks_rejects_cross_platform_contract_drift(
    tmp_path: pathlib.Path, field: str
) -> None:
    module = _load_module()
    generated = tmp_path / "plugins" / "hosted" / "generated"
    generated.mkdir(parents=True)
    fixture = tmp_path / "plugins" / "hosted" / "marketplace-review-fixture-v2.json"

    claude_lock = {
        **OPENAI_PACKAGE_LOCK,
        "platform": "claude",
        "artifact_sha256": "9d" * 32,
    }
    openai_lock = {
        **OPENAI_PACKAGE_LOCK,
        field: "aa" * 32,
    }
    (generated / "claude.lock.json").write_text(json.dumps(claude_lock))
    (generated / "claude.zip.lock.json").write_text(
        json.dumps({"platform": "claude", "archive_sha256": "0d" * 32})
    )
    (generated / "openai.lock.json").write_text(json.dumps(openai_lock))
    (generated / "openai.zip.lock.json").write_text(json.dumps(OPENAI_ARCHIVE_LOCK))
    fixture.write_text(json.dumps({"fixture_version": "v1", "payload_sha256": "ff" * 32}))

    with pytest.raises(SystemExit) as raised:
        module.load_locks(tmp_path, "hosted-alpha-agent-v1")

    assert field in str(raised.value)


def test_load_locks_reads_the_candidates_profile_not_the_generated_root(
    tmp_path: pathlib.Path,
) -> None:
    """A non-default candidate's locks live under `candidates/<profile>`.

    Reading the generated root instead returns the default candidate's locks --
    a different command surface, whose digests no server-side join accepts. The
    failure is remote and late, so pin the directory here.
    """
    module = _load_module()
    profile = "hosted-alpha-agent-v4"
    generated = tmp_path / "plugins" / "hosted" / "generated"
    candidate = generated / "candidates" / profile
    candidate.mkdir(parents=True)
    (tmp_path / "plugins" / "hosted" / "marketplace-review-fixture-v2.json").write_text(
        json.dumps({"fixture_version": "v2", "payload_sha256": "ff" * 32})
    )

    def _write(root: pathlib.Path, declared: str, artifact: str) -> None:
        package = {**OPENAI_PACKAGE_LOCK, "profile": declared, "artifact_sha256": artifact}
        (root / "claude.lock.json").write_text(
            json.dumps({**package, "platform": "claude"})
        )
        (root / "claude.zip.lock.json").write_text(
            json.dumps({"platform": "claude", "archive_sha256": "0d" * 32})
        )
        (root / "openai.lock.json").write_text(json.dumps(package))
        (root / "openai.zip.lock.json").write_text(json.dumps(OPENAI_ARCHIVE_LOCK))

    # Both present, as they are in a real checkout, and distinguishable.
    _write(generated, "hosted-alpha-agent-v1", "11" * 32)
    _write(candidate, profile, "44" * 32)

    locks = module.load_locks(tmp_path, profile)

    assert locks["profile"] == profile
    assert locks["openai_package"] == "44" * 32, "read the generated root, not the candidate"


def test_load_locks_refuses_a_lock_that_declares_another_profile(
    tmp_path: pathlib.Path,
) -> None:
    """The directory name is not evidence; the lock's own `profile` is."""
    module = _load_module()
    profile = "hosted-alpha-agent-v4"
    candidate = tmp_path / "plugins" / "hosted" / "generated" / "candidates" / profile
    candidate.mkdir(parents=True)
    (tmp_path / "plugins" / "hosted" / "marketplace-review-fixture-v2.json").write_text(
        json.dumps({"fixture_version": "v2", "payload_sha256": "ff" * 32})
    )
    stale = {**OPENAI_PACKAGE_LOCK, "profile": "hosted-alpha-agent-v1"}
    (candidate / "claude.lock.json").write_text(json.dumps({**stale, "platform": "claude"}))
    (candidate / "claude.zip.lock.json").write_text(
        json.dumps({"platform": "claude", "archive_sha256": "0d" * 32})
    )
    (candidate / "openai.lock.json").write_text(json.dumps(stale))
    (candidate / "openai.zip.lock.json").write_text(json.dumps(OPENAI_ARCHIVE_LOCK))

    with pytest.raises(SystemExit) as raised:
        module.load_locks(tmp_path, profile)

    assert "declares profile hosted-alpha-agent-v1" in str(raised.value)


def test_prepare_attaches_the_openai_locks_before_anything_else(monkeypatch) -> None:
    """Ordering is the whole point: after `run` starts, this is unrecoverable."""
    module = _load_module()
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_KEY_ID", "key-1")
    monkeypatch.setenv("EXOMEM_HOSTED_CONTRACT_IMPORT_SECRET", "s3cret")
    cp = _RecordingControlPlane(
        {
            "prepare-attach-openai-locks": (200, {"attached": True}),
            # Stop at the next call. Reaching it is what this test asserts.
            "prepare-stage": (500, {}),
        }
    )

    with pytest.raises(SystemExit):
        module.prepare(cp, "cand-1", "reviewer@example.invalid", _locks())

    assert cp.calls[0]["label"] == "prepare-attach-openai-locks"
    assert cp.calls[1]["label"] == "prepare-stage"


# --- preflight lock-drift gate ---------------------------------------------


def _contracts_response(schema: str, compatibility: str, command: str) -> dict:
    return {
        "liveCohortCandidateId": None,
        "agentContracts": [
            {
                "id": "cand-1",
                "state": "pending",
                "schemaDigest": schema,
                "compatibilityDigest": compatibility,
                "commandFingerprint": command,
            }
        ],
        "rolloutStatus": [
            {
                "candidateId": "cand-1",
                "state": "pending",
                "sourceRelease": "0.54.1",
                "routableCellCount": 0,
            }
        ],
    }


def _preflight_cp(contracts: dict) -> _RecordingControlPlane:
    return _RecordingControlPlane(
        {
            "preflight-contracts": (200, contracts),
            "preflight-clients": (200, {"bootstrapAuthorities": []}),
            "preflight-capacity": (
                200,
                {
                    "capacity": {
                        "runtimeCapacitySlots": 4,
                        "reservedRuntimeSlots": 0,
                        "provisionClaimCapacity": 2,
                        "activeProvisionClaims": 0,
                    }
                },
            ),
        }
    )


def test_preflight_is_green_when_the_repo_matches_the_candidate() -> None:
    module = _load_module()
    locks = _locks()
    cp = _preflight_cp(
        _contracts_response(locks["contract"], locks["compatibility"], locks["command_surface"])
    )
    assert module.preflight(cp, "cand-1", locks) is True


@pytest.mark.parametrize("field", ["contract", "compatibility", "command_surface"], ids=lambda f: f)
def test_preflight_fails_when_a_contract_digest_drifted(field: str, capsys) -> None:
    """Each digest is joined on by `create-stage` or `attach-openai-locks`.

    Both answer a bare 500 or a silent `false` when they disagree, and the first
    of them lands inside the promotion window.
    """
    module = _load_module()
    locks = _locks()
    drifted = dict(locks)
    drifted[field] = "aa" * 32
    cp = _preflight_cp(
        _contracts_response(locks["contract"], locks["compatibility"], locks["command_surface"])
    )
    assert module.preflight(cp, "cand-1", drifted) is False
    assert "repo locks match candidate" in capsys.readouterr().out


# --- explicit reviewer bootstrap client reuse -------------------------------


REUSABLE_PUBLIC_CLIENT_ID = "exomem-reviewer-bootstrap-11111111-1111-4111-8111-111111111111"
REUSABLE_CLIENT_RECORD_ID = "22222222-2222-4222-8222-222222222222"
STAGE_ID = "33333333-3333-4333-8333-333333333333"


def _prepare_cp(tmp_path, *, client_response: dict | None = None) -> _RecordingControlPlane:
    cp = _RecordingControlPlane(
        {
            "preflight-reuse-client": (
                200,
                {"eligible": True, "clientRecordId": REUSABLE_CLIENT_RECORD_ID},
            ),
            "prepare-stage": (200, {"stage": {"id": STAGE_ID}}),
            "prepare-client": (
                200,
                client_response or {"id": REUSABLE_CLIENT_RECORD_ID, "enabled": False},
            ),
            "prepare-invite": (201, {"inviteId": "invite-1"}),
        }
    )
    cp.state_dir = tmp_path
    return cp


def test_prepare_keeps_fresh_client_generation_as_the_default(monkeypatch, tmp_path) -> None:
    module = _load_module()
    generated = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(module, "attach_openai_locks", lambda *_: None)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: module.uuid.UUID(generated))
    cp = _prepare_cp(
        tmp_path,
        client_response={"id": "55555555-5555-4555-8555-555555555555", "enabled": False},
    )

    context = module.prepare(cp, "cand-1", "reviewer@example.invalid", _locks())

    client_call = next(call for call in cp.calls if call["label"] == "prepare-client")
    assert client_call["body"]["clientId"] == f"exomem-reviewer-bootstrap-{generated}"
    assert "existingClientRecordId" not in client_call["body"]
    assert context["clientId"] == f"exomem-reviewer-bootstrap-{generated}"
    assert all(call["label"] != "preflight-reuse-client" for call in cp.calls)


def test_prepare_reuses_only_the_explicit_exact_disabled_client(monkeypatch, tmp_path) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "attach_openai_locks", lambda *_: None)
    cp = _prepare_cp(tmp_path)

    context = module.prepare(
        cp,
        "cand-1",
        "reviewer@example.invalid",
        _locks(),
        REUSABLE_PUBLIC_CLIENT_ID,
    )

    labels = [call["label"] for call in cp.calls]
    assert labels == [
        "preflight-reuse-client",
        "prepare-stage",
        "prepare-client",
        "prepare-invite",
    ]
    stage_call = cp.calls[1]
    assert stage_call["body"]["oauthClientConfigSha256"] == module.client_config_sha256(
        platform="claude",
        admission_mode="pinned",
        client_id=REUSABLE_PUBLIC_CLIENT_ID,
        redirect_uris=[module.LOOPBACK_REDIRECT],
    )
    client_call = cp.calls[2]
    assert client_call["body"]["clientId"] == REUSABLE_PUBLIC_CLIENT_ID
    assert client_call["body"]["existingClientRecordId"] == REUSABLE_CLIENT_RECORD_ID
    assert context["oauthClientId"] == REUSABLE_CLIENT_RECORD_ID
    assert context["clientId"] == REUSABLE_PUBLIC_CLIENT_ID


@pytest.mark.parametrize(
    "reason",
    ("mismatched_configuration", "enabled_client", "prior_reviewer_authorization"),
)
def test_prepare_never_falls_back_when_explicit_reuse_is_ineligible(
    reason: str, monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "attach_openai_locks", lambda *_: None)
    cp = _prepare_cp(tmp_path)
    cp.responses["preflight-reuse-client"] = (200, {"eligible": False, "reason": reason})

    with pytest.raises(SystemExit, match="not eligible for reuse"):
        module.prepare(
            cp,
            "cand-1",
            "reviewer@example.invalid",
            _locks(),
            REUSABLE_PUBLIC_CLIENT_ID,
        )

    assert [call["label"] for call in cp.calls] == ["preflight-reuse-client"]


@pytest.mark.parametrize(
    "client_response",
    (
        {"id": "66666666-6666-4666-8666-666666666666", "enabled": False},
        {"id": REUSABLE_CLIENT_RECORD_ID, "enabled": True},
    ),
    ids=("different-record", "enabled-after-registration"),
)
def test_prepare_stops_if_control_plane_does_not_reuse_exactly(
    client_response: dict, monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "attach_openai_locks", lambda *_: None)
    cp = _prepare_cp(tmp_path, client_response=client_response)

    with pytest.raises(SystemExit, match="did not reuse"):
        module.prepare(
            cp,
            "cand-1",
            "reviewer@example.invalid",
            _locks(),
            REUSABLE_PUBLIC_CLIENT_ID,
        )

    assert "prepare-invite" not in [call["label"] for call in cp.calls]


def test_preflight_allows_a_full_client_partition_with_explicit_reuse() -> None:
    module = _load_module()
    locks = _locks()
    cp = _preflight_cp(
        _contracts_response(locks["contract"], locks["compatibility"], locks["command_surface"])
    )
    cp.responses["preflight-clients"] = (
        200,
        {"bootstrapAuthorities": [], "clients": [{} for _ in range(module.OPERATOR_CLIENT_BOUND)]},
    )
    cp.responses["preflight-reuse-client"] = (
        200,
        {"eligible": True, "clientRecordId": REUSABLE_CLIENT_RECORD_ID},
    )

    assert module.preflight(cp, "cand-1", locks, REUSABLE_PUBLIC_CLIENT_ID) is True


def test_run_resolves_the_connector_before_creating_the_authority(monkeypatch) -> None:
    """The connector document is fetched from chatgpt.com, and that can 403.

    Creating the authority is the irreversible step: it spends the invite and
    starts the clock whether or not anything after it succeeds. So a fetch that
    can fail for reasons outside this system must happen first.
    """
    module = _load_module()
    cp = _RecordingControlPlane({"run-authority": (500, {})})
    resolved: list[str] = []

    def _identity(connector, override=None):
        resolved.append(connector)
        assert cp.calls == [], "the connector must resolve before any control-plane call"
        raise SystemExit("connector document unreadable")

    monkeypatch.setattr(module, "chatgpt_cimd_identity", _identity)

    with pytest.raises(SystemExit):
        module.run(
            cp, {"inviteId": "i", "stageId": "s", "oauthClientId": "c"}, "tok", _locks(), "CONN"
        )

    assert resolved == ["CONN"]
    assert cp.calls == [], "nothing may be spent when the connector cannot be read"


def test_run_writes_the_outcome_file_promotion_evidence_reads(monkeypatch, tmp_path) -> None:
    """The two halves of the promotion hand off through one file.

    `promotion_evidence.py observe` reads `bootstrap-outcome-final.json` by
    default and takes exactly `tenantId`, `assignmentId` and `generation` from it.
    Nothing wrote it, so the operator had to hand-author it mid-window out of the
    raw admin response. This pins the contract from the producing side.
    """
    module = _load_module()
    monkeypatch.setattr(
        module, "chatgpt_cimd_identity", lambda c, o=None: ("https://c/x.json", ["https://c/cb"])
    )

    authority_id = "11111111-1111-4111-8111-111111111111"
    cp = _RecordingControlPlane(
        {
            "run-authority": (200, {"authority": {"id": authority_id}}),
            "run-authorize": (303, {}),
            "run-redeem": (200, {"destination": "https://x/cb?code=abc"}),
            "run-token": (
                200,
                {
                    "access_token": "setup-access-token",
                    "refresh_token": "setup-refresh-token",
                    "token_type": "Bearer",
                },
            ),
            "run-authority-outcome": (
                200,
                {
                    "bootstrapAuthorities": [
                        {
                            "id": authority_id,
                            "state": "consumed",
                            "outcomeTenantId": "tenant-1",
                            "outcomeAssignmentId": "assign-1",
                            "outcomeAssignmentGeneration": 4,
                        }
                    ]
                },
            ),
            "run-owner-status": (
                200,
                {"success": True, "status": {"state": "ready", "code": "CELL_READY"}},
            ),
            # Stop right after the outcome is written.
            "run-sibling-stage-claude": (500, {}),
        }
    )
    cp.state_dir = tmp_path
    monkeypatch.setattr(
        module,
        "seed_marketplace_review_fixture",
        lambda fixture, call_tool: {
            "fixture_version": fixture["fixture_version"],
            "payload_sha256": fixture["payload_sha256"],
            "note_count": 1,
            "verified": True,
        },
        raising=False,
    )
    monkeypatch.setattr(module, "HostedMCPToolCaller", lambda *_: object(), raising=False)

    with pytest.raises(SystemExit):
        module.run(
            cp,
            {
                "inviteId": "i",
                "stageId": "s",
                "oauthClientId": "c",
                "candidateId": "cand-1",
                "clientId": "cid",
                "state": "st",
                "codeChallenge": "ch",
                "codeVerifier": "cv",
                "stageExpiresAt": "2999-01-01T00:00:00.000Z",
            },
            "tok",
            _locks(),
            "CONN",
        )

    written = json.loads((tmp_path / "bootstrap-outcome-final.json").read_text())
    assert written == {"tenantId": "tenant-1", "assignmentId": "assign-1", "generation": 4}
    # Windows synthesizes st_mode -- a file reports 0o666 whatever chmod was
    # asked for -- so there the assertion reads a placeholder, not a permission.
    # The hosted suites get away with the same line only because a POSIX-only
    # call after theirs (os.geteuid) raises first and conftest converts THAT
    # into a skip; a bare mode assertion fails instead, and conftest will not
    # absorb an AssertionError because matching on assertion text would let it
    # swallow real defects. The Linux lane keeps gating it, which is where the
    # mode is an access-control fact.
    if has_posix_file_modes():
        assert (tmp_path / "bootstrap-outcome-final.json").stat().st_mode & 0o777 == 0o600


def _run_context() -> dict[str, str]:
    return {
        "inviteId": "invite-1",
        "stageId": "stage-1",
        "oauthClientId": "client-record-1",
        "candidateId": "cand-1",
        "clientId": "bootstrap-client-1",
        "state": "state-1",
        "codeChallenge": "challenge-1",
        "codeVerifier": "verifier-1",
        "stageExpiresAt": "2999-01-01T00:00:00.000Z",
    }


def _run_responses(*, token_status: int = 200, owner_status: tuple[int, dict] | None = None):
    authority_id = "11111111-1111-4111-8111-111111111111"
    return {
        "run-authority": (200, {"authority": {"id": authority_id}}),
        "run-authorize": (303, {}),
        "run-redeem": (200, {"destination": "https://x/cb?code=abc"}),
        "run-token": (
            token_status,
            (
                {
                    "access_token": "setup-access-token",
                    "refresh_token": "setup-refresh-token",
                    "token_type": "Bearer",
                }
                if token_status == 200
                else {"error": "invalid_grant"}
            ),
        ),
        "run-authority-outcome": (
            200,
            {
                "bootstrapAuthorities": [
                    {
                        "id": authority_id,
                        "state": "consumed",
                        "outcomeTenantId": "tenant-1",
                        "outcomeAssignmentId": "assignment-1",
                        "outcomeAssignmentGeneration": 1,
                    }
                ]
            },
        ),
        "run-owner-status": owner_status
        or (200, {"success": True, "status": {"state": "ready", "code": "CELL_READY"}}),
        "run-sibling-stage-claude": (500, {}),
    }


def test_run_requires_successful_setup_token_before_any_credential_call(
    monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module, "chatgpt_cimd_identity", lambda *_: ("https://c/x.json", ["https://c/cb"])
    )
    cp = _RecordingControlPlane(_run_responses(token_status=400))
    cp.state_dir = tmp_path

    with pytest.raises(SystemExit, match="token exchange failed"):
        module.run(cp, _run_context(), "invite-token", _locks(), "CONN")

    labels = [call["label"] for call in cp.calls]
    assert "run-authority-outcome" not in labels
    assert not any(label.startswith("run-canary-") for label in labels)


def test_run_readiness_failure_makes_zero_reviewer_credential_calls(
    monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module, "chatgpt_cimd_identity", lambda *_: ("https://c/x.json", ["https://c/cb"])
    )
    cp = _RecordingControlPlane(
        _run_responses(owner_status=(503, {"code": "PROVISIONING_UNAVAILABLE"}))
    )
    cp.state_dir = tmp_path

    with pytest.raises(SystemExit, match="owner status"):
        module.run(cp, _run_context(), "invite-token", _locks(), "CONN")

    labels = [call["label"] for call in cp.calls]
    assert not any(label.startswith("run-canary-") for label in labels)


def test_run_seeds_exact_fixture_after_cell_ready_and_before_credentials(
    monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module, "chatgpt_cimd_identity", lambda *_: ("https://c/x.json", ["https://c/cb"])
    )
    events: list[str] = []

    class _OrderedControlPlane(_RecordingControlPlane):
        def call(self, method, path, *, label, body=None, **kwargs):
            events.append(label)
            return super().call(method, path, label=label, body=body, **kwargs)

    cp = _OrderedControlPlane(_run_responses())
    cp.state_dir = tmp_path

    class _Caller:
        def __init__(self, base_url, bearer_token):
            assert base_url == cp.base_url
            assert bearer_token == "setup-access-token"

        def __call__(self, name, arguments):
            raise AssertionError("the shared seeder is stubbed in this order test")

    def seed(fixture, call_tool):
        events.append("seed-fixture")
        assert fixture is _locks()["fixture"] or fixture == _locks()["fixture"]
        assert isinstance(call_tool, _Caller)
        return {
            "fixture_version": "v2",
            "payload_sha256": "ff" * 32,
            "note_count": 1,
            "verified": True,
        }

    monkeypatch.setattr(module, "HostedMCPToolCaller", _Caller, raising=False)
    monkeypatch.setattr(module, "seed_marketplace_review_fixture", seed, raising=False)

    with pytest.raises(SystemExit, match="claude sibling stage failed"):
        module.run(cp, _run_context(), "invite-token", _locks(), "CONN")

    assert events.index("run-owner-status") < events.index("seed-fixture")
    assert events.index("seed-fixture") < events.index("run-sibling-stage-claude")
    receipt = json.loads((tmp_path / "reviewer-fixture-seed.json").read_text())
    assert receipt == {
        "fixture_version": "v2",
        "payload_sha256": "ff" * 32,
        "note_count": 1,
        "verified": True,
    }


def test_run_fixture_failure_makes_zero_reviewer_credential_calls(
    monkeypatch, tmp_path
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module, "chatgpt_cimd_identity", lambda *_: ("https://c/x.json", ["https://c/cb"])
    )
    monkeypatch.setattr(module, "HostedMCPToolCaller", lambda *_: object())
    monkeypatch.setattr(
        module,
        "seed_marketplace_review_fixture",
        lambda *_: (_ for _ in ()).throw(ValueError("fixture rejected")),
    )
    cp = _RecordingControlPlane(_run_responses())
    cp.state_dir = tmp_path

    with pytest.raises(SystemExit, match="reviewer fixture seeding failed"):
        module.run(cp, _run_context(), "invite-token", _locks(), "CONN")

    labels = [call["label"] for call in cp.calls]
    assert not any(label.startswith("run-canary-") for label in labels)
    assert not any(label.startswith("run-sibling-") for label in labels)


def test_owner_status_polling_records_only_content_free_progress(monkeypatch, tmp_path) -> None:
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    responses = iter(
        [
            (200, {"success": True, "status": {"state": "preparing", "code": "CELL_PREPARING"}}),
            (200, {"success": True, "status": {"state": "ready", "code": "CELL_READY"}}),
        ]
    )

    class _PollingControlPlane(_RecordingControlPlane):
        def call(self, method, path, *, label, body=None, **kwargs):
            self.calls.append({"method": method, "path": path, "label": label, "body": body})
            return next(responses)

    cp = _PollingControlPlane({})
    cp.state_dir = tmp_path

    module.wait_for_reviewer_cell(cp, _run_context())

    assert [call["label"] for call in cp.calls] == ["run-owner-status", "run-owner-status"]
    progress = json.loads((tmp_path / "reviewer-cell-readiness.json").read_text())
    assert progress == {
        "code": "CELL_READY",
        "poll_count": 2,
        "ready": True,
        "state": "ready",
    }
    assert "tenant" not in json.dumps(progress).lower()


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_hosted_mcp_caller_uses_bearer_header_and_decodes_json_or_sse(
    content_type: str, monkeypatch
) -> None:
    module = _load_module()
    captured: dict[str, object] = {}

    class _Headers:
        def get_content_type(self):
            return content_type

    class _Response:
        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            request_id = captured["payload"]["id"]
            envelope = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"structuredContent": {"path": "verified.md"}},
            }
            encoded = json.dumps(envelope).encode()
            return encoded if content_type == "application/json" else b"data: " + encoded + b"\n\n"

    class _Opener:
        def open(self, request, timeout):
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            return _Response()

    monkeypatch.setattr(module, "_OPENER", _Opener())
    token = "secret-bearer-value"

    result = module.HostedMCPToolCaller("https://example.invalid", token)(
        "read_memory", {"path": "verified.md"}
    )

    assert result == {"path": "verified.md"}
    headers = captured["headers"]
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Accept"] == "application/json, text/event-stream"
    assert token not in json.dumps(captured["payload"])


def test_hosted_mcp_caller_redacts_protocol_response_content(monkeypatch) -> None:
    module = _load_module()
    secret_body = "do-not-echo-this-fixture-content"

    class _Headers:
        def get_content_type(self):
            return "application/json"

    class _Response:
        headers = _Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size=-1):
            return json.dumps({"error": {"message": secret_body}}).encode()

    class _Opener:
        def open(self, request, timeout):
            return _Response()

    monkeypatch.setattr(module, "_OPENER", _Opener())

    with pytest.raises(SystemExit) as raised:
        module.HostedMCPToolCaller("https://example.invalid", "secret-token")(
            "remember", {"content": secret_body}
        )

    assert secret_body not in str(raised.value)
    assert "secret-token" not in str(raised.value)
