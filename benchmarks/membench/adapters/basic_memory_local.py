"""Basic Memory local adapter: drives the ``bm`` CLI through a runner seam.

Driven command contract (the shapes the fake-runner tests pin down):

- availability probe:  ``bm --version``
- ingest:              ``bm project add <project> <native_dir>`` then
                       ``bm reindex --search -p <project>``
- search:              ``bm tool search-notes --query <q> --project <project>
                       --page-size <limit> --json`` returning
                       ``{"results": [{file_path|permalink, title,
                       matched_chunk|content, score?}, ...]}``

Isolation: every invocation runs with ``BASIC_MEMORY_CONFIG_DIR`` pointing at
a benchmark-owned directory under the adapter workdir, and with
``BASIC_MEMORY_HOME`` / ``BASIC_MEMORY_CLOUD_MODE`` removed — the operator's
personal Basic Memory config (including cloud-mode routing) is never consulted
or mutated (same rationale as the sibling Track-A provider).

LIVE EXECUTION IS USER-RUN: the ``bm`` executable cannot run inside this
sandbox (it spawns ``uv`` against a read-only cache). The executable path is
injectable (constructor ``command=`` or the ``BM_COMMAND`` env var), ``setup``
probes availability and reports honest unavailability via
:class:`AdapterUnsupported` when the binary is absent, and all tests drive the
injectable ``runner`` seam with a fake subprocess.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    register_adapter,
)
from membench.ids import sentinels_in

COMMAND_TIMEOUT_SECONDS = 120.0


class _ProcLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], dict[str, str]], _ProcLike]


def _subprocess_runner(argv: list[str], env: dict[str, str]) -> _ProcLike:
    import subprocess

    return subprocess.run(  # noqa: S603 - benchmark-owned argv
        argv,
        env=env,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


class BasicMemoryLocalAdapter:
    name = "basic-memory-local"
    supports_group_reuse = False

    def __init__(
        self, *, command: str | None = None, runner: Runner | None = None
    ) -> None:
        self.command = command or os.environ.get("BM_COMMAND", "bm")
        self._runner: Runner = runner or _subprocess_runner
        self._workdir: Path | None = None
        self._env: dict[str, str] | None = None
        self._profile: Profile | None = None
        self._project: str | None = None
        self._probed_version: str | None = None

    # -- lifecycle --------------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.FILE_DROP, Capability.SEARCH})

    def _isolated_env(self, workdir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("BASIC_MEMORY_HOME", None)
        env.pop("BASIC_MEMORY_CLOUD_MODE", None)
        config_dir = workdir / "bm-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        env["BASIC_MEMORY_CONFIG_DIR"] = str(config_dir)
        return env

    def _run(self, args: list[str]) -> _ProcLike:
        if self._env is None:
            raise AdapterEnvironmentError("adapter not set up")
        return self._runner([self.command, *args], self._env)

    def setup(self, workdir: Path, profile: Profile) -> None:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self._workdir = workdir
        self._profile = profile
        self._env = self._isolated_env(workdir)
        self._project = f"membench-{workdir.name}"
        try:
            probe = self._run(["--version"])
        except (FileNotFoundError, OSError) as exc:
            self._env = None
            raise AdapterUnsupported(
                f"bm executable {self.command!r} unavailable ({exc}); live runs are "
                "user-run: install Basic Memory and set BM_COMMAND to the executable"
            ) from exc
        if probe.returncode != 0:
            self._env = None
            raise AdapterUnsupported(
                f"bm probe failed (exit {probe.returncode}): {probe.stderr.strip()[:200]}"
            )
        self._probed_version = probe.stdout.strip() or None

    def cleanup(self) -> None:
        self._env = None
        self._project = None

    # -- ingest -----------------------------------------------------------
    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        if self._project is None:
            raise AdapterEnvironmentError("adapter not set up")
        results: list[OpResult] = []
        steps: list[tuple[str, list[str]]] = [
            ("project_add", ["project", "add", self._project, str(native_dir)]),
            ("reindex", ["reindex", "--search", "-p", self._project]),
        ]
        for seq, (op, args) in enumerate(steps):
            started = time.perf_counter()
            try:
                proc = self._run(args)
                ok = proc.returncode == 0
                detail = None if ok else proc.stderr.strip()[:300]
            except Exception as exc:  # recorded, stays in denominators
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(
                OpResult(
                    seq=seq,
                    op=op,
                    source_id=None,
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )
        return results

    # -- search -----------------------------------------------------------
    def search(self, query: str, limit: int) -> list[Hit]:
        if self._project is None:
            raise AdapterEnvironmentError("adapter not set up")
        proc = self._run(
            [
                "tool",
                "search-notes",
                "--query",
                query,
                "--project",
                self._project,
                "--page-size",
                str(limit),
                "--json",
            ]
        )
        if proc.returncode != 0:
            raise AdapterEnvironmentError(
                f"bm search failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}"
            )
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise AdapterEnvironmentError(f"bm search emitted non-JSON output: {exc}") from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        hits: list[Hit] = []
        for rank, row in enumerate(rows or [], start=1):
            if not isinstance(row, dict):
                continue
            path = row.get("file_path") or row.get("permalink")
            if not isinstance(path, str):
                continue
            text = row.get("matched_chunk") or row.get("content") or ""
            title = row.get("title")
            hits.append(
                Hit(
                    rank=rank,
                    provider_path=path,
                    title=title if isinstance(title, str) else None,
                    excerpt=(text[:200] or None) if isinstance(text, str) else None,
                    sentinels=tuple(sentinels_in(text if isinstance(text, str) else "")),
                    raw=row,
                    text=text if isinstance(text, str) and text else None,
                )
            )
        return hits

    # -- state export ------------------------------------------------------
    def export_state(self) -> StateExport:
        raise AdapterUnsupported("basic-memory CLI profile does not declare STATE_EXPORT")

    def version_info(self) -> dict[str, str]:
        info = {
            "provider": self.name,
            "bm_command": self.command,
            "isolation": "BASIC_MEMORY_CONFIG_DIR (fresh per run)",
        }
        if self._probed_version:
            info["bm_version"] = self._probed_version
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("basic-memory-local", lambda **kw: BasicMemoryLocalAdapter(**kw))
