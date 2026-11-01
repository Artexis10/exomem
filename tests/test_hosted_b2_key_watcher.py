"""The out-of-cluster B2 key watcher: what it alerts on, what it sends, what it prints.

HTTP is injected at the transport. `FakeTransport` stands in for urllib's opener and
answers B2 native API v3 and receiver requests with producer-shaped responses.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import re
import sys
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 7, 23, tzinfo=UTC)

CAPABILITY = "fixture-capability-token"
ALERT_URL = f"https://alerts.example.test/hooks/{CAPABILITY}"
KEY_ID_ENV, KEY_ENV = "WATCHER_FIXTURE_KEY_ID", "WATCHER_FIXTURE_KEY"
WATCHER_KEY_ID, WATCHER_KEY = "0050000000000fixturewatcher", "K005fixturewatchersecret"
ACCOUNT_ID = "fixtureaccount01"
CUSTODIAN_KEY_ID = "0050000000000fixturecustod"
ROGUE_KEY_ID = "0050000000000fixturerogue1"
BACKUP_BUCKET_ID = "fixturebackupbucketid01"
STAGING_BUCKET_ID = "fixturestagingbucketid2"
CELL_PREFIX = "cells/abcdefghijklmnop/"
CELL_CAPABILITIES = ["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]


def _load_watcher() -> ModuleType:
    path = ROOT / "infra/scripts/b2_key_watcher.py"
    spec = importlib.util.spec_from_file_location("b2_key_watcher_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watcher = _load_watcher()


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeTransport:
    """Answers by endpoint. An outcome is a JSON body, an HTTP status or an exception.

    The outcomes of one endpoint are used in order; the last one repeats.
    """

    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self._outcomes = {name: list(items) for name, items in outcomes.items()}
        self.sent: list[tuple[str, dict[str, str], bytes]] = []

    def open(self, request: Any, /, *, timeout: float) -> _Response:
        assert timeout == 10
        url = request.full_url
        if url.startswith("https://alerts.example.test/"):
            endpoint = "alert"
        elif url.startswith("https://api.backblazeb2.com/b2api/v3/"):
            endpoint = "authorize"
        else:
            endpoint = url.rsplit("/", 1)[-1]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.sent.append((endpoint, headers, request.data or b""))
        queue = self._outcomes[endpoint]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(url, outcome, "fixture", {}, None)  # type: ignore[arg-type]
        return _Response(json.dumps(outcome).encode())

    def alerts(self) -> dict[str, bool]:
        return {
            body["alert"]: body["active"]
            for endpoint, _, raw in self.sent
            if endpoint == "alert"
            for body in [json.loads(raw)]
        }

    def endpoints(self) -> list[str]:
        return [endpoint for endpoint, _, _ in self.sent]


def authorized() -> dict[str, Any]:
    return {
        "accountId": ACCOUNT_ID,
        "authorizationToken": "fixture-session-token",
        "apiInfo": {"storageApi": {"apiUrl": "https://api001.backblazeb2.com"}},
    }


def listed_key(
    key_id: str, *, capabilities: list[str], bucket_ids: list[str] | None, prefix: str | None
) -> dict[str, Any]:
    return {
        "accountId": ACCOUNT_ID,
        "applicationKeyId": key_id,
        "keyName": f"fixture-name-{key_id[-4:]}",
        "capabilities": capabilities,
        "bucketIds": bucket_ids,
        "namePrefix": prefix,
        "expirationTimestamp": None,
        "options": ["s3"],
    }


def custodian_key() -> dict[str, Any]:
    return listed_key(
        CUSTODIAN_KEY_ID,
        capabilities=["listKeys", "writeKeys", "deleteKeys"],
        bucket_ids=None,
        prefix=None,
    )


def cell_key(
    key_id: str = "0050000000000fixturecell01",
    *,
    prefix: str = CELL_PREFIX,
    bucket_ids: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return listed_key(
        key_id,
        capabilities=capabilities or CELL_CAPABILITIES,
        bucket_ids=bucket_ids or [BACKUP_BUCKET_ID],
        prefix=prefix,
    )


def locked_settings() -> dict[str, Any]:
    return {
        "bucketType": "allPrivate",
        "fileLockEnabled": True,
        "defaultRetention": {"mode": "compliance", "period": {"duration": 30, "unit": "days"}},
        "lifecycleRules": [
            {
                "daysFromHidingToDeleting": 1,
                "daysFromStartingToCancelingUnfinishedLargeFiles": 2,
                "daysFromUploadingToHiding": None,
                "fileNamePrefix": "",
            }
        ],
        "defaultServerSideEncryption": {"algorithm": "AES256", "mode": "SSE-B2"},
        "replicationConfiguration": None,
        "corsRules": [],
    }


def unlocked_settings() -> dict[str, Any]:
    settings = locked_settings()
    settings.update(fileLockEnabled=False, defaultRetention=None)
    return settings


def listed_bucket(
    bucket_id: str, settings: dict[str, Any], *, readable: bool = True
) -> dict[str, Any]:
    """B2 v3 wire shape: three settings come wrapped, and withheld without the read capability."""
    retention = settings["defaultRetention"] or {"mode": None, "period": None}

    def wrap(value: Any) -> dict[str, Any]:
        return {"isClientAuthorizedToRead": readable, "value": value if readable else None}

    return {
        "accountId": ACCOUNT_ID,
        "bucketId": bucket_id,
        "bucketName": f"fixture-bucket-{bucket_id[-4:]}",
        "bucketType": settings["bucketType"],
        "bucketInfo": {},
        "corsRules": settings["corsRules"],
        "defaultServerSideEncryption": wrap(settings["defaultServerSideEncryption"]),
        "fileLockConfiguration": wrap(
            {"defaultRetention": retention, "isFileLockEnabled": settings["fileLockEnabled"]}
        ),
        "lifecycleRules": settings["lifecycleRules"],
        "options": ["s3"],
        "replicationConfiguration": wrap(settings["replicationConfiguration"]),
        "revision": 3,
    }


def account(
    *, per_cell: bool, pinned: list[str] | None = None, buckets: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "label": "fixture",
        "key_id_env": KEY_ID_ENV,
        "key_env": KEY_ENV,
        "pinned_key_ids": pinned if pinned is not None else [CUSTODIAN_KEY_ID],
        "per_cell": (
            {"bucket_id": BACKUP_BUCKET_ID, "capabilities": CELL_CAPABILITIES} if per_cell else None
        ),
        "buckets": buckets if buckets is not None else {BACKUP_BUCKET_ID: locked_settings()},
    }


def environment(*accounts: dict[str, Any], alert_url: str = ALERT_URL) -> dict[str, str]:
    return {
        "KEY_WATCHER_PINNED_SET": json.dumps({"schema_version": 1, "accounts": list(accounts)}),
        "KEY_WATCHER_ALERT_URL": alert_url,
        KEY_ID_ENV: WATCHER_KEY_ID,
        KEY_ENV: WATCHER_KEY,
    }


def transport(
    *,
    keys: list[Any] | None = None,
    buckets: list[Any] | None = None,
    authorize: Any = None,
    key_outcomes: list[Any] | None = None,
    alert_outcomes: list[Any] | None = None,
) -> FakeTransport:
    """Defaults to a clean account. An outcome list replaces one endpoint's answers."""
    listed = keys if keys is not None else [custodian_key()]
    return FakeTransport(
        {
            "authorize": [authorize if authorize is not None else authorized()],
            "b2_list_keys": key_outcomes or [{"keys": listed, "nextApplicationKeyId": None}],
            "b2_list_buckets": [
                {
                    "buckets": buckets
                    if buckets is not None
                    else [listed_bucket(BACKUP_BUCKET_ID, locked_settings())]
                }
            ],
            "alert": alert_outcomes or [{"ok": True}],
        }
    )


