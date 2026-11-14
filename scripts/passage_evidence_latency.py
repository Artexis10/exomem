"""Passage qualification through the maintained MCP arm and canary clock."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shlex
import shutil
import sqlite3
import statistics
import sys
import time
import tomllib
import uuid
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import mcp_read_latency_gate
from mcp_read_latency_gate import SESSION_ISSUE, Arm, worker_settings
from synth_vault import reference_source

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "benchmarks/passage-evidence/profile.json"
REFERENCE = ROOT / "benchmarks/recall-latency"


class CloudArm(Arm):
    """Keep the existing arm lifecycle, using the Cloud profile's bearer protocol."""

    def run_python(self, code: str, *arguments: str) -> str:
        if code == SESSION_ISSUE:
            return self.env["EXOMEM_CLOUD_CELL_TOKEN"]
        return super().run_python(code, *arguments)

    async def start(self, *, quiescence_limit: float) -> dict:
        previous = mcp_read_latency_gate.WORKER_GUARD
        # The existing launch guard is the narrow seam that runs inside the actual worker.
        mcp_read_latency_gate.WORKER_GUARD = (
            "from passage_evidence_latency import record_worker_launch\nrecord_worker_launch()\n"
            + previous)
        try:
            return await super().start(quiescence_limit=quiescence_limit)
        finally:
            mcp_read_latency_gate.WORKER_GUARD = previous


