"""cellctl's rehearsal entrypoint: the real reconcile loop, two doubles.

Copied into the rehearsal cellctl image as a standalone module, so it
imports nothing from the rest of this package. It builds every setting
through `cellctl.main`'s own builders from the chart-rendered pod
environment and calls the real `run_loop`, exactly as `cellctl.main.run`
does. Only the two external services cellctl calls are swapped:

- B2 key management stays the in-process `FakeB2`: every per-cell key it
  "creates" is the S3 double's one credential, so the real backup and
  restore Jobs authenticate against a live S3 endpoint. Object listing and
  deletion for D10's absence proof go to that same live endpoint, so the
  deletion scenario proves the prefix really is empty;
- Hetzner's read-only volume listing is `FakeHetznerVolumeProvider`: the
  rehearsal's local-path volumes have no Hetzner id.
"""

from __future__ import annotations

import asyncio
import logging
import os

import boto3
from botocore.config import Config
from cellctl.k8s_client import ClusterClient
from cellctl.main import build_cluster_config, build_reconcile_config, build_secrets_config
from cellctl.reconcile import run_loop
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.interface import ObjectVersion
from kubernetes import client as k8s
from kubernetes import config as k8s_config


class S3BackedFakeB2(FakeB2):
    """FakeB2's key management, the live S3 double's objects."""

    def __init__(self, *, endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
        super().__init__(fixed_credentials=(access_key, secret_key))
        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
        )

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        versions: list[ObjectVersion] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                versions.append(ObjectVersion(key=item["Key"], version_id="null"))
        return versions

    def delete_object_version(self, version: ObjectVersion) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=version.key)


def main() -> None:
    logging.basicConfig(level=os.environ.get("CELLCTL_LOG_LEVEL", "INFO"))
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    k8s_config.load_incluster_config()
    cluster_config = build_cluster_config()
    object_storage = S3BackedFakeB2(
        endpoint=cluster_config.object_storage_endpoint,
        bucket=cluster_config.object_storage_bucket,
        access_key=os.environ["REHEARSAL_S3_ACCESS_KEY"],
        secret_key=os.environ["REHEARSAL_S3_SECRET_KEY"],
    )
    asyncio.run(
        run_loop(
            os.environ["CELLCTL_DATABASE_DSN"],
            ClusterClient(k8s.ApiClient()),
            object_storage,
            FakeHetznerVolumeProvider(),
            build_secrets_config(),
            cluster_config,
            config=build_reconcile_config(),
            heartbeat_path=os.environ.get("CELLCTL_HEARTBEAT_PATH", "/tmp/cellctl-heartbeat"),
        )
    )


if __name__ == "__main__":
    main()
