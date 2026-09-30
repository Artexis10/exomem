#!/usr/bin/env python3
"""Measure independent CPU sessions sharing one read-only model file on Linux.

All model conversion and child logs live in a TemporaryDirectory. No tenant
text, product code, cache files, or service state are modified. Optional ONNX
conversion requires ``onnx`` in the probe's environment; failures are reported.
PSS totals cover the probe children, not unrelated processes or unmapped cache.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import selectors
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MODEL = "BAAI/bge-m3"
TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "A read-only file can share physical pages between processes.",
    "Unicode text: café, 中文, and a small 🧠.",
]
QUERY_TEXTS = [
    "How does shared model memory work?",
    "Find the latest deployment decision.",
    "What changed in the recall pipeline?",
    "Why did the embedding load fail?",
    "Show notes about model latency.",
    "Which runtime setting saves memory?",
    "When was the cloud limit measured?",
    "Summarise the active indexing plan.",
    "Where is the encoder configured?",
    "Who owns the release checklist?",
    "Compare the current and previous benchmark.",
    "List evidence for the memory estimate.",
    "Explain the tokenizer compatibility rule.",
    "What remains open in the migration?",
    "Find the production rollback steps.",
    "Which cells use the multilingual model?",
    "How many threads does ONNX use?",
    "What is the vector parity threshold?",
    "Locate the artifact integrity check.",
    "Is prepacking enabled for personal servers?",
]
CHUNK_TOPICS = (
    "platform", "runtime", "storage", "network", "service",
    "release", "search", "index", "memory", "model",
    "privacy", "backup", "restore", "worker", "gateway",
    "client", "server", "process", "system", "project",
)
FIELDS = (
    "Rss", "Pss", "Shared_Clean", "Shared_Dirty",
    "Private_Clean", "Private_Dirty", "Anonymous",
)
MIN_COSINE = 0.9999
CONFIG_SOURCE = (
    "https://raw.githubusercontent.com/microsoft/onnxruntime/v{version}/"
    "include/onnxruntime/core/session/onnxruntime_session_options_config_keys.h"
)


def parse_smaps_rollup(content: str) -> dict[str, int]:
    """Return the required counters in KiB; absent measurements are errors."""
    values = {}
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        if key in FIELDS:
            amount, unit = rest.split()
            if unit != "kB":
                raise ValueError(f"unexpected smaps unit: {unit}")
            values[key] = int(amount)
    missing = set(FIELDS) - values.keys()
    if missing:
        raise ValueError(f"missing smaps fields: {', '.join(sorted(missing))}")
    return values


def pss_accounting(samples: list[dict], previous_total: int | None) -> dict[str, int]:
    """Remeasure the whole cohort: older processes' PSS changes with sharing."""
    total = sum(sample["Pss"] for sample in samples)
    return {
        "node_total_pss_kib": total,
        "marginal_pss_kib": total - (previous_total or 0),
    }


def latency_corpora() -> dict[str, list[str]]:
    """Deterministic synthetic query and long-prose workloads."""
    chunks = []
    for topic in CHUNK_TOPICS:
        sentence = (
            f"The careful {topic} team reviews each system change with clear evidence and "
            "records the result before the next reliable release begins."
        )
        chunks.append(" ".join([sentence] * 19))
    return {"queries": list(QUERY_TEXTS), "chunks": chunks}


def latency_summary(repetitions: list[float], *, text_count: int) -> dict:
    """Summarise whole-corpus timings as the requested per-text median."""
    return {
        "repetitions_seconds": repetitions,
        "median_ms_per_text": statistics.median(repetitions) * 1000 / text_count,
    }


def latency_configurations(source: str) -> list[dict]:
    """The experimental pair plus the real product path with both knob values."""
    return [
        {"name": "probe-v0", "path": source, "product": False, "config": {}},
        {
            "name": "probe-no-prepack",
            "path": source,
            "product": False,
            "config": {"session.disable_prepacking": "1"},
        },
        {"name": "product-knob-off", "path": source, "product": True, "share_weights": False},
        {"name": "product-knob-on", "path": source, "product": True, "share_weights": True},
    ]


