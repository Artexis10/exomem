"""The `/media-worker/v1/` routes through the real app, ledger and parent publication.

Only the speech engine is absent: a worker is the test itself, which posts the structure a real one would.
All names, tokens and bytes are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.testclient import TestClient

from exomem import (
    __main__ as cli,
)
from exomem import (
    extract,
    media_jobs,
    media_processing,
    media_remote_routing,
    media_worker,
    media_worker_credentials,
    originals,
    preserve,
    server,
    server_media_worker,
    voice_embed,
    voice_profiles,
)
from exomem.asr_facts import AsrFacts
from exomem.auth_sessions import SessionAuthority
from exomem.session_oauth import ExomemSessionOAuthProxy
from exomem.vault import content_hash, parse_frontmatter

AUDIO = b"FAKE-AUDIO-BYTES-0123456789"
TOKEN = "mw1_token-of-the-first-worker"
OTHER_TOKEN = "mw1_token-of-the-second-worker"
UPLOAD_TOKEN = "the-static-upload-token"
REST_KEY = "the-owner-rest-key"
ISSUER = "https://memory.example"
BASE = "/media-worker/v1"
FACTS = AsrFacts(
    language="et", language_source="requested", language_requested="et", language_mode="single",
    decoding="batched", speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-2",
)
SEGMENTS = [{"start": 1.0, "end": 3.0, "text": "tere maailm"}, {"start": 65.0, "end": 68.0, "text": "head aega"}]
EXTRACTION = {"engine": "faster-whisper:test", "asr": FACTS.as_payload(), "segments": SEGMENTS}


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(server_media_worker, "CLAIM_WAIT_SECONDS", 0.0)  # an idle claim answers at once


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The ledger's and the credentials' time, which a test moves by changing `clock[0]`."""
    now = [time.time()]
    monkeypatch.setattr(media_jobs.time, "time", lambda: now[0])
    return now


def _route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vault: Path, entry: object) -> None:
    config = tmp_path / "media-config.json"
    config.write_text(json.dumps({"vaults": {str(vault.resolve()): entry}}), encoding="utf-8")
    monkeypatch.setenv(originals.CONFIG_ENV, str(config))


def _serve(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, media_types=("audio",), **block
) -> TestClient:
    """The real app with audio routed to remote workers, and the other credentials of the service set."""
    _route(tmp_path, monkeypatch, vault, {"remote_workers": {"media_types": list(media_types), **block}})
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", UPLOAD_TOKEN)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", REST_KEY)
    return TestClient(server.build_server(require_auth=False).http_app())


def _credential(vault: Path, token: str = TOKEN, *, name: str = "worker-one", **limits) -> str:
    return media_worker_credentials.issue(media_jobs.MediaJobStore(vault), token, name=name, **limits).id


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _audio(vault: Path, name: str = "talk.mp3", data: bytes = AUDIO) -> tuple[Path, Path]:
    """A preserved recording with its pending speech job; returns the original and its sidecar."""
    stub = preserve.preserve_bytes(vault, scope="Rental", category="audio", filename=name, data=data)
    media_processing.reconcile_media(vault, vault / stub.path, explicit=False)
    return vault / stub.path, vault / stub.sidecar_path


def _claim(client: TestClient, token: str = TOKEN, **changes) -> httpx.Response:
    body = {
        "protocol": 1, "slots": 4,
        "capabilities": {"media_types": ["audio", "video"], "engines": ["speech"], "device": "cpu", "diarize": False},
        **changes,
    }
    return client.post(f"{BASE}/claim", json=body, headers=_auth(token))


def _lease(client: TestClient, token: str = TOKEN) -> dict:
    claimed = _claim(client, token)
    assert claimed.status_code == 200, claimed.text
    return claimed.json()["lease"]


def _post(client: TestClient, lease: dict, action: str, token: str = TOKEN, **body) -> httpx.Response:
    return client.post(
        f"{BASE}/leases/{lease['job_id']}/{action}",
        json={"claim_revision": lease["claim_revision"], **body},
        headers=_auth(token),
    )


def _original(client: TestClient, lease: dict, token: str = TOKEN, **headers: str) -> httpx.Response:
    return client.get(
        f"{BASE}/leases/{lease['job_id']}/original",
        params={"claim_revision": lease["claim_revision"]},
        headers={**_auth(token), **headers},
    )


def _voiceprint(axis: int, scale: float = 1.0) -> list[float]:
    """A synthetic voiceprint: orthogonal axes are different speakers, and the scale marks the values in a leak check."""
    return [scale if index == axis else 0.0 for index in range(voice_embed.VOICE_EMBED_DIM)]


def _enrol(vault: Path, name: str, axis: int) -> None:
    voice_profiles.save_profile(
        voice_profiles.voice_profiles_path(vault), name, np.asarray(_voiceprint(axis)), vault_root=vault
    )


# The first speaker talks in the first segment, the second in the second; their voiceprints are far apart.
VOICEPRINTS = {"c0": _voiceprint(0, 0.8123456789), "c1": _voiceprint(1, 0.6123456789)}
TURNS = [{"start": 0.0, "end": 10.0, "cluster": "c0"}, {"start": 60.0, "end": 70.0, "cluster": "c1"}]
DIARIZED = {**EXTRACTION, "diarization": {"turns": TURNS, "clusters": VOICEPRINTS}}


