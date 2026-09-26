#!/usr/bin/env python3
"""A deliberately-never-ready variant of the 3.10 stand-in cell image, used
only to force a canary failure for the upgrade-then-restore scenario in
tests/test_k3s_integration.py. `cell-init` behaves identically to the good
image (so the StatefulSet's init container still succeeds); the runtime
process, in place of serving HTTP, just sleeps forever without ever binding
port 8765, so `/health/ready` never answers and the pod never becomes Ready
-- exactly the failure mode D6's upgrade-readiness deadline exists to catch.
"""
from __future__ import annotations

import os
import sys
import time

VAULT_DEFAULT = "/data/vault"
HOST_DEFAULT = "/data/host"


def _mkdir_owner_only(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def cell_init() -> None:
    args = sys.argv[2:]
    vault = VAULT_DEFAULT
    if "--vault" in args:
        vault = args[args.index("--vault") + 1]
    _mkdir_owner_only(vault)
    _mkdir_owner_only(HOST_DEFAULT)
    sys.exit(0)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "cell-init":
        cell_init()
        return
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
