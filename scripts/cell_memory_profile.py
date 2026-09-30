#!/usr/bin/env python3
"""Profile an Exomem Cloud cell's resident memory at fixed points (bound-cell-memory D1).

Every later memory change is judged against this report, so it measures one real
cell rather than an estimate from code. Each stage runs in its own fresh process,
because resident memory is a property of a process's whole history:

- ``setup``: ``cell-init`` over an empty volume (the init container's path),
  then a synthetic vault from ``synth_vault.gen_dense_vault``. Not measured.
- ``build``: the first embedding-sidecar build, ``EmbeddingIndex.rebuild_all``,
  which ``maintain --fix --rebuild-embeddings`` runs on a host. It gets its own
  process because a cloud cell cannot run it: write-mode maintenance refuses over
  MCP (``MAINTENANCE_REQUIRES_CLI``) and the watcher's boot seed deliberately
  embeds nothing already on the volume. Points: import, model load, index build.
- ``cell``: the cloud server itself, in-process. ``server.build_server`` under
  ``EXOMEM_CLOUD_CELL=1`` with a locally generated bearer, its ASGI app and
  lifespan (``LocalRuntimeActivation``: drain, watcher, warm-up, reaper), and
  real MCP ``tools/call`` requests over ``/mcp``. Points: import, model load,
  cell ready, first hybrid find (``ask_memory``), first governed write
  (``remember``), and one reaper tick. The tick runs the reaper's own
  ``_reap_once`` over ``default_slots()`` with its clock advanced past the idle
  threshold, instead of waiting fifteen minutes for the daemon.

The model is loaded explicitly right after import, through the same singleton
(``embeddings.get_model``) the cell loads lazily, so its cost is isolated from
startup. ``--encoder stub`` swaps the encoder for a deterministic hashing one at
the model's declared width: every vault-proportional structure is the same
size, and no native model memory is held. CI uses it; ``--encoder onnx`` is the
real served model.

At each point the report records tracemalloc (current, peak since the previous
point, its own overhead, and the top allocation sites as file:line),
``/proc/self/smaps_rollup`` (Rss, Pss, Private_Dirty, Anonymous), the peak RSS
since the previous point (``VmHWM``, reset through ``/proc/self/clear_refs``),
glibc ``mallinfo2``, and the product's no-allocation cache and model residency
counters (``resource_status``). Derived per point:

- ``untraced_anon_bytes`` = Anonymous - tracemalloc current - tracemalloc
  overhead: anonymous memory no Python allocation accounts for.
- ``model_native_bytes``: the ONNX Runtime session and tokenizer, estimated as
  the growth of ``untraced_anon_bytes`` across the model-load point, while the
  model stays resident (zero after a reap, and always zero for the stub). The
  runtime libraries are imported before the import point, so the delta holds
  the session rather than library initialisation. It is the load-time
  footprint; arena growth on first inference lands in the residual below.
- ``unreturned_allocator_bytes`` = untraced anon - model native: design D1's
  RSS - tracemalloc - ONNX arena, taken over Anonymous rather than Rss, since
  file-backed pages (shared-library text, mapped files) are not allocator
  memory. ``file_backed_bytes`` = Rss - Anonymous restores the Rss form. The
  residual also holds other native heaps (SQLite page caches, extension
  modules' own allocations); glibc's own count of free-but-held bytes is
  ``glibc.free_bytes``, the direct reading to compare it with.

The report is content-free: numbers, code locations and fixed vocabulary only.
Before writing it, the harness scans it for the vault root, the scratch root,
every note stem and title, and the query and write text, and refuses to write a
report containing any of them.

Usage::

    uv run --frozen --extra embeddings-onnx python scripts/cell_memory_profile.py \\
        --notes 3000 --encoder onnx --out cell-memory.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"

SCHEMA_VERSION = 1
BUILD_POINTS = ("import", "model_load", "index_build")
CELL_POINTS = (
    "import",
    "model_load",
    "cell_ready",
    "first_hybrid_find",
    "first_governed_write",
    "reaper_tick",
)
#: Fixed request text. It is scanned for like vault content: none of it may
#: reach the report.
QUERY = "how do the topic notes relate to each other"
WRITE_TITLE = "Memory profile probe"
WRITE_BODY = "A probe note the memory profiler writes through the governed path."
#: Threads a cell starts for one-off startup or request follow-up work. A point
#: is sampled once none is alive (bounded by --settle-seconds), so it measures
#: the state the work leaves rather than a race with it.
TRANSIENT_THREADS = (
    "exomem-local-activation",
    "exomem-matrix-warm",
    "exomem-working-set-warm",
    "exomem-due-state-warm",
    "exomem-refs-rebuild",
    "exomem-recall-reembed",
)
_SMAPS_FIELDS = {
    "Rss": "rss_bytes",
    "Pss": "pss_bytes",
    "Private_Dirty": "private_dirty_bytes",
    "Anonymous": "anonymous_bytes",
}
MIB = 1024 * 1024


# ---------------------------------------------------------------- measurement


def parse_smaps_rollup(text: str) -> dict[str, int]:
    """The four D1 fields of a ``smaps_rollup`` text, in bytes."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        name = _SMAPS_FIELDS.get(key.strip())
        if name is not None:
            out[name] = int(rest.split()[0]) * 1024
    return out