def mapping_accounting(pid: int, paths: list[str]) -> dict:
    """Separate private anonymous pages from actual model-file mappings."""
    private_anon = 0
    model_maps = []
    current = None
    for line in Path(f"/proc/{pid}/smaps").read_text().splitlines():
        if "-" in line.split()[0] and ":" not in line.split()[0]:
            if current is not None:
                private_anon += max(0, current.get("Anonymous", 0) - current.get("Shared_Dirty", 0))
                if current["model"]:
                    model_maps.append({k: v for k, v in current.items() if k != "model"})
            current = {"model": any(line.endswith(path) for path in paths)}
        elif current is not None:
            key, _, rest = line.partition(":")
            if key in FIELDS:
                current[key] = int(rest.split()[0])
    if current is not None:
        private_anon += max(0, current.get("Anonymous", 0) - current.get("Shared_Dirty", 0))
        if current["model"]:
            model_maps.append({k: v for k, v in current.items() if k != "model"})
    return {"private_anonymous_kib": private_anon, "model_file_mappings_kib": model_maps}


def session_options(*, disabled: bool = False, config: dict | None = None):
    import onnxruntime as ort

    from exomem import embedding_backend as backend
    from exomem import runtime_resources

    options = ort.SessionOptions()
    options.graph_optimization_level = (
        ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        if disabled else ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    )
    runtime_resources.configure_onnx_session_options(
        options, default_threads=backend.SERVED_DEFAULT_THREADS,
    )
    for key, value in (config or {}).items():
        options.add_session_config_entry(key, value)
    return options


def prepare(kind: str, source: str, directory: str) -> None:
    """Run conversion in a short-lived process to release all converter heaps."""
    import onnxruntime as ort

    target = Path(directory)
    if kind == "external":
        import onnx

        model = onnx.load(source)
        onnx.save_model(
            model, target / "external.onnx", save_as_external_data=True,
            all_tensors_to_one_file=True, location="weights.data", size_threshold=0,
        )
    else:
        options = session_options()
        if kind == "ort":
            options.optimized_model_filepath = str(target / "optimized.ort")
            options.add_session_config_entry("session.save_model_format", "ORT")
        elif kind in {"prepacked", "prepacked-small-inline"}:
            options.optimized_model_filepath = str(target / f"{kind}.onnx")
            options.add_session_config_entry(
                "session.optimized_model_external_initializers_file_name", f"{kind}.data",
            )
            options.add_session_config_entry(
                "session.optimized_model_external_initializers_min_size_in_bytes",
                "1024" if kind == "prepacked-small-inline" else "0",
            )
            options.add_session_config_entry("session.save_external_prepacked_constant_initializers", "1")
        else:
            raise ValueError(f"unknown preparation kind: {kind}")
        ort.InferenceSession(source, sess_options=options, providers=["CPUExecutionProvider"])