def _row(vault: Path, job_id: int) -> sqlite3.Row:
    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _publish(vault: Path) -> None:
    media_worker.MediaWorker(vault, execution_mode="process")._drain_parent_results()


# ---- admission -------------------------------------------------------------------------------------


def test_a_worker_credential_opens_no_other_route_of_the_service(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)

    download = client.get("/download", params={"path": "Knowledge Base/index.md"}, headers=_auth())
    upload = client.post("/upload", files={"file": ("a.txt", b"x")}, headers=_auth())
    rest = client.post("/api/ask_memory", json={"query": "x"}, headers=_auth())

    assert (download.status_code, upload.status_code, rest.status_code) == (401, 401, 401)


@pytest.mark.anyio
async def test_a_worker_credential_is_not_an_oauth_session_on_mcp(vault, tmp_path) -> None:
    class Provider:
        required_scopes: list[str] = []

        async def verify_token(self, token: str) -> None:
            raise AssertionError("a worker credential must never reach the identity provider")

    authority = SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root="root-of-tests", issuer=ISSUER, audience=f"{ISSUER}/mcp"
    )
    proxy = ExomemSessionOAuthProxy(
        session_authority=authority,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="client", upstream_client_secret="secret", token_verifier=Provider(),
        base_url=ISSUER, client_storage=MemoryStore(), jwt_signing_key="jwt-root-of-tests",
        require_authorization_consent=False,
    )
    mcp = server.ExomemFastMCP("probe", auth=proxy)
    server_media_worker.register_media_worker_routes(mcp, vault_root=vault)
    app = mcp.http_app(stateless_http=True, json_response=True)
    _credential(vault)
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            refused = await client.post("/mcp", json=initialize, headers=_auth())
            admitted = await client.post(
                f"{BASE}/claim", headers=_auth(),
                json={"protocol": 1, "slots": 1, "capabilities": {"media_types": [], "engines": []}},
            )

    assert refused.status_code == 401
    assert admitted.status_code == 204  # the same credential is a worker's on its own routes


@pytest.mark.parametrize("other", [UPLOAD_TOKEN, REST_KEY])
def test_no_other_credential_of_the_service_admits_a_worker_route(vault, tmp_path, monkeypatch, other) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)

    assert _claim(client, other).status_code == 401


def test_a_missing_credential_gets_401_on_claim(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)

    refused = client.post(f"{BASE}/claim", json={"protocol": 1})

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Bearer"


def test_an_unknown_credential_gets_401_on_claim(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)

    assert _claim(client, "mw1_a-token-nobody-issued").status_code == 401


def test_an_expired_credential_gets_401_on_claim(vault, tmp_path, monkeypatch, clock) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault, days=1)
    assert _claim(client).status_code == 204

    clock[0] += 2 * 86400

    assert _claim(client).status_code == 401


def test_a_revoked_credential_gets_401_on_claim(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault, name="laptop")
    assert _claim(client).status_code == 204

    assert media_worker_credentials.revoke(media_jobs.MediaJobStore(vault), "laptop") == 1

    assert _claim(client).status_code == 401


def test_an_unsupported_protocol_gets_426(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)

    refused = _claim(client, protocol=2)

    assert refused.status_code == 426
    assert refused.json()["code"] == "MEDIA_WORKER_PROTOCOL_UNSUPPORTED"


def test_a_malformed_claim_gets_400(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)

    assert _claim(client, slots=0).status_code == 400


# ---- claim -----------------------------------------------------------------------------------------


def test_a_claim_returns_a_routed_pending_job_with_what_the_grant_read(vault, tmp_path, monkeypatch, clock) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    credential_id = _credential(vault)
    binary, sidecar = _audio(vault)

    claimed = _claim(client)

    assert claimed.status_code == 200
    lease, job = claimed.json()["lease"], claimed.json()["job"]
    held = media_jobs.MediaJobStore(vault).get_lease(lease["job_id"])
    assert held.worker == credential_id
    assert lease == {
        "job_id": held.job.id, "claim_revision": 1, "expires_at": clock[0] + 120, "deadline_at": held.deadline_at,
        "heartbeat_seconds": 30.0,
    }
    assert (job["media_type"], job["language"], job["language_mode"], job["reprocess"]) == ("audio", None, None, False)
    assert job["original"] == {"size": len(AUDIO), "sha256": hashlib.sha256(AUDIO).hexdigest()}
    assert job["asr_policy"]["revision"] == "fw-policy-2"
    assert held.snapshot.sidecar_before_hash == content_hash(sidecar.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("declared", "language", "mode"),
    [({"language": "et"}, "et", "single"), ({"multilingual": True}, None, "multilingual")],
    ids=["language", "multilingual"],
)
def test_a_claim_carries_the_language_a_request_declared(vault, tmp_path, monkeypatch, declared, language, mode) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    stub = preserve.preserve_bytes(vault, scope="Rental", category="audio", filename="talk.mp3", data=AUDIO)
    media_processing.process_declared_media(vault, vault / stub.path, **declared)

    job = _claim(client).json()["job"]

    assert (job["language"], job["language_mode"]) == (language, mode)


@pytest.mark.parametrize(
    ("routed", "covered"),
    [(("video",), ["audio", "video"]), (("audio",), ["video"])],
    ids=["kind not routed", "kind not covered by the worker"],
)
def test_a_kind_that_is_not_routed_or_not_covered_is_not_leased(
    vault, tmp_path, monkeypatch, routed, covered
) -> None:
    client = _serve(vault, tmp_path, monkeypatch, media_types=routed)
    _credential(vault)
    _audio(vault)

    claimed = _claim(client, capabilities={"media_types": covered, "engines": ["speech"]})

    assert claimed.status_code == 204