def read_smaps_rollup() -> dict[str, int] | None:
    try:
        return parse_smaps_rollup(Path("/proc/self/smaps_rollup").read_text())
    except OSError:
        return None


def read_peak_rss() -> int | None:
    """``VmHWM``: peak RSS since process start or the last reset, in bytes."""
    try:
        match = re.search(r"^VmHWM:\s+(\d+)\s+kB", Path("/proc/self/status").read_text(), re.M)
    except OSError:
        return None
    return int(match.group(1)) * 1024 if match else None


def reset_peak_rss() -> bool:
    """Reset ``VmHWM`` so the next reading is this phase's peak (Linux 4.0+)."""
    try:
        Path("/proc/self/clear_refs").write_text("5")
    except OSError:
        return False
    return True


class _Mallinfo2(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena", "ordblks", "smblks", "hblks", "hblkhd",
            "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost",
        )
    ]


def glibc_mallinfo() -> dict[str, int] | None:
    """glibc's allocator totals across arenas, or None off glibc 2.33+."""
    try:
        fn = ctypes.CDLL(None).mallinfo2
    except (AttributeError, OSError):
        return None
    fn.restype = _Mallinfo2
    info = fn()
    return {
        "arena_bytes": int(info.arena),
        "mmap_bytes": int(info.hblkhd),
        "in_use_bytes": int(info.uordblks),
        "free_bytes": int(info.fordblks),
    }


_SITE_PREFIXES = (
    re.compile(r"^.*/(?:site|dist)-packages/"),
    re.compile(r"^.*/lib/python\d+\.\d+t?/"),
    re.compile(r"^.*/(?:src|scripts)/"),
)


def code_site(filename: str, lineno: int) -> str:
    """A package-relative ``file:line``: no absolute path reaches the report."""
    name = filename.replace("\\", "/")
    for prefix in _SITE_PREFIXES:
        stripped = prefix.sub("", name, count=1)
        if stripped != name:
            name = stripped
            break
    else:
        name = name.rsplit("/", 1)[-1]
    return f"{name}:{lineno}"


def tracemalloc_sample(top: int) -> dict[str, Any]:
    current, peak = tracemalloc.get_traced_memory()
    snapshot = tracemalloc.take_snapshot().filter_traces(
        (tracemalloc.Filter(False, tracemalloc.__file__),)
    )
    sites = [
        {
            "site": code_site(stat.traceback[0].filename, stat.traceback[0].lineno),
            "bytes": int(stat.size),
            "count": int(stat.count),
        }
        for stat in snapshot.statistics("lineno")[:top]
    ]
    return {
        "current_bytes": int(current),
        "phase_peak_bytes": int(peak),
        "overhead_bytes": int(tracemalloc.get_tracemalloc_memory()),
        "top": sites,
    }