def run(env: dict[str, str], fake: FakeTransport, now: datetime = NOW) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = watcher.run(env, fake, now, out, err)
    return code, out.getvalue(), err.getvalue()


def expected_transition_id(alert: str, active: bool, stamp: str) -> str:
    """Computed here with hmac, independently of the watcher's own helper."""
    material = f"b2-key-watcher|{alert}|{'true' if active else 'false'}|{stamp}".encode()
    return hmac.new(CAPABILITY.encode(), material, hashlib.sha256).hexdigest()


def transition_ids(fake: FakeTransport) -> dict[str, str]:
    return {
        body["alert"]: body["transition_id"]
        for endpoint, _, raw in fake.sent
        if endpoint == "alert"
        for body in [json.loads(raw)]
    }


def active_alerts(fake: FakeTransport) -> set[str]:
    return {alert for alert, active in fake.alerts().items() if active}


def test_a_clean_run_sends_every_token_inactive_then_the_heartbeat() -> None:
    fake = transport(
        keys=[custodian_key(), cell_key()],
        buckets=[
            listed_bucket(BACKUP_BUCKET_ID, locked_settings()),
            listed_bucket(STAGING_BUCKET_ID, unlocked_settings()),
        ],
    )
    pinned_buckets = {BACKUP_BUCKET_ID: locked_settings(), STAGING_BUCKET_ID: unlocked_settings()}

    code, out, _ = run(environment(account(per_cell=True, buckets=pinned_buckets)), fake)

    assert code == 0, out
    assert active_alerts(fake) == set()
    assert {name.split(":")[-1] for name in fake.alerts()} == set(watcher.ALERT_TOKENS) | {
        "KEY_WATCHER_RUN"
    }
    assert json.loads(fake.sent[-1][2])["alert"] == "KEY_WATCHER_RUN"