def latency_child(configuration: dict) -> None:
    """Time one session in one process after warming both latency corpora."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    from exomem import embedding_backend as backend
    from exomem import runtime_resources

    if configuration["product"]:
        os.environ[runtime_resources.ONNX_SHARE_WEIGHTS_ENV] = (
            "1" if configuration["share_weights"] else "0"
        )
        encoder = backend._OnnxEncoder(MODEL, "cpu")
    else:
        encoder = backend._OnnxEncoder.__new__(backend._OnnxEncoder)
        encoder.profile = backend.read_profile(MODEL)
        encoder._tokenizer = Tokenizer.from_file(backend.require_tokenizer(MODEL))
        encoder._tokenizer.enable_truncation(max_length=encoder.profile.max_seq)
        encoder._pad_id = encoder._tokenizer.token_to_id(encoder.profile.pad_token)
        if encoder._pad_id is None:
            raise ValueError("product padding token is absent")
        encoder._tokenizer.enable_padding(
            pad_id=encoder._pad_id, pad_token=encoder.profile.pad_token,
        )
        options = session_options(config=configuration["config"])
        encoder._session = ort.InferenceSession(
            configuration["path"], sess_options=options, providers=["CPUExecutionProvider"],
        )
        encoder._inputs = {item.name for item in encoder._session.get_inputs()}
        encoder.device = "cpu"
        encoder.share_weights = "session.disable_prepacking" in configuration["config"]

    corpora = latency_corpora()
    token_counts = {}
    for name, texts in corpora.items():
        encoded = encoder._tokenizer.encode_batch(texts)
        token_counts[name] = [sum(item.attention_mask) for item in encoded]
    if not all(350 <= count <= 500 for count in token_counts["chunks"]):
        raise ValueError(f"chunk token counts outside 350..500: {token_counts['chunks']}")

    for texts in corpora.values():
        encoder.encode(texts)
    timings = {}
    for name, texts in corpora.items():
        repetitions = []
        for _ in range(3):
            started = time.perf_counter()
            encoder.encode(texts)
            repetitions.append(time.perf_counter() - started)
        timings[name] = latency_summary(repetitions, text_count=len(texts))
    report = {
        "name": configuration["name"],
        "product": configuration["product"],
        "share_weights": encoder.share_weights,
        "token_counts": token_counts,
        "timings": timings,
    }
    encoder.release()
    print(json.dumps(report), flush=True)


def run_latency_comparison(source: str, scratch: Path) -> list[dict]:
    """Run each latency arm alone so no competing session distorts the result."""
    results = []
    for configuration in latency_configurations(source):
        log_path = scratch / f"latency-{configuration['name']}.stderr"
        process = None
        try:
            with log_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        __file__,
                        "--internal-mode",
                        "latency",
                        "--variant",
                        json.dumps(configuration),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                )
                stdout, _ = process.communicate(timeout=1800)
            if process.returncode != 0:
                detail = log_path.read_text(errors="replace")[-8000:]
                raise RuntimeError(
                    f"latency arm {configuration['name']} failed ({process.returncode}): {detail}"
                )
            result = json.loads(stdout.strip().splitlines()[-1])
            results.append(result)
            query_ms = result["timings"]["queries"]["median_ms_per_text"]
            chunk_ms = result["timings"]["chunks"]["median_ms_per_text"]
            print(
                f"{configuration['name']}: queries={query_ms:.1f} ms/text, "
                f"chunks={chunk_ms:.1f} ms/text",
                flush=True,
            )
        finally:
            if process is not None and process.poll() is None:
                stop(process)
    return results


def child(variant: dict, reference: str) -> None:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    from exomem import embedding_backend as backend

    started = time.perf_counter()
    mapping = None
    mapped_file = None
    if variant["name"] == "V0":
        encoder = backend._OnnxEncoder(MODEL, "cpu")
    else:
        # Reuse the exact product encode/tokenizer/pooling policy, with only the
        # session construction replaced. In particular int8 runs one text at a time.
        encoder = backend._OnnxEncoder.__new__(backend._OnnxEncoder)
        encoder.profile = backend.read_profile(MODEL)
        encoder._tokenizer = Tokenizer.from_file(backend.require_tokenizer(MODEL))
        encoder._tokenizer.enable_truncation(max_length=encoder.profile.max_seq)
        encoder._pad_id = encoder._tokenizer.token_to_id(encoder.profile.pad_token)
        if encoder._pad_id is None:
            raise ValueError("product padding token is absent")
        encoder._tokenizer.enable_padding(
            pad_id=encoder._pad_id, pad_token=encoder.profile.pad_token,
        )
        options = session_options(disabled=variant["disabled"], config=variant["config"])
        model = variant["path"]
        if variant["loader"] == "mmap-buffer":
            mapped_file = open(model, "rb")  # noqa: SIM115 — held for the entire session
            mapping = mmap.mmap(mapped_file.fileno(), 0, access=mmap.ACCESS_READ)
            model = mapping
        encoder._session = ort.InferenceSession(
            model, sess_options=options, providers=["CPUExecutionProvider"],
        )
        encoder._inputs = {item.name for item in encoder._session.get_inputs()}
        encoder.device = "cpu"
    load_seconds = time.perf_counter() - started
    encoder.encode(TEXTS)  # fixed warm-up; latency below measures an already warm session
    started = time.perf_counter()
    embeddings = encoder.encode(TEXTS)
    latency = time.perf_counter() - started
    if not np.isfinite(embeddings).all() or np.any(np.linalg.norm(embeddings, axis=1) == 0):
        raise ValueError("embedding finite/nonzero parity failed")
    report = {
        "pid": os.getpid(), "load_seconds": load_seconds,
        "warm_encode_seconds": latency,
        "intra_op_threads": encoder._session.get_session_options().intra_op_num_threads,
        "inter_op_threads": encoder._session.get_session_options().inter_op_num_threads,
    }
    if Path(reference).exists():
        baseline = np.load(reference)
        if embeddings.shape != baseline.shape or not np.isfinite(embeddings).all():
            raise ValueError("embedding shape/finite parity failed")
        a, b = embeddings.astype(np.float64), baseline.astype(np.float64)
        cosine = np.sum(a * b, axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
        if not np.isfinite(cosine).all():
            raise ValueError("embedding cosine is not finite")
        report.update(
            max_abs_diff=float(np.max(np.abs(a - b))), min_cosine=float(np.min(cosine)),
        )
    else:
        report.update(embeddings=embeddings.tolist(), max_abs_diff=0.0, min_cosine=1.0)
    print(json.dumps(report), flush=True)
    # Keep encoder and mapping alive. EOF or termination ends the child's lifetime.
    sys.stdin.buffer.read(1)
    encoder.release()
    if mapping is not None:
        mapping.close()
        mapped_file.close()


def stop(process: subprocess.Popen) -> None:
    """Reap owned processes on success, error, timeout, or interruption."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for stream in (process.stdin, process.stdout):
        if stream:
            stream.close()