def test_a_vault_with_no_media_configuration_routes_nothing(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    monkeypatch.delenv(originals.CONFIG_ENV)  # the vault has no media configuration at all
    _credential(vault)
    _audio(vault)

    assert _claim(client).status_code == 204


def test_an_invalid_remote_workers_block_routes_nothing_and_the_local_worker_keeps_the_job(
    vault, tmp_path, monkeypatch, caplog
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _route(tmp_path, monkeypatch, vault, {"remote_workers": {"media_types": ["pdf"]}})
    media_remote_routing._warn_once.cache_clear()
    _credential(vault)
    _audio(vault)

    with caplog.at_level("WARNING", logger=media_remote_routing.log.name):
        assert _claim(client).status_code == 204
        assert _claim(client).status_code == 204

    assert [r.getMessage() for r in caplog.records].count(
        "remote media workers are off: remote_workers.media_types must be a list drawn from ['audio', 'video']"
    ) == 1
    assert media_jobs.MediaJobStore(vault).claim_next() is not None  # a local claim still takes it


def test_a_vault_that_only_configures_remote_workers_has_no_media_store(vault, tmp_path, monkeypatch) -> None:
    _route(tmp_path, monkeypatch, vault, {"remote_workers": {"media_types": ["audio"]}})

    assert originals.configuration(vault) is None


def test_an_idle_claim_waits_and_returns_the_job_that_arrives(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    monkeypatch.setattr(server_media_worker, "CLAIM_WAIT_SECONDS", 10.0)
    monkeypatch.setattr(server_media_worker, "CLAIM_POLL_SECONDS", 0.05)
    arrival = threading.Timer(0.5, _audio, (vault,))
    arrival.start()
    try:
        claimed = _claim(client)
    finally:
        arrival.join()

    assert claimed.status_code == 200


def test_a_credential_at_its_lease_limit_gets_204(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault, max_leases=1)
    _audio(vault, "first.mp3", b"first-recording")
    _audio(vault, "second.mp3", b"second-recording")
    _lease(client)

    assert _claim(client, slots=4).status_code == 204


def test_a_claim_gives_a_job_back_whose_lease_expired(vault, tmp_path, monkeypatch, clock) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _credential(vault, OTHER_TOKEN, name="worker-two")
    _audio(vault)
    first = _lease(client)
    clock[0] += 3600  # past the lease, with no sweep in between

    second = _lease(client, OTHER_TOKEN)

    assert (second["job_id"], second["claim_revision"]) == (first["job_id"], 2)


def test_a_gated_original_is_never_leased(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    binary, _sidecar = _audio(vault)
    rules = vault / "Knowledge Base/_Governance/rules"
    rules.mkdir(parents=True)
    # A governance rule that cannot be read withholds every original from every audience.
    (rules / "unreadable.yaml").write_text("governance_version: 1\nid: not-an-id\n", encoding="utf-8")

    assert _claim(client).status_code == 204

    assert _row(vault, 1)["state"] == "pending"


# ---- lease calls -----------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["heartbeat", "release", "result"])
def test_another_workers_call_is_refused_and_the_lease_is_unchanged(vault, tmp_path, monkeypatch, action) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _credential(vault, OTHER_TOKEN, name="worker-two")
    _audio(vault)
    lease = _lease(client)
    held = media_jobs.MediaJobStore(vault).get_lease(lease["job_id"])
    body = {"reason": "shutdown"} if action == "release" else (
        {"kind": "extraction", "payload": EXTRACTION} if action == "result" else {}
    )

    refused = _post(client, lease, action, OTHER_TOKEN, **body)

    assert (refused.status_code, refused.json()["code"]) == (409, "MEDIA_LEASE_LOST")
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]) == held


def test_another_workers_original_read_is_refused(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _credential(vault, OTHER_TOKEN, name="worker-two")
    _audio(vault)
    lease = _lease(client)

    refused = _original(client, lease, OTHER_TOKEN)

    assert (refused.status_code, refused.json()["code"]) == (409, "MEDIA_LEASE_LOST")
    assert AUDIO not in refused.content


def test_a_heartbeat_extends_the_lease_and_records_the_worker_as_seen(vault, tmp_path, monkeypatch, clock) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    credential_id = _credential(vault)
    _audio(vault)
    lease = _lease(client)
    clock[0] += 50

    beat = _post(client, lease, "heartbeat")

    assert beat.status_code == 200
    assert beat.json()["expires_at"] == clock[0] + 120
    (listed,) = media_worker_credentials.list_credentials(media_jobs.MediaJobStore(vault))
    assert (listed.id, listed.last_seen_at) == (credential_id, clock[0])


@pytest.mark.parametrize(
    ("window", "status", "body", "content_range"),
    [
        (None, 200, AUDIO, None),
        ("bytes=3-8", 206, AUDIO[3:9], f"bytes 3-8/{len(AUDIO)}"),
        ("bytes=-4", 206, AUDIO[-4:], f"bytes {len(AUDIO) - 4}-{len(AUDIO) - 1}/{len(AUDIO)}"),
        ("bytes=10-", 206, AUDIO[10:], f"bytes 10-{len(AUDIO) - 1}/{len(AUDIO)}"),
        (f"bytes={len(AUDIO)}-", 416, b"", f"bytes */{len(AUDIO)}"),
    ],
    ids=["whole", "first-last", "suffix", "open end", "past the end"],
)
def test_a_ranged_original_read_returns_the_requested_bytes(
    vault, tmp_path, monkeypatch, window, status, body, content_range
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    read = _original(client, lease, **({"range": window} if window else {}))

    assert read.status_code == status
    if status != 416:
        assert read.content == body
    assert read.headers.get("content-range") == content_range


def test_an_original_whose_release_changed_during_the_lease_is_not_read(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)
    rules = vault / "Knowledge Base/_Governance/rules"
    rules.mkdir(parents=True)
    (rules / "unreadable.yaml").write_text("governance_version: 1\nid: not-an-id\n", encoding="utf-8")

    refused = _original(client, lease)

    assert refused.status_code == 404
    assert AUDIO not in refused.content


@pytest.mark.parametrize(
    ("reason", "state", "blocked_reason"),
    [("device_busy", "pending", None), ("insufficient_space", "blocked", media_jobs.HYDRATION_TOO_LARGE)],
)
def test_a_release_hands_the_job_back_or_blocks_it_by_its_reason(
    vault, tmp_path, monkeypatch, reason, state, blocked_reason
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    released = _post(client, lease, "release", reason=reason)

    assert released.status_code == 200
    row = _row(vault, lease["job_id"])
    assert (row["state"], row["blocked_reason"]) == (state, blocked_reason)
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]) is None


def test_a_device_busy_release_keeps_the_pin_and_frees_the_workers_slot(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault, max_leases=1)
    _audio(vault, "first.mp3", b"first-recording")
    _audio(vault, "second.mp3", b"second-recording")
    first = _lease(client)
    assert _claim(client).status_code == 204  # the one slot is taken

    _post(client, first, "release", reason="device_busy")

    assert _row(vault, first["job_id"])["pinned"] == 1
    assert _lease(client)["job_id"] != first["job_id"]  # the slot is free, and the handed-back job waits its retry time


def test_an_invalid_release_reason_gets_400_and_the_lease_is_unchanged(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)
    held = media_jobs.MediaJobStore(vault).get_lease(lease["job_id"])

    refused = _post(client, lease, "release", reason="gpu busy")

    assert refused.status_code == 400
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]) == held


# ---- result ----------------------------------------------------------------------------------------


def test_a_remote_extraction_is_published_with_the_asr_facts_and_the_timed_text(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)
    assert _original(client, lease).content == AUDIO

    posted = _post(client, lease, "result", kind="extraction", payload=EXTRACTION)
    assert posted.status_code == 200
    assert media_jobs.MediaJobStore(vault).has_parent_work()  # the supervisor drains it with no local child
    _publish(vault)

    content = sidecar.read_text(encoding="utf-8")
    facts = parse_frontmatter(content)[0]
    assert "[0:01] tere maailm" in content and "[1:05] head aega" in content
    assert facts["extracted_by"] == "faster-whisper:test+timed"
    assert (facts["asr_language"], facts["asr_language_source"], facts["asr_language_requested"]) == (
        "et", "requested", "et")
    assert (facts["asr_duration_seconds"], facts["asr_policy"], facts["asr_decoding"]) == (61.5, "fw-policy-2", "batched")
    assert media_jobs.status(vault)["jobs"] == []


def test_a_recording_with_no_segments_is_published_as_the_local_path_publishes_no_speech(
    vault, tmp_path, monkeypatch
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)

    _post(client, lease, "result", kind="extraction", payload={"engine": "faster-whisper:test", "segments": []})
    _publish(vault)

    assert extract.NO_SPEECH_TIMED_TEXT in sidecar.read_text(encoding="utf-8")


def test_a_sidecar_changed_between_the_grant_and_the_result_is_not_published(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)
    edited = sidecar.read_text(encoding="utf-8") + "\nA note the owner wrote while the worker ran.\n"
    sidecar.write_text(edited, encoding="utf-8")

    assert _post(client, lease, "result", kind="extraction", payload=EXTRACTION).status_code == 200
    _publish(vault)

    assert sidecar.read_text(encoding="utf-8") == edited


def test_a_result_is_accepted_after_the_lease_expired_and_before_the_sweep(vault, tmp_path, monkeypatch, clock) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)
    clock[0] += 3600

    assert _post(client, lease, "result", kind="extraction", payload=EXTRACTION).status_code == 200
    _publish(vault)

    assert "[0:01] tere maailm" in sidecar.read_text(encoding="utf-8")


