"""Single-use, content-bound withhold-tokens — the escalation affordance (D6).

A withheld or abstracted item may carry one escalation token, minted into its
notice. The token is a *capability reference*, not a bearer claim set: the wire
form `wh1.<jti>.<exp>.<hmac>` carries no path, no content, and no level, so a
token that leaks reveals nothing about what it unlocks. The bound claims
(audience, content fingerprints, max level) live in the per-machine sidecar
row that the `jti` names, and the HMAC over
`{jti, fingerprints, audience, exp}` is what makes that row unforgeable.

This is the second instantiation of the hosted transfer-grant-v2 + JTI pattern
(`hosted_transfer.mint_transfer_grant_v2` / `consume_transfer_jti`), reused
because it already answers the two hard parts: consume-once under a write lock
taken *before* the read, and a signing key that is per-machine rather than an
env secret.

Three properties worth stating outright:

- **Bound to content, not to paths.** Swapping the file after minting breaks
  the fingerprint match, so approval-by-substitution is impossible. The refusal
  offers a fresh escalation in one step rather than a re-confirm treadmill.
- **Box-local.** The HMAC key is a `SystemRandom` value in the sidecar meta —
  never an env secret, never synced. A vault copied to another machine cannot
  carry redeemable tokens with it, and "session" on stdio is the canonical
  audience plus lease identity, never a shared `session_id`.
- **Minting is a privilege.** An `empty` policy has nothing to escalate
  against; a `blocked` policy cannot be trusted to say what the ceiling is.
  Both refuse to mint, and neither creates a sidecar.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import policy as policy_module
from . import store
from .policy import DISCLOSURE_MAX, DISCLOSURE_MIN

TOKEN_VERSION = "wh1"
DEFAULT_TTL_SECONDS = 900  # 15 minutes: long enough to act on, short enough to forget
TOKENS_TABLE = "withhold_tokens"

#: Sidecar-meta key holding the per-machine HMAC secret. Stored with a
#: non-numeric prefix so SQLite's INTEGER column affinity on `meta.value`
#: can never coerce an all-digit hex string into an integer and silently drop
#: its leading zeros.
_KEY_META_KEY = "withhold_hmac_key"
_KEY_PREFIX = "whk1:"
_KEY_BYTES = 32

_KEY_CACHE: dict[str, bytes] = {}


class WithholdTokenError(Exception):
    """A mint or redemption refusal, carrying a stable code and a next step."""

    def __init__(self, code: str, reason: str, remediation: str | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.remediation = remediation


@dataclass(frozen=True)
class WithholdClaim:
    """The claims a `jti` names — recovered from the sidecar, not from the wire."""

    jti: str
    audience: str
    max_level: int
    fingerprints: tuple[str, ...]
    expires_at: int
    paths: tuple[str, ...] = ()


def clear_key_cache() -> None:
    _KEY_CACHE.clear()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _open(vault_root: Path) -> sqlite3.Connection:
    conn = store.open_connection(vault_root)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TOKENS_TABLE} ("
        "jti TEXT PRIMARY KEY, audience TEXT NOT NULL, max_level INTEGER NOT NULL, "
        "fingerprints TEXT NOT NULL, paths TEXT NOT NULL, expires_at INTEGER NOT NULL, "
        "minted_at REAL NOT NULL, consumed_at REAL)"
    )
    conn.commit()
    return conn


def _hmac_key(vault_root: Path) -> bytes:
    """The per-machine signing secret, created on first use and then stable.

    Deliberately reached only through the sidecar: there is no env override and
    no parameter, so there is no code path by which a caller-supplied value
    could become the signing key.
    """
    vault_root = Path(vault_root)
    cache_key = str(store.sidecar_path(vault_root))
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    conn = _open(vault_root)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_KEY_META_KEY,)
        ).fetchone()
        raw = str(row[0]) if row is not None else ""
        if not raw.startswith(_KEY_PREFIX):
            # `secrets.token_hex` is `SystemRandom` under the hood — the same
            # CSPRNG `sidecar_store.ensure_meta_table` uses for `instance`.
            raw = _KEY_PREFIX + secrets.token_hex(_KEY_BYTES)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (_KEY_META_KEY, raw),
            )
            conn.commit()
    finally:
        conn.close()
    key = bytes.fromhex(raw[len(_KEY_PREFIX) :])
    _KEY_CACHE[cache_key] = key
    return key


# ---------------------------------------------------------------------------
# Fingerprints and signing
# ---------------------------------------------------------------------------


def content_fingerprint(vault_root: Path, rel_path: str) -> str:
    """SHA-256 over the item's raw bytes.

    Bytes, not mtime: a rewrite with identical content is not a content
    change, and must not invalidate an approval a human already granted.
    """
    target = Path(vault_root) / rel_path
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        raise WithholdTokenError(
            "TOKEN_UNMINTABLE",
            f"cannot fingerprint {rel_path!r}: {exc}",
            "Re-run the query and escalate against the item as it exists now.",
        ) from exc


def _sign(
    key: bytes, *, jti: str, fingerprints: tuple[str, ...], audience: str, expires_at: int
) -> str:
    payload = json.dumps(
        {
            "jti": jti,
            "fingerprints": sorted(fingerprints),
            "audience": audience,
            "exp": expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _parse(token: str) -> tuple[str, int, str]:
    parts = str(token or "").split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        raise WithholdTokenError("TOKEN_INVALID", "token is not a wh1 capability")
    _, jti, raw_exp, signature = parts
    if not jti or not signature or not raw_exp.isdigit():
        raise WithholdTokenError("TOKEN_INVALID", "token is malformed")
    return jti, int(raw_exp), signature


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def mint(
    vault_root: Path,
    *,
    paths: list[str],
    audience: str,
    max_level: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint one escalation token over `paths` for `audience`, bounded at `max_level`.

    Refuses on an `empty` or `blocked` policy before opening the sidecar, so
    neither state can leave a token row (or even a sidecar file) behind.
    """
    vault_root = Path(vault_root)
    policy = policy_module.load(vault_root)
    if policy.empty:
        raise WithholdTokenError(
            "TOKEN_UNMINTABLE",
            "no governance policy is configured, so there is nothing to escalate against",
        )
    if policy.blocked:
        raise WithholdTokenError(
            "TOKEN_UNMINTABLE",
            "the governance policy is in a refused-compile state and cannot authorize "
            "an escalation",
            "Resolve the policy findings, then re-run the query to escalate.",
        )
    if not paths:
        raise WithholdTokenError("TOKEN_UNMINTABLE", "no items to bind the token to")
    if not (DISCLOSURE_MIN <= int(max_level) <= DISCLOSURE_MAX):
        raise WithholdTokenError("TOKEN_UNMINTABLE", "max_level is out of range")

    ordered = tuple(sorted(set(paths)))
    fingerprints = tuple(content_fingerprint(vault_root, p) for p in ordered)
    issued = int(time.time()) if now is None else int(now)
    expires_at = issued + max(1, int(ttl_seconds))
    jti = uuid.uuid4().hex
    signature = _sign(
        _hmac_key(vault_root),
        jti=jti,
        fingerprints=fingerprints,
        audience=audience,
        expires_at=expires_at,
    )

    conn = _open(vault_root)
    try:
        conn.execute(
            f"INSERT INTO {TOKENS_TABLE} "
            "(jti, audience, max_level, fingerprints, paths, expires_at, minted_at, "
            "consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                jti,
                audience,
                int(max_level),
                json.dumps(list(fingerprints)),
                json.dumps(list(ordered)),
                expires_at,
                float(issued),
            ),
        )
        # Opportunistic TTL sweep: piggy-backed on a write we are already
        # making, so expired rows never need a scheduler to disappear.
        conn.execute(f"DELETE FROM {TOKENS_TABLE} WHERE expires_at < ?", (issued,))
        conn.commit()
    finally:
        conn.close()
    return f"{TOKEN_VERSION}.{jti}.{expires_at}.{signature}"


