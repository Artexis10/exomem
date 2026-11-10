#!/usr/bin/env python3
"""Out-of-cluster watcher for object-store key and bucket drift.

Implements the key watcher of `contain-cloud-controller-compromise` D9. A scheduled
CI job runs it. It lists every application key and bucket of each pinned account
with a list-only key, and reports to a Substrate receiver that no in-cluster
component can reach.

An account may also pin a media watch (`collect-released-media` D4). With a second
list-only key, limited to the media bucket, the watcher counts the hide markers of
the pinned window and alerts when they exceed the pinned budget.

The pinned set may also pin a deletion watch (`automate-cell-storage-deletion` D4). The
watcher then reads the set of deleted cells from a Substrate endpoint, lists each deleted
cell's prefix in the backup and media buckets, and reports each cell's purge state to the
receiver: pending, hidden, or confirmed, and an anomaly when the purge stalls or comes early.

The pinned set is data, held in the `KEY_WATCHER_PINNED_SET` secret: this public
repository holds no key ID, bucket name or account ID. Output is value-free. It
prints account labels, reason tokens and counts only, because CI logs are public. A cell ID
goes only into an alert name that the receiver gets, never into the log.

Exit status: 0 when no alert is active and every delivery succeeded; 1 when a
drift alert or a deletion anomaly is active or a delivery failed (a pending or hidden
deletion is the normal course and keeps the run green); 2 when the configuration is invalid;
3 on an unexpected error. Exit 2 and 3 send no heartbeat, and neither does a run with a
failed check delivery, so the receiver also reports a run that did not finish reporting.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TextIO
from urllib.parse import urlsplit

# v4, not v3: on 2026-11-01 the v3 b2_list_keys of a live account returned 6 of
# its 25 keys (the v4 listing returned all 25), so a v3 watcher would be blind to
# most keys. v4 also reports a key's restriction as the `bucketIds` list.
API_VERSION = "v4"
AUTHORIZE_URL = f"https://api.backblazeb2.com/b2api/{API_VERSION}/b2_authorize_account"
SCHEMA_VERSION = 1
JOB = "b2-key-watcher"
HEARTBEAT_ALERT = "KEY_WATCHER_RUN"
TIMEOUT_SECONDS = 10
KEY_PAGE_SIZE = 1000
MAX_PAGES = 100
FILE_PAGE_SIZE = 10000
# A listing that needs more pages than this is a failure, never a partial count: 2.5 million versions.
MAX_FILE_PAGES = 250

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
# Only an account that pins a media watch sends these.
MEDIA_HIDE_BURST = "MEDIA_HIDE_BURST"
MEDIA_LISTING_FAILURE = "MEDIA_LISTING_FAILURE"
MEDIA_TOKENS = (MEDIA_HIDE_BURST, MEDIA_LISTING_FAILURE)
# automate-cell-storage-deletion D4: only a pinned set with a deletion watch sends these.
PURGE_KEY_UNEXPECTED = "PURGE_KEY_UNEXPECTED"
DELETIONS_UNKNOWN = "DELETIONS_UNKNOWN"
DELETION_LISTING_FAILURE = "DELETION_LISTING_FAILURE"
# Each of these goes to the receiver as `<token>:<cell id>`.
CELL_DELETION_PENDING = "CELL_DELETION_PENDING"
CELL_DELETION_HIDDEN = "CELL_DELETION_HIDDEN"
CELL_DELETION_ANOMALY = "CELL_DELETION_ANOMALY"
CELL_TOKENS = (CELL_DELETION_PENDING, CELL_DELETION_HIDDEN, CELL_DELETION_ANOMALY)
ACCOUNT_TOKENS = (*ALERT_TOKENS, PURGE_KEY_UNEXPECTED, *MEDIA_TOKENS)
# B2's documented file-version actions, protocol tokens.
HIDE_ACTION = "hide"
UPLOAD_ACTION = "upload"
START_ACTION = "start"
DAY_SECONDS = 86_400
# The spec's version bound: a hide may land one day after `purge_after`, and the provider may
# delete a version two days after it is due (automate-cell-storage-deletion D4).
HIDE_LATENESS_SECONDS = DAY_SECONDS
PROVIDER_DELAY_SECONDS = 2 * DAY_SECONDS

# A cell prefix is `cells/<cell id>/`. The cell id is the 16-character base32 body of
# cellctl's namespace rule `exo-cell-[a-z2-7]{16}`. That protocol fixes the shape, so a
# regex over this closed form is sound; it checks a format, not what anyone meant.
_CELL_ID = re.compile(r"[a-z2-7]{16}")
_CELL_PREFIX = re.compile(rf"cells/({_CELL_ID.pattern})/")
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
class MediaWatch:
    bucket_id: str
    key_id_env: str
    key_env: str
    hide_budget: int
    window_hours: int


@dataclass(frozen=True)
class Account:
    label: str
    key_id_env: str
    key_env: str
    pinned_key_ids: frozenset[str]
    per_cell: PerCell | None
    buckets: Mapping[str, Mapping[str, Any]]
    media: MediaWatch | None = None


@dataclass(frozen=True)
class DeletionWatch:
    """The deletion watch. `account` holds the per-cell keys and, as its per-cell bucket, the backups.
    `media_account` holds the media watch, or is None on a platform without a media bucket. The
    retention and the media grace come from those buckets' pinned settings, so they live in one place."""

    account: str
    media_account: str | None
    backup_key_id_env: str
    backup_key_env: str
    purge_capabilities: frozenset[str]
    skew_seconds: int
    backup_bucket_id: str
    retention_days: int
    media_grace_days: int | None


