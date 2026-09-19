"""Focused contract for the durable hosted launch coordinator."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "infra" / "scripts" / "accept_hosted_service.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("accept_hosted_service_launch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CANDIDATE_ID = "018f2d91-7c42-7000-8000-000000000021"


def _config() -> dict[str, Any]:
    runtime_target = {
        "releaseVersion": "0.77.0",
        "sourceCommit": "7" * 40,
        "runtimeImage": "ghcr.io/artexis10/exomem@sha256:" + "6" * 64,
        "runtimeCandidateSha256": "9" * 64,
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v4",
        "gatewayContractDigest": "1" * 64,
        "commandFingerprint": "2" * 64,
        "schemaDigest": "3" * 64,
        "compatibilityDigest": "4" * 64,
    }
    runtime_target_digest = hashlib.sha256(
        json.dumps(runtime_target, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    ).hexdigest()
    return {
        "schema_version": 1,
        "environment": "owner-alpha",
        "control_base_url": "https://control.example.test",
        "invitation": {
            "reference": "owner-invite-2026-09",
            "token": {"source": "env", "name": "TEST_OWNER_INVITE_TOKEN"},
        },
        "selected_host": "claude",
        "oauth": {
            "authorization_server_metadata": "https://control.example.test/.well-known/oauth-authorization-server/api/exomem/oauth",
            "resource": "https://control.example.test/api/exomem/mcp/v1",
            "client_id": "owner-alpha-client",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "owner_session": {"source": "env", "name": "TEST_OWNER_SESSION"},
        },
        "release": {
            "candidate_id": CANDIDATE_ID,
            "runtime_target": runtime_target,
            "runtime_target_digest": runtime_target_digest,
        },
        "deployment": {
            "source_commit": "7" * 40,
            "lock_digest": "8" * 64,
            "revision": "owner-alpha-20260919",
            "operator_credential": {"source": "env", "name": "TEST_OPERATOR_TOKEN"},
        },
        "resource_maximum": {
            "tenants": 1,
            "storage_bytes": 10_737_418_240,
            "runtime_slots": 1,
            "provision_claims": 1,
        },
        "deadlines_seconds": {
            "preflight": 30,
            "runtime_target": 60,
            "runtime_activation": 60,
            "consent": 3600,
            "service_ready": 3600,
            "milestone": 604800,
        },
        "polling": {"initial_seconds": 1, "maximum_seconds": 8},
    }


def _write_config(tmp_path: Path, value: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "launch.json"
    path.write_text(json.dumps(_config() if value is None else value), encoding="utf-8")
    return path


def _contracts(*, imported: bool = False, active: bool = False) -> dict[str, Any]:
    return {
        "success": True,
        "agentContracts": [
            {
                "id": CANDIDATE_ID,
                "state": "live" if active else "pending",
                "commandFingerprint": "2" * 64,
                "schemaDigest": "3" * 64,
                "compatibilityDigest": "4" * 64,
            }
        ],
        "liveCohortCandidateId": CANDIDATE_ID if active else None,
        "rolloutStatus": [
            {
                "candidateId": CANDIDATE_ID,
                "state": "live" if active else "pending",
                "sourceRelease": "0.77.0",
                "routableCellCount": 0,
                "routableSetDigest": "9" * 64,
                "routableObservationFresh": False,
                "observedSourceRelease": None,
                "observedProtocolVersion": None,
                "currentTargetSourceRelease": None,
            }
        ],
        "runtimeTargets": [
            {
                "candidateId": CANDIDATE_ID,
                "sourceRelease": "0.77.0",
                "importReady": True,
                "runtimeTargetDigest": _config()["release"]["runtime_target_digest"] if imported else None,
            }
        ],
    }


def _capacity() -> dict[str, Any]:
    return {
        "success": True,
        "capacity": {
            "storageCapacityBytes": 21_474_836_480,
            "reservedStorageBytes": 0,
            "runtimeCapacitySlots": 2,
            "reservedRuntimeSlots": 0,
            "provisionReservationCapacity": 2,
            "reservedProvisionSlots": 0,
            "provisionClaimCapacity": 2,
            "activeProvisionClaims": 0,
            "outstandingPaidInvites": 0,
        },
    }


def _fleet() -> dict[str, Any]:
    return {
        "success": True,
        "observation": {
            "artifact": "exomem-hosted-substrate-fleet-observation",
            "schemaVersion": 1,
            "observedAt": "2026-09-19T12:00:00Z",
            "routableCells": [],
            "tenantBindings": [],
            "assignments": [],
            "unfinishedOperations": [],
            "capacityClaims": [],
            "capacityActiveCellCount": 0,
            "reviewerAuthorities": [],
            "reviewerTenants": [],
        },
    }


class Control:
    def __init__(self, runner: Any, *, imported: bool = False, active: bool = False) -> None:
        self.runner = runner
        self.imported = imported
        self.active = active
        self.posts: list[dict[str, Any]] = []

    def contracts(self) -> dict[str, Any]:
        return _contracts(imported=self.imported, active=self.active)

    def capacity(self) -> dict[str, Any]:
        return _capacity()

    def fleet(self) -> dict[str, Any]:
        return _fleet()

    def inspect_invitation(self) -> dict[str, Any]:
        return {
            "success": True,
            "email": "owner@example.test",
            "expiresAt": "2026-09-30T12:00:00.000Z",
        }

    def lifecycle(self) -> dict[str, Any]:
        return {
            "success": True,
            "status": {"state": "ready", "code": "CELL_READY", "retryable": False},
        }

    def mutate_contracts(self, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append(body)
        if body["action"] == "import-runtime-target":
            self.imported = True
            return {
                "success": True,
                "candidateId": CANDIDATE_ID,
                "runtimeTargetDigest": _config()["release"]["runtime_target_digest"],
                "outcome": "imported",
            }
        assert body["action"] == "activate-runtime"
        self.active = True
        return {"success": True, "result": "activated"}


def _runner(tmp_path: Path, control: Control | None = None, **kwargs: Any) -> Any:
    module = kwargs.pop("module", None) or _load()
    launch = module.HostedLaunchRunner.from_config(
        _write_config(tmp_path),
        state_dir=tmp_path / "state",
        run_id="owner-launch-001",
        mode="live",
        milestone="owner",
        allow_loopback_fixture=True,
        control=control,
        **kwargs,
    )
    return module, launch


def test_launch_preflight_is_read_only_and_pins_public_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()
    control = Control(module)
    _, launch = _runner(tmp_path, control, module=module)

    report = launch.advance(execute=False, now=100.0)

    assert control.posts == []
    assert report["outcome"] == "preflight-passed"
    assert report["identity"]["invitation"]["reference"] == "owner-invite-2026-09"
    rendered = json.dumps(report)
    assert "owner-invite-secret" not in rendered
    assert "operator-secret" not in rendered
    assert launch.manifest()["stages"]["runtime_target"]["status"] == "pending"


def test_launch_resume_refuses_identity_or_unknown_config_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module, launch = _runner(tmp_path)
    launch.prepare(now=100.0)
    changed = _config()
    changed["release"]["candidate_id"] = "018f2d91-7c42-7000-8000-000000000022"
    changed_path = _write_config(tmp_path, changed)
    resumed = module.HostedLaunchRunner.from_config(
        changed_path,
        state_dir=tmp_path / "state",
        run_id="owner-launch-001",
        mode="live",
        milestone="owner",
        allow_loopback_fixture=True,
    )
    with pytest.raises(module.AcceptanceError, match="launch identity"):
        resumed.prepare(now=101.0)

    changed = _config()
    changed["unexpected"] = True
    with pytest.raises(module.AcceptanceError, match="configuration fields"):
        module.validate_launch_config(changed, mode="live", milestone="owner")


def test_launch_persists_effect_intent_before_call_and_resumes_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()

    class Interrupted(Control):
        def mutate_contracts(self, body: dict[str, Any]) -> dict[str, Any]:
            self.posts.append(body)
            raise KeyboardInterrupt

    control = Interrupted(module)
    _, launch = _runner(tmp_path, control, module=module)
    with pytest.raises(KeyboardInterrupt):
        launch.advance(execute=True, now=100.0)
    effect = launch.manifest()["effects"]["runtime_target"]
    assert effect["status"] == "intent"
    assert effect["effect_id"] == launch.effect_id("runtime_target")

    control.mutate_contracts = Control.mutate_contracts.__get__(control, Interrupted)
    launch.advance(execute=True, now=101.0)
    assert control.posts == [
        {"action": "import-runtime-target", "candidateId": CANDIDATE_ID},
        {"action": "import-runtime-target", "candidateId": CANDIDATE_ID},
        {
            "action": "activate-runtime",
            "candidateId": CANDIDATE_ID,
            "expectedLiveCandidateId": None,
            "expectedRoutableCellDigest": "9" * 64,
        },
    ]
    assert launch.manifest()["effects"]["runtime_target"]["status"] == "confirmed"


@pytest.mark.parametrize("effect", ["runtime_target", "runtime_activation"])
def test_launch_reconciles_lost_ack_without_repeating_effect(
    effect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()

    class LostAck(Control):
        def __init__(self) -> None:
            super().__init__(module, imported=effect == "runtime_activation")
            self.lost = False

        def mutate_contracts(self, body: dict[str, Any]) -> dict[str, Any]:
            result = super().mutate_contracts(body)
            current = "runtime_target" if body["action"] == "import-runtime-target" else "runtime_activation"
            if current == effect and not self.lost:
                self.lost = True
                raise module.AmbiguousLaunchEffect("response lost")
            return result

    control = LostAck()
    _, launch = _runner(tmp_path, control, module=module)
    first = launch.advance(execute=True, now=100.0)
    assert first["outcome"] == "pending"
    assert launch.manifest()["effects"][effect]["status"] == "uncertain"

    launch.advance(execute=True, now=102.0)

    actions = [post["action"] for post in control.posts]
    expected = "import-runtime-target" if effect == "runtime_target" else "activate-runtime"
    assert actions.count(expected) == 1
    assert launch.manifest()["effects"][effect]["status"] == "confirmed"


def test_launch_deadline_survives_restart_and_expires_to_needs_attention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()

    class AlwaysLost(Control):
        def mutate_contracts(self, body: dict[str, Any]) -> dict[str, Any]:
            self.posts.append(body)
            raise module.AmbiguousLaunchEffect("response lost")

    control = AlwaysLost(module)
    _, launch = _runner(tmp_path, control, module=module)
    launch.advance(execute=True, now=100.0)
    before = launch.manifest()["stages"]["runtime_target"]
    assert before["deadline_at"] == 160.0

    resumed = module.HostedLaunchRunner.from_config(
        _write_config(tmp_path),
        state_dir=tmp_path / "state",
        run_id="owner-launch-001",
        mode="live",
        milestone="owner",
        allow_loopback_fixture=True,
        control=control,
    )
    report = resumed.advance(execute=True, now=161.0)
    stage = resumed.manifest()["stages"]["runtime_target"]
    assert report["outcome"] == "needs-attention"
    assert stage["status"] == "blocked"
    assert stage["started_at"] == before["started_at"]
    assert stage["next_action"] == "verify the imported runtime target, then rerun this launch"

    resumed.rearm_expired_stages(now=200.0)
    rearmed = resumed.manifest()["stages"]["runtime_target"]
    assert rearmed["deadline_at"] == 260.0
    assert rearmed["deadline_history"] == [{"started_at": 100.0, "deadline_at": 160.0}]


def test_runtime_target_digest_binds_all_ten_reviewed_fields() -> None:
    module = _load()
    changed = _config()
    changed["release"]["runtime_target"]["runtimeImage"] = "ghcr.io/artexis10/exomem@sha256:" + "a" * 64

    with pytest.raises(module.AcceptanceError, match="canonical identity"):
        module.validate_launch_config(changed, mode="live", milestone="owner")


def test_launch_authorization_expiry_is_checkpointed_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()

    class Expired(Control):
        def contracts(self) -> dict[str, Any]:
            raise module.LaunchAuthorizationExpired("operator credential expired: operator-secret")

    _, launch = _runner(tmp_path, Expired(module), module=module)
    report = launch.advance(execute=True, now=100.0)

    assert report["outcome"] == "needs-attention"
    assert launch.manifest()["stages"]["preflight"]["next_action"] == (
        "refresh the configured operator credential reference, then rerun this launch"
    )
    rendered = json.dumps(launch.manifest())
    assert "operator-secret" not in rendered
    assert launch.manifest()["effects"] == {}


def test_launch_rejects_unknown_authoritative_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()

    class Unknown(Control):
        def contracts(self) -> dict[str, Any]:
            value = super().contracts()
            value["rolloutStatus"][0]["state"] = "mystery"
            return value

    _, launch = _runner(tmp_path, Unknown(module), module=module)
    with pytest.raises(module.AcceptanceError, match="rollout state"):
        launch.advance(execute=False, now=100.0)


def test_launch_concurrent_invocation_is_fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()
    control = Control(module)
    _, first = _runner(tmp_path, control, module=module)
    _, second = _runner(tmp_path, control, module=module)

    with first.lock():
        with pytest.raises(module.AcceptanceError, match="already running"):
            with second.lock():
                pass


def test_launch_cli_requires_explicit_mode_and_milestone(tmp_path: Path) -> None:
    runner = _load()
    assert runner.main(
        [
            "launch",
            "--config",
            str(_write_config(tmp_path)),
            "--state-dir",
            str(tmp_path / "state"),
            "--run-id",
            "owner-launch-001",
        ]
    ) == 2


def test_launch_cli_rejects_local_mode_with_live_destination(tmp_path: Path) -> None:
    runner = _load()
    assert runner.main(
        [
            "launch",
            "--mode",
            "local",
            "--milestone",
            "owner",
            "--config",
            str(_write_config(tmp_path)),
            "--state-dir",
            str(tmp_path / "state"),
            "--run-id",
            "owner-launch-001",
        ]
    ) == 2


def test_oauth_exchange_rejects_changed_token_origin_before_sending_code() -> None:
    module = _load()
    oauth = module.OAuthPKCEClient(
        authorization_server_metadata="https://control.example.test/.well-known/oauth",
        resource="https://control.example.test/api/exomem/mcp/v1",
        client_id="existing-client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )
    discovery = {
        "issuer": "https://control.example.test/api/exomem/oauth",
        "authorization_endpoint": "https://control.example.test/api/exomem/oauth/authorize",
        "token_endpoint": "https://attacker.example/token",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["exomem.read", "exomem.write", "offline_access"],
    }

    with pytest.raises(module.AcceptanceError, match="crosses origins"):
        oauth.exchange_code(discovery, code="secret-code", state="state", expected_state="state", code_verifier="verifier")


def test_legacy_commands_import_when_fcntl_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def without_fcntl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fcntl":
            raise ImportError("not available")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    module = _load()

    assert module.fcntl is None
    assert "prepare" in module._parser()._actions[1].choices


def _owner_client(
    module: Any,
    calls: list[tuple[dict[str, Any], str | None]],
    *,
    lose_first_commit: bool = False,
    released_leaf: bool = False,
    empty_first_recall: bool = False,
) -> Any:
    fact = "hosted owner launch marker for run owner-launch-001"

    class Client:
        lost = False
        recalls = 0

        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Hosted Exomem"}}

        def list_tools(self) -> dict[str, Any]:
            return {"tools": [{"name": "remember"}, {"name": "ask_memory"}, {"name": "read_memory"}]}

        def capture(self, arguments: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
            calls.append((dict(arguments), idempotency_key))
            if arguments.get("validate_only") is True:
                if released_leaf:
                    return {
                        "structuredContent": {
                            "mutated": False,
                            "draft_id": "owner-draft",
                            "draft_hash": "a" * 64,
                            "draft_token": "owner-draft-token",
                            "destination": "Knowledge Base/Notes/Insights/owner-launch.md",
                            "has_non_review_blockers": False,
                            "committable_without_review": True,
                        }
                    }
                return {
                    "structuredContent": {
                        "state": "needs_review",
                        "diagnostics": {
                            "draft_id": "owner-draft",
                            "draft_hash": "a" * 64,
                            "draft_token": "owner-draft-token",
                            "relation_review_hash": "a" * 64,
                            "reviewed_none_required": True,
                            "has_non_review_blockers": False,
                            "committable_after_review": True,
                        },
                    }
                }
            if lose_first_commit and not self.lost:
                self.lost = True
                raise module.AcceptanceError("memory commit response was lost")
            if released_leaf:
                return {
                    "structuredContent": {
                        "path": "Knowledge Base/Notes/Insights/owner-launch.md",
                        "creation": {
                            "mutated": True,
                            "written_paths": ["Knowledge Base/Notes/Insights/owner-launch.md"],
                            "creation": {"draft_id": "owner-draft", "draft_hash": "a" * 64},
                        },
                    }
                }
            return {
                "structuredContent": {
                    "ok": True,
                    "state": "committed",
                    "terminal": True,
                    "status": "committed",
                    "mutated": True,
                }
            }

        def recall(self, query: str) -> dict[str, Any]:
            assert "owner-launch-001" in query
            self.recalls += 1
            if empty_first_recall and self.recalls == 1:
                return {"structuredContent": {"result": {"hits": []}}}
            return {
                "structuredContent": {
                    "result": {
                        "hits": [
                            {
                                "path": "Knowledge Base/Notes/Insights/owner-launch.md",
                                "title": "Owner launch evidence",
                            }
                        ]
                    }
                }
            }

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "tools/call"
            assert params["name"] == "read_memory"
            return {"structuredContent": {"result": {"content": fact}}}

    return Client()


def test_launch_resumes_after_consumed_invite_to_lifecycle_and_useful_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()

    class Consumed(Control):
        def inspect_invitation(self) -> dict[str, Any]:
            raise AssertionError("a consumed invite must not be inspected after protected state exists")

    control = Consumed(module, imported=True, active=True)
    _, launch = _runner(tmp_path, control, module=module)
    launch.save_oauth_tokens(
        {"access_token": "owner-access", "refresh_token": "owner-refresh", "expires_in": 900}
    )
    calls: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, calls))

    report = launch.advance(execute=True, now=100.0)

    manifest = launch.manifest()
    assert manifest["stages"]["consent"]["status"] == "passed"
    assert manifest["stages"]["service_ready"]["status"] == "passed"
    assert report["outcome"] == "needs-attention"
    assert manifest["stages"]["milestone"]["next_action"].startswith("complete owner host confirmation")
    assert calls[0][0]["validate_only"] is True
    assert calls[1][1] == launch.effect_id("owner_memory")


def test_launch_memory_lost_ack_replays_same_reviewed_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()

    class Consumed(Control):
        def inspect_invitation(self) -> dict[str, Any]:
            raise AssertionError("consumed invitation was re-read")

    control = Consumed(module, imported=True, active=True)
    _, launch = _runner(tmp_path, control, module=module)
    launch.save_oauth_tokens(
        {"access_token": "owner-access", "refresh_token": "owner-refresh", "expires_in": 900}
    )
    calls: list[tuple[dict[str, Any], str | None]] = []
    client = _owner_client(module, calls, lose_first_commit=True)
    monkeypatch.setattr(launch, "_mcp_client", lambda: client)

    first = launch.advance(execute=True, now=100.0)
    assert first["outcome"] == "pending"
    assert launch.manifest()["effects"]["owner_memory"]["status"] == "uncertain"

    launch.advance(execute=True, now=102.0)

    commits = [(arguments, key) for arguments, key in calls if arguments.get("validate_only") is not True]
    assert len(commits) == 2
    assert commits[0] == commits[1]
    assert commits[0][1] == launch.effect_id("owner_memory")
    assert launch.manifest()["effects"]["owner_memory"]["status"] == "confirmed"


def test_launch_accepts_released_v4_leaf_memory_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.save_oauth_tokens({"access_token": "owner-access", "refresh_token": "owner-refresh"})
    calls: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, calls, released_leaf=True))

    report = launch.advance(execute=True, now=100.0)

    assert report["stages"]["service_ready"]["status"] == "passed"
    assert launch.manifest()["effects"]["owner_memory"]["status"] == "confirmed"


def test_released_leaf_receipt_must_match_prepared_draft() -> None:
    module = _load()
    result = {
        "structuredContent": {
            "path": "Knowledge Base/Notes/Insights/owner-launch.md",
            "creation": {
                "mutated": True,
                "written_paths": ["Knowledge Base/Notes/Insights/owner-launch.md"],
                "creation": {"draft_id": "foreign-draft", "draft_hash": "b" * 64},
            },
        }
    }

    assert not module.released_memory_receipt(
        result,
        expected_draft_id="owner-draft",
        expected_draft_hash="a" * 64,
        expected_path="Knowledge Base/Notes/Insights/owner-launch.md",
    )


def test_launch_poll_owner_waits_to_next_check_without_extending_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()

    class Preparing(Control):
        observations = 0

        def lifecycle(self) -> dict[str, Any]:
            self.observations += 1
            state = "preparing" if self.observations == 1 else "ready"
            return {"success": True, "status": {"state": state, "code": state.upper(), "retryable": state != "ready"}}

    _, launch = _runner(tmp_path, Preparing(module, imported=True, active=True), module=module)
    launch.save_oauth_tokens({"access_token": "owner-access", "refresh_token": "owner-refresh"})
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, []))
    current = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    report = launch.run_until_checkpoint(execute=True, clock=lambda: current[0], sleeper=sleep)

    assert sleeps == [1.0]
    assert report["stages"]["service_ready"]["deadline_at"] == 3700.0
    assert report["stages"]["service_ready"]["status"] == "passed"


def test_launch_polls_async_recall_without_recommitting_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.save_oauth_tokens({"access_token": "owner-access", "refresh_token": "owner-refresh"})
    calls: list[tuple[dict[str, Any], str | None]] = []
    client = _owner_client(module, calls, empty_first_recall=True)
    monkeypatch.setattr(launch, "_mcp_client", lambda: client)
    current = [100.0]

    def sleep(seconds: float) -> None:
        current[0] += seconds

    report = launch.run_until_checkpoint(execute=True, clock=lambda: current[0], sleeper=sleep)

    commits = [arguments for arguments, _ in calls if arguments.get("validate_only") is not True]
    assert len(commits) == 1
    assert client.recalls == 2
    assert report["stages"]["service_ready"]["status"] == "passed"


def test_passed_consent_deadline_does_not_cap_service_ready_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()
    config = _config()
    config["deadlines_seconds"]["consent"] = 1
    launch = module.HostedLaunchRunner.from_config(
        _write_config(tmp_path, config),
        state_dir=tmp_path / "state",
        run_id="owner-launch-001",
        mode="live",
        milestone="owner",
        control=Control(module, imported=True, active=True),
    )
    launch.save_oauth_tokens({"access_token": "owner-access", "refresh_token": "owner-refresh"})
    states = iter(("preparing", "ready"))
    launch.control.lifecycle = lambda: {
        "success": True,
        "status": {"state": (state := next(states)), "code": state.upper(), "retryable": state != "ready"},
    }
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, []))

    launch.advance(execute=True, now=100.0)
    report = launch.advance(execute=True, now=102.0)

    assert report["stages"]["consent"]["status"] == "passed"
    assert report["stages"]["service_ready"]["status"] == "passed"


def test_lost_oauth_token_ack_requires_new_grant_for_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.oauth_request_path.parent.mkdir(parents=True, exist_ok=True)
    launch.oauth_request_path.write_text(
        json.dumps(
            {
                "url": "https://control.example/authorize",
                "state": "state-1",
                "code_verifier": "verifier-1",
                "status": "pending",
            }
        ),
        encoding="utf-8",
    )

    class OAuth:
        calls = 0

        def discover(self) -> dict[str, Any]:
            return {"token_endpoint": "https://control.example/token"}

        def exchange_code(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise module.AcceptanceError("OAuth token exchange failed: TimeoutError")

    oauth = OAuth()
    monkeypatch.setattr(launch, "_oauth_client", lambda: oauth)

    first = launch.advance(
        execute=True,
        now=100.0,
        authorization_code="possibly-consumed-code",
        callback_state="state-1",
    )
    second = launch.advance(
        execute=True,
        now=101.0,
        authorization_code="possibly-consumed-code",
        callback_state="state-1",
    )

    assert oauth.calls == 1
    assert first["stages"]["consent"]["status"] == "blocked"
    assert "new authorization grant" in second["stages"]["consent"]["next_action"]
    assert "same owner, client and invitation identity" in second["stages"]["consent"]["next_action"]


def test_oauth_process_death_persists_ambiguous_intent_before_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.oauth_request_path.parent.mkdir(parents=True, exist_ok=True)
    launch.oauth_request_path.write_text(
        json.dumps({"url": "https://control.example/old", "state": "state", "code_verifier": "verifier", "status": "pending"}),
        encoding="utf-8",
    )

    class Dies:
        def discover(self) -> dict[str, Any]:
            return {}

        def exchange_code(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise KeyboardInterrupt

    monkeypatch.setattr(launch, "_oauth_client", lambda: Dies())
    with pytest.raises(KeyboardInterrupt):
        launch.advance(execute=True, now=100.0, authorization_code="code", callback_state="state")
    assert json.loads(launch.oauth_request_path.read_text())["status"] == "exchange-in-flight"

    class Fresh:
        def discover(self) -> dict[str, Any]:
            return {}

        def authorization_request(self, discovery: dict[str, Any]) -> dict[str, str]:
            return {"url": "https://control.example/new", "state": "new-state", "code_verifier": "new-verifier"}

    launch.control_injected = False
    monkeypatch.setattr(launch, "_oauth_client", lambda: Fresh())
    launch.advance(execute=True, now=101.0)
    renewed = json.loads(launch.oauth_request_path.read_text())
    assert renewed["status"] == "pending"
    assert renewed["state"] == "new-state"


def test_expired_access_token_refreshes_once_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.save_oauth_tokens({"access_token": "expired", "refresh_token": "refresh-1", "expires_at": 99.0})

    class OAuth:
        calls = 0

        def discover(self) -> dict[str, Any]:
            return {}

        def refresh(self, discovery: dict[str, Any], *, refresh_token: str) -> dict[str, Any]:
            self.calls += 1
            assert refresh_token == f"refresh-{self.calls}"
            return {"access_token": f"fresh-{self.calls}", "refresh_token": f"refresh-{self.calls + 1}", "expires_in": 900}

    oauth = OAuth()
    monkeypatch.setattr(launch, "_oauth_client", lambda: oauth)
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, []))

    report = launch.advance(execute=True, now=100.0)

    assert oauth.calls == 1
    assert report["stages"]["service_ready"]["status"] == "passed"
    assert launch.load_oauth_tokens()["refresh_token"] == "refresh-2"
    tokens = launch.load_oauth_tokens()
    tokens["expires_at"] = 101.0
    launch.save_oauth_tokens(tokens)
    launch.advance(execute=True, now=102.0)
    assert oauth.calls == 2
    assert launch.load_oauth_tokens()["refresh_token"] == "refresh-3"


def test_lost_refresh_ack_never_replays_rotating_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()
    _, launch = _runner(tmp_path, Control(module, imported=True, active=True), module=module)
    launch.save_oauth_tokens({"access_token": "expired", "refresh_token": "refresh-1", "expires_at": 99.0})

    class OAuth:
        calls = 0

        def discover(self) -> dict[str, Any]:
            return {}

        def refresh(self, discovery: dict[str, Any], *, refresh_token: str) -> dict[str, Any]:
            self.calls += 1
            raise KeyboardInterrupt

    oauth = OAuth()
    monkeypatch.setattr(launch, "_oauth_client", lambda: oauth)
    with pytest.raises(KeyboardInterrupt):
        launch.advance(execute=True, now=100.0)
    report = launch.advance(execute=True, now=101.0)

    assert oauth.calls == 1
    assert report["stages"]["consent"]["status"] == "blocked"
    assert "same owner, client and invitation identity" in report["stages"]["consent"]["next_action"]

    class FreshRefresh:
        def discover(self) -> dict[str, Any]:
            return {}

        def refresh(self, discovery: dict[str, Any], *, refresh_token: str) -> dict[str, Any]:
            assert refresh_token == "refresh-new"
            return {"access_token": "fresh", "refresh_token": "refresh-next", "expires_in": 900}

    launch.save_oauth_tokens({"access_token": "new-grant", "refresh_token": "refresh-new", "expires_at": 102.0})
    monkeypatch.setattr(launch, "_oauth_client", lambda: FreshRefresh())
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, []))
    resumed = launch.advance(execute=True, now=103.0)
    assert resumed["stages"]["service_ready"]["status"] == "passed"
    assert launch.load_oauth_tokens()["refresh_token"] == "refresh-next"


def test_real_control_path_creates_private_pkce_request_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "owner-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    module = _load()
    config_path = _write_config(tmp_path)
    launch = module.HostedLaunchRunner.from_config(
        config_path,
        state_dir=tmp_path / "state",
        run_id="owner-20260919",
        mode="live",
        milestone="owner",
    )
    launch.control = Control(module, imported=True, active=True)

    class OAuth:
        def discover(self) -> dict[str, Any]:
            return {"issuer": "https://control.example.test"}

        def authorization_request(self, discovery: dict[str, Any]) -> dict[str, str]:
            return {"url": "https://control.example.test/authorize", "state": "state", "code_verifier": "verifier"}

    monkeypatch.setattr(launch, "_oauth_client", lambda: OAuth())

    report = launch.advance(execute=True, now=100.0)

    assert report["stages"]["consent"]["status"] == "blocked"
    assert json.loads(launch.oauth_request_path.read_text(encoding="utf-8"))["status"] == "pending"
    assert launch.oauth_request_path.stat().st_mode & 0o077 == 0


def test_first_callback_resumes_after_invitation_was_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "consumed-invite-secret")
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("TEST_OWNER_SESSION", "owner-session-secret")
    module = _load()

    class Consumed(Control):
        def inspect_invitation(self) -> dict[str, Any]:
            raise AssertionError("callback resume must not inspect the consumed invitation")

        def capacity(self) -> dict[str, Any]:
            value = _capacity()
            value["capacity"]["reservedStorageBytes"] = value["capacity"]["storageCapacityBytes"]
            value["capacity"]["reservedRuntimeSlots"] = value["capacity"]["runtimeCapacitySlots"]
            value["capacity"]["activeProvisionClaims"] = value["capacity"]["provisionClaimCapacity"]
            return value

    _, launch = _runner(tmp_path, Consumed(module, imported=True, active=True), module=module)
    launch.oauth_request_path.parent.mkdir(parents=True, exist_ok=True)
    launch.oauth_request_path.write_text(
        json.dumps({"url": "https://control.example/authorize", "state": "state", "code_verifier": "verifier", "status": "pending"}),
        encoding="utf-8",
    )

    class OAuth:
        def discover(self) -> dict[str, Any]:
            return {}

        def exchange_code(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"access_token": "owner-access", "refresh_token": "owner-refresh", "expires_in": 900}

    calls: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(launch, "_oauth_client", lambda: OAuth())
    monkeypatch.setattr(launch, "_mcp_client", lambda: _owner_client(module, calls))

    report = launch.advance(
        execute=True,
        now=100.0,
        authorization_code="fresh-code",
        callback_state="state",
    )

    assert report["stages"]["consent"]["status"] == "passed"
    assert report["stages"]["service_ready"]["status"] == "passed"


def test_hosted_launch_control_uses_exact_public_routes_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
            if self.path == "/api/exomem/admin/contracts":
                self._json(_contracts())
            elif self.path == "/api/exomem/admin/capacity":
                self._json(_capacity())
            elif self.path == "/api/exomem/admin/fleet":
                self._json(_fleet())
            elif self.path == "/api/exomem/status":
                self._json({"success": True, "status": {"state": "ready", "code": "CELL_READY", "retryable": False}})
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
            if self.path == "/api/exomem/access/inspect":
                self._json({"success": True, "email": "owner@example.test", "expiresAt": "2026-09-30T12:00:00.000Z"})
            elif self.path == "/api/exomem/admin/contracts":
                self._json({"success": True, "result": "activated"})
            else:
                self.send_error(404)

        def _json(self, value: dict[str, Any]) -> None:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        config = _config()
        base = f"http://127.0.0.1:{server.server_port}"
        config["control_base_url"] = base
        config["oauth"]["authorization_server_metadata"] = base + "/.well-known/oauth-authorization-server/api/exomem/oauth"
        config["oauth"]["resource"] = base + "/api/exomem/mcp/v1"
        validated = module.validate_launch_config(config, mode="local", milestone="owner", allow_loopback_fixture=True)
        monkeypatch.setenv("TEST_OWNER_INVITE_TOKEN", "invite-secret")
        monkeypatch.setenv("TEST_OPERATOR_TOKEN", "operator-secret")
        monkeypatch.setenv("TEST_OWNER_SESSION", "session-secret")
        control = module.HostedLaunchControl(validated, allow_loopback_fixture=True)

        control.contracts()
        control.capacity()
        control.fleet()
        control.inspect_invitation()
        control.lifecycle()
        control.mutate_contracts(
            {
                "action": "activate-runtime",
                "candidateId": CANDIDATE_ID,
                "expectedLiveCandidateId": None,
                "expectedRoutableCellDigest": "9" * 64,
            }
        )
        direct_requests = list(requests)
        requests.clear()
        config_path = _write_config(tmp_path, config)
        assert module.main(
            [
                "launch", "--mode", "local", "--milestone", "owner",
                "--config", str(config_path), "--state-dir", str(tmp_path / "main-state"),
                "--run-id", "owner-loopback-001",
            ]
        ) == 0
        assert [request["path"] for request in requests] == [
            "/api/exomem/admin/contracts",
            "/api/exomem/admin/capacity",
            "/api/exomem/admin/fleet",
            "/api/exomem/access/inspect",
        ]
    finally:
        server.shutdown()
        thread.join()

    assert [request["path"] for request in direct_requests] == [
        "/api/exomem/admin/contracts",
        "/api/exomem/admin/capacity",
        "/api/exomem/admin/fleet",
        "/api/exomem/access/inspect",
        "/api/exomem/status",
        "/api/exomem/admin/contracts",
    ]
    assert all(request["headers"].get("Authorization") == "Bearer operator-secret" for request in direct_requests[:3])
    assert "Authorization" not in direct_requests[3]["headers"]
    assert direct_requests[3]["headers"]["Origin"] == base
    assert direct_requests[3]["body"] == {"token": "invite-secret"}
    assert direct_requests[4]["headers"]["Cookie"] == "exomem_session=session-secret"