def mint_quietly(
    vault_root: Path,
    *,
    paths: list[str],
    audience: str,
    max_level: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str | None:
    """`mint`, but `None` instead of raising — for notice assembly.

    A sidecar hiccup must degrade the escalation affordance, never the recall
    response that carries it.
    """
    try:
        return mint(
            vault_root,
            paths=paths,
            audience=audience,
            max_level=max_level,
            ttl_seconds=ttl_seconds,
        )
    except (WithholdTokenError, sqlite3.Error, OSError):
        return None


# ---------------------------------------------------------------------------
# Verify / redeem
# ---------------------------------------------------------------------------


def _load_claim(conn: sqlite3.Connection, jti: str) -> tuple[WithholdClaim, float | None]:
    row = conn.execute(
        f"SELECT audience, max_level, fingerprints, paths, expires_at, consumed_at "
        f"FROM {TOKENS_TABLE} WHERE jti = ?",
        (jti,),
    ).fetchone()
    if row is None:
        raise WithholdTokenError("TOKEN_UNKNOWN", "no such escalation token")
    audience, max_level, raw_fp, raw_paths, expires_at, consumed_at = row
    claim = WithholdClaim(
        jti=jti,
        audience=str(audience),
        max_level=int(max_level),
        fingerprints=tuple(json.loads(raw_fp)),
        expires_at=int(expires_at),
        paths=tuple(json.loads(raw_paths)),
    )
    return claim, consumed_at


def _check_signature(
    vault_root: Path, claim: WithholdClaim, *, audience: str, exp: int, signature: str
) -> None:
    expected = _sign(
        _hmac_key(vault_root),
        jti=claim.jti,
        fingerprints=claim.fingerprints,
        audience=claim.audience,
        expires_at=claim.expires_at,
    )
    if not hmac.compare_digest(expected, signature):
        raise WithholdTokenError("TOKEN_INVALID", "token signature does not verify")
    if exp != claim.expires_at:
        raise WithholdTokenError("TOKEN_INVALID", "token expiry does not match its record")
    if not hmac.compare_digest(claim.audience, audience):
        raise WithholdTokenError("TOKEN_INVALID", "token was issued to a different audience")


def _check_content(vault_root: Path, claim: WithholdClaim) -> None:
    """Refuse when the approved bytes are gone — with a one-step next move."""
    current: list[str] = []
    for rel_path in claim.paths:
        try:
            current.append(
                hashlib.sha256((Path(vault_root) / rel_path).read_bytes()).hexdigest()
            )
        except OSError:
            current.append("")
    if sorted(current) != sorted(claim.fingerprints):
        raise WithholdTokenError(
            "TOKEN_CONTENT_DRIFT",
            "the item's content changed after this escalation was approved",
            "Request a fresh escalation for the item as it exists now; the "
            "previous approval covered different content.",
        )


def verify(
    vault_root: Path, token: str, *, audience: str, now: int | None = None
) -> WithholdClaim:
    """Validate a token without consuming it. Raises `WithholdTokenError`."""
    vault_root = Path(vault_root)
    jti, exp, signature = _parse(token)
    if not store.sidecar_path(vault_root).exists():
        raise WithholdTokenError("TOKEN_UNKNOWN", "no such escalation token")
    conn = _open(vault_root)
    try:
        claim, _consumed = _load_claim(conn, jti)
    finally:
        conn.close()
    _check_signature(vault_root, claim, audience=audience, exp=exp, signature=signature)
    moment = int(time.time()) if now is None else int(now)
    if moment > claim.expires_at:
        raise WithholdTokenError(
            "TOKEN_EXPIRED",
            "this escalation has expired",
            "Re-run the query to request a fresh escalation.",
        )
    return claim


def redeem(
    vault_root: Path, token: str, *, audience: str, now: int | None = None
) -> WithholdClaim:
    """Validate and consume exactly once.

    Ordering is load-bearing: signature, expiry, and content are checked
    BEFORE the consuming update, so a refused redemption never burns a
    still-valid token — otherwise a transiently drifting file would silently
    destroy an approval.
    """
    vault_root = Path(vault_root)
    claim = verify(vault_root, token, audience=audience, now=now)
    _check_content(vault_root, claim)

    moment = time.time() if now is None else float(now)
    conn = _open(vault_root)
    try:
        # BEGIN IMMEDIATE takes the write lock before the read, so two racing
        # redemptions cannot both observe `consumed_at IS NULL`. This is the
        # `consume_transfer_jti` semantics, kept local to the vault's sidecar.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                f"UPDATE {TOKENS_TABLE} SET consumed_at = ? "
                "WHERE jti = ? AND consumed_at IS NULL",
                (moment, claim.jti),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                raise WithholdTokenError(
                    "TOKEN_CONSUMED",
                    "this escalation has already been used",
                    "Re-run the query to request a fresh escalation.",
                )
            conn.execute("COMMIT")
        except WithholdTokenError:
            raise
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return claim


def sweep(vault_root: Path, *, now: int | None = None) -> int:
    """Delete expired rows; returns how many. No-op when no sidecar exists."""
    vault_root = Path(vault_root)
    if not store.sidecar_path(vault_root).exists():
        return 0
    moment = int(time.time()) if now is None else int(now)
    conn = _open(vault_root)
    try:
        cursor = conn.execute(
            f"DELETE FROM {TOKENS_TABLE} WHERE expires_at < ?", (moment,)
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def describe(claim: WithholdClaim) -> dict[str, Any]:
    """Public, content-free description of what a token would unlock."""
    return {"max_level": claim.max_level, "expires_at": claim.expires_at}