class CapturingClient:
    """Retain the parsed terminal without a second clock or a second call."""

    def __init__(self, client):
        self.client = client
        self.result = None

    async def call_tool(self, *args, **kwargs):
        self.result = await self.client.call_tool(*args, **kwargs)
        return self.result


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def manifest() -> dict:
    paths = [*sorted(path for path in (ROOT / "src/exomem").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts),
             *sorted((ROOT / "scripts").glob("*.py")), PROFILE,
             *sorted((ROOT / "benchmarks/recall_parity").glob("*.py")),
             REFERENCE / "profile.json", REFERENCE / "cases.json", REFERENCE / "prose.txt",
             ROOT / "pyproject.toml", ROOT / "uv.lock"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def freeze_source(root: Path) -> dict:
    hashes = manifest()
    for relative in hashes:
        destination = root / "frozen-source" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != hashes[relative]:
            raise RuntimeError("source changed during its byte snapshot")
    write_json(root / "source-before.json", hashes)
    return hashes


def runtime(profile: dict) -> dict:
    from packaging.utils import canonicalize_name

    import exomem

    installed = {}
    for distribution in importlib.metadata.distributions():
        installed.setdefault(canonicalize_name(distribution.metadata["Name"]), distribution.version)
    locked = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    versions = {}
    for package in locked:
        versions.setdefault(canonicalize_name(package["name"]), set()).add(package["version"])
    mismatches = {name: version for name, version in installed.items()
                  if name != "exomem" and version not in versions.get(name, set())}
    cgroup = Path("/sys/fs/cgroup")
    limits = {name: (cgroup / name).read_text().strip()
              for name in ("cpu.max", "memory.max", "memory.swap.max", "cpuset.cpus.effective")}
    quota, period = limits["cpu.max"].split()
    if (mismatches or not Path(exomem.__file__).is_relative_to(ROOT / "src")
            or quota == "max" or int(quota) != int(period) * profile["reference_cpus"]
            or limits["memory.max"] != str(profile["memory_limit_gib"] * 1024**3)
            or limits["memory.swap.max"] != "0"
            or len(os.sched_getaffinity(0)) != profile["reference_cpus"]):
        raise RuntimeError(f"runtime contract differs: dependencies={mismatches}, limits={limits}, origin={exomem.__file__}")
    return {"python": sys.version, "module_origin": exomem.__file__, "dependencies": installed,
            "dependency_mismatches": mismatches, "cgroup": limits, "affinity": sorted(os.sched_getaffinity(0))}


def record_worker_launch() -> None:
    """Attest identity in the process that will execute the maintained worker entry."""
    receipt = os.environ.pop("PASSAGE_WORKER_RECEIPT")
    profile = json.loads(PROFILE.read_text())
    modules = {name: importlib.util.find_spec(name).origin for name in
               ("exomem", "exomem.service_manager", "fastmcp", "httpx2", "numpy", "onnxruntime")}
    write_json(Path(receipt), {"pid": os.getpid(), "ppid": os.getppid(),
               "executable": sys.executable, "prefix": sys.prefix,
               "proc_exe": str(Path("/proc/self/exe").resolve()), "module_origins": modules,
               "runtime": runtime(profile), "source": manifest(),
               "matrix_bytes": os.environ.get("EXOMEM_PASSAGE_MATRIX_BYTES")})


def resource_observation() -> dict:
    """Raw cumulative cgroup counters plus host pressure; missing reads stay unavailable."""
    paths = {f"cgroup/{name}": Path("/sys/fs/cgroup") / name for name in
             ("memory.current", "memory.peak", "memory.events", "memory.stat", "memory.pressure",
              "cpu.stat", "cpu.pressure")}
    paths.update({f"host/{name}": Path("/proc/pressure") / name for name in ("cpu", "memory")})
    values = {}
    for name, path in paths.items():
        try:
            values[name] = {"state": "observed", "raw": path.read_text().strip()}
        except OSError as error:
            values[name] = {"state": "unavailable", "error": type(error).__name__}
    return {"sampled_at": time.time(), "values": values}


async def observe_resources(path: Path, stopped: asyncio.Event, seconds: float) -> None:
    with path.open("w") as output:
        while True:
            row = resource_observation()
            row["final"] = stopped.is_set()
            output.write(json.dumps(row) + "\n")
            output.flush()
            if stopped.is_set():
                return
            try:
                await asyncio.wait_for(stopped.wait(), timeout=seconds)
            except TimeoutError:
                pass


def paired_slots(profile: dict, cases: list[dict]) -> list[dict]:
    return [{"mode": mode, "repeat": repeat, "callers": callers, "case": case["id"], "client": client}
            for repeat in range(profile["repeats"])
            for mode in (profile["modes"] if repeat % 2 == 0 else profile["modes"][::-1])
            for callers in profile["callers"] for case in cases for client in range(callers)]


def slot_key(row: dict) -> tuple:
    return tuple(row[name] for name in ("mode", "repeat", "callers", "case", "client"))


def retain_slots(slots: list[dict], samples: list[dict]) -> list[dict]:
    indexed = {slot_key(row): row for row in samples}
    if len(indexed) != len(samples) or set(indexed) - {slot_key(slot) for slot in slots}:
        raise RuntimeError("duplicate or undeclared attempt slots")
    return [indexed.get(slot_key(slot), {**slot, "outcome": "unattempted", "failure": "missing-attempt",
                "terminal_received": False, "elapsed_ms": None, "response": None}) for slot in slots]


def paired_comparison(samples: list[dict], profile: dict) -> list[dict]:
    # This maintained case bootstrap needs no new statistical dependency or local estimator.
    sys.path.insert(0, str(ROOT / "benchmarks"))
    from recall_parity.scoring import percentile_bootstrap_ci

    baseline, candidate = profile["modes"]
    output = []
    for callers in profile["callers"]:
        cases = []
        for identifier in dict.fromkeys(row["case"] for row in samples):
            arms = {mode: [row for row in samples if row["mode"] == mode
                           and row["callers"] == callers and row["case"] == identifier]
                    for mode in profile["modes"]}
            expected = profile["repeats"] * callers
            complete = all(len(rows) == expected and all(
                row["terminal_received"] and row["elapsed_ms"] is not None for row in rows)
                for rows in arms.values())
            failures = {mode: sum(row["failure"] is not None for row in rows) for mode, rows in arms.items()}
            delta = (statistics.fmean(row["elapsed_ms"] for row in arms[candidate])
                     - statistics.fmean(row["elapsed_ms"] for row in arms[baseline])) if complete else None
            cases.append({"case": identifier, "state": "complete" if complete else "incomplete",
                          "attempts_per_mode": expected, "failures": failures, "delta_ms": delta})
        row = {"callers": callers, "difference": f"{candidate} minus {baseline}", "cases": cases,
               "state": "unavailable", "cases_in_interval": 0, "confidence": profile["pairing"]["confidence"],
               "scope": "query differences conditional on the captured rounds; no independent host experiments"}
        if all(case["state"] == "complete" for case in cases):
            differences = [case["delta_ms"] for case in cases]
            low, high = percentile_bootstrap_ci(differences, **profile["pairing"])
            failures = any(sum(case["failures"].values()) for case in cases)
            row.update(state="unqualified" if failures else "measured", cases_in_interval=len(cases),
                       mean_delta_ms=statistics.fmean(differences), lower_ms=low, upper_ms=high,
                       interpretation="unqualified" if failures else "no change" if low <= 0 <= high
                       else "lower latency" if high < 0 else "higher latency")
        output.append(row)
    return output


def fixture(vault: Path, profile: dict) -> list[str]:
    from exomem import add, source_capture

    reference = json.loads((REFERENCE / "profile.json").read_text())
    words, _digest = reference_source(reference, REFERENCE)
    paths = []
    for number in range(profile["sources"]):
        passages = []
        for ordinal in range(profile["passages_per_source"]):
            start = (number * profile["passages_per_source"] + ordinal) * profile["passage_words"]
            text = " ".join(words[(start + offset) % len(words)] for offset in range(profile["passage_words"]))
            passages.append({"key": f"passage-{ordinal:02d}", "text": text})
        passages = source_capture.validate_passages(passages)
        title = f"Reference source {number:04d}"
        content = "\n\n".join(passage["text"] for passage in passages)
        digest = source_capture.capture_digest(
            vault, content=content, title=title, source_type="document", url=None, tags=[],
            why_captured=None, domain=None, projects=None, slug=None, raw_protection=False, passages=passages)
        rendered = add._render_source(
            title=title, source_type="document", date_iso="2026-01-15", url=None, tags=[],
            why_captured=None, content=content,
            exomem_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/passage-latency/{number}")),
            source_identity={"system": "public-prose-latency", "record_id": str(number), "revision": "1"},
            capture_input_digest=digest, passages=passages)
        relative = f"Knowledge Base/Sources/reference-{number:04d}.md"
        (vault / relative).parent.mkdir(parents=True, exist_ok=True)
        (vault / relative).write_text(rendered)
        paths.append(relative)
    return paths


def verify_rows(vault: Path, index, paths: list[str], profile: dict) -> dict:
    import numpy as np

    from exomem import find as find_module
    from exomem import lexstore, passage_evidence, source_capture

    checked = chunks = 0
    digest = hashlib.sha256()
    spots = []
    with sqlite3.connect(index.path) as vectors, sqlite3.connect(lexstore.lexical_path(vault)) as lexical:
        for relative in paths:
            raw = (vault / relative).read_bytes()
            source_hash = hashlib.sha256(raw).hexdigest()
            digest.update(relative.encode() + bytes.fromhex(source_hash))
            page = find_module._CACHE.get(vault / relative, vault)
            declared = passage_evidence.declared_chunks(page)
            stored = vectors.execute("SELECT chunk_idx, chunk_text, vector FROM chunks WHERE file_path=? ORDER BY chunk_idx", (relative,)).fetchall()
            if len(declared) != profile["passages_per_source"] or len(stored) != len(declared):
                raise RuntimeError(f"declaration/chunk count differs for {relative}")
            lexical_hash = lexical.execute("SELECT content_hash FROM pages WHERE path=?", (relative,)).fetchone()
            if lexical_hash != (source_hash,):
                raise RuntimeError(f"lexical source hash differs for {relative}")
            passages = []
            for chunk, (number, text, blob) in zip(declared, stored, strict=True):
                captured = page.body[chunk.start:chunk.start + chunk.length]
                if (number != chunk.ordinal - 1 or text != chunk.text
                        or hashlib.sha256(captured.encode()).hexdigest() != chunk.passage_hash):
                    raise RuntimeError(f"stored passage binding differs for {relative}:{number}")
                vector = np.frombuffer(blob, dtype=np.float32)
                if len(vector) != index.identity.dim or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1):
                    raise RuntimeError("stored vector is invalid")
                passages.append({"key": chunk.key, "text": captured})
            receipt = source_capture.capture_digest(
                vault, content="\n\n".join(p["text"] for p in passages), title=page.title,
                source_type="document", url=None, tags=[], why_captured=None, domain=None,
                projects=None, slug=None, raw_protection=False, passages=passages)
            if receipt != page.frontmatter["capture_input_digest"]:
                raise RuntimeError("canonical capture receipt differs")
            checked += 1
            chunks += len(stored)
            if relative in (paths[0], paths[len(paths) // 2], paths[-1]):
                spots.append({"path": relative, "source_sha256": source_hash,
                              "capture_input_digest": receipt, "first_passage_sha256": declared[0].passage_hash})
        total, distinct = vectors.execute(
            "SELECT count(*), count(DISTINCT file_path) FROM chunks WHERE file_path IN (SELECT value FROM json_each(?))",
            (json.dumps(paths),)).fetchone()
    if checked != profile["sources"] or total != chunks or distinct != checked:
        raise RuntimeError("fixture totals differ")
    return {"source_rows": checked, "passage_chunks": chunks, "distinct_chunk_sources": distinct,
            "all_bindings_checked": chunks, "canonical_receipts_checked": checked,
            "source_bytes_sha256": digest.hexdigest(), "spot_checks": spots,
            "matrix_mode": index.chunk_scoring_mode}


def prepare(arm: Arm, profile: dict) -> dict:
    import numpy as np
    from managed_recall import enter_managed_recall

    from exomem import (
        due_state,
        embeddings,
        freshness,
        lexstore,
        logging_config,
        memory_refs,
        recall_space,
    )
    from exomem.governance.principal import library_scope

    os.environ.update(arm.env)
    # Preparation runs before the coordinator exists, under the offline fixture owner.
    for name in ("EXOMEM_WRITER_LEASE_URL", "EXOMEM_WRITER_LEASE_VAULT_ID",
                 "EXOMEM_WRITER_LEASE_REPLICA_ID", "EXOMEM_WRITER_LEASE_TOKEN",
                 "EXOMEM_WRITER_LEASE_TOPOLOGY"):
        os.environ.pop(name, None)
    logging_config.configure_logging(arm.logs, process="cli")
    arm.init_vault()
    with library_scope():
        paths = fixture(arm.vault, profile)
        model = embeddings.get_model()
        probe = embeddings.embed_texts(["reference encoder warmup"], is_query=True)
        identity = recall_space.current_identity(int(probe.shape[1]))
        if not model.profile.artifact_digest or identity.fingerprint != model.profile.fingerprint():
            raise RuntimeError("served encoder identity is unavailable")
        original = embeddings.embed_texts

        def seeded(texts, *, is_query=False):
            if is_query:
                raise RuntimeError("the fixture builder cannot replace query encoding")
            # Same deterministic corpus-only method as recall_latency_gate._prepare_worker.
            vectors = []
            for text in texts:
                seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
                vector = np.random.default_rng(seed).standard_normal(identity.dim).astype(np.float32)
                vectors.append(vector / np.linalg.norm(vector))
            return np.asarray(vectors, dtype=np.float32).reshape((-1, identity.dim))

        try:
            embeddings.embed_texts = seeded
            index = embeddings.get_embedding_index(arm.vault)
            index.rebuild_all()
        finally:
            embeddings.embed_texts = original
        lexstore.ensure_fresh(arm.vault)
        memory_refs.ReferenceIndex(arm.vault).rebuild_all()
        due_state.reconcile(arm.vault)
        if not all(freshness.rebaseline(arm.vault).values()):
            raise RuntimeError("the fixture projection is incomplete")
        rows = verify_rows(arm.vault, index, paths, profile)
        admission = enter_managed_recall(arm.vault, full_startup=True)
        return {"rows": rows, "admission": admission, "encoder": dataclasses.asdict(identity),
                "encoder_artifact_digest": model.profile.artifact_digest}


def frozen_cases(profile: dict, identity: dict, scoring_mode: str = "disk") -> list[dict]:
    specifications = json.loads((REFERENCE / "cases.json").read_text())
    # The reference protocol fixes series names; these are not classifications of prose.
    queries = [case for case in specifications if case["series"] == "hybrid"][:profile["queries"]]
    if len(queries) != profile["queries"]:
        raise RuntimeError("reference query count differs")
    return [{"id": case["id"], "tool": "search_memory",
             "arguments": {"query": case["query"], "options": {"result_level": "passage"}, "limit": profile["limit"]},
             "expected": {"response_schema": {
                 "type": "object", "required": ["hits", "withheld_chunks", "scoring_mode", "recall_space"],
                 "properties": {"hits": {"type": "array", "minItems": profile["limit"], "maxItems": profile["limit"]},
                                "withheld_chunks": {"const": 0}, "scoring_mode": {"const": scoring_mode},
                                "recall_space": {"const": identity}}}}} for case in queries]


def dispatch_overlap(lines: str, expected: int) -> dict:
    active = set()
    peak = starts = ends = 0
    events = []
    for line in lines.splitlines():
        record = json.loads(line)
        line = record["message"]
        # The worker records the public search tool under its normalized command name.
        if "tool_start tool=ask_memory " not in line and not any(
                f"tool_{event} tool=ask_memory " in line for event in ("success", "failure", "error")):
            continue
        fields = dict(part.split("=", 1) for part in shlex.split(line) if "=" in part)
        request = fields["request_id"]
        event = fields.get("kind", fields.get("event", ""))
        if event == "tool_start":
            active.add(request)
            starts += 1
            peak = max(peak, len(active))
        else:
            active.discard(request)
            ends += 1
        events.append({"event": event, "request_id": request, "timestamp": record["ts"]})
    return {"starts": starts, "ends": ends, "peak_dispatched": peak,
            "overlap_verified": starts == ends == expected and peak == expected, "events": events}


def retain_batch(rows: list[dict], terminals, output, log: Path, offset: int, callers: int) -> None:
    for row in rows:
        terminals.write(json.dumps(row) + "\n")
    terminals.flush()
    try:
        with log.open() as source:
            source.seek(offset)
            overlap = dispatch_overlap(source.read(), callers)
    except Exception as error:  # noqa: BLE001 - failed observation cannot discard received terminals
        overlap = {"observation_state": "failed", "error_type": type(error).__name__,
                   "starts": None, "ends": None, "peak_dispatched": None,
                   "overlap_verified": False, "events": None}
    for row in rows:
        row["dispatch"] = overlap
        output.write(json.dumps(row) + "\n")
    output.flush()


async def measure(arm: Arm, root: Path, profile: dict, preparation: dict, *,
                  scoring_mode: str = "disk", repeat_indices: list[int] | None = None,
                  case_values: list[dict] | None = None) -> dict:
    import httpx2 as httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from exomem.recall_canary import load_cases, measure_call
    from exomem.request_latency_contract import load_contract

    contract = load_contract()
    bars = {callers: contract.bar(name) for callers, name in
            zip(profile["callers"], profile["decision"]["bars"], strict=True)}
    repeats = list(range(profile["repeats"])) if repeat_indices is None else repeat_indices
    cases_path = root / "frozen-cases.json"
    values = frozen_cases(profile, preparation["encoder"], scoring_mode) if case_values is None else json.loads(json.dumps(case_values))
    for case in values:
        case["expected"]["response_schema"]["properties"]["scoring_mode"] = {"const": scoring_mode}
    write_json(cases_path, values)
    write_json(root / "frozen-protocol.json", {"profile": profile,
               "bars": {callers: bar.model_dump(mode="json") for callers, bar in bars.items()},
               "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest()})
    cases = load_cases(cases_path.read_bytes())
    samples = []
    if repeat_indices is None:
        freeze_source(root)
    else:
        write_json(root / "source-before.json", manifest())
    arm.env["PASSAGE_WORKER_RECEIPT"] = str(root / "worker-launch.json")
    stopped = asyncio.Event()
    observer = asyncio.create_task(observe_resources(
        root / "resources.jsonl", stopped, profile["resource_sample_seconds"]))
    try:
        startup = await arm.start(quiescence_limit=180)
        worker = json.loads((root / "worker-launch.json").read_text())
        if (worker["pid"] != arm.worker.pid or worker["executable"] != arm.python
                or worker["source"] != json.loads((root / "source-before.json").read_text())
                or worker["runtime"]["dependencies"] != runtime(profile)["dependencies"]
                or worker["matrix_bytes"] != arm.worker_settings["EXOMEM_PASSAGE_MATRIX_BYTES"]):
            raise RuntimeError("controlled worker identity differs from its controller receipt")
        async with AsyncExitStack() as stack:
            clients = [arm.client]
            for _ in range(3):
                secret = arm.run_python(SESSION_ISSUE).splitlines()[-1]
                headers = {"Authorization": f"Bearer {secret}", "x-exomem-internal-ingress": "local",
                           "x-exomem-internal-ingress-proof": arm.ingress_key}

                def factory(headers=None, timeout=None, auth=None, **_):
                    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(arm.socket)),
                                             headers=headers, timeout=timeout, auth=auth)

                client = Client(StreamableHttpTransport("http://127.0.0.1/mcp", headers=headers,
                                                        httpx_client_factory=factory), timeout=120)
                clients.append(await stack.enter_async_context(client))
            session_ids = [id(client.session) for client in clients]
            write_json(root / "mcp-clients.json", {
                "independent_client_contexts": len(clients), "sdk_session_objects": session_ids,
                "distinct_sdk_sessions": len(set(session_ids)),
                "remote_session_ids": [client.transport.get_session_id() for client in clients],
                "note": "the normal stateless HTTP server does not allocate remote MCP session IDs"})
            if len(set(session_ids)) != len(clients):
                raise RuntimeError("MCP clients share an SDK session")
            clients = [CapturingClient(client) for client in clients]
            warmup = []
            deadline = time.monotonic() + profile["warmup_timeout_seconds"]
            while True:
                observations = []
                for client in clients:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(f"warmup did not prove {scoring_mode} before its deadline")
                    client.result = None
                    observation = {**dataclasses.asdict(await measure_call(client, cases[0], timeout=remaining)),
                                   "response": client.result.data if client.result else None}
                    observations.append(observation)
                    warmup.append(observation)
                    write_json(root / "warmup.json", warmup)
                if all(row["failure"] is None for row in observations):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"warmup did not prove {scoring_mode} before its deadline")
                await asyncio.sleep(min(profile["warmup_probe_interval_seconds"], remaining))
            arm.require_timeable()
            log = arm.logs / "exomem.log"
            with (root / "samples.jsonl").open("w") as output, (root / "terminals.jsonl").open("w") as terminals:
                for callers in profile["callers"]:
                    for repeat in repeats:
                        for case in cases:
                            offset = log.stat().st_size
                            barrier = asyncio.Barrier(callers)

                            async def call(number, *, barrier=barrier, case=case, callers=callers, repeat=repeat):
                                await barrier.wait()
                                clients[number].result = None
                                observation = await measure_call(clients[number], case, timeout=120)
                                return {"mode": scoring_mode, "callers": callers, "repeat": repeat, "case": case.id,
                                        "bar_id": bars[callers].id, "sampled_at": time.time(),
                                        "client": number, **dataclasses.asdict(observation),
                                        "response": clients[number].result.data if clients[number].result else None}

                            rows = await asyncio.gather(*(call(number) for number in range(callers)))
                            samples.extend(rows)
                            retain_batch(rows, terminals, output, log, offset, callers)
                        print(f"measured mode={scoring_mode} callers={callers} repeat={repeat + 1}", flush=True)
            arm.require_timeable()
    finally:
        await arm.stop()
        stopped.set()
        await observer
        write_json(root / "source-after.json", manifest())
    conditions = summarize_mode(samples, profile)
    unchanged = (root / "source-before.json").read_bytes() == (root / "source-after.json").read_bytes()
    reduced = profile["sources"] != json.loads(PROFILE.read_text())["sources"]
    passed = (len(samples) == profile["queries"] * len(repeats) * sum(profile["callers"]) and unchanged
              and all(row["failures"] == 0 and row["terminals"] == row["attempts"]
                      and row["dispatch_verified"] and (row["latency_passed"] or reduced)
                      for row in conditions))
    return {"state": "passed" if passed else "failed", "conditions": conditions,
            "source_unchanged": unchanged, "startup": startup,
            "frozen_cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "limitations": ["synthetic corpus vectors; no recall quality claim", "file mode only"]}


def summarize_mode(samples: list[dict], profile: dict) -> list[dict]:
    from exomem import latency_watch
    from exomem.request_latency_contract import load_contract

    contract = load_contract()
    bars = {callers: contract.bar(name) for callers, name in
            zip(profile["callers"], profile["decision"]["bars"], strict=True)}
    conditions = []
    for callers, bar in bars.items():
        selected = [sample for sample in samples if sample["callers"] == callers]
        observations = [latency_watch.make_sample(
            at=sample.get("sampled_at", time.time()), tool="search_memory", client="passage-latency", deep=False,
            total_ms=sample["elapsed_ms"], spans=None, bar_id=bar.id, outcome=sample["outcome"],
            source="mcp-canary", contract=contract, trusted_tool=True) for sample in selected]
        judged = latency_watch.summarize(
            observations, now=time.time(), contract=contract,
            coverage={"population": "declared-series", "evictions": 0})
        conditions.append({"callers": callers, "attempts": len(selected), "latency": judged,
                           "failures": sum(sample["failure"] is not None for sample in selected),
                           "terminals": sum(sample["terminal_received"] for sample in selected),
                           "dispatch_verified": bool(selected) and all(
                               sample.get("dispatch", {}).get("overlap_verified", False) for sample in selected),
                           "outcomes": dict(Counter(sample["outcome"] for sample in selected)),
                           "latency_passed": len(judged) == 1 and judged[0]["verdict"] == "observed-within-bar"})
    return conditions


def vector_digest(path: Path) -> dict:
    digest = hashlib.sha256()
    count = 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for relative, number, text, vector, mtime in conn.execute(
                "SELECT file_path, chunk_idx, chunk_text, vector, file_mtime FROM chunks ORDER BY file_path, chunk_idx"):
            metadata = json.dumps([relative, number, text, mtime], separators=(",", ":")).encode()
            digest.update(len(metadata).to_bytes(8, "big") + metadata + vector)
            count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def copied_fixture(baseline: Path, destination: Path, profile: dict, preparation: dict, *,
                   coordination: Path | None = None) -> dict:
    from exomem import index_paths
    from exomem.governance.principal import library_scope

    if destination.exists():
        # A fresh target prevents a mistaken baseline path from overwriting retained evidence.
        raise RuntimeError("paired fixture destination already exists; use a fresh working copy")
    for name in ("vault", "state"):
        shutil.copytree(baseline / "arm" / name, destination / name, copy_function=shutil.copy2)
    os.environ.update(EXOMEM_VAULT_PATH=str(destination / "vault"), EXOMEM_STATE_ROOT=str(destination / "state"),
                      EXOMEM_WRITER_LEASE_STATE_DIR=str(coordination or destination / "lease"))
    path = index_paths.sidecar_path(destination / "vault")
    proxy = SimpleNamespace(path=path, identity=SimpleNamespace(**preparation["encoder"]), chunk_scoring_mode=None)
    paths = [f"Knowledge Base/Sources/reference-{number:04d}.md" for number in range(profile["sources"])]
    with library_scope():
        rows = verify_rows(destination / "vault", proxy, paths, profile)
    if rows["source_bytes_sha256"] != preparation["rows"]["source_bytes_sha256"]:
        raise RuntimeError("copied canonical Source bytes differ from the retained fixture")
    return {"rows": rows, "vector_digest": vector_digest(path), "vector_path": str(path)}


def resource_summary(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    current = [int(row["values"]["cgroup/memory.current"]["raw"]) for row in rows
               if row["values"]["cgroup/memory.current"]["state"] == "observed"]
    peaks = [int(row["values"]["cgroup/memory.peak"]["raw"]) for row in rows
             if row["values"]["cgroup/memory.peak"]["state"] == "observed"]
    counters = {}
    for name in ("cgroup/cpu.stat", "cgroup/memory.events"):
        observations = [row["values"][name] for row in rows if row["values"][name]["state"] == "observed"]
        parsed = [dict((key, int(value)) for key, value in (line.split() for line in row["raw"].splitlines()))
                  for row in observations]
        counters[name] = ({key: parsed[-1][key] - parsed[0][key] for key in parsed[0].keys() & parsed[-1].keys()}
                          if parsed else None)
    return {"observations": len(rows), "segment_sampled_memory_current_max_bytes": max(current) if current else None,
            "container_cumulative_memory_peak_bytes": max(peaks) if peaks else None,
            "segment_counter_deltas": counters,
            "unavailable_reads": sum(value["state"] != "observed" for row in rows for value in row["values"].values()),
            "pressure": "raw cgroup and host observations remain in resources.jsonl; no pressure exclusion"}


def make_arm(name: str, root: Path, profile: dict) -> CloudArm:
    settings = worker_settings(embeddings=True, passthrough=json.loads((REFERENCE / "profile.json").read_text())["worker_environment"])
    settings.pop("EXOMEM_PRELOAD_MODELS", None)
    settings.update(EXOMEM_CPU_THREADS=str(profile["cpu_threads"]), EXOMEM_SYNC_WORKERS=str(profile["sync_workers"]),
                    EXOMEM_CLOUD_RESOURCE_POLICY="service-v1", EXOMEM_CLOUD_CELL="1",
                    EXOMEM_CLOUD_CELL_ID="passage-latency", EXOMEM_CLOUD_CELL_TOKEN=uuid.uuid4().hex)
    settings.update(profile["mode_environment"][name])
    return CloudArm(name, sys.executable, root / "arm", root, worker_settings=settings, file_watcher=True)


async def paired(root: Path, baseline: Path, profile: dict, *, plan_only: bool = False) -> dict:
    if root.resolve().is_relative_to(baseline.resolve()) or baseline.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("paired output and retained baseline must have separate roots")
    preparation_report = json.loads((baseline / "preparation.json").read_text())
    preparation = preparation_report["preparation"]
    raw_cases = (baseline / "frozen-cases.json").read_bytes()
    historical_report = json.loads((baseline / "measure-report.json").read_text())
    if hashlib.sha256(raw_cases).hexdigest() != historical_report["frozen_cases_sha256"]:
        raise RuntimeError("retained query bytes differ from their original receipt")
    cases = json.loads(raw_cases)
    if len(cases) != profile["queries"]:
        raise RuntimeError("retained query count differs from the candidate profile")
    slots = paired_slots(profile, cases)
    (root / "frozen-cases.json").write_bytes(raw_cases)
    write_json(root / "attempt-slots.json", slots)
    from exomem.request_latency_contract import load_contract

    write_json(root / "frozen-protocol.json", {
        "profile": profile, "bars": [load_contract().bar(name).model_dump(mode="json") for name in profile["decision"]["bars"]],
        "cases_sha256": hashlib.sha256(raw_cases).hexdigest(), "attempt_slots": len(slots),
        "order": list(dict.fromkeys((slot["repeat"], slot["mode"]) for slot in slots)),
        "comparison": "same candidate source and publication repair; historical failure is not a comparison arm",
        "sampling_unit": "query; its paired mean includes every repeat and concurrent-client slot",
        "choice_rule": "choose disk if it qualifies; otherwise resident if it qualifies; otherwise unqualified",
        "interval_scope": "conditional on captured rounds, not independent host experiments",
        "memory_peak_scope": "container cumulative; segment sampled maxima and counter deltas are separate"})
    frozen = freeze_source(root)
    if plan_only:
        return {"state": "planned", "attempt_slots": len(slots), "source": frozen}
    captured_root = Path(preparation_report["command"][preparation_report["command"].index("--root") + 1])
    if root.resolve() != captured_root or root.resolve().is_relative_to(baseline.resolve()):
        raise RuntimeError("use a fresh copy at the original internal fixture root, outside the retained baseline")
    if len(profile["modes"]) != 2:
        raise RuntimeError("this paired protocol requires exactly two declared scoring modes")
    baseline_vectors = next((baseline / "arm/state").glob("*/.embeddings.sqlite"))
    expected_vectors = vector_digest(baseline_vectors)
    segments = []
    failure = None
    try:
        for repeat, mode in dict.fromkeys((slot["repeat"], slot["mode"]) for slot in slots):
            segment = root / "segments" / f"{repeat}-{mode}"
            segment.mkdir(parents=True, mode=0o700)
            copied = copied_fixture(baseline, root / "arm", profile, preparation,
                                    coordination=segment / "fixture-lease")
            write_json(segment / "fixture-before.json", copied)
            if copied["vector_digest"] != expected_vectors:
                raise RuntimeError("copied vector rows differ from the retained fixture")
            arm = make_arm(mode, root, profile)
            segment_report = {"mode": mode, "repeat": repeat, "controller_runtime": runtime(profile),
                              "mode_environment": profile["mode_environment"][mode], "state": "unavailable"}
            try:
                measured = await measure(arm, segment, profile, preparation, scoring_mode=mode,
                                         repeat_indices=[repeat], case_values=cases)
                segment_report.update(measured)
                segment_report["state"] = "segment-measured"
                after = vector_digest(Path(copied["vector_path"]))
                write_json(segment / "vectors-after.json", after)
                if after != expected_vectors:
                    raise RuntimeError("worker changed the frozen corpus vector rows")
                segment_report["resources"] = resource_summary(segment / "resources.jsonl")
            except Exception as error:
                segment_report["failure"] = {"type": type(error).__name__, "message": str(error)}
                raise
            finally:
                write_json(segment / "report.json", segment_report)
                # The worker is stopped; preserving its whole root keeps native logs and stage receipts.
                (root / "arm").rename(segment / "arm")
                segments.append({"mode": mode, "repeat": repeat, "path": str(segment)})
    except Exception as error:  # noqa: BLE001 - a broken segment retains every scheduled slot below
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        write_json(root / "source-after.json", manifest())
    samples = []
    for segment in segments:
        segment_slots = [slot for slot in slots if slot["mode"] == segment["mode"] and slot["repeat"] == segment["repeat"]]
        observations, read_errors = {}, {}
        for name in ("terminals", "samples"):
            rows = []
            try:
                with (Path(segment["path"]) / f"{name}.jsonl").open() as source:
                    for line in source:
                        rows.append(json.loads(line))
            except (OSError, ValueError) as error:
                read_errors[name] = type(error).__name__
            retain_slots(segment_slots, rows)
            observations[name] = rows
        terminals = {slot_key(row): row for row in observations["terminals"]}
        enriched = {slot_key(row): row for row in observations["samples"]}
        if enriched.keys() - terminals.keys():
            raise RuntimeError("duplicate or undeclared attempt slots")
        for key, terminal in terminals.items():
            missing = {"observation_state": "failed" if read_errors else "missing", "read_errors": read_errors,
                       "starts": None, "ends": None, "peak_dispatched": None, "overlap_verified": False, "events": None}
            dispatch = enriched.get(key, {}).get("dispatch", missing)
            if read_errors:
                dispatch = {**dispatch, "observation_state": "failed", "read_errors": read_errors, "overlap_verified": False}
            samples.append({**terminal, "dispatch": dispatch})
    retained = retain_slots(slots, samples)
    with (root / "samples.jsonl").open("w") as output:
        for sample in retained:
            output.write(json.dumps(sample) + "\n")
    conditions = {mode: summarize_mode([row for row in retained if row["mode"] == mode], profile)
                  for mode in profile["modes"]}
    unchanged = frozen == manifest()
    qualifications = {}
    for mode, rows in conditions.items():
        reports = [json.loads((Path(segment["path"]) / "report.json").read_text())
                   for segment in segments if segment["mode"] == mode]
        qualifications[mode] = (unchanged and len(reports) == profile["repeats"]
            and all(report.get("failure") is None and report["source_unchanged"] for report in reports)
            and all(row["failures"] == 0 and row["terminals"] == row["attempts"]
                    and row["dispatch_verified"] and row["latency_passed"] for row in rows))
    choice = next((mode for mode in profile["modes"] if qualifications[mode]), None)
    return {"state": "passed" if choice is not None else "failed", "failure": failure, "conditions": conditions,
            "choice": choice or "unqualified", "qualifications": qualifications,
            "complete": all(sample["terminal_received"] for sample in retained),
            "source_unchanged": unchanged, "segments": segments, "slots": len(retained),
            "comparison": paired_comparison(retained, profile), "fixture": preparation,
            "limitations": ["synthetic corpus vectors; no recall quality claim", "file mode only",
                            "container memory peak is cumulative across segments"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "measure", "paired"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.baseline is not None and (args.root.resolve().is_relative_to(args.baseline.resolve())
            or args.baseline.resolve().is_relative_to(args.root.resolve())):
        parser.error("paired output and retained baseline must have separate roots")
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.root.chmod(0o700)
    profile = json.loads(PROFILE.read_text())
    if args.smoke:
        profile["sources"] = 2
        profile["queries"] = 1
        profile["repeats"] = 1
    report = {"state": "unverified", "profile": profile, "command": sys.argv, "reduced": args.smoke}
    try:
        if args.mode == "paired":
            if args.baseline is None or args.smoke:
                raise RuntimeError("paired mode requires the retained baseline and does not generate a smoke fixture")
            if not args.plan_only:
                report["runtime"] = runtime(profile)
            report.update(asyncio.run(paired(args.root, args.baseline, profile, plan_only=args.plan_only)))
            return 0 if report["state"] in ("planned", "passed") else 1
        if args.plan_only:
            raise RuntimeError("plan-only applies to paired mode")
        arm = make_arm("disk", args.root, profile)
        os.environ.update(arm.env)
        report["runtime"] = runtime(profile)
        if args.mode == "prepare":
            report["preparation"] = prepare(arm, profile)
            report["state"] = "prepared"
            write_json(args.root / "preparation.json", report)
        else:
            preparation = json.loads((args.root / "preparation.json").read_text())
            report["preparation"] = preparation["preparation"]
            report.update(asyncio.run(measure(arm, args.root, profile, report["preparation"])))
            if args.smoke and report["state"] == "passed":
                report["state"] = "smoke-passed"
    except Exception as error:
        report.update(state="failed", failure={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        write_json(args.root / f"{args.mode}-report.json", report)
    print(json.dumps({key: value for key, value in report.items() if key in ("state", "conditions")}))
    return 0 if report["state"] in ("prepared", "passed", "smoke-passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