def read_ready(process: subprocess.Popen, timeout: float) -> dict:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError(f"child did not become ready within {timeout}s")
        line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"child exited before ready (exit {process.wait()})")
    return json.loads(line)


def run_variant(variant: dict, scratch: Path, reference: Path, max_procs: int) -> dict:
    result = {**variant, "status": "passed", "measurements": []}
    children = []
    reports = []
    logs = []
    previous = None
    try:
        for count in range(1, max_procs + 1):
            log_path = scratch / f"{variant['name']}-{count}.stderr"
            with log_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [sys.executable, __file__, "--internal-mode", "child",
                     "--variant", json.dumps(variant), "--reference", str(reference)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                )
            children.append(process)
            logs.append(log_path)
            report = read_ready(process, 300)
            if "embeddings" in report:
                if variant["name"] != "V0":
                    raise ValueError("V0 reference is unavailable")
                import numpy as np

                np.save(reference, np.asarray(report.pop("embeddings"), dtype=np.float32))
            reports.append(report)
            samples = []
            paths = [variant["path"], *variant.get("external_paths", [])]
            for owned in children:
                if owned.poll() is not None:
                    raise RuntimeError("a ready child exited during the measurement")
                sample = parse_smaps_rollup(Path(f"/proc/{owned.pid}/smaps_rollup").read_text())
                sample.update(pid=owned.pid, **mapping_accounting(owned.pid, paths))
                samples.append(sample)
            accounting = pss_accounting(samples, previous)
            previous = accounting["node_total_pss_kib"]
            row = {"n": count, **accounting, "processes": samples, "encodes": list(reports)}
            result["measurements"].append(row)
            print(
                f"{variant['name']} N={count}: PSS={previous / 1024:.1f} MiB, "
                f"marginal={accounting['marginal_pss_kib'] / 1024:.1f} MiB, "
                f"min cosine={min(r['min_cosine'] for r in reports):.9f}", flush=True,
            )
            if report["min_cosine"] < MIN_COSINE:
                raise ValueError(f"parity failed: min cosine {report['min_cosine']} < {MIN_COSINE}")
    except Exception as error:  # noqa: BLE001 — variant failures are measurement results
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        for process in children:
            stop(process)
        result["child_stderr"] = [path.read_text(errors="replace")[-8000:] for path in logs]
    return result


