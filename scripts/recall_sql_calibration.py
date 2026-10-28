"""Lexical calibration through the maintained bound-command worker.

This module observes application queries. It contains no replacement retrieval
SQL and does not decide absolute, installed or original-series acceptance.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path

# The packaged contract fixes these two lexical timing rows.
SQL_ROWS = ("governed-stage-bm25", "governed-stage-keyword")


class SqlObserver:
    """Observe real selection handles without changing their type or lifetime."""

    def __init__(self, digest) -> None:
        # Retain closed objects so Python cannot reuse an identity between calls.
        self.previous: dict[int, sqlite3.Connection] = {}
        self.digest = digest

    @contextmanager
    def observe(self):
        from exomem import find, lexstore

        owner = threading.get_native_id()
        self.connections = {}
        self.query_connections = set()
        self.closed = set()
        self.rows = {}
        self.work = []
        capture = lexstore.LexicalStore._capture_read_view
        close = lexstore._CatalogReadView.close
        # Resolve the complete observer interface before changing any method.
        queries = {name: getattr(lexstore.LexicalStore, name)
                   for name in ("_bm25_query", "_substring_query", "_keyword_groups_query")}
        work = {name: getattr(find, name) for name in ("_resolve_page", "_keyword_hit")}
        originals = []

        @wraps(capture)
        def measured_capture(store, *args, **kwargs):
            view = capture(store, *args, **kwargs)
            if threading.get_native_id() == owner and view is not None and view.connection is not None:
                self.connections[id(view.connection)] = view.connection
            return view

        @wraps(close)
        def measured_close(view):
            connection = view.connection
            try:
                return close(view)
            finally:
                if threading.get_native_id() == owner and id(connection) in self.connections:
                    try:
                        connection.execute("SELECT 1")
                    except sqlite3.ProgrammingError:
                        self.closed.add(id(connection))

        lexstore.LexicalStore._capture_read_view = measured_capture
        lexstore._CatalogReadView.close = measured_close
        # These are maintained SQL-query boundaries, not interpretations of SQL text.
        for name, original in queries.items():
            originals.append((lexstore.LexicalStore, name, original))

            @wraps(original)
            def measured_query(store, conn, *args, _original=original, _name=name, **kwargs):
                result = _original(store, conn, *args, **kwargs)
                if threading.get_native_id() == owner:
                    self.query_connections.add(id(conn))
                    self.rows.setdefault(_name, []).append(result)
                return result

            setattr(lexstore.LexicalStore, name, measured_query)
        # These boundaries make page resolution and hit construction observable.
        for name, original in work.items():
            originals.append((find, name, original))

            @wraps(original)
            def measured_work(*args, _original=original, _name=name, **kwargs):
                if threading.get_native_id() != owner:
                    return _original(*args, **kwargs)
                started = time.perf_counter_ns()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.work.append((_name, started, time.perf_counter_ns(), owner))

            setattr(find, name, measured_work)
        try:
            yield
        finally:
            lexstore.LexicalStore._capture_read_view = capture
            lexstore._CatalogReadView.close = close
            for target, name, original in originals:
                setattr(target, name, original)

    def result(self, hits, trace, *, projection: str) -> dict:
        rows = {name: [[asdict(row) if is_dataclass(row) else row for row in batch]
                       for batch in batches] for name, batches in self.rows.items()}
        captured = set(self.connections)
        fresh = not (captured & self.previous.keys()) if captured else None
        self.previous.update(self.connections)
        inside = None
        if projection == "keyword" and self.work:
            inside = all(any(
                interval.name == "keyword" and interval.thread == thread
                and interval.start <= start <= end <= interval.end
                for interval in trace.intervals if interval.end is not None
            ) for _name, start, end, thread in self.work)
        return {
            "connections": len(captured), "query_connections": len(self.query_connections),
            "fresh": fresh, "closed": captured <= self.closed if captured else None,
            "queries_bound": self.query_connections <= captured if self.query_connections else None,
            "keyword_page_resolutions": sum(name == "_resolve_page" for name, *_rest in self.work),
            "keyword_hit_builds": sum(name == "_keyword_hit" for name, *_rest in self.work),
            "keyword_work_inside_row": inside,
            "bm25_digest": self.digest(rows.get("_bm25_query", [])),
            "keyword_digest": self.digest([rows.get("_substring_query", []),
                                           rows.get("_keyword_groups_query", [])]),
            "hits_digest": self.digest(hits),
        }


def projected_requests(gate, cases: list[dict]):
    """Retain every case; only the declared lexical projection changes."""
    for principal in ("owner", "admitted"):
        for case in cases:
            for mode in ("hybrid", "keyword"):
                arguments = {**case["arguments"], "mode": mode, "result_level": "page"}
                yield {**case, "principal": principal, "arguments": arguments,
                       "projection": mode, "band": gate.reference_word_band(case["query"]),
                       "arguments_digest": gate._document_digest(arguments)}


def summarize_rows(gate, samples: list[dict]) -> list[dict]:
    groups = {}
    for sample in samples:
        # The row names are keys of the packaged latency contract.
        for row in SQL_ROWS:
            if sample["projection"] == "keyword" and row == "governed-stage-bm25":
                continue
            key = (sample["principal"], sample["projection"], sample["series"], sample["band"], row)
            groups.setdefault(key, []).append(sample)
            groups.setdefault((key[0], key[1], None, key[3], row), []).append(sample)
    report = []
    for (principal, projection, series, band, row), selected in groups.items():
        values = [sample["rows"][row] for sample in selected
                  if sample["state"] in {"ok", "empty"} and sample["rows"][row] is not None]
        budget = gate.CONTRACT.bar(row)
        sufficient = len(values) >= budget.min_samples
        report.append({"principal": principal, "projection": projection, "series": series,
                       "band": band, "row": row, "samples": len(values),
                       "observations": len(selected),
                       "empty": sum(s["state"] == "empty" for s in selected),
                       "warming": sum(s["state"] == "warming" for s in selected),
                       "unknown": sum(s["state"] == "unknown" for s in selected),
                       "state": "measured" if sufficient else "insufficient",
                       "p50_ms": gate._percentile(values, .5) if sufficient else None,
                       "p95_ms": gate._percentile(values, .95) if sufficient else None,
                       "budget_ms": budget.threshold_ms, "twice_budget_applied": False})
    return report


def run(gate, output: Path, *, quiescence_bound: float,
        python: Path | None = None, runtime_prefix: Path | None = None) -> int:
    import tempfile

    profile = json.loads((gate.BENCHMARK / "profile.json").read_text())
    cases, prose_digest = gate.reference_cases(profile)
    requests = list(projected_requests(gate, cases))
    expected = {(r["id"], r["principal"], r["projection"]) for r in requests}
    report = {"schema_version": 1, "mode": "sql-calibration", "state": "not measured",
              "contract_revision": gate.CONTRACT.revision,
              "source_commit": gate._git_bytes("rev-parse", "HEAD").decode().strip(),
              "source_clean": not bool(gate._git_bytes("status", "--porcelain")),
              "profile_digest": gate._document_digest(profile),
              "cases_digest": gate._document_digest(cases), "prose_digest": prose_digest,
              "expected_samples": len(requests), "completed_samples": 0, "coverage_complete": False,
              "samples": [], "rows": [], "environment": None,
              "unmeasured": ["encoder", "original-series-acceptance", "installed-acceptance"]}
    try:
        if not expected or len(expected) != len(requests):
            raise gate.MeasurementRefused("reference-case-identities-invalid")
        first, final, waited = gate.await_quiescence(
            gate.one_minute_load, bound_seconds=quiescence_bound, sleep=time.sleep,
        )
        report["load"] = {"first": first, "final": final, "waited_seconds": waited}
        if final > gate.MAX_LOAD_AVERAGE:
            report.update(state="refused", reason="load-above-reference")
        else:
            cpus = sorted(gate.os.sched_getaffinity(0))[:gate.REFERENCE["reference_cpus"]]
            if len(cpus) != gate.REFERENCE["reference_cpus"]:
                raise gate.MeasurementRefused("two-cpu-reference-unavailable")
            with tempfile.TemporaryDirectory(prefix="exomem-recall-sql-") as temporary, ExitStack() as cleanup:
                scratch = Path(temporary)
                worker = gate._Worker(
                    gate.ROOT, scratch / "vault", scratch / "state", cpus, profile,
                    python=python, runtime_prefix=runtime_prefix, calibration="sql",
                    runtime_policy="external-calibration-worker" if python else "parent-benchmark",
                )
                cleanup.callback(worker.close)
                report["environment"] = worker.environment
                if worker.environment["sqlite_version"] not in profile["sqlite_versions"]:
                    raise gate.MeasurementRefused("sqlite-outside-reference")
                for principal in ("owner", "admitted"):
                    # Exclude one warm-up per population and projection for each caller.
                    warmup = {(r["series"], r["projection"]): r for r in reversed(requests)
                              if r["principal"] == principal}
                    for request in warmup.values():
                        worker.exchange({**request, "rows": gate.REFERENCE["stage_rows"]})
                    for request in requests:
                        if request["principal"] != principal:
                            continue
                        sample = worker.exchange({**request, "rows": gate.REFERENCE["stage_rows"]})
                        report["samples"].append({
                            "case": request["id"], "series": request["series"], "principal": principal,
                            "projection": request["projection"], "band": request["band"],
                            "arguments_digest": request["arguments_digest"], "state": sample["state"],
                            "load": gate.one_minute_load(), "elapsed_ms": sample["elapsed_ms"],
                            "setup_ms": sample["sql_rows"].get("governed-stage-request-setup"),
                            "rows": {row: sample["sql_rows"].get(row) for row in SQL_ROWS},
                            "sql": sample["sql"],
                        })
                report["rows"] = summarize_rows(gate, report["samples"])
                report["state"] = "measured" if all(
                    s["state"] in {"ok", "empty"} and s["sql"]["fresh"] is True
                    and s["sql"]["closed"] is True and s["sql"]["queries_bound"] is not False
                    and s["sql"]["keyword_work_inside_row"] is not False
                    and s["rows"]["governed-stage-keyword"] is not None
                    and (s["projection"] == "keyword" or s["rows"]["governed-stage-bm25"] is not None)
                    for s in report["samples"]
                ) else "failed"
    except (ValueError, OSError) as error:
        report.update(state=("failed" if report["samples"] else "refused")
                      if isinstance(error, gate.MeasurementRefused) else "not measured",
                      reason=error.code if isinstance(error, gate.MeasurementRefused) else type(error).__name__,
                      phase=(error.phase or "sql-calibration") if isinstance(error, gate.MeasurementRefused)
                      else "sql-calibration")
    finally:
        report["completed_samples"] = len(report["samples"])
        observed = {(s["case"], s["principal"], s["projection"]) for s in report["samples"]}
        report["coverage_complete"] = observed == expected and len(report["samples"]) == len(requests)
    if report["state"] == "measured" and not report["coverage_complete"]:
        report["state"] = "failed"
    gate.validate_document(report, "sql-calibration.schema.json")
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"state": report["state"], "artifact": output.name}))
    return 0
