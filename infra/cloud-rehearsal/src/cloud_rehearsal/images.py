"""Every third-party image the rehearsal runs, pinned by digest.

K3s comes from the same gate contract the cellctl 3.10 suite reads, so both
live suites exercise one Kubernetes version. The S3 double is Versity
Gateway's POSIX backend: it speaks the S3 protocol restic uses and is
published on Docker Hub (MinIO no longer publishes there).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
K3S_GATE = REPO_ROOT / "infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json"

K3S = json.loads(K3S_GATE.read_text(encoding="utf-8"))["k3sImage"]
POSTGRES = "postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
PG_DATABASE_BOOT = "postgres"
S3_DOUBLE = "versity/versitygw:v1.8.0@sha256:30292fc2eeacc67a36993b01f7a7a5e3361a19cced0e80c1d71cfa2a4b0a2499"
TRAEFIK = "traefik:v3.5@sha256:16acb89c6db341182970d6fdafece31303b0a380a8ed7aa51682e225229bf1d2"
HELM = "alpine/helm:3.19@sha256:b1a7293aa1f89f1c234d223b9cfc441abe4af4b4534ae6ba11a4824f647771be"
BUSYBOX = "rancher/mirrored-library-busybox:1.37.0@sha256:101b4afd76732482eff9b95cae5f94bcf295e521fbec4e01b69c5421f3f3f3e5"

# What K3s v1.35 itself runs with traefik, servicelb and metrics-server
# disabled. Imported by tag: K3s's own manifests reference them by tag.
K3S_SYSTEM_IMAGES = (
    "rancher/mirrored-pause:3.6",
    "rancher/mirrored-coredns-coredns:1.14.4",
    "rancher/local-path-provisioner:v0.0.36",
    "rancher/mirrored-library-busybox:1.37.0",
)