def _without_vault_keys(value: Any) -> Any:
    """Drop the per-vault breakdowns, which are keyed by the vault's path."""
    if isinstance(value, Mapping):
        return {k: _without_vault_keys(v) for k, v in value.items() if k != "by_vault"}
    if isinstance(value, list):
        return [_without_vault_keys(v) for v in value]
    return value


def product_counters() -> dict[str, Any]:
    """The product's own residency counters: no loads, no sidecar reads."""
    from exomem import resource_status

    counters: dict[str, Any] = {
        "models": resource_status._model_residency(),
        "caches": _without_vault_keys(resource_status._cache_residency()),
    }
    contract = sys.modules.get("exomem.semantic_contract")
    if contract is not None:
        try:
            with contract._CORPUS_CONTEXT_CACHE_LOCK:
                contexts = [entry[1] for entry in contract._CORPUS_CONTEXT_CACHE.values()]
            counters["caches"]["semantic_corpus"] = {
                "contexts": len(contexts),
                "pages": sum(len(ctx.pages) for ctx in contexts),
            }
        except Exception:  # noqa: BLE001 - a counter must never fail a run
            counters["caches"]["semantic_corpus"] = None
    return counters


def sample(name: str, *, top: int, started: float, **facts: Any) -> dict[str, Any]:
    """One fixed point. Resets the phase peaks for the next point."""
    point = {
        "name": name,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "tracemalloc": tracemalloc_sample(top),
        "smaps_rollup": read_smaps_rollup(),
        "phase_peak_rss_bytes": read_peak_rss(),
        "glibc": glibc_mallinfo(),
        "threads": threading.active_count(),
        "counters": product_counters() if "exomem" in sys.modules else None,
        "facts": facts,
    }
    tracemalloc.reset_peak()
    reset_peak_rss()
    return point


# ------------------------------------------------------------------- derived


def derive(points: list[dict[str, Any]], *, native_model: bool = True) -> None:
    """Add the derived fields (module docstring) to each point, in place.

    ``native_model`` is False for the stub encoder, which holds no native memory,
    so nothing is attributed to a model.
    """
    def untraced(point: dict[str, Any]) -> int | None:
        smaps = point.get("smaps_rollup") or {}
        tm = point["tracemalloc"]
        if "anonymous_bytes" not in smaps:
            return None
        return smaps["anonymous_bytes"] - tm["current_bytes"] - tm["overhead_bytes"]

    by_name = {p["name"]: p for p in points}
    base = untraced(by_name["import"]) if "import" in by_name else None
    loaded = untraced(by_name["model_load"]) if "model_load" in by_name else None
    model_native = max(0, loaded - base) if base is not None and loaded is not None else None
    if not native_model:
        model_native = 0
    for point in points:
        smaps = point.get("smaps_rollup") or {}
        residual = untraced(point)
        models = ((point.get("counters") or {}).get("models")) or {}
        resident = bool(models.get("embeddings")) if point["name"] != "import" else False
        native = (model_native or 0) if resident else 0
        point["derived"] = {
            "untraced_anon_bytes": residual,
            "model_native_bytes": native if model_native is not None else None,
            "unreturned_allocator_bytes": (
                residual - native if residual is not None and model_native is not None else None
            ),
            "file_backed_bytes": (
                smaps["rss_bytes"] - smaps["anonymous_bytes"]
                if "rss_bytes" in smaps and "anonymous_bytes" in smaps
                else None
            ),
        }


# --------------------------------------------------------------- content-free


def content_leaks(report: Any, forbidden: Iterable[str]) -> list[str]:
    """Every forbidden string found in any key or string value of ``report``."""
    needles = sorted({s for s in forbidden if s and len(s) >= 4}, key=len, reverse=True)
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(str(key))
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            for needle in needles:
                if needle in value:
                    found.add(needle)

    visit(report)
    return sorted(found)


