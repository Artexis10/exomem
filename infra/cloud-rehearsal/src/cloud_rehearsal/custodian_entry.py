"""The key custodian's rehearsal entrypoint: the real watch loop, a double for B2.

Copied into the rehearsal cellctl image as a standalone module, so it imports nothing from the rest
of this package. It runs `cellctl.custodian.serve`, exactly as `cellctl.custodian.run` does, under the
chart's key-custodian identity. Only the B2 account is swapped: every key it "creates" is the S3
double's one credential, so a cell's real backup and restore Jobs authenticate against a live S3
endpoint. The credential arrives where the chart puts the custodian's own B2 key.
"""

from __future__ import annotations

import logging
import os

from cellctl.custodian import DEFAULT_HEARTBEAT_PATH, serve
from cellctl.storage.fake_b2_keys import FakeKeyStore
from cellctl.storage.interface import ObjectStorageKey
from kubernetes import client as k8s
from kubernetes import config as k8s_config


class SharedCredentialKeyStore(FakeKeyStore):
    """FakeKeyStore's listing, with the S3 double's one credential as every created key."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        super().__init__()
        self._credential = (key_id, key_secret)

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        created = super().create_prefix_key(cell_id)
        return ObjectStorageKey(
            key_id=self._credential[0], key_secret=self._credential[1], name_prefix=created.name_prefix
        )


def main() -> None:
    logging.basicConfig(level=os.environ.get("KEY_CUSTODIAN_LOG_LEVEL", "INFO"))
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)
    k8s_config.load_incluster_config()
    serve(
        k8s.CoreV1Api(),
        SharedCredentialKeyStore(os.environ["KEY_CUSTODIAN_B2_KEY_ID"], os.environ["KEY_CUSTODIAN_B2_APPLICATION_KEY"]),
        os.environ.get("KEY_CUSTODIAN_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH),
    )


if __name__ == "__main__":
    main()