def test_a_key_outside_the_pinned_set_alerts_unknown_key() -> None:
    rogue = listed_key(
        ROGUE_KEY_ID, capabilities=["listKeys", "writeKeys"], bucket_ids=None, prefix=None
    )
    fake = transport(keys=[custodian_key(), rogue])

    code, _, _ = run(environment(account(per_cell=False)), fake)

    assert code == 1
    assert active_alerts(fake) == {"fixture:UNKNOWN_KEY"}


def test_a_key_listed_on_a_later_page_is_still_checked() -> None:
    rogue = listed_key(ROGUE_KEY_ID, capabilities=["listKeys"], bucket_ids=None, prefix=None)
    fake = transport(
        key_outcomes=[
            {"keys": [custodian_key()], "nextApplicationKeyId": ROGUE_KEY_ID},
            {"keys": [rogue], "nextApplicationKeyId": None},
        ]
    )

    run(environment(account(per_cell=False)), fake)

    assert active_alerts(fake) == {"fixture:UNKNOWN_KEY"}
    second_page = [json.loads(raw) for name, _, raw in fake.sent if name == "b2_list_keys"][1]
    assert second_page["startApplicationKeyId"] == ROGUE_KEY_ID


@pytest.mark.parametrize(
    "key",
    [
        cell_key(bucket_ids=[STAGING_BUCKET_ID]),
        cell_key(bucket_ids=[BACKUP_BUCKET_ID, STAGING_BUCKET_ID]),
        cell_key(prefix="cells/"),
        cell_key(prefix="cells/ABCDEFGHIJKLMNOP/"),
        cell_key(capabilities=[*CELL_CAPABILITIES, "bypassGovernance"]),
        cell_key(capabilities=["listBuckets", "listFiles"]),
    ],
    ids=[
        "other-bucket",
        "two-buckets",
        "no-cell-id",
        "bad-cell-id",
        "extra-capability",
        "missing-capability",
    ],
)
def test_a_key_that_is_not_the_per_cell_shape_alerts(key: dict[str, Any]) -> None:
    fake = transport(keys=[custodian_key(), key])

    code, _, _ = run(environment(account(per_cell=True)), fake)

    assert code == 1
    assert active_alerts(fake) == {"fixture:PER_CELL_KEY_SHAPE"}