def vault_strings(vault_root: Path, scratch_root: Path, notes: Iterable[str]) -> set[str]:
    """What the report must never carry: roots, note stems and titles, probe text.

    ``notes`` are the synthetic notes' vault-relative paths. The init scaffold
    is left out on purpose: it is shipped product text with generic names
    ("index", "log") that would match code locations.
    """
    strings = {str(vault_root), str(scratch_root), QUERY, WRITE_TITLE, WRITE_BODY}
    for rel in notes:
        path = vault_root / rel
        strings.add(path.stem)
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2048]
        except OSError:
            continue
        match = re.search(r"^title:\s*(.+)$", head, re.M)
        if match:
            strings.add(match.group(1).strip().strip("\"'"))
    return strings


# --------------------------------------------------------------- environment


def cell_environment(scratch: Path, *, encoder: str, base: Mapping[str, str]) -> dict[str, str]:
    """The cloud image's and cellctl's cell environment, with all state in `scratch`.

    Every inherited ``EXOMEM_*`` (and legacy ``KB_MCP_*`` alias) is dropped so an
    operator's or test runner's own settings never shape the measurement.
    """
    env = {k: v for k, v in base.items() if not k.startswith(("EXOMEM_", "KB_MCP_"))}
    home = scratch / "home"
    env.update(
        {
            # cellctl manifests._runtime_env
            "EXOMEM_CLOUD_CELL": "1",
            "EXOMEM_CLOUD_CELL_ID": "cell-memory-profile",
            "EXOMEM_CLOUD_CELL_TOKEN": secrets.token_urlsafe(32),
            "EXOMEM_VAULT_PATH": str(scratch / "vault"),
            "TMPDIR": str(scratch / "tmp"),
            "EXOMEM_LOG_DIR": str(scratch / "tmp" / "exomem-logs"),
            # Dockerfile `hosted` and `cloud` stages
            "EXOMEM_CONTAINER_VARIANT": "cloud",
            "EXOMEM_DISABLE_RANKING": "1",
            "EXOMEM_EMBED_BACKEND": "onnx",
            "EXOMEM_RECALL_MODEL": "BAAI/bge-m3",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            # Harness isolation: nothing lands outside the scratch root.
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "EXOMEM_STATE_ROOT": str(scratch / "state"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(scratch / "state" / "writer-lease"),
        }
    )
    if encoder == "onnx":
        # The image bakes the model and runs offline; locally it is the HF cache.
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    return env


def _prepare_process(scratch: Path) -> None:
    """Import-path setup and the one redirect the cell's topology needs.

    Standalone custody reads its host-control root from the passwd home, not
    ``$HOME``; in the image that home is the cell's own ``/data/host``. Here it
    would be the operator's, so it is pointed into the scratch root instead.
    """
    for path in (str(SRC), str(SCRIPTS)):
        if path not in sys.path:
            sys.path.insert(0, path)
    (scratch / "tmp").mkdir(parents=True, exist_ok=True)
    from exomem.governance import authorization_custody

    host_root = scratch / "home" / ".local" / "state" / "exomem" / "standalone-host-control-v1"
    authorization_custody._standalone_host_control_root = lambda: host_root


class _StubEncoder:
    """A deterministic hashing encoder at the served model's declared width."""

    backend = "stub"
    device = "cpu"
    concurrent_encodes = True

    def __init__(self, model: str) -> None:
        from exomem import embedding_backend, recall_space

        self.dim = recall_space.declared_dim(model)
        self.profile = embedding_backend.EncoderProfile(
            model=model,
            pooling="cls",
            query_prefix="",
            passage_prefix="",
            max_seq=512,
            pad_token="<pad>",
            revision="0" * 40,
            quantization="stub",
            file_format="stub",
            artifact_digest="stub-memory-profile",
        )

    def encode(self, texts: list[str], **_kwargs: Any) -> Any:
        import numpy as np

        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode("utf-8")) % self.dim] += 1.0
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
        return out

    def release(self) -> None:
        pass


