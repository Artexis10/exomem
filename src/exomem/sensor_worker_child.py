"""Entry point for the disposable sensor worker child.

Locks the vault's sensor slot before importing any model code, lowers its own
priority, and runs `sensor_worker.run_child`. The supervisor has already forced
CPU (`CUDA_VISIBLE_DEVICES=""`) and one thread in this process's environment.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from .runtime_resources import bootstrap

    bootstrap()
    from .logging_config import configure_logging, resolve_log_dir

    configure_logging(resolve_log_dir(), process="sensor")

    parser = argparse.ArgumentParser(prog="python -m exomem.sensor_worker_child")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--cpu", type=float, required=True)
    parser.add_argument("--judgements", type=int, required=True)
    parser.add_argument("--idle-seconds", type=float, required=True)
    args = parser.parse_args(argv)

    from . import sensing_ledger
    from .media_worker_child import _VaultLock

    vault_root = Path(args.vault).resolve()
    lock = _VaultLock(sensing_ledger.ledger_path(vault_root).with_name("sensor-worker.lock"))
    if not lock.acquire():
        return 0
    try:
        from .runtime_resources import lower_background_priority

        lower_background_priority()
        from .sensor_worker import run_child

        return run_child(
            vault_root,
            parent_pid=args.parent_pid,
            cpu_allotment=max(0.0, args.cpu),
            judgement_allotment=max(0, args.judgements),
            idle_seconds=max(0.1, args.idle_seconds),
        )
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