def test_two_keys_for_one_prefix_alert_a_duplicate() -> None:
    fake = transport(
        keys=[
            custodian_key(),
            cell_key("0050000000000fixturecell01"),
            cell_key("0050000000000fixturecell02"),
        ]
    )

    run(environment(account(per_cell=True)), fake)

    assert active_alerts(fake) == {"fixture:DUPLICATE_PREFIX_KEY"}


def test_a_pinned_key_missing_from_the_listing_alerts() -> None:
    fake = transport(keys=[])

    run(environment(account(per_cell=False)), fake)

    assert active_alerts(fake) == {"fixture:PINNED_KEY_MISSING"}


def test_a_pinned_bucket_missing_from_the_listing_alerts() -> None:
    fake = transport(buckets=[listed_bucket(STAGING_BUCKET_ID, unlocked_settings())])

    run(environment(account(per_cell=False)), fake)

    assert active_alerts(fake) == {"fixture:BUCKET_MISSING"}


def test_a_removed_default_retention_alerts_setting_drift() -> None:
    drifted = locked_settings()
    drifted["defaultRetention"] = None
    fake = transport(buckets=[listed_bucket(BACKUP_BUCKET_ID, drifted)])

    run(environment(account(per_cell=False)), fake)

    assert active_alerts(fake) == {"fixture:BUCKET_SETTING_DRIFT"}


def test_a_setting_that_b2_withholds_alerts_because_it_cannot_be_verified() -> None:
    fake = transport(buckets=[listed_bucket(BACKUP_BUCKET_ID, locked_settings(), readable=False)])

    run(environment(account(per_cell=False)), fake)

    assert active_alerts(fake) == {"fixture:BUCKET_SETTING_DRIFT"}


def test_lifecycle_and_cors_ordering_that_carries_no_meaning_does_not_alert() -> None:
    pinned = locked_settings()
    pinned["lifecycleRules"] = [
        {
            "daysFromHidingToDeleting": 1,
            "daysFromStartingToCancelingUnfinishedLargeFiles": 2,
            "daysFromUploadingToHiding": None,
            "fileNamePrefix": "",
        },
        {
            "daysFromHidingToDeleting": 1,
            "daysFromStartingToCancelingUnfinishedLargeFiles": None,
            "daysFromUploadingToHiding": 7,
            "fileNamePrefix": "tmp/",
        },
    ]
    pinned["corsRules"] = [
        {
            "corsRuleName": "browser",
            "allowedOrigins": ["https://a.example.test", "https://b.example.test"],
            "allowedOperations": ["s3_put"],
            "allowedHeaders": ["*"],
            "exposeHeaders": ["ETag"],
            "maxAgeSeconds": 0,
        }
    ]
    observed = json.loads(json.dumps(pinned))
    observed["lifecycleRules"].reverse()
    observed["corsRules"][0]["allowedOrigins"].reverse()
    fake = transport(buckets=[listed_bucket(BACKUP_BUCKET_ID, observed)])

    run(environment(account(per_cell=False, buckets={BACKUP_BUCKET_ID: pinned})), fake)

    assert active_alerts(fake) == set()


def test_a_rejected_watcher_credential_alerts_and_clears_nothing_it_cannot_check() -> None:
    fake = transport(authorize=401)

    code, _, _ = run(environment(account(per_cell=False)), fake)

    assert code == 1
    assert fake.alerts()["fixture:AUTH_FAILURE"] is True
    # A blind check must not send "inactive" for a drift token that it could not evaluate.
    assert "fixture:UNKNOWN_KEY" not in fake.alerts()
    assert "KEY_WATCHER_RUN" in fake.alerts()


def test_a_failed_key_listing_alerts_without_clearing_the_key_tokens() -> None:
    fake = transport(key_outcomes=[503])

    code, _, _ = run(environment(account(per_cell=False)), fake)

    assert code == 1
    assert fake.alerts()["fixture:LISTING_FAILURE"] is True
    assert "fixture:UNKNOWN_KEY" not in fake.alerts()
    assert fake.alerts()["fixture:BUCKET_MISSING"] is False
    assert fake.endpoints().count("b2_list_keys") == 2  # one retry after the 5xx