@dataclass(frozen=True)
class PinnedSet:
    accounts: list[Account]
    deletions: DeletionWatch | None


@dataclass(frozen=True)
class Deletion:
    """One cell of Substrate's deletion set; times are Unix seconds."""

    iat: int
    purge_after: int
    stopped_at: int | None


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


def parse_pinned_set(raw: str) -> PinnedSet:
    if not raw.strip():
        raise ConfigError("the pinned set is empty")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("the pinned set is not JSON") from exc
    top = {"schema_version", "accounts"}
    # The deletion watch is optional, so a pinned set written before it still parses.
    if isinstance(document, dict) and "deletions" in document:
        top.add("deletions")
    document = _exact_object(document, top, "pinned set")
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ConfigError("pinned set: unsupported schema_version")
    if not isinstance(document["accounts"], list) or not document["accounts"]:
        raise ConfigError("pinned set: accounts must be a non-empty list")

    accounts: list[Account] = []
    for index, raw_account in enumerate(document["accounts"]):
        path = f"accounts[{index}]"
        fields = {"label", "key_id_env", "key_env", "pinned_key_ids", "per_cell", "buckets"}
        # The media watch is optional, so a pinned set written before it still parses.
        if isinstance(raw_account, dict) and "media" in raw_account:
            fields.add("media")
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
        if per_cell is not None and per_cell.bucket_id not in buckets:
            raise ConfigError(f"{path}.per_cell.bucket_id: not one of the pinned buckets")
        media = _parse_media(item["media"], buckets, f"{path}.media") if "media" in item else None
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
                media=media,
            )
        )
    deletions = _parse_deletions(document["deletions"], accounts) if "deletions" in document else None
    return PinnedSet(accounts, deletions)


def _parse_media(value: Any, buckets: Mapping[str, Any], path: str) -> MediaWatch:
    fields = {"bucket_id", "key_id_env", "key_env", "hide_budget", "window_hours"}
    item = _exact_object(value, fields, path)
    if not isinstance(item["bucket_id"], str) or item["bucket_id"] not in buckets:
        raise ConfigError(f"{path}.bucket_id: not one of the pinned buckets")
    for name in ("key_id_env", "key_env"):
        if not isinstance(item[name], str) or not _ENV_NAME.fullmatch(item[name]):
            raise ConfigError(f"{path}.{name}: invalid environment name")
    if type(item["hide_budget"]) is not int or item["hide_budget"] < 0:
        raise ConfigError(f"{path}.hide_budget: expected a whole number, zero or more")
    if type(item["window_hours"]) is not int or item["window_hours"] < 1:
        raise ConfigError(f"{path}.window_hours: expected a whole number of hours, one or more")
    return MediaWatch(item["bucket_id"], item["key_id_env"], item["key_env"], item["hide_budget"],
                      item["window_hours"])


