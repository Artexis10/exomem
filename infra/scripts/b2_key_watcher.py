#!/usr/bin/env python3
"""Out-of-cluster watcher for object-store key and bucket drift.

Implements the key watcher of `contain-cloud-controller-compromise` D9. A scheduled
CI job runs it. It lists every application key and bucket of each pinned account
with a list-only key, and reports to a Substrate receiver that no in-cluster
component can reach.

The pinned set is data, held in the `KEY_WATCHER_PINNED_SET` secret: this public
repository holds no key ID, bucket name or account ID. Output is value-free. It
prints account labels, reason tokens and counts only, because CI logs are public.

Exit status: 0 when no alert is active and every delivery succeeded; 1 when a
drift alert is active or a delivery failed; 2 when the configuration is invalid;
3 on an unexpected error. Exit 2 and 3 send no heartbeat, so the receiver also
reports a run that never got as far as evaluating the accounts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TextIO
from urllib.parse import urlsplit

AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
SCHEMA_VERSION = 1
JOB = "b2-key-watcher"
HEARTBEAT_ALERT = "KEY_WATCHER_RUN"
TIMEOUT_SECONDS = 10
KEY_PAGE_SIZE = 1000
MAX_PAGES = 100

# The reason tokens are this script's own alert protocol with the receiver: a closed
# set the script owns, not data that a customer or the vault defines.
UNKNOWN_KEY = "UNKNOWN_KEY"
PER_CELL_KEY_SHAPE = "PER_CELL_KEY_SHAPE"
DUPLICATE_PREFIX_KEY = "DUPLICATE_PREFIX_KEY"
PINNED_KEY_MISSING = "PINNED_KEY_MISSING"
BUCKET_MISSING = "BUCKET_MISSING"
BUCKET_SETTING_DRIFT = "BUCKET_SETTING_DRIFT"
AUTH_FAILURE = "AUTH_FAILURE"
LISTING_FAILURE = "LISTING_FAILURE"
ALERT_TOKENS = (
    UNKNOWN_KEY,
    PER_CELL_KEY_SHAPE,
    DUPLICATE_PREFIX_KEY,
    PINNED_KEY_MISSING,
    BUCKET_MISSING,
    BUCKET_SETTING_DRIFT,
    AUTH_FAILURE,
    LISTING_FAILURE,
)
_KEY_TOKENS = (UNKNOWN_KEY, PER_CELL_KEY_SHAPE, DUPLICATE_PREFIX_KEY, PINNED_KEY_MISSING)
_BUCKET_TOKENS = (BUCKET_MISSING, BUCKET_SETTING_DRIFT)

# A cell prefix is `cells/<cell id>/`. The cell id is the 16-character base32 body of
# cellctl's namespace rule `exo-cell-[a-z2-7]{16}`. That protocol fixes the shape, so a
# regex over this closed form is sound; it checks a format, not what anyone meant.
_CELL_PREFIX = re.compile(r"cells/[a-z2-7]{16}/")
_ACCOUNT_LABEL = re.compile(r"[a-z0-9-]{1,32}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_BUCKET_SETTING_FIELDS = frozenset(
    {
        "bucketType",
        "fileLockEnabled",
        "defaultRetention",
        "lifecycleRules",
        "defaultServerSideEncryption",
        "replicationConfiguration",
        "corsRules",
    }
)


class ConfigError(Exception):
    """The pinned set or the receiver address is invalid; the message names no value."""


class ApiError(Exception):
    """A B2 call failed or returned a shape the watcher does not accept."""


class Opener(Protocol):
    """The transport: urllib's OpenerDirector in production, a fake in tests."""

    def open(self, request: urllib.request.Request, /, *, timeout: float) -> Any: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class PerCell:
    bucket_id: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class Account:
    label: str
    key_id_env: str
    key_env: str
    pinned_key_ids: frozenset[str]
    per_cell: PerCell | None
    buckets: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Session:
    api_url: str
    token: str
    account_id: str


# --- Pinned set -----------------------------------------------------------------


