"""Disk-only passage qualification through the maintained MCP arm and canary clock."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import shlex
import sqlite3
import sys
import time
import tomllib
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

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
             ROOT / "pyproject.toml", ROOT / "uv.lock"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


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


def frozen_cases(profile: dict, identity: dict) -> list[dict]:
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
                                "withheld_chunks": {"const": 0}, "scoring_mode": {"const": "disk"},
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


async def measure(arm: Arm, root: Path, profile: dict, preparation: dict) -> dict:
    import httpx2 as httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from exomem import latency_watch
    from exomem.recall_canary import load_cases, measure_call
    from exomem.request_latency_contract import load_contract

    contract = load_contract()
    bars = {callers: contract.bar(name) for callers, name in
            zip(profile["callers"], profile["decision"]["bars"], strict=True)}
    cases_path = root / "frozen-cases.json"
    write_json(cases_path, frozen_cases(profile, preparation["encoder"]))
    write_json(root / "frozen-protocol.json", {"profile": profile,
               "bars": {callers: bar.model_dump(mode="json") for callers, bar in bars.items()},
               "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest()})
    cases = load_cases(cases_path.read_bytes())
    samples = []
    write_json(root / "source-before.json", manifest())
    try:
        startup = await arm.start(quiescence_limit=180)
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
            warmup = [{**dataclasses.asdict(await measure_call(client, cases[0], timeout=120)),
                       "response": client.result.data if client.result else None} for client in clients]
            write_json(root / "warmup.json", warmup)
            if any(row["failure"] for row in warmup):
                raise RuntimeError(f"warmup failed: {warmup}")
            arm.require_timeable()
            log = arm.logs / "exomem.log"
            with (root / "samples.jsonl").open("w") as output:
                for callers in profile["callers"]:
                    for repeat in range(profile["repeats"]):
                        for case in cases:
                            offset = log.stat().st_size
                            barrier = asyncio.Barrier(callers)

                            async def call(number, *, barrier=barrier, case=case, callers=callers, repeat=repeat):
                                await barrier.wait()
                                clients[number].result = None
                                observation = await measure_call(clients[number], case, timeout=120)
                                return {"callers": callers, "repeat": repeat, "case": case.id,
                                        "bar_id": bars[callers].id, "sampled_at": time.time(),
                                        "client": number, **dataclasses.asdict(observation),
                                        "response": clients[number].result.data if clients[number].result else None}

                            rows = await asyncio.gather(*(call(number) for number in range(callers)))
                            with log.open() as source:
                                source.seek(offset)
                                overlap = dispatch_overlap(source.read(), callers)
                            for row in rows:
                                row["dispatch"] = overlap
                                output.write(json.dumps(row) + "\n")
                                samples.append(row)
                            output.flush()
                        print(f"measured callers={callers} repeat={repeat + 1}", flush=True)
            arm.require_timeable()
    finally:
        await arm.stop()
        write_json(root / "source-after.json", manifest())
    conditions = []
    for callers, bar in bars.items():
        selected = [sample for sample in samples if sample["callers"] == callers]
        observations = [latency_watch.make_sample(
            at=sample["sampled_at"], tool="search_memory", client="passage-latency", deep=False,
            total_ms=sample["elapsed_ms"], spans=None, bar_id=bar.id, outcome=sample["outcome"],
            source="mcp-canary", contract=contract, trusted_tool=True) for sample in selected]
        judged = latency_watch.summarize(
            observations, now=time.time(), contract=contract,
            coverage={"population": "declared-series", "evictions": 0})
        conditions.append({"callers": callers, "attempts": len(selected), "latency": judged,
                           "failures": sum(sample["failure"] is not None for sample in selected),
                           "terminals": sum(sample["terminal_received"] for sample in selected),
                           "dispatch_verified": all(sample["dispatch"]["overlap_verified"] for sample in selected),
                           "latency_passed": len(judged) == 1 and judged[0]["verdict"] == "observed-within-bar"})
    unchanged = (root / "source-before.json").read_bytes() == (root / "source-after.json").read_bytes()
    reduced = profile["sources"] != json.loads(PROFILE.read_text())["sources"]
    passed = (len(samples) == profile["queries"] * profile["repeats"] * sum(profile["callers"]) and unchanged
              and all(row["failures"] == 0 and row["terminals"] == row["attempts"]
                      and row["dispatch_verified"] and (row["latency_passed"] or reduced)
                      for row in conditions))
    return {"state": "passed" if passed else "failed", "conditions": conditions,
            "source_unchanged": unchanged, "startup": startup,
            "frozen_cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "limitations": ["synthetic corpus vectors; no recall quality claim", "file mode only"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "measure"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.root.chmod(0o700)
    profile = json.loads(PROFILE.read_text())
    if args.smoke:
        profile["sources"] = 2
        profile["queries"] = 1
        profile["repeats"] = 1
    settings = worker_settings(embeddings=True, passthrough=json.loads((REFERENCE / "profile.json").read_text())["worker_environment"])
    settings.pop("EXOMEM_PRELOAD_MODELS", None)
    settings.update(EXOMEM_CPU_THREADS=str(profile["cpu_threads"]), EXOMEM_SYNC_WORKERS=str(profile["sync_workers"]),
                    EXOMEM_CLOUD_RESOURCE_POLICY="service-v1", EXOMEM_CLOUD_CELL="1",
                    EXOMEM_CLOUD_CELL_ID="passage-latency", EXOMEM_CLOUD_CELL_TOKEN=uuid.uuid4().hex)
    arm = CloudArm("disk", sys.executable, args.root / "arm", args.root, worker_settings=settings,
                   file_watcher=True)
    os.environ.update(arm.env)
    report = {"state": "unverified", "profile": profile, "command": sys.argv, "reduced": args.smoke}
    try:
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