def _retention_days(retention: Any, path: str) -> int:
    period = _dig(retention, "period")
    if not (isinstance(period, dict) and period.get("unit") == "days"
            and type(period.get("duration")) is int and period["duration"] >= 1):
        raise ConfigError(f"{path}: the backup bucket's default retention is not pinned in days")
    return period["duration"]


def _hide_grace_days(rules: list[Any], path: str) -> int:
    whole = [rule for rule in rules if isinstance(rule, dict) and rule.get("fileNamePrefix") == ""]
    days = whole[0].get("daysFromHidingToDeleting") if len(whole) == 1 else None
    if type(days) is not int or days < 1:
        raise ConfigError(f"{path}: the media bucket pins no whole-bucket rule that deletes hidden versions")
    return days


def _parse_deletions(value: Any, accounts: list[Account]) -> DeletionWatch:
    path = "deletions"
    fields = {"account", "media_account", "backup_key_id_env", "backup_key_env", "purge_capabilities",
              "skew_margin_minutes"}
    item = _exact_object(value, fields, path)
    by_label = {account.label: account for account in accounts}
    account = by_label.get(item["account"]) if isinstance(item["account"], str) else None
    if account is None or account.per_cell is None:
        raise ConfigError(f"{path}.account: not a pinned account with a per-cell shape")
    for name in ("backup_key_id_env", "backup_key_env"):
        if not isinstance(item[name], str) or not _ENV_NAME.fullmatch(item[name]):
            raise ConfigError(f"{path}.{name}: invalid environment name")
    capabilities = frozenset(_string_list(item["purge_capabilities"], f"{path}.purge_capabilities"))
    if capabilities == account.per_cell.capabilities:
        raise ConfigError(f"{path}.purge_capabilities: the purge shape must differ from the per-cell shape")
    skew = item["skew_margin_minutes"]
    if type(skew) is not int or skew < 0:
        raise ConfigError(f"{path}.skew_margin_minutes: expected a whole number, zero or more")
    retention = _retention_days(account.buckets[account.per_cell.bucket_id]["defaultRetention"], f"{path}.account")
    grace = None
    if item["media_account"] is not None:
        media_account = by_label.get(item["media_account"]) if isinstance(item["media_account"], str) else None
        if media_account is None or media_account.media is None:
            raise ConfigError(f"{path}.media_account: not a pinned account with a media watch")
        rules = media_account.buckets[media_account.media.bucket_id]["lifecycleRules"]
        grace = _hide_grace_days(rules, f"{path}.media_account")
    return DeletionWatch(
        account=account.label,
        media_account=item["media_account"],
        backup_key_id_env=item["backup_key_id_env"],
        backup_key_env=item["backup_key_env"],
        purge_capabilities=capabilities,
        skew_seconds=skew * 60,
        backup_bucket_id=account.per_cell.bucket_id,
        retention_days=retention,
        media_grace_days=grace,
    )


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


# A reset or a truncated response raises OSError or HTTPException, not URLError. Catching
# only URLError would let one account's connection error end the run before any delivery.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


def _send(opener: Opener, request: urllib.request.Request) -> bytes:
    """Send once, and once more after a 5xx, a timeout or a network error."""
    try:
        return _open(opener, request)
    except _TRANSPORT_ERRORS as exc:
        client_error = isinstance(exc, urllib.error.HTTPError) and exc.code < 500
        if client_error:
            raise
    return _open(opener, request)


def _call_json(opener: Opener, request: urllib.request.Request) -> dict[str, Any]:
    try:
        payload = json.loads(_send(opener, request))
    except (*_TRANSPORT_ERRORS, ValueError) as exc:
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
        f"{session.api_url}/b2api/{API_VERSION}/{endpoint}",
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


def iter_versions(
    opener: Opener, session: Session, bucket_id: str, prefix: str | None = None
) -> Iterator[dict[str, Any]]:
    """Every file version in the bucket, or under the prefix, over every page.

    A caller that stops at an ApiError has seen only part of the listing, and must treat the
    whole listing as failed.
    """
    body: dict[str, Any] = {"bucketId": bucket_id, "maxFileCount": FILE_PAGE_SIZE}
    if prefix is not None:
        body["prefix"] = prefix
    for _ in range(MAX_FILE_PAGES):
        page = _api_post(opener, session, "b2_list_file_versions", body)
        entries = page.get("files")
        if not isinstance(entries, list):
            raise ApiError("unexpected file page")
        for entry in entries:
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("fileName"), str)
                and isinstance(entry.get("action"), str)
                and type(entry.get("uploadTimestamp")) is int
            ):
                raise ApiError("unexpected file entry")
            yield entry
        next_name, next_id = page.get("nextFileName"), page.get("nextFileId")
        if next_name is None and next_id is None:
            return
        if not (isinstance(next_name, str) and next_name and isinstance(next_id, str) and next_id):
            raise ApiError("unexpected file cursor")
        body = {**body, "startFileName": next_name, "startFileId": next_id}
    raise ApiError("file listing did not end")