def _exact_object(value: Any, fields: set[str] | frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected an object")
    if set(value) != set(fields):
        raise ConfigError(f"{path}: fields differ from the schema")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{path}: expected a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ConfigError(f"{path}: duplicate entry")
    return value


def _parse_bucket_settings(value: Any, path: str) -> dict[str, Any]:
    settings = _exact_object(value, _BUCKET_SETTING_FIELDS, path)
    typed = (
        ("bucketType", (str,)),
        ("fileLockEnabled", (bool,)),
        ("defaultRetention", (dict, type(None))),
        ("lifecycleRules", (list,)),
        ("defaultServerSideEncryption", (dict,)),
        ("replicationConfiguration", (dict, type(None))),
        ("corsRules", (list,)),
    )
    for name, types in typed:
        if not isinstance(settings[name], types):
            raise ConfigError(f"{path}.{name}: wrong type")
    return settings


def parse_pinned_set(raw: str) -> list[Account]:
    if not raw.strip():
        raise ConfigError("the pinned set is empty")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("the pinned set is not JSON") from exc
    document = _exact_object(document, {"schema_version", "accounts"}, "pinned set")
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ConfigError("pinned set: unsupported schema_version")
    if not isinstance(document["accounts"], list) or not document["accounts"]:
        raise ConfigError("pinned set: accounts must be a non-empty list")

    accounts: list[Account] = []
    for index, raw_account in enumerate(document["accounts"]):
        path = f"accounts[{index}]"
        fields = {"label", "key_id_env", "key_env", "pinned_key_ids", "per_cell", "buckets"}
        item = _exact_object(raw_account, fields, path)
        label = item["label"]
        if not isinstance(label, str) or not _ACCOUNT_LABEL.fullmatch(label):
            raise ConfigError(f"{path}.label: invalid")
        if any(account.label == label for account in accounts):
            raise ConfigError(f"{path}.label: duplicate")
        for name in ("key_id_env", "key_env"):
            if not isinstance(item[name], str) or not _ENV_NAME.fullmatch(item[name]):
                raise ConfigError(f"{path}.{name}: invalid environment name")

        per_cell = None
        if item["per_cell"] is not None:
            shape = _exact_object(
                item["per_cell"], {"bucket_id", "capabilities"}, f"{path}.per_cell"
            )
            if not isinstance(shape["bucket_id"], str) or not shape["bucket_id"]:
                raise ConfigError(f"{path}.per_cell.bucket_id: invalid")
            capabilities = _string_list(shape["capabilities"], f"{path}.per_cell.capabilities")
            per_cell = PerCell(shape["bucket_id"], frozenset(capabilities))

        if not isinstance(item["buckets"], dict):
            raise ConfigError(f"{path}.buckets: expected an object")
        buckets = {
            bucket_id: _parse_bucket_settings(settings, f"{path}.buckets[{position}]")
            for position, (bucket_id, settings) in enumerate(item["buckets"].items())
        }
        accounts.append(
            Account(
                label=label,
                key_id_env=item["key_id_env"],
                key_env=item["key_env"],
                pinned_key_ids=frozenset(
                    _string_list(item["pinned_key_ids"], f"{path}.pinned_key_ids")
                ),
                per_cell=per_cell,
                buckets=buckets,
            )
        )
    return accounts


def receiver_capability(url: str) -> str:
    """The last path segment of the receiver URL: its capability, and the key of every transition ID."""
    return urlsplit(url).path.rsplit("/", 1)[-1]


def parse_alert_url(raw: str) -> str:
    if not raw.startswith("https://") or any(ch.isspace() for ch in raw):
        raise ConfigError("the alert URL is missing or not an https URL")
    if not receiver_capability(raw):
        raise ConfigError("the alert URL has no capability as its last path segment")
    return raw


# --- B2 native API --------------------------------------------------------------


def _open(opener: Opener, request: urllib.request.Request) -> bytes:
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        body: bytes = response.read()
    return body


def _send(opener: Opener, request: urllib.request.Request) -> bytes:
    """Send once, and once more after a 5xx, a timeout or a network error."""
    try:
        return _open(opener, request)
    except (urllib.error.URLError, TimeoutError) as exc:
        client_error = isinstance(exc, urllib.error.HTTPError) and exc.code < 500
        if client_error:
            raise
    return _open(opener, request)


def _call_json(opener: Opener, request: urllib.request.Request) -> dict[str, Any]:
    try:
        payload = json.loads(_send(opener, request))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ApiError("request failed") from exc
    if not isinstance(payload, dict):
        raise ApiError("unexpected response")
    return payload


def _dig(value: Any, *names: str) -> Any:
    for name in names:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def authorize(opener: Opener, key_id: str, key: str) -> Session:
    basic = base64.b64encode(f"{key_id}:{key}".encode()).decode("ascii")
    request = urllib.request.Request(AUTHORIZE_URL, headers={"Authorization": f"Basic {basic}"})
    payload = _call_json(opener, request)
    api_url = _dig(payload, "apiInfo", "storageApi", "apiUrl")
    token = _dig(payload, "authorizationToken")
    account_id = _dig(payload, "accountId")
    if not (
        isinstance(api_url, str)
        and api_url.startswith("https://")
        and isinstance(token, str)
        and isinstance(account_id, str)
    ):
        raise ApiError("unexpected authorization response")
    return Session(api_url=api_url.rstrip("/"), token=token, account_id=account_id)


def _api_post(
    opener: Opener, session: Session, endpoint: str, body: dict[str, Any]
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{session.api_url}/b2api/v3/{endpoint}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": session.token, "Content-Type": "application/json"},
        method="POST",
    )
    return _call_json(opener, request)


def list_keys(opener: Opener, session: Session) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        body: dict[str, Any] = {"accountId": session.account_id, "maxKeyCount": KEY_PAGE_SIZE}
        if cursor is not None:
            body["startApplicationKeyId"] = cursor
        page = _api_post(opener, session, "b2_list_keys", body)
        entries = page.get("keys")
        if not isinstance(entries, list):
            raise ApiError("unexpected key page")
        for entry in entries:
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("applicationKeyId"), str)
                and isinstance(entry.get("capabilities"), list)
                and all(isinstance(item, str) for item in entry["capabilities"])
                and isinstance(entry.get("namePrefix"), str | type(None))
                and isinstance(entry.get("bucketIds"), list | type(None))
            ):
                raise ApiError("unexpected key entry")
            keys.append(entry)
        cursor = page.get("nextApplicationKeyId")
        if cursor is None:
            return keys
        if not isinstance(cursor, str) or not cursor:
            raise ApiError("unexpected key cursor")
    raise ApiError("key listing did not end")