def test_an_identical_retried_result_succeeds(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    first = _post(client, lease, "result", kind="extraction", payload=EXTRACTION)
    retried = _post(client, lease, "result", kind="extraction", payload=EXTRACTION)

    assert (first.status_code, retried.status_code) == (200, 200)


def test_a_different_second_result_for_the_lease_gets_409(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)
    _post(client, lease, "result", kind="extraction", payload=EXTRACTION)
    other = {**EXTRACTION, "segments": [{"start": 0.0, "end": 2.0, "text": "a different transcript"}]}

    second = _post(client, lease, "result", kind="extraction", payload=other)

    assert (second.status_code, second.json()["code"]) == (409, "MEDIA_RESULT_CONFLICT")
    _publish(vault)
    assert "tere maailm" in sidecar.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {"engine": "faster-whisper:test"},
        {"engine": "faster-whisper:test", "segments": [{"start": 0, "end": 1}]},
        {"engine": "faster-whisper:test", "segments": [], "text": "a transcript the worker rendered itself"},
        {"engine": "faster-whisper:test", "segments": [], "asr": {"language": "et", "language_source": "guessed"}},
    ],
    ids=["no segments", "segment without text", "a rendered transcript", "invalid asr facts"],
)
def test_an_extraction_that_is_not_structure_the_service_can_render_gets_400(
    vault, tmp_path, monkeypatch, payload
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    refused = _post(client, lease, "result", kind="extraction", payload=payload)

    assert refused.status_code == 400
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]).result_recorded is False