def _cell_of(name: str) -> str | None:
    match = _CELL_PREFIX.match(name)
    return match.group(1) if match else None


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


def _near_miss(key: Mapping[str, Any], shape: PerCell) -> bool:
    """A key for this bucket, inside one full cell prefix, with capabilities within the shape.

    A shorter prefix such as `cells/` reaches many cells, so it is not a near miss.
    """
    prefix = key.get("namePrefix")
    return (
        key.get("bucketIds") == [shape.bucket_id]
        and frozenset(key["capabilities"]) <= shape.capabilities
        and isinstance(prefix, str)
        and _CELL_PREFIX.match(prefix) is not None
    )


def evaluate_keys(
    account: Account,
    keys: list[dict[str, Any]],
    purge: frozenset[str] | None = None,
    deleted: frozenset[str] = frozenset(),
) -> set[str]:
    """`purge` is the purge shape's capabilities when this account holds the deletion watch, and
    `deleted` the deletion set, empty when the watcher could not read it."""
    active: set[str] = set()
    if account.pinned_key_ids - {key["applicationKeyId"] for key in keys}:
        active.add(PINNED_KEY_MISSING)
    prefixes: set[str] = set()
    for key in keys:
        if key["applicationKeyId"] in account.pinned_key_ids:
            continue
        if account.per_cell is not None and _matches_per_cell(key, account.per_cell):
            if key["namePrefix"] in prefixes:
                active.add(DUPLICATE_PREFIX_KEY)
            prefixes.add(key["namePrefix"])
        elif (
            purge is not None
            and account.per_cell is not None
            and _matches_per_cell(key, PerCell(account.per_cell.bucket_id, purge))
        ):
            # The custodian mints a purge key only for a deleted cell. Without the set, every
            # purge key is reported, because none can be shown to belong to a deletion.
            if _cell_of(key["namePrefix"]) not in deleted:
                active.add(PURGE_KEY_UNEXPECTED)
        elif account.per_cell is not None and _near_miss(key, account.per_cell):
            active.add(PER_CELL_KEY_SHAPE)
        else:
            # Anything else, such as an account-wide writeKeys key, is a rogue key. Only a near
            # miss may use the shape token: a wider key reported as shape drift would hide
            # behind an already-active shape alert.
            active.add(UNKNOWN_KEY)
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


