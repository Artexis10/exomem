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
        ["preflight", "--candidate-id", "c", "--state-dir", "/tmp/s"],
        ["prepare", "--candidate-id", "c", "--state-dir", "/tmp/s", "--email", "a@b.c"],
        [
            "run",
            "--candidate-id",
            "c",
            "--state-dir",
            "/tmp/s",
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
        "fixture_version": "v1",
        "fixture_digest": "ff" * 32,
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


def test_load_locks_rejects_cross_platform_contract_drift(tmp_path: pathlib.Path) -> None:
    module = _load_module()
    generated = tmp_path / "plugins" / "hosted" / "generated"
    generated.mkdir(parents=True)
    fixture = tmp_path / "plugins" / "hosted" / "marketplace-review-fixture-v1.json"

    claude_lock = {
        **OPENAI_PACKAGE_LOCK,
        "platform": "claude",
        "artifact_sha256": "9d" * 32,
    }
    openai_lock = {
        **OPENAI_PACKAGE_LOCK,
        "schema_contract_sha256": "aa" * 32,
    }
    (generated / "claude.lock.json").write_text(json.dumps(claude_lock))
    (generated / "claude.zip.lock.json").write_text(
        json.dumps({"platform": "claude", "archive_sha256": "0d" * 32})
    )
    (generated / "openai.lock.json").write_text(json.dumps(openai_lock))
    (generated / "openai.zip.lock.json").write_text(json.dumps(OPENAI_ARCHIVE_LOCK))
    fixture.write_text(json.dumps({"fixture_version": "v1", "payload_sha256": "ff" * 32}))

    with pytest.raises(SystemExit) as raised:
        module.load_locks(tmp_path)

    assert "schema_contract_sha256" in str(raised.value)


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
            "run-token": (200, {}),
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
            # Stop right after the outcome is written.
            "run-sibling-stage-claude": (500, {}),
        }
    )
    cp.state_dir = tmp_path

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
