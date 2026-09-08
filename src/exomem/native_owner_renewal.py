"""Background renewal of explicitly approved native owner custody."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .governance.authorization_serving_membership import (
    MAX_ATTESTATION_TTL_SECONDS,
)

log = logging.getLogger(__name__)

_OWNER_ID_ENV = "EXOMEM_GITHUB_USER_ID"
_RENEWAL_INTERVAL_SECONDS = MAX_ATTESTATION_TTL_SECONDS // 4
_RETRY_SECONDS = 60
_MAX_WAIT_SECONDS = 60


class OwnerCustodyRenewal:
    """Renew native custody while durable same-authority consent remains valid."""

    def __init__(
        self,
        vault_root: Path,
        *,
        review_store_factory: Callable[[Path], Any] | None = None,
        renewer: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        shutdown_event: Any | None = None,
    ) -> None:
        if review_store_factory is None:
            from .native_owner_reviews import OwnerReviewStore

            review_store_factory = OwnerReviewStore
        if renewer is None:
            from .native_custody_renewal import renew_standalone_custody

            renewer = renew_standalone_custody
        self.vault_root = Path(vault_root).absolute()
        self._review_store_factory = review_store_factory
        self._renewer = renewer
        self._clock = clock
        self._shutdown = shutdown_event or threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._thread: threading.Thread | None = None

    def _pinned_owner_id(self) -> str | None:
        raw = os.environ.get(_OWNER_ID_ENV, "").strip()
        if not raw.isascii() or not raw.isdigit() or int(raw) < 1:
            return None
        return f"github:{raw}"

    def _matches_reviewed_installation(self, review: Any, owner_id: str) -> bool:
        from . import native_owner_maintenance, native_owner_preparation
        from .governance import authorization_custody

        body = review.body
        if not isinstance(body, Mapping):
            return False
        reviewed_unit = body.get("service_unit")
        setup = body.get("setup")
        if not isinstance(reviewed_unit, str) or not isinstance(setup, Mapping):
            return False
        reviewed_custody = native_owner_maintenance._custody_environment(  # noqa: SLF001
            body.get("custody_environment")
        )
        binding, managed_environment = native_owner_preparation.deployment_binding(
            self.vault_root
        )
        if binding["service_unit"] != reviewed_unit:
            return False

        pinned_owner = owner_id.removeprefix("github:")
        stable_environment = {
            "EXOMEM_VAULT_PATH": str(self.vault_root),
            "EXOMEM_GITHUB_USER_ID": pinned_owner,
            "EXOMEM_OWNER_SERVICE_UNIT": reviewed_unit,
            "EXOMEM_VOCABULARY_AUTHORITY_DIR": managed_environment.get(
                "EXOMEM_VOCABULARY_AUTHORITY_DIR", ""
            ),
            **reviewed_custody,
        }
        if any(
            managed_environment.get(name) != value or os.environ.get(name) != value
            for name, value in stable_environment.items()
        ):
            return False

        external = authorization_custody.load_external_custody(self.vault_root)
        membership_path, _membership, replica_id = (
            authorization_custody._standalone_membership_file(  # noqa: SLF001
                self.vault_root,
                external=external,
            )
        )
        actual_custody = {
            authorization_custody.KEYRING_FILE_ENV: str(external.keyring_path),
            authorization_custody.CONTROL_FILE_ENV: str(external.control_path),
            authorization_custody.MEMBERSHIP_FILE_ENV: str(membership_path),
            authorization_custody.REPLICA_ID_ENV: replica_id,
        }
        expected_keyring_digest = setup.get("keyring_digest")
        return (
            dict(reviewed_custody) == actual_custody
            and isinstance(expected_keyring_digest, str)
            and sha256(external.keyring).hexdigest() == expected_keyring_digest
        )

    def _renew_if_allowed(self, actual: int) -> bool:
        owner_id = self._pinned_owner_id()
        if owner_id is None:
            return True
        try:
            review = self._review_store_factory(self.vault_root).renewal_review(
                owner_id=owner_id,
                now=actual,
            )
            if review is None:
                return True
            if (
                review.action != "activation"
                or review.state != "completed"
                or review.body.get("renewal") != "same-authority"
                or not self._matches_reviewed_installation(review, owner_id)
            ):
                return False
            self._renewer(self.vault_root, now=actual)
        except Exception as exc:  # noqa: BLE001 - custody admission remains fail-closed
            log.warning(
                "native owner custody renewal unavailable (%s)",
                type(exc).__name__,
            )
            return False
        return True

    def _run(self, due: float) -> None:
        while not self._shutdown.is_set():
            remaining = due - self._clock()
            if remaining > 0:
                self._shutdown.wait(min(remaining, _MAX_WAIT_SECONDS))
                continue
            actual = int(self._clock())
            succeeded = self._renew_if_allowed(actual)
            due = actual + (
                _RENEWAL_INTERVAL_SECONDS if succeeded else _RETRY_SECONDS
            )

    def start(self) -> None:
        """Attempt renewal synchronously, then maintain it on wall-clock time."""

        with self._lock:
            if (
                self._started
                or self._shutdown.is_set()
                or self._pinned_owner_id() is None
                or not os.environ.get("EXOMEM_VOCABULARY_AUTHORITY_DIR", "").strip()
                or not os.environ.get("EXOMEM_OWNER_SERVICE_UNIT", "").strip()
            ):
                return
            self._started = True
            actual = int(self._clock())
            succeeded = self._renew_if_allowed(actual)
            due = actual + (
                _RENEWAL_INTERVAL_SECONDS if succeeded else _RETRY_SECONDS
            )
            thread = threading.Thread(
                target=self._run,
                args=(due,),
                name="exomem-owner-custody-renewal",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        """Join custody publication before service-lifespan teardown continues.

        This process-shutdown handshake is never a request-path wait. Returning
        while renewal still writes custody would let runtime teardown race it.
        """

        self._shutdown.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