def test_the_alert_body_and_transition_id_match_the_receiver_contract() -> None:
    rogue = listed_key(ROGUE_KEY_ID, capabilities=["listKeys"], bucket_ids=None, prefix=None)
    fake = transport(keys=[custodian_key(), rogue])

    run(environment(account(per_cell=False)), fake)

    alert_requests = [(headers, body) for name, headers, body in fake.sent if name == "alert"]
    expected = expected_transition_id("fixture:UNKNOWN_KEY", True, "2026-11-02")
    sent = {json.loads(body)["alert"]: (headers, body) for headers, body in alert_requests}
    headers, body = sent["fixture:UNKNOWN_KEY"]
    assert json.loads(body) == {
        "schema_version": 1,
        "transition_id": expected,
        "job": "b2-key-watcher",
        "alert": "fixture:UNKNOWN_KEY",
        "active": True,
    }
    assert list(json.loads(body)) == ["schema_version", "transition_id", "job", "alert", "active"]
    assert headers["x-exomem-alert-transition"] == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert all(len(body) <= 4096 for _, body in alert_requests)

    heartbeat_headers, heartbeat = sent["KEY_WATCHER_RUN"]
    hour_id = expected_transition_id("KEY_WATCHER_RUN", False, "2026-11-02T07")
    assert json.loads(heartbeat)["transition_id"] == hour_id
    assert json.loads(heartbeat)["active"] is False
    assert heartbeat_headers["x-exomem-alert-transition"] == hour_id
    assert (
        fake.endpoints()[-1] == "alert"
        and json.loads(fake.sent[-1][2])["alert"] == "KEY_WATCHER_RUN"
    )


def test_a_repeat_dedupes_per_day_for_checks_and_per_hour_for_the_heartbeat() -> None:
    rogue = listed_key(ROGUE_KEY_ID, capabilities=["listKeys"], bucket_ids=None, prefix=None)
    env = environment(account(per_cell=False))

    def ids_at(now: datetime) -> dict[str, str]:
        fake = transport(keys=[custodian_key(), rogue])
        run(env, fake, now)
        return transition_ids(fake)

    morning = ids_at(datetime(2026, 11, 2, 7, 23, tzinfo=UTC))
    later_that_day = ids_at(datetime(2026, 11, 2, 19, 23, tzinfo=UTC))
    next_day = ids_at(datetime(2026, 11, 3, 7, 23, tzinfo=UTC))

    assert later_that_day["fixture:UNKNOWN_KEY"] == morning["fixture:UNKNOWN_KEY"]
    assert next_day["fixture:UNKNOWN_KEY"] != morning["fixture:UNKNOWN_KEY"]
    assert later_that_day["KEY_WATCHER_RUN"] != morning["KEY_WATCHER_RUN"]
    assert (
        ids_at(datetime(2026, 11, 2, 7, 59, tzinfo=UTC))["KEY_WATCHER_RUN"]
        == morning["KEY_WATCHER_RUN"]
    )


def test_transition_ids_cannot_be_minted_without_the_receiver_capability() -> None:
    fake = transport()
    other_url = "https://alerts.example.test/hooks/another-capability"
    other = transport()

    run(environment(account(per_cell=False)), fake)
    run(environment(account(per_cell=False), alert_url=other_url), other)

    plain_hash = hashlib.sha256(b"b2-key-watcher|KEY_WATCHER_RUN|false|2026-11-02T07").hexdigest()
    assert transition_ids(fake)["KEY_WATCHER_RUN"] != plain_hash
    assert transition_ids(fake)["KEY_WATCHER_RUN"] != transition_ids(other)["KEY_WATCHER_RUN"]


