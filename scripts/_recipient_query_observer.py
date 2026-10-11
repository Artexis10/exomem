"""Temporary, aggregate-only journey observation pending timeout attribution."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from functools import wraps
from pathlib import Path

LOG_NAME = "journey-query-timeout.jsonl"


@contextlib.contextmanager
def observe_query_timeouts(log_path: Path):
    """Observe only this test process; leave query results and exceptions intact.

    Nested durations overlap and include observer overhead. They do not identify
    host I/O or garbage collection as a cause.
    ``read_session.finish`` includes the final temporary-state check and teardown.
    """
    originals = []

    def restore():
        for owner, name, original, wrapper in reversed(originals):
            with contextlib.suppress(Exception):
                if getattr(owner, name, None) is wrapper:
                    setattr(owner, name, original)

    try:
        from exomem.collection_store import (
            connection,
            derived_rows,
            field_admission,
            governance,
            typed_storage,
        )
        from exomem.query_engine import cursors, route, runtime, typed_rows

        state = threading.local()
        emission_lock = threading.Lock()
        emitted = False

        def begin(label):
            try:
                frame = getattr(state, "active", None)
                if frame is not None:
                    mark = (label, time.perf_counter())
                    frame["stack"].append(mark)
                    return frame, mark
            except Exception:  # noqa: BLE001 - observation must not change execution
                pass
            return None

        def finish(measurement):
            try:
                if measurement is not None:
                    frame, mark = measurement
                    label, started = mark
                    phase = frame["phases"].setdefault(label, {"elapsed_ms": 0.0, "calls": 0})
                    phase["elapsed_ms"] += (time.perf_counter() - started) * 1000
                    phase["calls"] += 1
                    frame["stack"].pop()
            except Exception:  # noqa: BLE001 - observation must not change execution
                pass

        def replace(owner, name, wrapper):
            original = getattr(owner, name)
            originals.append((owner, name, original, wrapper))
            setattr(owner, name, wrapper)

        def measure(owner, name, label):
            original = getattr(owner, name)

            @wraps(original)
            def call(*args, **kwargs):
                measurement = begin(label)
                try:
                    return original(*args, **kwargs)
                finally:
                    finish(measurement)

            replace(owner, name, call)

        # These fixed targets are technical call boundaries, not user vocabulary.
        for owner, name, label in (
            (connection, "open_query_reader", "open_query_reader"),
            (derived_rows, "attach", "derived.attach"),
            (derived_rows, "row_source", "derived.row_source"),
            (governance.OperationAuthorization, "__init__", "authorization.init"),
            (governance.OperationAuthorization, "field_plan", "authorization.field_plan"),
            (governance.OperationAuthorization, "summary_release", "authorization.summary_release"),
            (field_admission, "canonical_basis", "field_admission.canonical_basis"),
            (runtime.ReadSession, "selected_values", "selected_values"),
            (typed_storage, "selected_current_values", "typed.selected_current_values"),
            (typed_rows, "admit_query", "admit_query"),
            (cursors, "_binding", "cursor.binding"),
            (cursors, "_visible", "cursor.visible"),
            (cursors, "execute_rows", "execute_rows"),
        ):
            measure(owner, name, label)

        def deadline_elapsed(session):
            return (time.monotonic() - session._deadline + session.limits.timeout_ms / 1000) * 1000

        original_check = runtime.ReadSession.check

        @wraps(original_check)
        def check(session):
            try:
                return original_check(session)
            except runtime.QueryError as error:
                try:
                    frame = getattr(state, "active", None)
                    if error.code == "QUERY_TIMEOUT" and frame is not None and frame["failure_phase_stack"] is None:
                        frame["failure_phase_stack"] = [label for label, _ in frame["stack"]]
                        frame["timeout_ms"] = session.limits.timeout_ms
                        frame["query_deadline_elapsed_ms"] = deadline_elapsed(session)
                except Exception:  # noqa: BLE001 - preserve the original query exception
                    pass
                raise

        replace(runtime.ReadSession, "check", check)
        original_session = runtime.read_session

        @contextlib.contextmanager
        @wraps(original_session)
        def read_session(*args, **kwargs):
            measurement = begin("read_session.setup")
            try:
                with original_session(*args, **kwargs) as session:
                    finish(measurement)
                    measurement = begin("read_session.body")
                    frame = getattr(state, "active", None)
                    if frame is not None:
                        frame["body_returned"] = False
                    try:
                        yield session
                        try:
                            if frame is not None:
                                frame["body_returned"] = True
                                frame["body_return_deadline_elapsed_ms"] = deadline_elapsed(session)
                        except Exception:  # noqa: BLE001 - observation must not change execution
                            pass
                    finally:
                        finish(measurement)
                        measurement = begin("read_session.finish")
            finally:
                finish(measurement)

        replace(runtime, "read_session", read_session)
        original_route = route.run

        def emit(frame, error, started):
            nonlocal emitted
            try:
                with emission_lock:
                    if emitted:
                        return
                    emitted = True  # One bounded failure record per observed child, even if I/O fails.
                    record = {"observer": "journey-query-timeout-v1",
                              "error": {"code": error.code, "type": type(error).__name__},
                              "route_elapsed_ms": (time.perf_counter() - started) * 1000,
                              **{key: value for key, value in frame.items() if key != "stack"},
                              "phase_timings_overlap": True}
                    encoded = json.dumps(record, allow_nan=False) + "\n"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(encoded)
            except Exception:  # noqa: BLE001 - encoding and I/O cannot replace the original exception
                pass

        @wraps(original_route)
        def run(*args, **kwargs):
            previous = getattr(state, "active", None)
            try:
                frame = {"stack": [], "phases": {}, "timeout_ms": None,
                         "query_deadline_elapsed_ms": None, "failure_phase_stack": None,
                         "body_returned": None, "body_return_deadline_elapsed_ms": None}
                started = time.perf_counter()
            except Exception:  # noqa: BLE001 - observation must not change execution
                return original_route(*args, **kwargs)
            state.active = frame
            try:
                return original_route(*args, **kwargs)
            except runtime.QueryError as error:
                if error.code == "QUERY_TIMEOUT":
                    emit(frame, error, started)
                raise
            finally:
                state.active = previous

        replace(route, "run", run)
    except Exception:  # noqa: BLE001 - incomplete observation must not prevent the test service
        restore()
    try:
        yield
    finally:
        restore()
