"""The signed grant and the destination check for an export, import or restore (contain-cloud-controller-compromise D4).

Substrate signs a grant for each export, import and restore. The Job that moves
the data checks, before any byte moves, that the grant is for this cell,
operation and operation ID, and that every presigned URL it uses names the
object store, the bucket and the grant's own object key. `cellctl` relays the
grant and presigns the URLs.

Today `cellctl` also renders the trust anchors (the grant's verifying key, the
object-store endpoint and the bucket) into the Job's environment, so a
compromised `cellctl` can change them and this check only reports. Once D2 pins
the anchors in the admission policy, `cellctl` can change neither the grant nor
the destination without the cell noticing. Report mode
(`cloud_containment.ENFORCE`) logs each mismatch and lets the Job run; enforce
mode refuses with `OperationRefused`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, unquote, urlsplit

from . import cloud_cell, cloud_containment, cloud_origin

GRANT_AUDIENCE = "exomem-cell-operation"
GRANT_PUBLIC_KEY_ENV = "EXOMEM_CLOUD_GRANT_PUBLIC_KEY"
GRANT_KEY_INVALID = "CLOUD_GRANT_KEY_INVALID"
OBJECT_STORE_ENDPOINT_ENV = "EXOMEM_CLOUD_OBJECT_STORE_ENDPOINT"
OBJECT_STORE_BUCKET_ENV = "EXOMEM_CLOUD_OBJECT_STORE_BUCKET"
# A grant is signed before its Job runs. A replay still names one operation ID, one key and, for an import, one
# digest, and an import is idempotent per import ID, so a long window adds no new capability.
GRANT_MAX_LIFETIME = 72 * 3600
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DEFAULT_PORT = {"https": 443, "http": 80}  # nosemgrep: ep-word-set -- the URL schemes' default ports.
# The query names that cellctl's S3 clients put in a presigned URL (checked with botocore 1.43.111): the SigV4
# presign names, the multipart names of an upload part, and the version and response names of a download.
# Any other name, such as `x-amz-acl` or `x-amz-copy-source`, changes what the request does.
_PRESIGN_QUERY = frozenset({  # nosemgrep: ep-word-set -- parameter names fixed by the SigV4 and S3 protocols.
    "x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires", "x-amz-signedheaders",
    "x-amz-signature", "uploadid", "partnumber", "versionid", "response-content-disposition",
})


class Operation(StrEnum):
    """The grant's `operation` claim. D4 fixes these three."""

    EXPORT = "export"
    IMPORT = "import"
    RESTORE = "restore"


# Every grant carries these claims. The import and restore grants add the ones that bind their payload.
_COMMON_CLAIMS = frozenset({  # nosemgrep: ep-word-set -- the grant's claim names, fixed by D4.
    "aud", "iat", "exp", "jti", "tenant_id", "cell_id", "operation", "operation_id", "object_key",
})
_PAYLOAD_CLAIMS = {  # nosemgrep: ep-word-set -- the grant's claim names, fixed by D4.
    Operation.EXPORT: frozenset(),
    Operation.IMPORT: frozenset({"ciphertext_bytes", "ciphertext_sha256"}),
    Operation.RESTORE: frozenset({"snapshot_id"}),
}


class Reason(StrEnum):
    """Why a check would refuse (report mode) or refused (enforce mode). The event names exactly one."""

    GRANT_MISSING = "grant_missing"
    GRANT_UNCONFIGURED = "grant_unconfigured"
    GRANT_INVALID = "grant_invalid"
    GRANT_OPERATION = "grant_operation"
    GRANT_REQUEST = "grant_request"
    DESTINATION_UNCONFIGURED = "destination_unconfigured"
    URL_MALFORMED = "url_malformed"
    URL_SCHEME = "url_scheme"
    URL_USERINFO = "url_userinfo"
    URL_HOST = "url_host"
    URL_PATH = "url_path"
    URL_FOREIGN_KEY = "url_foreign_key"
    URL_GRANT_KEY = "url_grant_key"
    URL_QUERY = "url_query"
    URL_MISSING = "url_missing"
    CHECK_FAILED = "check_failed"
    NONE = "none"


