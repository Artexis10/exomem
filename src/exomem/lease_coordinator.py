"""Reference strongly-consistent writer-lease coordinator.

Run with ``python -m exomem.lease_coordinator``. SQLite ``BEGIN IMMEDIATE``
serializes grants on one coordinator node; deploy a linearizable managed backend
behind the same HTTP contract when coordinator high availability is required.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class SQLiteLeaseStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leases ("
                "vault_id TEXT PRIMARY KEY, holder TEXT, expires_at REAL, "
                "fencing_token INTEGER NOT NULL DEFAULT 0)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _record(row, *, granted: bool = False) -> dict:  # noqa: ANN001
        return {
            "holder": row[0] if row else None,
            "expires_at": row[1] if row and row[0] else None,
            "fencing_token": int(row[2]) if row else 0,
            "granted": granted,
        }

    def acquire(self, vault_id: str, replica_id: str, ttl_seconds: float) -> dict:
        now = self.clock()
        expires = now + ttl_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder, expires_at, fencing_token FROM leases WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO leases(vault_id, holder, expires_at, fencing_token) VALUES (?, ?, ?, 1)",
                    (vault_id, replica_id, expires),
                )
                conn.execute("COMMIT")
                return {
                    "holder": replica_id,
                    "expires_at": expires,
                    "fencing_token": 1,
                    "granted": True,
                }
            holder, old_expiry, token = row
            active = holder is not None and old_expiry is not None and old_expiry > now
            if active and holder != replica_id:
                conn.execute("COMMIT")
                return self._record(row)
            new_token = token if active and holder == replica_id else token + 1
            conn.execute(
                "UPDATE leases SET holder = ?, expires_at = ?, fencing_token = ? WHERE vault_id = ?",
                (replica_id, expires, new_token, vault_id),
            )
            conn.execute("COMMIT")
            return {
                "holder": replica_id,
                "expires_at": expires,
                "fencing_token": new_token,
                "granted": True,
            }

    def renew(self, vault_id: str, replica_id: str, fencing_token: int, ttl_seconds: float) -> dict:
        now = self.clock()
        expires = now + ttl_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder, expires_at, fencing_token FROM leases WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
            valid = bool(
                row
                and row[0] == replica_id
                and row[1] is not None
                and row[1] > now
                and row[2] == fencing_token
            )
            if valid:
                conn.execute(
                    "UPDATE leases SET expires_at = ? WHERE vault_id = ?", (expires, vault_id)
                )
                row = (replica_id, expires, fencing_token)
            conn.execute("COMMIT")
            return self._record(row, granted=valid)

    def release(self, vault_id: str, replica_id: str, fencing_token: int) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder, expires_at, fencing_token FROM leases WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
            valid = bool(row and row[0] == replica_id and row[2] == fencing_token)
            if valid:
                conn.execute(
                    "UPDATE leases SET holder = NULL, expires_at = NULL WHERE vault_id = ?",
                    (vault_id,),
                )
                row = (None, None, fencing_token)
            conn.execute("COMMIT")
            return self._record(row, granted=valid)

    def status(self, vault_id: str) -> dict:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT holder, expires_at, fencing_token FROM leases WHERE vault_id = ?",
                (vault_id,),
            ).fetchone()
            if row and row[0] is not None and (row[1] is None or row[1] <= now):
                conn.execute(
                    "UPDATE leases SET holder = NULL, expires_at = NULL WHERE vault_id = ?",
                    (vault_id,),
                )
                row = (None, None, row[2])
            conn.execute("COMMIT")
            return self._record(row)


class SQLiteStateStore:
    """Small strongly-consistent JSON store used for encrypted OAuth records."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS shared_state ("
                "namespace TEXT NOT NULL, collection TEXT NOT NULL, key TEXT NOT NULL, "
                "value TEXT NOT NULL, expires_at REAL, PRIMARY KEY(namespace, collection, key))"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _collection(value: object) -> str:
        return str(value) if value is not None else ""

    def get(self, namespace: str, collection: object, key: str) -> tuple[dict | None, float | None]:
        now = self.clock()
        coll = self._collection(collection)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value, expires_at FROM shared_state WHERE namespace=? AND collection=? AND key=?",
                (namespace, coll, key),
            ).fetchone()
            if row and row[1] is not None and row[1] <= now:
                conn.execute(
                    "DELETE FROM shared_state WHERE namespace=? AND collection=? AND key=?",
                    (namespace, coll, key),
                )
                row = None
            conn.execute("COMMIT")
        if not row:
            return None, None
        remaining = None if row[1] is None else max(0.0, float(row[1]) - now)
        return json.loads(row[0]), remaining

    def put(
        self, namespace: str, collection: object, key: str, value: dict, ttl: float | None
    ) -> None:
        expires = None if ttl is None else self.clock() + ttl
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO shared_state(namespace, collection, key, value, expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(namespace, collection, key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
                (
                    namespace,
                    self._collection(collection),
                    key,
                    json.dumps(value, separators=(",", ":")),
                    expires,
                ),
            )
            conn.execute("COMMIT")

    def put_if_absent(
        self, namespace: str, collection: object, key: str, value: dict, ttl: float | None
    ) -> bool:
        """Atomically insert a state value unless a live value already exists."""
        now = self.clock()
        expires = None if ttl is None else now + ttl
        coll = self._collection(collection)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM shared_state "
                "WHERE namespace=? AND collection=? AND key=? "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (namespace, coll, key, now),
            )
            result = conn.execute(
                "INSERT OR IGNORE INTO shared_state"
                "(namespace, collection, key, value, expires_at) VALUES(?,?,?,?,?)",
                (
                    namespace,
                    coll,
                    key,
                    json.dumps(value, separators=(",", ":")),
                    expires,
                ),
            )
            conn.execute("COMMIT")
        return bool(result.rowcount)

    def list_keys(self, namespace: str, collection: object) -> list[str]:
        """List live opaque keys without returning encrypted values."""
        now = self.clock()
        coll = self._collection(collection)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM shared_state "
                "WHERE namespace=? AND collection=? "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (namespace, coll, now),
            )
            rows = conn.execute(
                "SELECT key FROM shared_state "
                "WHERE namespace=? AND collection=? ORDER BY key",
                (namespace, coll),
            ).fetchall()
            conn.execute("COMMIT")
        return [str(row[0]) for row in rows]

    def delete(self, namespace: str, collection: object, key: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM shared_state WHERE namespace=? AND collection=? AND key=?",
                (namespace, self._collection(collection), key),
            )
        return bool(result.rowcount)