def check_account(
    account: Account,
    environ: Mapping[str, str],
    opener: Opener,
    purge: frozenset[str] | None = None,
    deleted: frozenset[str] = frozenset(),
) -> tuple[dict[str, bool], list[dict[str, Any]] | None]:
    """Map each reason token whose check ran to whether it is active, and return the key
    listing, None when it failed.

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
        return {AUTH_FAILURE: True}, None
    states = {AUTH_FAILURE: False}
    listing_failed = False
    keys = None
    tokens = _KEY_TOKENS if purge is None else (*_KEY_TOKENS, PURGE_KEY_UNEXPECTED)
    try:
        keys = list_keys(opener, session)
        key_alerts = evaluate_keys(account, keys, purge, deleted)
        states.update({token: token in key_alerts for token in tokens})
    except ApiError:
        listing_failed = True
    try:
        bucket_alerts = evaluate_buckets(account, list_buckets(opener, session))
        states.update({token: token in bucket_alerts for token in _BUCKET_TOKENS})
    except ApiError:
        listing_failed = True
    states[LISTING_FAILURE] = listing_failed
    return states, keys


def check_media(
    media: MediaWatch,
    environ: Mapping[str, str],
    opener: Opener,
    now: datetime,
    deleted: frozenset[str] = frozenset(),
    watched: frozenset[str] = frozenset(),
) -> tuple[dict[str, bool], int | None, dict[str, list[dict[str, Any]]] | None]:
    """The media tokens whose check ran, the hide count of the window, None when unknown, and
    the versions under each watched cell's prefix, None when the listing failed.

    The count skips the hides under a deleted cell's prefix: a purge hides every name of
    the cell, and that is no burst. A blind watch sends no burst state: "inactive" would
    clear a live burst alert.
    """
    key_id = environ.get(media.key_id_env, "")
    key = environ.get(media.key_env, "")
    since_ms = int(now.timestamp() * 1000) - media.window_hours * 3_600_000
    hides = 0
    found: dict[str, list[dict[str, Any]]] = {cell: [] for cell in watched}
    try:
        if not key_id or not key:
            raise ApiError("credential missing")
        session = authorize(opener, key_id, key)
        for entry in iter_versions(opener, session, media.bucket_id):
            cell = _cell_of(entry["fileName"])
            if cell in found:
                found[cell].append(entry)
            if entry["action"] == HIDE_ACTION and entry["uploadTimestamp"] >= since_ms and cell not in deleted:
                hides += 1
    except ApiError:
        return {MEDIA_LISTING_FAILURE: True}, None, None
    return {MEDIA_LISTING_FAILURE: False, MEDIA_HIDE_BURST: hides > media.hide_budget}, hides, found


# --- automate-cell-storage-deletion D4: the deletion watch -----------------------


def _read_deletions(opener: Opener, url: str) -> dict[str, Deletion]:
    if not url.startswith("https://") or any(ch.isspace() for ch in url) or not receiver_capability(url):
        raise ApiError("the deletion set URL is missing or not an https URL with a capability")
    payload = _call_json(opener, urllib.request.Request(url, headers={"Accept": "application/json"}))
    if set(payload) != {"schema_version", "cells"} or payload["schema_version"] != SCHEMA_VERSION:
        raise ApiError("unexpected deletion set")
    cells: dict[str, Deletion] = {}
    for entry in payload["cells"] if isinstance(payload["cells"], list) else [None]:
        if not (
            isinstance(entry, dict)
            and set(entry) == {"cell_id", "iat", "purge_after", "stopped_at"}
            and isinstance(entry["cell_id"], str)
            and _CELL_ID.fullmatch(entry["cell_id"])
            and entry["cell_id"] not in cells
            and type(entry["iat"]) is int
            and type(entry["purge_after"]) is int
            and (entry["stopped_at"] is None or type(entry["stopped_at"]) is int)
        ):
            raise ApiError("unexpected deletion set entry")
        cells[entry["cell_id"]] = Deletion(entry["iat"], entry["purge_after"], entry["stopped_at"])
    return cells


def read_deletion_set(environ: Mapping[str, str], opener: Opener) -> dict[str, Deletion] | None:
    """Substrate's deletion set, None when the watcher could not read it in full. The URL's last
    segment is the watcher's receiver capability, the only capability that the endpoint accepts."""
    try:
        return _read_deletions(opener, environ.get("KEY_WATCHER_DELETIONS_URL", ""))
    except ApiError:
        return None


def list_backup_prefixes(
    watch: DeletionWatch, environ: Mapping[str, str], opener: Opener, cells: frozenset[str]
) -> dict[str, list[dict[str, Any]]] | None:
    """The backup versions under each cell's prefix, None when any listing failed.

    Each run lists every cell in the set: the watcher keeps no state, and only a listing shows
    whether a cell has left its window. The key is authorized even with no cell to list, so a
    refused key shows before the first deletion.
    """
    key_id = environ.get(watch.backup_key_id_env, "")
    key = environ.get(watch.backup_key_env, "")
    try:
        if not key_id or not key:
            raise ApiError("credential missing")
        session = authorize(opener, key_id, key)
        return {
            cell: list(iter_versions(opener, session, watch.backup_bucket_id, f"cells/{cell}/"))
            for cell in sorted(cells)
        }
    except ApiError:
        return None