def variants(source: str, scratch: Path) -> list[dict]:
    baseline = {
        "name": "V0", "path": source, "loader": "product-path", "format": "ONNX",
        "external_paths": [source + ".data"],
        "disabled": False, "config": {}, "prepare": None,
    }
    choices = [baseline]
    for prepacking in (True, False):
        suffix = "prepack" if prepacking else "no-prepack"
        config = {} if prepacking else {"session.disable_prepacking": "1"}
        choices.append({
            "name": f"V1-{suffix}", "path": str(scratch / "optimized.ort"),
            "loader": "mmap-buffer", "format": "ORT", "disabled": True,
            "config": {**config, "session.use_ort_model_bytes_directly": "1",
                       "session.use_ort_model_bytes_for_initializers": "1"}, "prepare": "ort",
        })
        choices.append({
            "name": f"V2-{suffix}", "path": str(scratch / "external.onnx"),
            "external_paths": [str(scratch / "weights.data")],
            "loader": "path", "format": "ONNX external data", "disabled": False,
            "config": config, "prepare": "external",
        })
        choices.append({
            "name": f"V3-{suffix}", "path": str(scratch / "optimized.ort"),
            "loader": "path", "format": "ORT", "disabled": True,
            "config": {**config, "session.use_memory_mapped_ort_model": "1",
                       "session.use_ort_model_bytes_for_initializers": "1"}, "prepare": "ort",
        })
    choices.append({
        "name": "V4-external-prepacked", "path": str(scratch / "prepacked.onnx"),
        "external_paths": [str(scratch / "prepacked.data")],
        "loader": "path", "format": "optimized ONNX external prepacked data",
        "disabled": True, "config": {}, "prepare": "prepacked",
    })
    choices.append({
        "name": "V4b-prepacked-small-inline",
        "path": str(scratch / "prepacked-small-inline.onnx"),
        "external_paths": [str(scratch / "prepacked-small-inline.data")],
        "loader": "path", "format": "optimized ONNX external prepacked data (small tensors inline)",
        "disabled": True, "config": {}, "prepare": "prepacked-small-inline",
    })
    choices.append({
        "name": "V5-device-allocator", "path": str(scratch / "optimized.ort"),
        "loader": "path", "format": "ORT", "disabled": True,
        "config": {"session.use_memory_mapped_ort_model": "1",
                   "session.use_ort_model_bytes_for_initializers": "1",
                   "session.disable_prepacking": "1",
                   "session.use_device_allocator_for_initializers": "1"}, "prepare": "ort",
    })
    return choices


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-procs", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--latency-only", action="store_true")
    parser.add_argument(
        "--internal-mode", choices=("child", "prepare", "latency"), help=argparse.SUPPRESS,
    )
    parser.add_argument("--variant", help=argparse.SUPPRESS)
    parser.add_argument("--reference", help=argparse.SUPPRESS)
    parser.add_argument("--source", help=argparse.SUPPRESS)
    parser.add_argument("--scratch", help=argparse.SUPPRESS)
    parser.add_argument("--kind", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    if args.internal_mode == "child":
        child(json.loads(args.variant), args.reference)
        return 0
    if args.internal_mode == "prepare":
        prepare(args.kind, args.source, args.scratch)
        return 0
    if args.internal_mode == "latency":
        latency_child(json.loads(args.variant))
        return 0
    if args.out is None:
        parser.error("--out is required")
    if not Path("/proc/self/smaps_rollup").exists():
        parser.error("Linux /proc smaps_rollup is required")
    import onnxruntime as ort

    from exomem import embedding_backend as backend

    served = backend.served_artifact(MODEL)
    # Refuse rebuilding a missing cache artifact: this probe never acquires models.
    target = backend.artifact_dir(MODEL, served)
    expected = {
        "model": MODEL, "revision": served.revision,
        "quantization": served.quantization, "file_format": served.file_format,
    }
    if backend._installed_digest(target, expected) is None:
        raise FileNotFoundError("cached served model is required; no downloads/builds allowed")
    source, digest = backend.ensure_artifact(MODEL, served)
    metadata = {
        "model": MODEL, "artifact_digest": digest, "onnxruntime_version": ort.__version__,
        "session_config_source": CONFIG_SOURCE.format(version=ort.__version__),
        "texts": TEXTS, "parity_floor": MIN_COSINE, "max_procs": args.max_procs,
        "pss_scope": "sum of probe children only; excludes parent and unmapped page cache",
        "load_scope": "tokenizer/profile/session construction; V0 includes product artifact verification",
        "warm_scope": "one encode of all fixed texts, after one identical warm-up",
        "variants": [], "preparations": {},
    }
    # SIGTERM must follow the same finally path as Ctrl-C, including child reaping.
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="shared-weights-probe-") as directory:
        scratch = Path(directory)
        if args.latency_only:
            metadata["latency"] = run_latency_comparison(source, scratch)
            args.out.write_text(json.dumps(metadata, indent=2) + "\n")
            return 0
        reference = scratch / "baseline.npy"
        for variant in variants(source, scratch):
            kind = variant["prepare"]
            if kind and kind not in metadata["preparations"]:
                started = time.perf_counter()
                log = scratch / f"prepare-{kind}.stderr"
                process = None
                try:
                    with log.open("wb") as stderr:
                        process = subprocess.Popen(
                            [sys.executable, __file__, "--internal-mode", "prepare",
                             "--kind", kind, "--source", source, "--scratch", directory],
                            stdout=stderr, stderr=stderr,
                        )
                    code = process.wait(timeout=600)
                    metadata["preparations"][kind] = {
                        "status": "passed" if code == 0 else "failed",
                        "exit_code": code, "seconds": time.perf_counter() - started,
                        "output": log.read_text(errors="replace")[-8000:],
                    }
                except subprocess.TimeoutExpired:
                    metadata["preparations"][kind] = {"status": "failed", "error": "conversion timeout"}
                finally:
                    if process is not None:
                        stop(process)
            if kind and metadata["preparations"][kind]["status"] == "failed":
                result = {**variant, "status": "failed", "error": "artifact preparation failed"}
            else:
                result = run_variant(variant, scratch, reference, args.max_procs)
            metadata["variants"].append(result)
            args.out.write_text(json.dumps(metadata, indent=2) + "\n")
            print(f"{variant['name']}: {result['status']} {result.get('error', '')}", flush=True)
            if variant["name"] == "V0" and result["status"] != "passed":
                return 1  # Without a valid baseline no other measurement is meaningful.
        metadata["artifacts"] = {
            path.name: path.stat().st_size
            for path in scratch.iterdir() if path.suffix in {".onnx", ".ort", ".data"}
        }
        args.out.write_text(json.dumps(metadata, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