def create_app(
    *, database: Path | None = None, bearer_token: str | None = None, clock=time.time
) -> Starlette:
    database = database or Path(
        os.environ.get("EXOMEM_LEASE_COORDINATOR_DB", "writer-leases.sqlite")
    )
    bearer_token = (
        bearer_token
        if bearer_token is not None
        else (os.environ.get("EXOMEM_LEASE_COORDINATOR_TOKEN", "").strip() or None)
    )
    store = SQLiteLeaseStore(database, clock=clock)
    state_store = SQLiteStateStore(database, clock=clock)

    def authorized(request: Request) -> bool:
        if not bearer_token:
            return True
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and secrets.compare_digest(
            header[7:].strip(), bearer_token
        )

    async def lease(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        vault_id = request.path_params["vault_id"]
        operation = request.path_params.get("operation")
        if request.method == "GET":
            return JSONResponse(store.status(vault_id))
        try:
            body = await request.json()
            replica_id = str(body["replica_id"])
            if operation == "acquire":
                result = store.acquire(vault_id, replica_id, _ttl(body))
            elif operation == "renew":
                result = store.renew(vault_id, replica_id, int(body["fencing_token"]), _ttl(body))
            elif operation == "release":
                result = store.release(vault_id, replica_id, int(body["fencing_token"]))
            else:
                return JSONResponse({"error": "unknown operation"}, status_code=404)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "invalid request"}, status_code=400)
        return JSONResponse(result)

    async def state(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        namespace = request.path_params["namespace"]
        operation = request.path_params["operation"]
        try:
            body = await request.json()
            collection = body.get("collection")
            keys = body.get("keys")
            if operation == "get":
                result = state_store.get(namespace, collection, str(body["key"]))[0]
            elif operation == "ttl":
                result = list(state_store.get(namespace, collection, str(body["key"])))
            elif operation == "put":
                state_store.put(
                    namespace, collection, str(body["key"]), dict(body["value"]), _state_ttl(body)
                )
                result = None
            elif operation == "put-if-absent":
                result = state_store.put_if_absent(
                    namespace,
                    collection,
                    str(body["key"]),
                    dict(body["value"]),
                    _state_ttl(body),
                )
            elif operation == "list-keys":
                result = state_store.list_keys(namespace, collection)
            elif operation == "delete":
                result = state_store.delete(namespace, collection, str(body["key"]))
            elif operation == "get-many":
                result = [state_store.get(namespace, collection, str(key))[0] for key in keys]
            elif operation == "ttl-many":
                result = [list(state_store.get(namespace, collection, str(key))) for key in keys]
            elif operation == "put-many":
                values = body["values"]
                if len(keys) != len(values):
                    raise ValueError
                ttl = _state_ttl(body)
                for key, value in zip(keys, values, strict=True):
                    state_store.put(namespace, collection, str(key), dict(value), ttl)
                result = None
            elif operation == "delete-many":
                result = sum(state_store.delete(namespace, collection, str(key)) for key in keys)
            else:
                return JSONResponse({"error": "unknown operation"}, status_code=404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid request"}, status_code=400)
        return JSONResponse({"result": result})

    return Starlette(
        routes=[
            Route("/v1/vaults/{vault_id:str}/lease", lease, methods=["GET"]),
            Route("/v1/vaults/{vault_id:str}/lease/{operation:str}", lease, methods=["POST"]),
            Route("/v1/state/{namespace:str}/{operation:str}", state, methods=["POST"]),
        ]
    )


def _ttl(body: dict) -> float:
    ttl = float(body["ttl_seconds"])
    if ttl <= 0 or ttl > 3600:
        raise ValueError("ttl_seconds must be between 0 and 3600")
    return ttl


def _state_ttl(body: dict) -> float | None:
    raw = body.get("ttl")
    if raw is None:
        return None
    ttl = float(raw)
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    return ttl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Exomem SQLite writer-lease coordinator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--database", type=Path, default=None)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(create_app(database=args.database), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