def cell_states(
    deletion: Deletion,
    entries: list[dict[str, Any]],
    keys: list[dict[str, Any]],
    watch: DeletionWatch,
    now: int,
) -> dict[str, bool] | None:
    """The cell's per-cell alerts, derived again from what exists now; None once the cell has
    left its watch window. `entries` holds the versions under the cell's prefix in both buckets,
    and `keys` the keys whose name prefix lies under it."""
    newest: dict[str, tuple[int, bool]] = {}
    hides: list[int] = []
    versions = False
    for entry in entries:
        action, at = entry["action"], entry["uploadTimestamp"] // 1000
        if action == HIDE_ACTION:
            hides.append(at)
        if action in (UPLOAD_ACTION, START_ACTION):
            versions = True
        if action != START_ACTION:
            # A name is current when its newest entry, apart from `start` entries, is an upload.
            # On a tie the upload wins, so an unclear name counts as still current.
            seen, candidate = newest.get(entry["fileName"]), (at, action == UPLOAD_ACTION)
            newest[entry["fileName"]] = candidate if seen is None else max(seen, candidate)
    remains = any(current for _, current in newest.values()) or bool(keys)
    margin = max(watch.media_grace_days or 0, watch.retention_days + 1) * DAY_SECONDS
    # Anchored at the newest hide, so a purge that a hold or an outage delayed is still watched to its end.
    bound = max(deletion.purge_after + HIDE_LATENESS_SECONDS, max(hides, default=0)) + margin + PROVIDER_DELAY_SECONDS
    if now >= bound + DAY_SECONDS and not remains:
        return None
    pending = now < deletion.purge_after or remains
    early = False
    if deletion.stopped_at is not None:
        opens = max(deletion.iat, deletion.stopped_at) + watch.skew_seconds
        closes = deletion.purge_after - watch.skew_seconds
        early = any(opens <= at < closes for at in hides)
    anomaly = (
        (remains and now >= deletion.purge_after + HIDE_LATENESS_SECONDS)
        or (versions and now >= bound)
        or early
        # A cell with no stop time never opens its early-hide window; the alert shows the missing report.
        or (deletion.stopped_at is None and now >= deletion.purge_after)
    )
    return {
        CELL_DELETION_PENDING: pending,
        CELL_DELETION_HIDDEN: not pending and versions,
        CELL_DELETION_ANOMALY: anomaly,
    }


# --- Receiver delivery ----------------------------------------------------------