def test_a_failure_result_fails_the_job_and_writes_the_failure_to_the_sidecar(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)

    posted = _post(client, lease, "result", kind="failure", payload={"error": "RuntimeError: decoder crashed"})
    _publish(vault)

    assert posted.status_code == 200
    assert _row(vault, lease["job_id"])["state"] == "failed"
    assert "RuntimeError: decoder crashed" in sidecar.read_text(encoding="utf-8")


# ---- credentials -----------------------------------------------------------------------------------


def test_a_credential_is_stored_as_a_hash_and_never_as_its_token(vault) -> None:
    _credential(vault)

    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        stored = connection.execute("SELECT * FROM media_worker_credentials").fetchone()

    assert TOKEN not in stored
    assert hashlib.sha256(TOKEN.encode()).hexdigest() in stored


def test_the_cli_writes_the_token_to_a_private_file_and_prints_only_the_credential_id(
    vault, tmp_path, capsys
) -> None:
    out = tmp_path / "worker.token"

    code = cli.main(["media-worker", "credential", "issue", "--name", "laptop", "--out", str(out), "--vault", str(vault)])

    token = out.read_text(encoding="ascii").strip()
    printed = capsys.readouterr().out
    assert code == 0 and oct(out.stat().st_mode & 0o777) == "0o600"
    (listed,) = media_worker_credentials.list_credentials(media_jobs.MediaJobStore(vault))
    assert listed.id in printed and token not in printed
    assert media_worker_credentials.authenticate(media_jobs.MediaJobStore(vault), token) == listed
    assert (listed.max_leases, round((listed.expires_at - listed.created_at) / 86400)) == (1, 90)


def test_the_cli_refuses_a_token_file_that_exists_and_issues_nothing(vault, tmp_path, capsys) -> None:
    out = tmp_path / "worker.token"
    out.write_text("keep me", encoding="utf-8")

    code = cli.main(["media-worker", "credential", "issue", "--name", "laptop", "--out", str(out), "--vault", str(vault)])

    assert code == 2 and out.read_text(encoding="utf-8") == "keep me"
    assert media_worker_credentials.list_credentials(media_jobs.MediaJobStore(vault)) == []


def test_the_cli_lists_the_last_time_each_worker_was_seen_and_revokes_by_name(
    vault, tmp_path, monkeypatch, capsys
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault, name="laptop")
    assert _claim(client).status_code == 204

    assert cli.main(["media-worker", "credential", "list", "--vault", str(vault)]) == 0
    listed = capsys.readouterr().out
    assert cli.main(["media-worker", "credential", "revoke", "--name", "laptop", "--vault", str(vault)]) == 0
    assert cli.main(["media-worker", "credential", "list", "--vault", str(vault)]) == 0
    revoked = capsys.readouterr().out

    assert "laptop  active" in listed and "last-seen never" not in listed
    assert "laptop  revoked" in revoked.splitlines()[-1]


@pytest.mark.parametrize(
    "block",
    [
        [],
        {"media_types": ["pdf"]},
        {"media_types": "audio"},
        {"lease_seconds": 0},
        {"lease_seconds": 60, "heartbeat_seconds": 60},
        {"local_fallback": "yes"},
        {"presence_sec": 30},
        {"outcome_seconds": 0},
    ],
)
def test_an_invalid_remote_workers_block_is_reported_not_applied(block) -> None:
    assert isinstance(media_remote_routing.parse(block), str)


def test_a_remote_workers_block_gets_the_documented_defaults() -> None:
    assert media_remote_routing.parse({"media_types": ["audio"]}) == media_remote_routing.RemoteWorkers(
        media_types=frozenset({"audio"}), local_fallback=True, lease_seconds=120.0, heartbeat_seconds=30.0,
        presence_seconds=90.0,
    )


# ---- result outcomes -------------------------------------------------------------------------------


def _published(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload=EXTRACTION):
    """A leased job whose result is posted and published, so that its lease and its job are gone."""
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    lease = _lease(client)
    assert _post(client, lease, "result", kind="extraction", payload=payload).status_code == 200
    _publish(vault)
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]) is None
    return client, lease, sidecar


def test_an_identical_result_retried_after_publication_succeeds(vault, tmp_path, monkeypatch, clock) -> None:
    client, lease, sidecar = _published(vault, tmp_path, monkeypatch)
    published = sidecar.read_text(encoding="utf-8")
    clock[0] += 3600
    assert _claim(client).status_code == 204  # a claim sweeps, and an hour is inside the retention

    retried = _post(client, lease, "result", kind="extraction", payload=EXTRACTION)

    assert retried.status_code == 200 and retried.json() == {"recorded": True}
    _publish(vault)
    assert sidecar.read_text(encoding="utf-8") == published  # the retry published nothing more