def list_buckets(opener: Opener, session: Session) -> list[dict[str, Any]]:
    page = _api_post(opener, session, "b2_list_buckets", {"accountId": session.account_id})
    entries = page.get("buckets")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("bucketId"), str) for entry in entries
    ):
        raise ApiError("unexpected bucket page")
    return entries


# --- Matching -------------------------------------------------------------------


def _matches_per_cell(key: Mapping[str, Any], shape: PerCell) -> bool:
    prefix = key.get("namePrefix")
    return (
        key.get("bucketIds") == [shape.bucket_id]
        and isinstance(prefix, str)
        and _CELL_PREFIX.fullmatch(prefix) is not None
        and frozenset(key["capabilities"]) == shape.capabilities
    )


def evaluate_keys(account: Account, keys: list[dict[str, Any]]) -> set[str]:
    active: set[str] = set()
    if account.pinned_key_ids - {key["applicationKeyId"] for key in keys}:
        active.add(PINNED_KEY_MISSING)
    prefixes: set[str] = set()
    for key in keys:
        if key["applicationKeyId"] in account.pinned_key_ids:
            continue
        if account.per_cell is None:
            active.add(UNKNOWN_KEY)
        elif not _matches_per_cell(key, account.per_cell):
            active.add(PER_CELL_KEY_SHAPE)
        elif key["namePrefix"] in prefixes:
            active.add(DUPLICATE_PREFIX_KEY)
        else:
            prefixes.add(key["namePrefix"])
    return active


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sorted_lists(rule: Any) -> Any:
    if not isinstance(rule, dict):
        return rule
    return {
        name: sorted(value, key=_dump) if isinstance(value, list) else value
        for name, value in rule.items()
    }