def test_a_receiver_5xx_is_retried_once_and_a_client_error_fails_the_run() -> None:
    retried = transport(alert_outcomes=[503, {"ok": True}])
    code, _, _ = run(environment(account(per_cell=False)), retried)
    assert code == 0

    refused = transport(alert_outcomes=[403])
    code, out, _ = run(environment(account(per_cell=False)), refused)
    assert code == 1
    assert re.search(r"deliveries: 0 of \d+ succeeded", out)
    assert refused.endpoints().count("alert") == len(refused.alerts())  # no retry on a 4xx


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(unexpected=1),
        lambda document: document.update(schema_version=2),
        lambda document: document["accounts"][0].update(unexpected=1),
        lambda document: document["accounts"][0]["buckets"][BACKUP_BUCKET_ID].update(unexpected=1),
        lambda document: document["accounts"][0]["buckets"][BACKUP_BUCKET_ID].pop("corsRules"),
        lambda document: document["accounts"][0].update(label="Not A Label"),
    ],
    ids=[
        "top-level-field",
        "schema-version",
        "account-field",
        "bucket-field",
        "missing-bucket-field",
        "label",
    ],
)
def test_an_invalid_pinned_set_fails_loudly_and_sends_nothing(mutate: Any) -> None:
    env = environment(account(per_cell=False))
    document = json.loads(env["KEY_WATCHER_PINNED_SET"])
    mutate(document)
    env["KEY_WATCHER_PINNED_SET"] = json.dumps(document)
    fake = transport()

    code, out, err = run(env, fake)

    assert code == 2
    assert fake.sent == []
    assert BACKUP_BUCKET_ID not in out + err


@pytest.mark.parametrize(
    "overrides",
    [
        {"KEY_WATCHER_PINNED_SET": ""},
        {"KEY_WATCHER_ALERT_URL": ""},
        {"KEY_WATCHER_ALERT_URL": f"http://alerts.example.test/hooks/{CAPABILITY}"},
        {"KEY_WATCHER_ALERT_URL": "https://alerts.example.test/hooks/"},
    ],
    ids=["empty-pinned-set", "missing-alert-url", "plain-http-alert-url", "no-capability-segment"],
)
def test_missing_configuration_fails_loudly_and_sends_nothing(overrides: dict[str, str]) -> None:
    fake = transport()

    code, _, err = run({**environment(account(per_cell=False)), **overrides}, fake)

    assert code == 2
    assert fake.sent == []
    assert CAPABILITY not in err


def test_output_names_no_identifier_from_the_accounts_or_the_receiver() -> None:
    rogue = listed_key(ROGUE_KEY_ID, capabilities=["listKeys"], bucket_ids=None, prefix=CELL_PREFIX)
    drifted = locked_settings()
    drifted["defaultRetention"] = None
    fake = transport(
        keys=[custodian_key(), rogue], buckets=[listed_bucket(BACKUP_BUCKET_ID, drifted)]
    )

    code, out, err = run(environment(account(per_cell=False)), fake)

    assert code == 1
    assert "UNKNOWN_KEY" in out and "BUCKET_SETTING_DRIFT" in out
    forbidden = [
        WATCHER_KEY_ID,
        WATCHER_KEY,
        CUSTODIAN_KEY_ID,
        ROGUE_KEY_ID,
        BACKUP_BUCKET_ID,
        ACCOUNT_ID,
        CELL_PREFIX,
        "fixture-name-",
        "fixture-bucket-",
        "alerts.example.test",
        CAPABILITY,
        "fixture-session-token",
    ]
    assert [item for item in forbidden if item in out + err] == []


def test_an_unexpected_error_prints_only_its_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTransport({"authorize": [RuntimeError(f"details naming {BACKUP_BUCKET_ID}")]})
    for name, value in environment(account(per_cell=False)).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(watcher.urllib.request, "build_opener", lambda *_handlers: fake)

    code = watcher.main()

    captured = capsys.readouterr()
    assert code == 3
    assert "RuntimeError" in captured.err
    assert BACKUP_BUCKET_ID not in captured.out + captured.err
    assert fake.endpoints() == ["authorize"]  # no heartbeat after a crash