def test_a_different_result_for_a_published_revision_gets_409(vault, tmp_path, monkeypatch) -> None:
    client, lease, sidecar = _published(vault, tmp_path, monkeypatch)
    other = {**EXTRACTION, "segments": [{"start": 0.0, "end": 2.0, "text": "a different transcript"}]}

    refused = _post(client, lease, "result", kind="extraction", payload=other)

    assert (refused.status_code, refused.json()["code"]) == (409, "MEDIA_RESULT_CONFLICT")
    assert "a different transcript" not in sidecar.read_text(encoding="utf-8")


def test_another_workers_retry_of_a_published_revision_is_a_lost_lease(vault, tmp_path, monkeypatch) -> None:
    client, lease, _sidecar = _published(vault, tmp_path, monkeypatch)
    _credential(vault, OTHER_TOKEN, name="worker-two")

    refused = _post(client, lease, "result", OTHER_TOKEN, kind="extraction", payload=EXTRACTION)

    assert (refused.status_code, refused.json()["code"]) == (409, "MEDIA_LEASE_LOST")


def test_an_outcome_older_than_the_retention_is_forgotten_by_the_next_claim(vault, tmp_path, monkeypatch, clock) -> None:
    client, lease, _sidecar = _published(vault, tmp_path, monkeypatch)
    clock[0] += 24 * 3600 + 1

    assert _claim(client).status_code == 204
    retried = _post(client, lease, "result", kind="extraction", payload=EXTRACTION)

    assert (retried.status_code, retried.json()["code"]) == (409, "MEDIA_LEASE_LOST")
    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        assert connection.execute("SELECT count(*) FROM media_lease_outcomes").fetchone() == (0,)


def test_the_retention_of_outcomes_is_the_configured_one(vault, tmp_path, monkeypatch, clock) -> None:
    _route(tmp_path, monkeypatch, vault, {"remote_workers": {"media_types": ["audio"], "outcome_seconds": 600}})
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    client = TestClient(server.build_server(require_auth=False).http_app())
    _credential(vault)
    _audio(vault)
    lease = _lease(client)
    assert _post(client, lease, "result", kind="extraction", payload=EXTRACTION).status_code == 200
    _publish(vault)
    clock[0] += 601

    assert _claim(client).status_code == 204

    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        assert connection.execute("SELECT count(*) FROM media_lease_outcomes").fetchone() == (0,)


def test_an_outcome_holds_a_hash_and_never_the_post(vault, tmp_path, monkeypatch) -> None:
    _published(vault, tmp_path, monkeypatch)

    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        (row,) = connection.execute("SELECT * FROM media_lease_outcomes").fetchall()

    assert media_jobs.submission_hash("extraction", EXTRACTION) in row
    assert "tere maailm" not in " ".join(str(value) for value in row)


# ---- remote diarisation ----------------------------------------------------------------------------


def test_a_diarized_cluster_that_matches_an_enrolled_voice_is_published_under_its_name(
    vault, tmp_path, monkeypatch
) -> None:
    _enrol(vault, "Alex", axis=0)
    _client, _lease_, sidecar = _published(vault, tmp_path, monkeypatch, DIARIZED)

    content = sidecar.read_text(encoding="utf-8")
    facts = parse_frontmatter(content)[0]
    assert "[0:01] [Alex]: tere maailm" in content
    assert "[1:05] [Speaker A]: head aega" in content  # the cluster that nobody enrolled stays anonymous
    assert facts["extracted_by"] == "faster-whisper:test+timed+diarized"
    assert facts["speaker_verification"] == "profile-matched"
    assert facts["speakers"] == ["Alex", "Speaker A"]


def test_a_diarized_result_with_no_enrolled_voice_is_published_with_anonymous_speakers(
    vault, tmp_path, monkeypatch
) -> None:
    _client, _lease_, sidecar = _published(vault, tmp_path, monkeypatch, DIARIZED)

    content = sidecar.read_text(encoding="utf-8")
    facts = parse_frontmatter(content)[0]
    assert "[0:01] [Speaker A]: tere maailm" in content and "[1:05] [Speaker B]: head aega" in content
    assert facts["speaker_verification"] == "anonymous"
    assert facts["extracted_by"].endswith("+timed+diarized")


def test_a_voice_profile_store_that_cannot_be_read_leaves_the_speakers_anonymous(
    vault, tmp_path, monkeypatch
) -> None:
    _enrol(vault, "Alex", axis=0)
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    _client, _lease_, sidecar = _published(vault, tmp_path, monkeypatch, DIARIZED)

    content = sidecar.read_text(encoding="utf-8")
    assert "[0:01] [Speaker A]: tere maailm" in content
    assert parse_frontmatter(content)[0]["speaker_verification"] == "anonymous"


