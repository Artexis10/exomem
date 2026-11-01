"""The signed grant and the destination check for a Job that moves a cell's data (contain-cloud-controller-compromise D4).

Substrate signs a grant; `cellctl` presigns the URLs. The Job checks both before any byte moves.
Report mode (today's constant) logs each mismatch and lets the Job run; enforce mode, passed as an
argument here, refuses. The grant is signed with a throwaway key and the URLs are strings: no store is reached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exomem import cloud_containment, cloud_grant
from exomem.cloud_grant import Operation, OperationRefused, Reason

CELL = "cellabcdefgh2345"
OTHER_CELL = "cellzyxwvuts5432"
TENANT = "6f1c1b7e-2a4b-4c55-9d8e-0a1b2c3d4e5f"
OPERATION_ID = "1d4f6a2e-5b3c-4e7d-8f9a-0b1c2d3e4f5a"
ENDPOINT = "https://objects.example"
BUCKET = "exomem-cells"
EXPORT_KEY = f"cells/{CELL}/exports/{OPERATION_ID}.zip"
IMPORT_KEY = f"cells/{CELL}/imports/{OPERATION_ID}.age"
DIGEST = hashlib.sha256(b"ciphertext").hexdigest()


def _pem(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


@pytest.fixture
def signer() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def env(signer) -> dict[str, str]:
    """The three trust anchors a Job receives."""
    return {
        cloud_grant.GRANT_PUBLIC_KEY_ENV: _pem(signer),
        cloud_grant.OBJECT_STORE_ENDPOINT_ENV: ENDPOINT,
        cloud_grant.OBJECT_STORE_BUCKET_ENV: BUCKET,
    }


def _grant(signer, operation: Operation = Operation.EXPORT, *, drop: tuple[str, ...] = (), key=None, **claims) -> str:
    now = int(time.time())
    payload = {
        "aud": cloud_grant.GRANT_AUDIENCE, "iat": now, "exp": now + 900, "jti": str(uuid.uuid4()),
        "tenant_id": TENANT, "cell_id": CELL, "operation": operation, "operation_id": OPERATION_ID,
        "object_key": IMPORT_KEY if operation is Operation.IMPORT else EXPORT_KEY,
    }
    if operation is Operation.IMPORT:
        payload |= {"ciphertext_bytes": 10, "ciphertext_sha256": DIGEST}
    if operation is Operation.RESTORE:
        payload |= {"snapshot_id": DIGEST}
    payload = {name: value for name, value in {**payload, **claims}.items() if name not in drop}
    return jwt.encode(payload, key or signer, algorithm="EdDSA", headers={"typ": "JWT"})


def _url(key: str = EXPORT_KEY, *, base: str = ENDPOINT, bucket: str = BUCKET) -> str:
    return f"{base}/{bucket}/{key}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=0123abcd"


def _reasons(env, *, grant, urls, operation=Operation.EXPORT, operation_id=OPERATION_ID, bound=None):
    return cloud_grant.reasons_for(operation=operation, operation_id=operation_id, cell_id=CELL, urls=urls,
                                   grant=grant, bound=bound, env=env)


def _events(caplog):
    return [record for record in caplog.records if getattr(record, "event", None) == cloud_containment.OPERATION_EVENT]


# The grant


def test_an_export_grant_and_the_exact_object_url_pass(signer, env):
    assert _reasons(env, grant=_grant(signer), urls=[_url()]) == []


def test_an_import_grant_passes_only_for_the_ciphertext_the_request_names(signer, env):
    grant = _grant(signer, Operation.IMPORT)
    same = {"ciphertext_bytes": 10, "ciphertext_sha256": DIGEST}
    other = {"ciphertext_bytes": 10, "ciphertext_sha256": hashlib.sha256(b"other").hexdigest()}
    assert _reasons(env, grant=grant, urls=[_url(IMPORT_KEY)], operation=Operation.IMPORT, bound=same) == []
    assert _reasons(env, grant=grant, urls=[_url(IMPORT_KEY)], operation=Operation.IMPORT, bound=other) == [
        Reason.GRANT_REQUEST]


def test_a_restore_grant_verifies_so_the_restore_job_can_reuse_the_check(signer, env):
    grant = _grant(signer, Operation.RESTORE, object_key=f"cells/{CELL}/backups/repo")
    url = _url(f"cells/{CELL}/backups/repo")
    assert _reasons(env, grant=grant, urls=[url], operation=Operation.RESTORE) == []
    assert _reasons(env, grant=grant, urls=[url], operation=Operation.EXPORT) == [Reason.GRANT_INVALID]


def test_no_grant_is_reported_as_missing(env):
    assert _reasons(env, grant=None, urls=[_url()]) == [Reason.GRANT_MISSING]


def test_a_grant_without_a_configured_key_is_reported_as_unconfigured(signer, env):
    del env[cloud_grant.GRANT_PUBLIC_KEY_ENV]
    assert _reasons(env, grant=_grant(signer), urls=[_url()]) == [Reason.GRANT_UNCONFIGURED]


_NOW = int(time.time())


@pytest.mark.parametrize("claims", [
    pytest.param({"aud": "exomem-cloud-origin"}, id="another-audience"),
    pytest.param({"cell_id": OTHER_CELL}, id="another-cell"),
    pytest.param({"extra": "claim"}, id="an-extra-claim"),
    pytest.param({"iat": _NOW - 1000, "exp": _NOW - 100}, id="expired"),
    pytest.param({"exp": _NOW + cloud_grant.GRANT_MAX_LIFETIME + 600}, id="lifetime-past-the-maximum"),
    pytest.param({"tenant_id": "not-a-uuid"}, id="tenant-not-a-uuid"),
    pytest.param({"object_key": f"cells/{OTHER_CELL}/exports/{OPERATION_ID}.zip"}, id="another-cells-object-key"),
    pytest.param({"object_key": f"cells/{CELL}/../{OTHER_CELL}/exports/x.zip"}, id="a-traversing-object-key"),
])
def test_a_grant_that_is_not_exactly_the_agreed_claims_is_invalid(signer, env, claims):
    assert _reasons(env, grant=_grant(signer, **claims), urls=[_url()]) == [Reason.GRANT_INVALID]


def test_a_grant_missing_a_claim_is_invalid(signer, env):
    assert _reasons(env, grant=_grant(signer, drop=("jti",)), urls=[_url()]) == [Reason.GRANT_INVALID]


def test_a_grant_signed_by_another_key_is_invalid(env):
    forged = _grant(None, key=Ed25519PrivateKey.generate())
    assert _reasons(env, grant=forged, urls=[_url()]) == [Reason.GRANT_INVALID]


def test_text_that_is_not_a_grant_is_invalid(env):
    assert _reasons(env, grant="not.a.jwt", urls=[_url()]) == [Reason.GRANT_INVALID]


def test_an_import_grant_cannot_drop_the_ciphertext_it_binds(signer, env):
    grant = _grant(signer, Operation.IMPORT, drop=("ciphertext_sha256",))
    assert _reasons(env, grant=grant, urls=[_url(IMPORT_KEY)], operation=Operation.IMPORT) == [Reason.GRANT_INVALID]


def test_a_grant_for_another_operation_is_reported_as_for_another_operation(signer, env):
    assert _reasons(env, grant=_grant(signer, operation_id=str(uuid.uuid4())), urls=[_url()]) == [
        Reason.GRANT_OPERATION]
    assert _reasons(env, grant=_grant(signer, operation="import"), urls=[_url()]) == [Reason.GRANT_OPERATION]


# The destination


@pytest.mark.parametrize(("url", "reason"), [
    pytest.param(_url(base="https://attacker.example"), Reason.URL_HOST, id="another-host"),
    pytest.param(f"https://{BUCKET}.objects.example/{EXPORT_KEY}?sig=1", Reason.URL_HOST, id="virtual-host-style"),
    pytest.param(_url(base="https://objects.example:8443"), Reason.URL_HOST, id="another-port"),
    pytest.param(_url(base="http://objects.example"), Reason.URL_SCHEME, id="plain-http"),
    pytest.param(f"https://user:secret@objects.example/{BUCKET}/{EXPORT_KEY}?sig=1", Reason.URL_USERINFO,
                 id="userinfo"),
    pytest.param(f"https://objects.example@attacker.example/{BUCKET}/{EXPORT_KEY}?sig=1", Reason.URL_USERINFO,
                 id="a-host-hidden-behind-userinfo"),
    pytest.param(_url(bucket="another-bucket"), Reason.URL_PATH, id="another-bucket"),
    pytest.param(f"{ENDPOINT}/{BUCKET}?sig=1", Reason.URL_PATH, id="no-object-key"),
    pytest.param(_url(f"cells/{OTHER_CELL}/exports/{OPERATION_ID}.zip"), Reason.URL_FOREIGN_KEY,
                 id="another-cells-prefix"),
    pytest.param(_url(f"exports/{OPERATION_ID}.zip"), Reason.URL_FOREIGN_KEY, id="outside-the-cells-prefix"),
    pytest.param(_url(f"cells/{CELL}/../{OTHER_CELL}/exports/x.zip"), Reason.URL_PATH, id="dot-dot"),
    pytest.param(_url(f"cells/{CELL}/%2e%2e/{OTHER_CELL}/exports/x.zip"), Reason.URL_PATH, id="encoded-dot-dot"),
    pytest.param(_url(f"cells%2F{OTHER_CELL}%2Fexports%2Fx.zip"), Reason.URL_PATH, id="encoded-slash"),
    pytest.param(_url(f"cells/{CELL}//exports/x.zip"), Reason.URL_PATH, id="double-slash"),
    pytest.param(_url() + "#fragment", Reason.URL_MALFORMED, id="fragment"),
    pytest.param(_url().replace("/exports", "\\exports"), Reason.URL_MALFORMED, id="backslash"),
    pytest.param(_url().replace("/exports", "/exp\norts"), Reason.URL_MALFORMED, id="newline"),
    pytest.param(f"{ENDPOINT}:notaport/{BUCKET}/{EXPORT_KEY}?sig=1", Reason.URL_MALFORMED, id="unparseable-port"),
    pytest.param(_url(f"cells/{CELL}/exports/{uuid.uuid4()}.zip"), Reason.URL_GRANT_KEY, id="another-key-than-the-grant"),
    pytest.param(_url() + "&x-amz-acl=public-read", Reason.URL_QUERY, id="an-acl-parameter"),
    pytest.param(_url() + "&X-Amz-Copy-Source=%2Fother-bucket%2Fkey", Reason.URL_QUERY, id="a-copy-source-parameter"),
    pytest.param(_url() + "&x%2Damz%2Dacl=public-read", Reason.URL_QUERY, id="an-encoded-parameter-name"),
])
def test_a_url_that_leaves_the_pinned_object_is_refused(signer, env, url, reason):
    assert _reasons(env, grant=_grant(signer), urls=[url]) == [reason]


def test_the_default_port_written_out_names_the_same_destination(signer, env):
    assert _reasons(env, grant=_grant(signer), urls=[_url(base="https://objects.example:443")]) == []


def test_an_http_anchor_admits_an_http_url_and_refuses_an_https_one(signer, env):
    env[cloud_grant.OBJECT_STORE_ENDPOINT_ENV] = "http://127.0.0.1:9000"
    grant = _grant(signer)
    assert _reasons(env, grant=grant, urls=[_url(base="http://127.0.0.1:9000")]) == []
    assert _reasons(env, grant=grant, urls=[_url(base="https://127.0.0.1:9000")]) == [Reason.URL_SCHEME]


def test_the_query_names_cellctl_presigns_are_admitted_in_either_case(signer, env):
    """Defect: a name cellctl really presigns is refused, and enforce mode would stop every export."""
    query = ("partNumber=1&uploadId=abc&versionId=v1&response-content-disposition=attachment"
             "&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=k%2F20260101&X-Amz-Date=20260101T000000Z"
             "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=0123abcd")
    url = f"{ENDPOINT}/{BUCKET}/{EXPORT_KEY}?{query}"
    assert _reasons(env, grant=_grant(signer), urls=[url]) == []
    assert _reasons(env, grant=_grant(signer), urls=[url.replace("uploadId", "uploadid")]) == []


def test_every_part_url_of_an_export_is_checked(signer, env):
    urls = [_url() + "&partNumber=1", _url(base="https://attacker.example") + "&partNumber=2"]
    assert _reasons(env, grant=_grant(signer), urls=urls) == [Reason.URL_HOST]


def test_an_operation_with_no_url_is_reported_instead_of_passing(signer, env):
    assert _reasons(env, grant=_grant(signer), urls=[]) == [Reason.URL_MISSING]


@pytest.mark.parametrize("anchors", [
    pytest.param({cloud_grant.OBJECT_STORE_ENDPOINT_ENV: ""}, id="no-endpoint"),
    pytest.param({cloud_grant.OBJECT_STORE_BUCKET_ENV: ""}, id="no-bucket"),
    pytest.param({cloud_grant.OBJECT_STORE_ENDPOINT_ENV: "https://objects.example/prefix"}, id="endpoint-with-a-path"),
    pytest.param({cloud_grant.OBJECT_STORE_ENDPOINT_ENV: "objects.example"}, id="endpoint-without-a-scheme"),
])
def test_a_destination_that_is_not_pinned_is_reported_as_unconfigured(signer, env, anchors):
    env.update(anchors)
    assert _reasons(env, grant=_grant(signer), urls=[_url()]) == [Reason.DESTINATION_UNCONFIGURED]


# Report and enforce


def test_report_mode_logs_what_it_would_refuse_and_lets_the_job_run(env, caplog):
    """This release serves exactly as before: the mode is a constant of the image, not an argument of the Job."""
    caplog.set_level(logging.INFO)
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                    urls=[_url(base="https://attacker.example")], grant=None, env=env)
    assert [(record.fields["reason"], record.fields["verdict"]) for record in _events(caplog)] == [
        ("grant_missing", "would_refuse"), ("url_host", "would_refuse")]


def test_report_mode_logs_a_passing_operation_so_a_clean_release_has_evidence(signer, env, caplog):
    caplog.set_level(logging.INFO)
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                    urls=[_url()], grant=_grant(signer), env=env)
    (record,) = _events(caplog)
    assert record.fields == {"operation": "export", "operation_id": OPERATION_ID, "cell_id": CELL,
                             "reason": "none", "verdict": "pass"}


def test_enforce_mode_refuses_an_operation_without_a_grant(env, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(OperationRefused) as refused:
        cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                        urls=[_url()], grant=None, env=env, enforce=True)
    assert refused.value.reason is Reason.GRANT_MISSING
    assert [record.fields["verdict"] for record in _events(caplog)] == ["refused"]


def test_enforce_mode_refuses_a_valid_grant_with_a_url_beyond_it(signer, env):
    with pytest.raises(OperationRefused) as refused:
        cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                        urls=[_url(base="https://attacker.example")], grant=_grant(signer), env=env,
                                        enforce=True)
    assert refused.value.reason is Reason.URL_HOST


def test_enforce_mode_serves_a_valid_grant_and_its_url(signer, env):
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                    urls=[_url()], grant=_grant(signer), env=env, enforce=True)


def test_enforce_mode_with_no_trust_anchors_refuses_everything(signer):
    """Fail closed: a Job whose anchors are missing cannot tell a good grant from a forged one."""
    with pytest.raises(OperationRefused) as refused:
        cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                        urls=[_url()], grant=_grant(signer), env={}, enforce=True)
    assert refused.value.reason is Reason.GRANT_UNCONFIGURED


def test_a_check_that_fails_unexpectedly_never_stops_report_mode_and_stops_enforce_mode(env, caplog, monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("unexpected")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(cloud_grant, "reasons_for", broken)
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                    urls=[_url()], grant=None, env=env)
    assert [record.fields["reason"] for record in _events(caplog)] == ["check_failed"]
    with pytest.raises(RuntimeError):
        cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                        urls=[_url()], grant=None, env=env, enforce=True)


def test_an_event_names_only_a_reason_the_operation_and_the_cell(signer, env, caplog):
    """Defect: a log line carries a presigned URL, a grant or a claim, which a log reader can replay."""
    caplog.set_level(logging.INFO)
    grant = _grant(signer)
    url = _url(base="https://attacker.example")
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=CELL,
                                    urls=[url], grant=grant, env=env)
    (record,) = _events(caplog)
    assert set(record.fields) == {"operation", "operation_id", "cell_id", "reason", "verdict"}
    text = json.dumps(vars(record), default=str)
    assert not any(secret in text for secret in (grant, "attacker.example", "X-Amz-Signature", TENANT))


def test_an_operation_id_that_is_not_a_uuid_is_logged_as_invalid(env, caplog):
    """Defect: the controller's argument reaches the log as it is, so a forged line follows the real one."""
    caplog.set_level(logging.INFO)
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id="x\nevent=forged", cell_id=CELL,
                                    urls=[_url()], grant=None, env=env)
    assert {record.fields["operation_id"] for record in _events(caplog)} == {"invalid"}


def test_a_cell_id_that_is_not_a_cell_id_cannot_forge_a_second_event_line(env, capsys):
    """Defect: the verify branch never checks the cell ID, so a newline in the environment value forged a line."""
    cloud_containment.log_to_stderr()
    forged = f"{CELL}\nts=2026-01-01 level=INFO event=cloud_operation_check cell_id={CELL} reason=none verdict=pass"
    cloud_grant.authorize_operation(operation=Operation.EXPORT, operation_id=OPERATION_ID, cell_id=forged,
                                    urls=[_url()], grant=None, env=env)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2  # the missing grant and the foreign key, which the fake cell ID makes of the URL
    assert all(line.count("event=cloud_operation_check") == 1 and "cell_id=invalid" in line for line in lines)