def _import_runtime_libraries() -> None:
    """Import the encoder's runtime libraries at the import point.

    The cell imports them lazily inside the first model load. Taking them here
    leaves the model-load delta holding the session and tokenizer alone, which
    is what the ONNX estimate is meant to be; what the libraries keep resident
    after a reap is then part of the import floor, where it belongs.
    """
    for name in ("onnxruntime", "tokenizers"):
        try:
            __import__(name)
        except ImportError:
            pass


def _load_model(encoder: str) -> None:
    from exomem import embedding_backend, embeddings

    if encoder == "stub":
        embedding_backend.ensure_served_artifact = lambda _name: None
        embedding_backend.load_encoder = lambda name, **_kw: _StubEncoder(name)
    embeddings.get_model()


def _settle(timeout: float, vault_root: Path | None = None) -> dict[str, Any]:
    """Wait for transient startup/follow-up threads and queued derived work."""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        busy = [t for t in threading.enumerate() if t.name in TRANSIENT_THREADS]
        pending = 0
        if vault_root is not None:
            from exomem import index_sync

            status = index_sync.deferred_work_status(vault_root)
            pending = int(status["semantic_upserts"]["count"]) + int(
                status["full_upserts"]["count"]
            )
        if not busy and not pending:
            return {"settled": True, "settle_seconds": round(time.monotonic() - started, 3)}
        time.sleep(0.25)
    return {"settled": False, "settle_seconds": round(time.monotonic() - started, 3)}


# -------------------------------------------------------------------- stages


def stage_setup(args: argparse.Namespace) -> dict[str, Any]:
    scratch = Path(args.scratch)
    _prepare_process(scratch)
    from synth_vault import gen_dense_vault

    from exomem import cell_init

    vault = scratch / "vault"
    cell_init.run_cell_init(vault, host_root=scratch / "home")
    rels = gen_dense_vault(vault, args.notes, links_per_note=args.links_per_note)
    markdown = sum((vault / rel).stat().st_size for rel in rels)
    # `rels` is for the parent's content scan only; it never enters the report.
    return {"notes": len(rels), "markdown_bytes": markdown, "rels": rels}