def _normalise(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Ignore ordering that carries no meaning, and nothing else.

    Each lifecycle rule applies by its own file-name prefix, so the rule order is not
    meaningful. The lists inside a CORS rule are sets of origins, operations and
    headers. The order of CORS rules is kept, because a request can take the first rule
    that matches it.
    """
    normalised = dict(settings)
    rules, cors = normalised["lifecycleRules"], normalised["corsRules"]
    if isinstance(rules, list):
        normalised["lifecycleRules"] = sorted(rules, key=_dump)
    if isinstance(cors, list):
        normalised["corsRules"] = [_sorted_lists(rule) for rule in cors]
    return normalised


def observed_settings(bucket: Mapping[str, Any]) -> dict[str, Any] | None:
    """The bucket's settings in the pinned shape, or None when B2 withheld one.

    B2 withholds a setting from a key that lacks its read capability. A setting that
    cannot be read cannot be verified, so the caller treats None as drift.
    """
    names = ("fileLockConfiguration", "defaultServerSideEncryption", "replicationConfiguration")
    wrapped = [bucket.get(name) for name in names]
    if not all(_dig(item, "isClientAuthorizedToRead") is True for item in wrapped):
        return None
    lock, encryption, replication = (_dig(item, "value") for item in wrapped)
    if not isinstance(lock, dict):
        return None
    retention = lock.get("defaultRetention")
    if retention == {"mode": None, "period": None}:
        # B2 reports an unset default retention this way; a pinned null means the same.
        retention = None
    return {
        "bucketType": bucket.get("bucketType"),
        "fileLockEnabled": lock.get("isFileLockEnabled"),
        "defaultRetention": retention,
        "lifecycleRules": bucket.get("lifecycleRules"),
        "defaultServerSideEncryption": encryption,
        "replicationConfiguration": replication,
        "corsRules": bucket.get("corsRules"),
    }


def evaluate_buckets(account: Account, buckets: list[dict[str, Any]]) -> set[str]:
    active: set[str] = set()
    by_id = {bucket["bucketId"]: bucket for bucket in buckets}
    for bucket_id, pinned in account.buckets.items():
        bucket = by_id.get(bucket_id)
        if bucket is None:
            active.add(BUCKET_MISSING)
            continue
        observed = observed_settings(bucket)
        if observed is None or _normalise(observed) != _normalise(pinned):
            active.add(BUCKET_SETTING_DRIFT)
    return active


def check_account(account: Account, environ: Mapping[str, str], opener: Opener) -> dict[str, bool]:
    """Map each reason token whose check ran to whether it is active.

    A check that could not run reports nothing: sending "inactive" would clear a live
    drift alert while the watcher is blind. The failure token carries that state.
    """
    key_id = environ.get(account.key_id_env, "")
    key = environ.get(account.key_env, "")
    try:
        if not key_id or not key:
            raise ApiError("credential missing")
        session = authorize(opener, key_id, key)
    except ApiError:
        return {AUTH_FAILURE: True}
    states = {AUTH_FAILURE: False}
    listing_failed = False
    try:
        key_alerts = evaluate_keys(account, list_keys(opener, session))
        states.update({token: token in key_alerts for token in _KEY_TOKENS})
    except ApiError:
        listing_failed = True
    try:
        bucket_alerts = evaluate_buckets(account, list_buckets(opener, session))
        states.update({token: token in bucket_alerts for token in _BUCKET_TOKENS})
    except ApiError:
        listing_failed = True
    states[LISTING_FAILURE] = listing_failed
    return states


# --- Receiver delivery ----------------------------------------------------------


def transition_id(capability: str, alert: str, active: bool, stamp: str) -> str:
    """HMAC-SHA256 keyed by the receiver capability, so only a holder of it can mint the ID.

    A plain hash of these public strings would let another holder of a scheduler
    capability register the ID first under its own job; the receiver then drops the
    genuine alert as a duplicate. The stamp is the UTC day for checks and the UTC
    hour for the heartbeat, which keeps a repeat inside that period idempotent.
    """
    material = f"{JOB}|{alert}|{'true' if active else 'false'}|{stamp}"
    return hmac.new(
        capability.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def alert_request(url: str, alert: str, active: bool, stamp: str) -> urllib.request.Request:
    identifier = transition_id(receiver_capability(url), alert, active, stamp)
    body = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "transition_id": identifier,
            "job": JOB,
            "alert": alert,
            "active": active,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Exomem-Alert-Transition": identifier},
        method="POST",
    )


def deliver(opener: Opener, url: str, alert: str, active: bool, stamp: str) -> bool:
    try:
        _send(opener, alert_request(url, alert, active, stamp))
    except (urllib.error.URLError, TimeoutError):
        return False
    return True


# --- Run ------------------------------------------------------------------------


def run(
    environ: Mapping[str, str],
    opener: Opener,
    now: datetime,
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        accounts = parse_pinned_set(environ.get("KEY_WATCHER_PINNED_SET", ""))
        url = parse_alert_url(environ.get("KEY_WATCHER_ALERT_URL", ""))
    except ConfigError as exc:
        print(f"configuration invalid: {exc}", file=err)
        return 2

    utc = now.astimezone(UTC)
    day, hour = utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m-%dT%H")
    # Evaluate every account before any delivery: the heartbeat then proves that all of them ran.
    results = [(account.label, check_account(account, environ, opener)) for account in accounts]

    active_total = 0
    deliveries: list[tuple[str, bool, str]] = []
    for label, states in results:
        active_tokens = [token for token in ALERT_TOKENS if states.get(token)]
        active_total += len(active_tokens)
        print(
            f"account {label}: {len(active_tokens)} active ({', '.join(active_tokens) or 'none'})",
            file=out,
        )
        deliveries += [
            (f"{label}:{token}", states[token], day) for token in ALERT_TOKENS if token in states
        ]
    deliveries.append((HEARTBEAT_ALERT, False, hour))

    delivered = sum(deliver(opener, url, *delivery) for delivery in deliveries)
    print(f"deliveries: {delivered} of {len(deliveries)} succeeded", file=out)
    return 1 if active_total or delivered != len(deliveries) else 0


def main() -> int:
    opener = urllib.request.build_opener(_RefuseRedirects)
    try:
        return run(os.environ, opener, datetime.now(UTC), sys.stdout, sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a traceback could print a key or bucket ID into a public log
        print(f"unexpected error: {type(exc).__name__}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
