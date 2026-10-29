"""Worker-side client of the node media-slot lease (`cloud-multimodal-processing` D10).

A cell's media supervisor asks for a slot before it starts a media child and releases the
slot when the child exits. Anything short of a grant leaves the job pending, as a
memory-blocked job waits: a service that is unreachable, restarting or refusing never
counts as a stop and never reaches the tenant.

Only the cell ID, the engine names, the byte count and the job timeout of the next job
leave the cell. The wire constants live here, beside the caller, and the service
(`media_slot_lease`) imports them.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

URL_ENV = "EXOMEM_CLOUD_MEDIA_SLOT_URL"
CELL_ID_ENV = "EXOMEM_CLOUD_CELL_ID"
SLOT_PORT = 8768
ACQUIRE_PATH = "/internal/media-slots/acquire/v1"
POLL_PATH = "/internal/media-slots/poll/v1"
RELEASE_PATH = "/internal/media-slots/release/v1"

# Transport bounds, not policy: the supervisor thread waits this long on a dead service.
_REQUEST_SECONDS = 2.0
_REPLY_BYTES = 4096


class SlotClient:
    """One cell's view of its single entry: queued, held, or none."""

    def __init__(self, url: str, cell_id: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname or parts.port is None or not cell_id:
            raise ValueError("the media slot URL needs http://host:port and a cell ID")
        self._host, self._port, self._cell = parts.hostname, parts.port, cell_id
        self._lease: str | None = None
        #: The bytes the held entry reserves. Compared with bytes, never with names.
        self._anon_bytes = 0
        self._noted = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SlotClient | None:
        """The client of a cell whose URL cellctl rendered, or None: the lease is off."""
        values = os.environ if env is None else env
        url = (values.get(URL_ENV) or "").strip()
        if not url:
            return None
        return cls(url, (values.get(CELL_ID_ENV) or "").strip())

    def acquire(self, engine: str, anon_bytes: int, timeout_seconds: float) -> bool:
        """Whether this cell holds a slot for a child that needs `anon_bytes` now.

        `engine` names what the child loads, for the service log; `timeout_seconds` is the
        child's watchdog. False means wait and ask again. The first ask joins the queue;
        later asks keep the place, so they must come more often than the service's queue
        window.
        """
        if self._lease is not None and anon_bytes != self._anon_bytes:
            self.release()  # the entry was sized for another job
        try:
            if self._lease is None:
                status, reply = self._call(
                    ACQUIRE_PATH,
                    {
                        "cell": self._cell,
                        "engine": engine,
                        "anon_bytes": anon_bytes,
                        "timeout_seconds": timeout_seconds,
                    },
                )
            else:
                status, reply = self._call(POLL_PATH, {"cell": self._cell, "lease": self._lease})
        except (OSError, http.client.HTTPException, ValueError) as error:
            return self._note("unreachable", f"the service did not answer ({error!r})")
        if status == 404 and self._lease is not None:
            self._lease = None  # expired, or the service restarted: the next ask is a new request
            return self._note("lost", "the service dropped this cell's place")
        if status != 200:
            code = reply.get("code")
            return self._note(f"refused {status} {code}", f"the service refused ({status} {code})")
        lease, granted = reply.get("lease"), reply.get("granted")
        if not isinstance(lease, str) or not isinstance(granted, bool):
            return self._note("garbled", "the service answered with an unknown shape")
        self._lease, self._anon_bytes = lease, anon_bytes
        if not granted:
            return self._note("queued", "waiting in the queue")
        self._note("granted", "holding a slot")
        return True

    def release(self) -> None:
        """Give the slot back, or leave the queue. A lost release expires at the service."""
        lease = self._lease
        self._lease = None
        if lease is None:
            return
        try:
            self._call(RELEASE_PATH, {"cell": self._cell, "lease": lease})
        except (OSError, http.client.HTTPException, ValueError):
            pass

    def _note(self, state: str, message: str) -> bool:
        """Log a change of state once, so a polled wait does not fill the log. Always False."""
        if state != self._noted:
            self._noted = state
            log.info("media slot: %s", message)
        return False

    def _call(self, path: str, body: dict[str, object]) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=_REQUEST_SECONDS)
        try:
            connection.request(
                "POST",
                path,
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(_REPLY_BYTES + 1)
            if len(raw) > _REPLY_BYTES:
                raise ValueError("the media slot reply is too long")
            parsed = json.loads(raw) if raw else {}
            return response.status, parsed if isinstance(parsed, dict) else {}
        finally:
            connection.close()