def stage_build(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    tracemalloc.start(1)
    scratch = Path(args.scratch)
    _prepare_process(scratch)
    from exomem import embeddings

    _import_runtime_libraries()
    points = [sample("import", top=args.top, started=started)]
    _load_model(args.encoder)
    points.append(sample("model_load", top=args.top, started=started))
    chunks = embeddings.get_embedding_index(scratch / "vault").rebuild_all()
    points.append(sample("index_build", top=args.top, started=started, chunks=int(chunks)))
    return {"points": points}


def _tool_result(response: Any) -> dict[str, Any]:
    body = response.json().get("result") or {}
    structured = body.get("structuredContent") or {}
    inner = structured.get("result", structured)
    return inner if isinstance(inner, dict) else {}


async def _cell_scenario(args: argparse.Namespace, points: list[dict], started: float) -> bool:
    import asyncio

    import httpx

    from exomem import model_reaper, server

    scratch = Path(args.scratch)
    vault = scratch / "vault"
    token = os.environ["EXOMEM_CLOUD_CELL_TOKEN"]
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "mcp-protocol-version": "2025-11-25",
    }
    ok = True
    mcp = server.build_server(require_auth=True)
    app = mcp.http_app(stateless_http=True, json_response=True)
    request_id = 0

    async def call(client: httpx.AsyncClient, name: str, arguments: dict) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await client.post("/mcp", headers=headers, json=payload)
        response.raise_for_status()
        return _tool_result(response)

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://cell.local", timeout=args.timeout
            ) as client:
                deadline = time.monotonic() + args.timeout
                ready = False
                while time.monotonic() < deadline:
                    if (await client.get("/health/ready")).status_code == 200:
                        ready = True
                        break
                    await asyncio.sleep(0.5)
                ok &= ready
                settle = await asyncio.to_thread(_settle, args.settle_seconds)
                points.append(
                    sample("cell_ready", top=args.top, started=started, ready=ready, **settle)
                )

                attempts, code, result = 0, None, {}
                while time.monotonic() < deadline:
                    attempts += 1
                    result = await call(client, "ask_memory", {"query": QUERY})
                    error = result.get("error") if result.get("success") is False else None
                    code = (error or {}).get("code")
                    if code != "RETRIEVAL_INDEX_WARMING":
                        break
                    retry_ms = (error or {}).get("retry_after_ms") or 250
                    await asyncio.sleep(max(0.25, retry_ms / 1000))
                served = code is None and isinstance(result.get("hits"), list)
                ok &= served
                settle = await asyncio.to_thread(_settle, args.settle_seconds)
                points.append(
                    sample(
                        "first_hybrid_find",
                        top=args.top,
                        started=started,
                        served=served,
                        error_code=code,
                        attempts=attempts,
                        hits=len(result.get("hits") or []),
                        degraded=sorted(str(lane) for lane in result.get("degraded") or []),
                        **settle,
                    )
                )

                result = await call(
                    client,
                    "remember",
                    {"content": WRITE_BODY, "title": WRITE_TITLE, "status": "draft"},
                )
                committed = result.get("state") == "committed"
                ok &= committed
                settle = await asyncio.to_thread(_settle, args.settle_seconds, vault)
                points.append(
                    sample(
                        "first_governed_write",
                        top=args.top,
                        started=started,
                        committed=committed,
                        error_code=(result.get("error") or {}).get("code"),
                        **settle,
                    )
                )

                threshold = model_reaper.idle_seconds()
                reaped = model_reaper._reap_once(
                    model_reaper.default_slots(), time.monotonic() + threshold + 1.0, threshold
                )
                points.append(
                    sample("reaper_tick", top=args.top, started=started, reaped=sorted(reaped))
                )
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()
    return bool(ok)


def stage_cell(args: argparse.Namespace) -> dict[str, Any]:
    import asyncio

    started = time.monotonic()
    tracemalloc.start(1)
    _prepare_process(Path(args.scratch))
    import httpx  # noqa: F401 - measured as part of the cell's import

    from exomem import embeddings, model_reaper, resource_status, server  # noqa: F401

    _import_runtime_libraries()
    points = [sample("import", top=args.top, started=started)]
    _load_model(args.encoder)
    points.append(sample("model_load", top=args.top, started=started))
    ok = asyncio.run(_cell_scenario(args, points, started))
    return {"points": points, "ok": ok}


STAGES = {"setup": stage_setup, "build": stage_build, "cell": stage_cell}


# -------------------------------------------------------------- orchestration


def _run_stage(stage: str, args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    out = Path(args.scratch) / f"{stage}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--stage", stage,
        "--scratch", args.scratch,
        "--stage-out", str(out),
        "--notes", str(args.notes),
        "--links-per-note", str(args.links_per_note),
        "--encoder", args.encoder,
        "--top", str(args.top),
        "--timeout", str(args.timeout),
        "--settle-seconds", str(args.settle_seconds),
    ]
    started = time.monotonic()
    proc = subprocess.run(command, env=env, cwd=str(ROOT), check=False)
    if proc.returncode != 0 or not out.exists():
        raise SystemExit(f"stage {stage} failed with exit code {proc.returncode}")
    result = json.loads(out.read_text(encoding="utf-8"))
    result["wall_seconds"] = round(time.monotonic() - started, 3)
    return result


def _git_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout.strip() or None