def test_a_remote_diarized_transcript_is_the_transcript_the_local_path_renders(
    vault, tmp_path, monkeypatch
) -> None:
    """The same segments, turns and voiceprints give the same text, speakers and verification either way."""

    class Segment:
        def __init__(self, item: dict) -> None:
            self.start, self.end, self.text = item["start"], item["end"], item["text"]

    class Whisper:
        def transcribe(self, audio, **options):
            return iter([Segment(item) for item in SEGMENTS]), None

    _enrol(vault, "Alex", axis=0)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    monkeypatch.setattr(extract, "_decode_audio_float32", lambda _path: [0.0])
    monkeypatch.setattr(extract, "_get_whisper", lambda: Whisper())
    monkeypatch.setattr(extract, "_run_diarization", lambda _path: [(0.0, 10.0, "c0"), (60.0, 70.0, "c1")])
    monkeypatch.setattr(
        voice_embed, "embed_spans", lambda _path, spans: np.asarray(VOICEPRINTS["c0" if spans[0][0] < 30 else "c1"])
    )
    local = extract._transcribe(Path("talk.mp3"), "audio", vault, timestamps=True)
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    assert _post(client, lease, "result", kind="extraction", payload=DIARIZED).status_code == 200

    [result] = media_jobs.MediaJobStore(vault).pending_results()
    assert result.payload["text"] == local.text
    assert result.payload["speaker_verification"] == local.speaker_verification == "profile-matched"
    assert result.payload["speakers"] == [{"speaker": turn["speaker"]} for turn in local.speakers]
    assert result.payload["engine"].endswith("+timed+diarized") and local.engine.endswith("+timed+diarized")


def test_no_voiceprint_reaches_the_ledger_the_sidecar_or_a_log(vault, tmp_path, monkeypatch, caplog) -> None:
    _enrol(vault, "Alex", axis=0)
    caplog.set_level(logging.DEBUG)
    _client, _lease_, _sidecar = _published(vault, tmp_path, monkeypatch, DIARIZED)

    leaked = [str(value) for value in (0.8123456789, 0.6123456789)]
    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        ledger = "\n".join(connection.iterdump())
    stored = ledger + "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in vault.rglob("*") if path.is_file()
    )
    assert not [value for value in leaked if value in stored or value in caplog.text]


@pytest.mark.parametrize(
    "diarization",
    [
        {"turns": TURNS, "clusters": {"c0": VOICEPRINTS["c0"][:191], "c1": VOICEPRINTS["c1"]}},
        {"turns": TURNS, "clusters": {"c0": [float("nan")] * 192, "c1": VOICEPRINTS["c1"]}},
        {"turns": [*TURNS, {"start": 0.0, "end": 1.0, "cluster": "c9"}], "clusters": VOICEPRINTS},
        {"turns": TURNS, "clusters": {f"c{n}": VOICEPRINTS["c0"] for n in range(65)}},
    ],
    ids=["wrong dimension", "nan", "turn of an unknown cluster", "more than 64 clusters"],
)
def test_a_malformed_diarization_block_gets_400_and_records_nothing(vault, tmp_path, monkeypatch, diarization) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)
    body = {"claim_revision": lease["claim_revision"], "kind": "extraction", "payload": {**EXTRACTION, "diarization": diarization}}

    refused = client.post(
        f"{BASE}/leases/{lease['job_id']}/result", content=json.dumps(body),
        headers={**_auth(), "content-type": "application/json"},
    )

    assert refused.status_code == 400
    assert "0.8123456789" not in refused.text
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]).result_recorded is False


# ---- local routing, presence and the supervisor ---------------------------------------------------


def _image(vault: Path, name: str = "receipt.png") -> None:
    """A preserved photo with its pending job: a kind that is never routed to remote workers."""
    stub = preserve.preserve_bytes(vault, scope="Rental", category="photos", filename=name, data=b"\x89PNG fake")
    media_processing.reconcile_media(vault, vault / stub.path, explicit=False)