class OperationRefused(Exception):
    """Enforce mode: the operation may not move data. It carries the first reason."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason = reason


def _own_prefix(cell_id: str) -> str:
    return f"cells/{cell_id}/"


def _is_relative_segment(segment: str) -> bool:
    """Whether a path segment is empty, `.` or `..`, the three that path resolution treats specially."""
    # nosemgrep: ep-word-membership -- the closed set of RFC 3986 path-resolution segments, not a vocabulary.
    return segment in ("", ".", "..")


def _key_is_own(key: object, cell_id: str) -> bool:
    """Whether a decoded object key sits under this cell's prefix, with no empty, `.` or `..` segment."""
    if not isinstance(key, str) or not key.startswith(_own_prefix(cell_id)):
        return False
    return not any(_is_relative_segment(segment) for segment in key.split("/"))


@dataclass(frozen=True, slots=True)
class Destination:
    """The object store the cell may reach: the endpoint and bucket anchors."""

    scheme: str
    host: str
    port: int
    bucket: str

    @classmethod
    def parse(cls, endpoint: str, bucket: str) -> Destination | None:
        """The destination the anchors name, or None when the endpoint is not a bare http(s) origin or the bucket is empty."""
        try:
            parts = urlsplit(endpoint)
            port = parts.port
        except ValueError:
            return None
        if (parts.scheme not in _DEFAULT_PORT or not parts.hostname or "@" in parts.netloc
                or (parts.path and parts.path != "/") or parts.query or parts.fragment
                or not bucket or "/" in bucket):
            return None
        return cls(parts.scheme, parts.hostname, _DEFAULT_PORT[parts.scheme] if port is None else port, bucket)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Destination | None:
        return cls.parse(env.get(OBJECT_STORE_ENDPOINT_ENV, "").strip(), env.get(OBJECT_STORE_BUCKET_ENV, "").strip())

    def reason(self, url: object, *, cell_id: str, grant_key: str | None = None) -> Reason | None:
        """The first way a presigned URL leaves this destination, or None.

        The shape is the one `cellctl` produces: path style, `/<bucket>/<object key>`, with the
        signature in the query. A virtual-host URL names another host and is refused as one.
        The query may carry only the names of `_PRESIGN_QUERY`, compared without case.
        """
        # A parser silently drops tabs and newlines and clients read a backslash as a slash, so refuse these first.
        if not isinstance(url, str) or any(ord(char) <= 0x20 or ord(char) >= 0x7F or char == "\\" for char in url):
            return Reason.URL_MALFORMED
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return Reason.URL_MALFORMED
        if parts.fragment:
            return Reason.URL_MALFORMED
        if parts.scheme != self.scheme:
            return Reason.URL_SCHEME
        if "@" in parts.netloc:
            return Reason.URL_USERINFO
        if parts.hostname != self.host or (_DEFAULT_PORT[parts.scheme] if port is None else port) != self.port:
            return Reason.URL_HOST
        try:
            # Decode each segment once, as the object store does. An encoded slash then shows up inside a segment.
            segments = [unquote(segment, errors="strict") for segment in parts.path.split("/")]
        except UnicodeDecodeError:
            return Reason.URL_PATH
        if (len(segments) < 3 or segments[0] != "" or segments[1] != self.bucket
                or any("/" in segment or any(ord(char) < 0x20 or ord(char) == 0x7F for char in segment)
                       for segment in segments)
                or any(_is_relative_segment(segment) for segment in segments[1:])):
            return Reason.URL_PATH
        key = "/".join(segments[2:])
        if not _key_is_own(key, cell_id):
            return Reason.URL_FOREIGN_KEY
        if grant_key is not None and key != grant_key:
            return Reason.URL_GRANT_KEY
        if any(name.lower() not in _PRESIGN_QUERY for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
            return Reason.URL_QUERY
        return None


def _canonical_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _grant_reason(
    token: str, *, key_pem: str, cell_id: str, operation: Operation, operation_id: str,
) -> tuple[dict | None, Reason | None]:
    """The grant's claims, or the reason they cannot be used for this operation."""
    try:
        key = cloud_origin.load_public_key(key_pem, GRANT_PUBLIC_KEY_ENV, GRANT_KEY_INVALID)
    except cloud_cell.CloudConfigError:
        return None, Reason.GRANT_UNCONFIGURED
    claims = cloud_origin.signed_claims(
        token, key=key, cell_id=cell_id, audience=GRANT_AUDIENCE,
        fields=_COMMON_CLAIMS | _PAYLOAD_CLAIMS[operation], lifetime=GRANT_MAX_LIFETIME,
    )
    if (claims is None or not _key_is_own(claims["object_key"], cell_id)
            or not all(cloud_origin.canonical_uuid(claims[name]) for name in ("tenant_id", "operation_id"))
            or not isinstance(claims["jti"], str) or not claims["jti"]
            or not (operation is not Operation.IMPORT or (
                type(claims["ciphertext_bytes"]) is int and claims["ciphertext_bytes"] > 0
                and _canonical_sha256(claims["ciphertext_sha256"])))
            or not (operation is not Operation.RESTORE or _canonical_sha256(claims["snapshot_id"]))):
        return None, Reason.GRANT_INVALID
    if claims["operation"] != operation or claims["operation_id"] != operation_id:
        return None, Reason.GRANT_OPERATION
    return claims, None


def reasons_for(
    *, operation: Operation, operation_id: str, cell_id: str, urls: Sequence[str], grant: str | None,
    bound: Mapping[str, object] | None = None, env: Mapping[str, str] | None = None,
) -> list[Reason]:
    """Every distinct reason this operation would be refused, in the order checked; empty when it passes.

    `bound` names the claims that the controller-supplied request must repeat, such as an import's
    `ciphertext_sha256`: a grant that disagrees with them is for another payload.
    """
    values = os.environ if env is None else env
    reasons: dict[Reason, None] = {}
    claims = None
    key_pem = values.get(GRANT_PUBLIC_KEY_ENV, "").strip()
    if not key_pem:
        reasons[Reason.GRANT_UNCONFIGURED] = None
    if not grant:
        reasons[Reason.GRANT_MISSING] = None
    if key_pem and grant:
        claims, problem = _grant_reason(
            grant, key_pem=key_pem, cell_id=cell_id, operation=operation, operation_id=operation_id)
        if problem is not None:
            reasons[problem] = None
        elif any(claims[name] != value for name, value in (bound or {}).items()):
            reasons[Reason.GRANT_REQUEST] = None
    if not urls:
        reasons[Reason.URL_MISSING] = None
    destination = Destination.from_env(values)
    if destination is None:
        reasons[Reason.DESTINATION_UNCONFIGURED] = None
    else:
        grant_key = claims["object_key"] if claims is not None else None
        for url in urls:
            problem = destination.reason(url, cell_id=cell_id, grant_key=grant_key)
            if problem is not None:
                reasons[problem] = None
    return list(reasons)


def authorize_operation(
    *, operation: Operation, operation_id: str, cell_id: str, urls: Sequence[str], grant: str | None,
    bound: Mapping[str, object] | None = None, env: Mapping[str, str] | None = None,
    enforce: bool = cloud_containment.ENFORCE,
) -> None:
    """Log what would stop this operation from moving data; in enforce mode, refuse it.

    Call this before the first byte moves. The log carries the operation, its ID, the cell ID and a
    reason token, never a URL, a token or a claim. `cloud_containment.emit` logs the IDs only in
    their canonical form.
    """
    try:
        reasons = reasons_for(operation=operation, operation_id=operation_id, cell_id=cell_id, urls=urls,
                              grant=grant, bound=bound, env=env)
    except Exception:  # noqa: BLE001 -- report mode must not change what a Job does; enforce mode fails closed below.
        if enforce:
            raise
        reasons = [Reason.CHECK_FAILED]
    verdict = cloud_containment.refusal_verdict(enforce) if reasons else cloud_containment.Verdict.PASS
    for reason in reasons or [Reason.NONE]:
        cloud_containment.emit(cloud_containment.OPERATION_EVENT, verdict, operation=operation,
                               operation_id=operation_id, cell_id=cell_id, reason=reason)
    if reasons and enforce:
        raise OperationRefused(reasons[0])