def transition_id(capability: str, alert: str, active: bool, stamp: str) -> str:
    """HMAC-SHA256 keyed by the receiver capability, so only a holder of it can mint the ID.

    A plain hash of these public strings would let another holder of a scheduler
    capability register the ID first under its own job; the receiver then drops the
    genuine alert as a duplicate. The stamp is the UTC hour for an active alert and
    the heartbeat, and the UTC day for an inactive alert. A drift that clears and
    returns within one day then gets a new ID at its return, which the receiver
    does not drop as the morning's duplicate; a repeat inside the stamp's period
    stays idempotent.
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
    except _TRANSPORT_ERRORS:
        return False
    return True


# --- Run ------------------------------------------------------------------------


def _keys_under(keys: list[dict[str, Any]], cell: str) -> list[dict[str, Any]]:
    return [key for key in keys if (key.get("namePrefix") or "").startswith(f"cells/{cell}/")]


def run(
    environ: Mapping[str, str],
    opener: Opener,
    now: datetime,
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        pinned = parse_pinned_set(environ.get("KEY_WATCHER_PINNED_SET", ""))
        url = parse_alert_url(environ.get("KEY_WATCHER_ALERT_URL", ""))
    except ConfigError as exc:
        print(f"configuration invalid: {exc}", file=err)
        return 2

    utc = now.astimezone(UTC)
    day, hour = utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m-%dT%H")
    watch = pinned.deletions
    deletions = read_deletion_set(environ, opener) if watch is not None else None
    # An unread set is empty here, so every hide counts and every purge key is reported.
    deleted = frozenset(deletions or ())
    # A cell's window opens at its grant's `iat`.
    watched = frozenset(cell for cell, deletion in (deletions or {}).items() if deletion.iat <= utc.timestamp())
    cell_keys: list[dict[str, Any]] | None = None
    cell_media: dict[str, list[dict[str, Any]]] | None = {}
    # Evaluate every account before any delivery: the heartbeat then proves that all of them ran.
    results = []
    for account in pinned.accounts:
        holds_cells = watch is not None and account.label == watch.account
        purge = watch.purge_capabilities if watch is not None and holds_cells else None
        states, keys = check_account(account, environ, opener, purge, deleted)
        if holds_cells:
            cell_keys = keys
        hides = None
        if account.media is not None:
            holds_media = watch is not None and account.label == watch.media_account
            media_states, hides, found = check_media(
                account.media, environ, opener, utc, deleted, watched if holds_media else frozenset()
            )
            states |= media_states
            if holds_media:
                cell_media = found
        results.append((account, states, hides))

    deletion_states: dict[str, bool] = {}
    cells: dict[str, dict[str, bool]] = {}
    if watch is not None:
        deletion_states[DELETIONS_UNKNOWN] = deletions is None
        if deletions is not None:
            backups = list_backup_prefixes(watch, environ, opener, watched)
            # Per-cell states come only from a run that read every listing in full.
            if backups is None or cell_keys is None or cell_media is None:
                deletion_states[DELETION_LISTING_FAILURE] = True
            else:
                deletion_states[DELETION_LISTING_FAILURE] = False
                for cell in sorted(watched):
                    state = cell_states(deletions[cell], backups[cell] + cell_media.get(cell, []),
                                        _keys_under(cell_keys, cell), watch, int(utc.timestamp()))
                    if state is not None:
                        cells[cell] = state

    active_total = 0
    deliveries: list[tuple[str, bool, str]] = []
    for account, states, hides in results:
        label = account.label
        active_tokens = [token for token in ACCOUNT_TOKENS if states.get(token)]
        active_total += len(active_tokens)
        print(
            f"account {label}: {len(active_tokens)} active ({', '.join(active_tokens) or 'none'})",
            file=out,
        )
        if account.media is not None:
            # The count and nothing else: the budget and the window stay in the pinned set.
            print(
                f"account {label}: {'unknown' if hides is None else hides} media hides in the window",
                file=out,
            )
        deliveries += [
            (f"{label}:{token}", states[token], hour if states[token] else day)
            for token in ACCOUNT_TOKENS
            if token in states
        ]
    if watch is not None:
        active_total += sum(deletion_states.values())
        deliveries += [(token, active, hour if active else day) for token, active in deletion_states.items()]
        # Counts only: a cell ID goes to the receiver in an alert name, never to the public log.
        if deletions is None:
            print(f"deletions: unknown ({DELETIONS_UNKNOWN})", file=out)
        elif deletion_states[DELETION_LISTING_FAILURE]:
            print(f"deletions: {len(deletions)} in the set; a listing failed ({DELETION_LISTING_FAILURE}), "
                  "so no cell changed state", file=out)
        else:
            counts = {token: sum(state[token] for state in cells.values()) for token in CELL_TOKENS}
            confirmed = sum(
                not state[CELL_DELETION_PENDING] and not state[CELL_DELETION_HIDDEN] for state in cells.values()
            )
            print(f"deletions: {len(deletions)} in the set, {len(cells)} watched: "
                  f"{counts[CELL_DELETION_PENDING]} pending, {counts[CELL_DELETION_HIDDEN]} hidden, "
                  f"{confirmed} confirmed, {counts[CELL_DELETION_ANOMALY]} anomalous", file=out)
        for cell, state in cells.items():
            # Pending and hidden are the normal course of a deletion and keep the run green.
            active_total += state[CELL_DELETION_ANOMALY]
            deliveries += [(f"{token}:{cell}", state[token], hour if state[token] else day) for token in CELL_TOKENS]

    delivered = sum(deliver(opener, url, *delivery) for delivery in deliveries)
    # The heartbeat vouches that every account was evaluated and reported. After a failed
    # delivery it would hide a transition that never arrived, so it is withheld and the
    # receiver's missing-heartbeat alert fires instead.
    heartbeat_sent = delivered == len(deliveries) and deliver(
        opener, url, HEARTBEAT_ALERT, False, hour
    )
    print(f"deliveries: {delivered + heartbeat_sent} of {len(deliveries) + 1} succeeded", file=out)
    if not heartbeat_sent and delivered != len(deliveries):
        print("heartbeat withheld: a check delivery failed", file=out)
    return 1 if active_total or not heartbeat_sent else 0


def main() -> int:
    opener = urllib.request.build_opener(_RefuseRedirects)
    try:
        return run(os.environ, opener, datetime.now(UTC), sys.stdout, sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a traceback could print a key or bucket ID into a public log
        print(f"unexpected error: {type(exc).__name__}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