def run(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Run all stages in a fresh scratch root; return the report and any leaks."""
    scratch = Path(tempfile.mkdtemp(prefix="exomem-cell-memory-"))
    args.scratch = str(scratch)
    try:
        env = cell_environment(scratch, encoder=args.encoder, base=os.environ)
        setup = _run_stage("setup", args, env)
        notes = setup.pop("rels")
        build = _run_stage("build", args, env)
        cell = _run_stage("cell", args, env)
        derive(build["points"], native_model=args.encoder == "onnx")
        derive(cell["points"], native_model=args.encoder == "onnx")
        report = {
            "schema": SCHEMA_VERSION,
            "harness": "scripts/cell_memory_profile.py",
            "commit": _git_commit(),
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "cpus": os.cpu_count(),
            "encoder": args.encoder,
            "model": env["EXOMEM_RECALL_MODEL"],
            "vault": {**setup, "links_per_note": args.links_per_note},
            "build": build,
            "cell": cell,
            "ok": bool(cell.get("ok")),
        }
        leaks = content_leaks(report, vault_strings(scratch / "vault", scratch, notes))
        return report, leaks
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _mib(value: int | None) -> str:
    return "-" if value is None else f"{value / MIB:.1f}"


def render_table(report: dict[str, Any]) -> str:
    header = (
        f"{'stage':<5} {'point':<21} {'rss':>8} {'anon':>8} {'traced':>8} {'tm_peak':>8} "
        f"{'rss_peak':>8} {'model':>8} {'unret':>8} {'gfree':>7} {'pages':>6} {'rows':>7}"
    )
    lines = [
        f"vault: {report['vault']['notes']} notes, {_mib(report['vault']['markdown_bytes'])} MiB "
        f"markdown; encoder {report['encoder']} ({report['model']}); MiB unless noted",
        header,
        "-" * len(header),
    ]
    for stage in ("build", "cell"):
        for p in report[stage]["points"]:
            smaps = p.get("smaps_rollup") or {}
            caches = ((p.get("counters") or {}).get("caches")) or {}
            pages = ((caches.get("find") or {}).get("pages") or {}).get("entries")
            rows = ((caches.get("vector_matrices") or {}).get("embedding") or {}).get("rows")
            derived = p.get("derived") or {}
            lines.append(
                f"{stage:<5} {p['name']:<21} {_mib(smaps.get('rss_bytes')):>8} "
                f"{_mib(smaps.get('anonymous_bytes')):>8} "
                f"{_mib(p['tracemalloc']['current_bytes']):>8} "
                f"{_mib(p['tracemalloc']['phase_peak_bytes']):>8} "
                f"{_mib(p.get('phase_peak_rss_bytes')):>8} "
                f"{_mib(derived.get('model_native_bytes')):>8} "
                f"{_mib(derived.get('unreturned_allocator_bytes')):>8} "
                f"{_mib((p.get('glibc') or {}).get('free_bytes')):>7} "
                f"{'-' if pages is None else pages:>6} {'-' if rows is None else rows:>7}"
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--notes", type=int, default=3000, help="synthetic notes (default 3000)")
    parser.add_argument(
        "--links-per-note", type=int, default=25,
        help="wikilinks per note; 25 is the latency lanes' dense shape (default)",
    )
    parser.add_argument(
        "--encoder", choices=("onnx", "stub"), default="onnx",
        help="onnx: the served model; stub: deterministic hashing at its width",
    )
    parser.add_argument("--top", type=int, default=15, help="allocation sites per point")
    parser.add_argument("--timeout", type=float, default=900.0, help="cell readiness/find budget")
    parser.add_argument("--settle-seconds", type=float, default=120.0)
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--stage", choices=tuple(STAGES), help=argparse.SUPPRESS)
    parser.add_argument("--scratch", help=argparse.SUPPRESS)
    parser.add_argument("--stage-out", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage:
        result = STAGES[args.stage](args)
        Path(args.stage_out).write_text(json.dumps(result), encoding="utf-8")
        # A cell leaves worker threads behind; the stage's answer is written.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    report, leaks = run(args)
    if leaks:
        print(
            f"refusing to write the report: {len(leaks)} vault string(s) reached it",
            file=sys.stderr,
        )
        return 3
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(render_table(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