def _local_child(vault: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the local media child until it goes idle, with a stub engine; the files it took, in order."""
    taken: list[str] = []
    original = media_worker.MediaWorker._process

    def process(worker, job):
        taken.append(job.binary_path.name)
        return original(worker, job)

    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setattr(media_worker.MediaWorker, "_process", process)
    monkeypatch.setattr(
        extract, "extract_text",
        lambda path, **_kw: extract.ExtractResult(text="a transcript", media_type=extract.media_type_for(path), engine="test"),
    )
    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0
    return taken


def _without_digest(sidecar: Path) -> None:
    """Take the recorded digest of the original out of its sidecar."""
    lines = sidecar.read_text(encoding="utf-8").split("\n")
    sidecar.write_text("\n".join(line for line in lines if not line.startswith("binary_sha256:")), encoding="utf-8")


def _supervise(vault: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float], seconds: float = 60.0) -> list[float]:
    """Run the media supervisor on a fake clock with no real child, and return when it launched one."""
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    now = [1000.0]
    launches: list[float] = []

    class _ExitedChild:
        pid = 2_147_483_645
        returncode = 0

        def poll(self):
            return 0

    def launch():
        launches.append(now[0])
        return _ExitedChild()

    def poll(_timeout):
        now[0] += 5.0
        clock[0] += 5.0
        if now[0] >= 1000.0 + seconds:
            worker._stop_event.set()
        return False

    monkeypatch.setattr(media_worker, "_clock", lambda: now[0])
    monkeypatch.setattr(media_worker, "_job_store_signature", lambda _store: None)  # ask the store at every pass
    monkeypatch.setattr(media_worker, "_probe_writer_authority", lambda: None)
    monkeypatch.setattr(worker, "_launch_child", launch)
    monkeypatch.setattr(worker._wake, "wait", poll)
    worker._supervise()
    return launches


def test_a_present_worker_makes_the_local_child_leave_the_routed_kind_to_it(vault, tmp_path, monkeypatch) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    assert _claim(client).status_code == 204  # the worker is heard from, with nothing to take yet
    _audio(vault)
    _image(vault)

    assert _local_child(vault, monkeypatch) == ["receipt.png"]

    assert _row(vault, 1)["state"] == "pending"


def test_an_absent_worker_with_local_fallback_on_lets_the_local_child_take_the_job(
    vault, tmp_path, monkeypatch, clock
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    assert _claim(client).status_code == 204
    clock[0] += 91  # past the 90 seconds that a worker stays present after its last contact
    _audio(vault)

    assert _local_child(vault, monkeypatch) == ["talk.mp3"]


def test_local_fallback_off_with_no_worker_leaves_the_routed_job_pending_while_other_kinds_proceed(
    vault, tmp_path, monkeypatch
) -> None:
    _serve(vault, tmp_path, monkeypatch, local_fallback=False)
    _audio(vault)
    _image(vault)

    assert _local_child(vault, monkeypatch) == ["receipt.png"]

    assert _row(vault, 1)["state"] == "pending"


@pytest.mark.parametrize("cause", ["withheld", "no_digest"])
def test_an_original_that_no_remote_claim_can_take_is_processed_locally_even_with_local_fallback_off(
    vault, tmp_path, monkeypatch, cause
) -> None:
    client = _serve(vault, tmp_path, monkeypatch, local_fallback=False)
    _credential(vault)
    _binary, sidecar = _audio(vault)
    if cause == "withheld":
        rules = vault / "Knowledge Base/_Governance/rules"
        rules.mkdir(parents=True)
        # A governance rule that cannot be read withholds every original from every audience.
        (rules / "unreadable.yaml").write_text("governance_version: 1\nid: not-an-id\n", encoding="utf-8")
    else:
        _without_digest(sidecar)
    assert _claim(client).status_code == 204  # a present worker, whose claim finds nothing it may take

    assert _local_child(vault, monkeypatch) == ["talk.mp3"]


def test_the_supervisor_starts_a_local_child_for_an_original_no_worker_can_take_though_none_connects(
    vault, tmp_path, monkeypatch, clock
) -> None:
    _serve(vault, tmp_path, monkeypatch, local_fallback=False)
    _binary, sidecar = _audio(vault)
    _without_digest(sidecar)

    assert _supervise(vault, monkeypatch, clock, seconds=30.0) != []


def test_the_supervisor_returns_an_expired_leases_job_with_no_local_child_and_no_claim(
    vault, tmp_path, monkeypatch, clock
) -> None:
    client = _serve(vault, tmp_path, monkeypatch, local_fallback=False, lease_seconds=20)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)  # the worker then goes silent

    launches = _supervise(vault, monkeypatch, clock, seconds=60.0)

    row = _row(vault, lease["job_id"])
    assert (row["state"], row["pinned"]) == ("pending", 0)
    assert media_jobs.MediaJobStore(vault).get_lease(lease["job_id"]) is None
    assert launches == []  # the job is routed and no worker is there, so a child would find nothing


def test_the_supervisor_prunes_old_result_outcomes_with_no_claim(vault, tmp_path, monkeypatch, clock) -> None:
    _published(vault, tmp_path, monkeypatch)
    clock[0] += 24 * 3600 + 1

    _supervise(vault, monkeypatch, clock, seconds=20.0)

    with sqlite3.connect(media_jobs.job_store_path(vault)) as connection:
        assert connection.execute("SELECT count(*) FROM media_lease_outcomes").fetchone() == (0,)


def test_status_shows_each_lease_each_workers_presence_and_the_reason_a_job_came_back(
    vault, tmp_path, monkeypatch, clock
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _credential(vault, OTHER_TOKEN, name="worker-two")
    _credential(vault, "mw1_token-of-the-third-worker", name="worker-three")  # never connects
    _audio(vault, "first.mp3", b"first-recording")
    first = _lease(client)
    assert _post(client, first, "release", reason="device_busy").status_code == 200
    _audio(vault, "second.mp3", b"second-recording")
    clock[0] += 10
    held = _lease(client, OTHER_TOKEN)
    clock[0] += 20

    status = media_jobs.tool_status(vault)

    (lease,) = status["remote_leases"]
    assert (lease["worker"], lease["job_id"], lease["age_seconds"]) == ("worker-two", held["job_id"], 20)
    assert lease["path"].endswith("second.mp3") and lease["expires_at"]
    workers = {worker["name"]: worker for worker in status["remote_workers"]}
    assert (workers["worker-one"]["present_for"], workers["worker-two"]["present_for"]) == (["audio"], ["audio"])
    assert (workers["worker-three"]["present_for"], workers["worker-three"]["last_seen"]) == ([], None)
    assert workers["worker-one"]["revoked"] is False and workers["worker-one"]["expires_at"]
    (returned,) = [job for job in status["jobs"] if job["path"].endswith("first.mp3")]
    assert "device_busy" in returned["return_reason"]


def test_a_hydration_block_from_a_workers_own_space_names_the_worker_not_the_store_policy(
    vault, tmp_path, monkeypatch
) -> None:
    client = _serve(vault, tmp_path, monkeypatch)
    _credential(vault)
    _audio(vault)
    lease = _lease(client)

    assert _post(client, lease, "release", reason="insufficient_space").status_code == 200

    (job,) = media_jobs.tool_status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert "worker-one" in job["next_action"]
    assert "max_temporary_bytes" not in job["next_action"]
