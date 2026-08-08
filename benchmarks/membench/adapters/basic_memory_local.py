"""Basic Memory local adapter: drives the ``bm`` CLI through a runner seam.

Driven command contract (the shapes the fake-runner tests pin down):

- availability probe:  ``bm --version``
- ingest:              ``bm project add <project> <native_dir>`` then
                       ``bm reindex --search -p <project>``
- search:              ``bm tool search-notes <q> --project <project>
                       --page-size <limit> --local`` returning
                       ``{"results": [{file_path|permalink, title,
                       matched_chunk|content, score?}, ...]}``

Isolation: every invocation runs with ``BASIC_MEMORY_CONFIG_DIR`` pointing at
a benchmark-owned directory under the adapter workdir, and with
``BASIC_MEMORY_HOME`` / ``BASIC_MEMORY_CLOUD_MODE`` removed — the operator's
personal Basic Memory config (including cloud-mode routing) is never consulted
or mutated (same rationale as the sibling Track-A provider).

LIVE EXECUTION WORKS HERE (verified against bm 0.22.1 on 2026-08-05); the
earlier "cannot run inside this sandbox" note was stale and had never been
re-tested. The executable path is
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
    #: Altitudes this adapter can honour: writes source notes, and conclusion notes with typed relations.
    supported_altitudes = frozenset({"raw_source", "compiled"})

    def __init__(
        self,
        *,
        altitude: str = "raw_source",
        command: str | None = None,
        runner: Runner | None = None,
        mode: str = "leaf",
        search_style: str = "neutral",
    ) -> None:
        #: Altitude this run asked for; validated by `ingestion_altitude`.
        self.altitude = altitude
        # The CLI hands every provider the run-shape kwargs, so a contender
        # must accept them and say honestly which it can honour rather than
        # silently ignoring one and letting the manifest claim a shape that
        # was never applied.
        #
        # `mode`: this adapter drives the `bm` CLI, which is the leaf-equivalent
        # surface. There is no wire (MCP) surface to measure, so `wire` is
        # refused rather than quietly served as leaf — that would report a
        # transport comparison that never happened.
        if mode != "leaf":
            raise AdapterUnsupported(
                f"basic-memory-local has no {mode!r} surface; it drives the bm CLI "
                "(leaf-equivalent). Run it with --mode leaf."
            )
        # `search_style`: exomem's neutral/product-default split exists to
        # separate raw retrieval from product tuning. Basic Memory exposes one
        # search surface, so the distinction is not applicable — recorded as
        # such instead of implying a tuning choice was made.
        if search_style not in ("neutral", "product-default"):
            raise AdapterUnsupported(f"unknown search_style {search_style!r}")
        self.mode = mode
        self.search_style = search_style
        self.command = command or os.environ.get("BM_COMMAND", "bm")
        self._runner: Runner = runner or _subprocess_runner
        self._workdir: Path | None = None
        self._env: dict[str, str] | None = None
        self._profile: Profile | None = None
        self._project: str | None = None
        self._probed_version: str | None = None

    # -- lifecycle --------------------------------------------------------
    @property
    def ingestion_altitude(self) -> str:
        """The altitude this run selected; validated against what we support.

        Refusing here rather than degrading is the 4b.29 rule applied to
        altitude: a run that cannot apply a tier to a contender must say so, not
        quietly measure it at a different one.
        """

        if self.altitude not in self.supported_altitudes:
            raise AdapterUnsupported(
                f"{self.name} cannot honour altitude {self.altitude!r}; "
                f"supports {sorted(self.supported_altitudes)}"
            )
        return self.altitude

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.FILE_DROP, Capability.SEARCH})

    def _isolated_env(self, workdir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("BASIC_MEMORY_HOME", None)
        env.pop("BASIC_MEMORY_CLOUD_MODE", None)
        config_dir = workdir / "bm-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        env["BASIC_MEMORY_CONFIG_DIR"] = str(config_dir)
        # bm 0.22.x resolves an embedding model (BAAI/bge-small-en-v1.5) for
        # search. With the operator's HF cache unwritable it fails on a cache
        # *permission* error rather than on network, which reads like "search
        # is broken" and would invalidate every contender run. A benchmark-owned
        # cache under the workdir removes that failure mode; the caller's HF_HOME
        # is honoured when already set, so a warm shared cache still wins.
        if not env.get("HF_HOME"):
            hf_home = workdir / "hf-cache"
            hf_home.mkdir(parents=True, exist_ok=True)
            env["HF_HOME"] = str(hf_home)
        env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
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
                # QUERY is positional as of bm 0.22.x — `--query` was removed,
                # and so was `--json`: search already emits JSON on stdout.
                # Verified live against 0.22.1; the previous flags produced
                # `No such option: --query`, exit 2.
                query,
                "--project",
                self._project,
                "--page-size",
                str(limit),
                # Force local API routing so a cloud-mode config on the host
                # can never silently redirect a contender's measurement.
                "--local",
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
