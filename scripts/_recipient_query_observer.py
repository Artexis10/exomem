"""Temporary, aggregate-only journey observation pending timeout attribution."""

from __future__ import annotations

import contextlib
import gc
import json
import sys
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
    CPU deltas cover observed session entry to the first observed failure.
    Process CPU includes other service threads; it does not prove contention.
    Coarse phase CPU excludes new per-row samples. The first-check location
    and wall-clock anchor help align a failure with the service log.
    Parent phases and one largest hydration retain context without a call log.
    Derived row-source offsets cover wrapper intervals, not isolated SQL waits.
    GC totals cover collections in this query thread; unavailable clocks leave them null.
    Continuation failures retain bounded technical exception frames without values or messages.
    """
    originals = []
    gc_callback = None

    def restore():
        if gc_callback is not None:
            with contextlib.suppress(ValueError):
                gc.callbacks.remove(gc_callback)
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
        from exomem.governance import authorization_session_authority as authority
        from exomem.governance import store as authority_store
        from exomem.governance import (
            authorization_custody,
            policy as governance_policy,
            schema_v4,
            store as governance_store,
        )
        from exomem.query_engine import cursors, route, runtime, typed_rows

        state = threading.local()
        emission_lock = threading.Lock()
        emitted = False

        def observe_gc(phase, info):
            # The Python GC callback protocol fixes start/stop; retain only query-local timing.
            try:
                if phase == "start":
                    frame = getattr(state, "active", None)
                    state.gc_started = None if frame is None else (
                        frame, time.perf_counter(), time.thread_time(),
                    )
                elif phase == "stop":
                    started = getattr(state, "gc_started", None)
                    state.gc_started = None
                    if started is not None:
                        frame, elapsed, cpu = started
                        frame["gc"]["elapsed_ms"] += (time.perf_counter() - elapsed) * 1000
                        frame["gc"]["thread_cpu_ms"] += (time.thread_time() - cpu) * 1000
                        frame["gc"]["collections"] += 1
            except Exception:  # noqa: BLE001 - observation cannot alter collection or query execution
                frame = getattr(state, "active", None)
                if frame is not None:
                    frame["gc"] = None

        gc_callback = observe_gc
        gc.callbacks.append(gc_callback)

        def phase_cpu(label):
            # These fixed technical phases run a bounded number of times per query.
            if label in (
                "read_session.setup", "read_session.body", "open_query_reader", "authorization.init",
            ) or label.startswith(("authorization.input.", "authorization.policy.", "derived.row_source")):
                # These namespaces contain only the bounded constructor and row-source seams below.
                try:
                    return time.thread_time()
                except Exception:  # noqa: BLE001 - an unavailable clock remains unknown
                    pass
            return None

        def begin(label):
            try:
                frame = getattr(state, "active", None)
                if frame is not None:
                    mark = (label, time.perf_counter(), phase_cpu(label))
                    frame["stack"].append(mark)
                    return frame, mark
            except Exception:  # noqa: BLE001 - observation must not change execution
                pass
            return None

        def finish(measurement):
            try:
                if measurement is not None:
                    frame, mark = measurement
                    label, started, cpu_started = mark
                    cpu_ended = phase_cpu(label)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    labels = (label,)
                    if label == "authorization.init":
                        # Parent labels come only from this observer's fixed technical call boundaries.
                        parent = frame["stack"][-2][0] if len(frame["stack"]) > 1 else "root"
                        labels += (f"{label}@{parent}",)
                    elif label.startswith("authorization.input."):
                        # Immediate constructor children use its fixed caller phase.
                        parent = frame["stack"][-3][0] if len(frame["stack"]) > 2 else "root"
                        labels += (f"{label}@{parent}",)
                    for key in labels:
                        phase = frame["phases"].setdefault(
                            key, {"elapsed_ms": 0.0, "calls": 0,
                                  "thread_cpu_ms": 0.0 if cpu_started is not None else None},
                        )
                        phase["elapsed_ms"] += elapsed_ms
                        phase["calls"] += 1
                        if cpu_started is None or cpu_ended is None:
                            phase["thread_cpu_ms"] = None
                        elif phase["thread_cpu_ms"] is not None:
                            phase["thread_cpu_ms"] += (cpu_ended - cpu_started) * 1000
                        if label.startswith("derived.row_source"):
                            clock = getattr(state, "query_clock", None)
                            first = (started - clock[0]) * 1000 if clock else None
                            last = first + elapsed_ms if first is not None else None
                            # First/last wrapper offsets may span gaps between calls; they are not a duration sum.
                            phase.setdefault("first_started_after_query_ms", first)
                            phase["last_finished_after_query_ms"] = last
                    if label == "typed.selected_current_values":
                        previous = frame["largest_selected_values"]
                        clock = getattr(state, "query_clock", None)
                        if previous is None or elapsed_ms > previous["elapsed_ms"]:
                            frame["largest_selected_values"] = {
                                "elapsed_ms": elapsed_ms,
                                "started_after_query_ms": (started - clock[0]) * 1000 if clock else None,
                                "parent_phases": [entry[0] for entry in frame["stack"][:-1]],
                            }
                    frame["stack"].pop()
            except Exception:  # noqa: BLE001 - observation must not change execution
                pass

        def replace(owner, name, wrapper):
            original = getattr(owner, name)
            originals.append((owner, name, original, wrapper))
            setattr(owner, name, wrapper)

        def measure(owner, name, label, *, parent_only=None):
            original = getattr(owner, name)

            @wraps(original)
            def call(*args, **kwargs):
                if parent_only is not None:
                    frame = getattr(state, "active", None)
                    if frame is None or not frame["stack"] or frame["stack"][-1][0] != parent_only:
                        return original(*args, **kwargs)
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

        # These direct metadata seams are fixed API calls; they collect no SQL or row content.
        for name in ("is_derived", "target", "assess"):
            measure(derived_rows, name, f"derived.row_source.{name}", parent_only="derived.row_source")

        # Measure direct inputs at the consumer alias; nested calls are excluded.
        for owner, name, label in (
            (governance.policy, "load", "policy_load"),
            (governance, "effective_principal", "principal"),
            (governance.lifecycle, "tombstoned_paths", "tombstones"),
            (governance.access, "_policy_state", "access_policy"),
            (authority_store, "open_authorization_session_connection", "authority_connection"),
            (authority, "active_session_purpose", "session_purpose"),
            (governance.egress, "_declared_purpose", "declared_purpose"),
        ):
            measure(owner, name, f"authorization.input.{label}", parent_only="authorization.init")

        # Split the broad policy-load span into fixed acquisition and compile seams.
        # These wrappers run only inside this query's policy load and record no paths or documents.
        measure(
            governance_policy,
            "_load_v4_active_policy",
            "authorization.policy.v4_active",
            parent_only="authorization.input.policy_load",
        )
        for owner, name, label in (
            (authorization_custody, "load_authorization_custody", "custody_load"),
            (governance_store, "_initial_policy_read", "store_acquire"),
            (governance_policy, "_activation_store_family_probe", "activation_store_probe"),
            (schema_v4, "load_active_policy", "active_policy_read"),
            (governance_policy, "compile_prospective", "workspace_snapshot"),
        ):
            measure(
                owner,
                name,
                f"authorization.policy.{label}",
                parent_only="authorization.policy.v4_active",
            )
        for owner, name, label in (
            (governance_policy, "_clear_guard_generation", "workspace_guard"),
            (governance_policy, "_probe_authoring_tree", "workspace_tree_probe"),
            (governance_policy, "compile_documents", "workspace_compile"),
        ):
            measure(
                owner,
                name,
                f"authorization.policy.{label}",
                parent_only="authorization.policy.workspace_snapshot",
            )

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
                        frame["failure_phase_stack"] = [mark[0] for mark in frame["stack"]]
                        caller = sys._getframe(1)
                        module = caller.f_globals.get("__name__", "")
                        if module.startswith("exomem.query_engine."):
                            frame["failure_check_caller"] = {
                                "module": module, "function": caller.f_code.co_name,
                                "line": caller.f_lineno,
                            }
                        parent = caller.f_back
                        if parent is not None:
                            module = parent.f_globals.get("__name__", "")
                            # Native storage checks the same deadline; retain its technical frame too.
                            if module.startswith(("exomem.query_engine.", "exomem.collection_store.")):
                                frame["failure_check_parent"] = {
                                    "module": module, "function": parent.f_code.co_name,
                                    "line": parent.f_lineno,
                                }
                        frame["timeout_ms"] = session.limits.timeout_ms
                        frame["query_deadline_elapsed_ms"] = deadline_elapsed(session)
                        started = getattr(state, "query_clock", None)
                        if started is not None:
                            ended = (time.perf_counter(), time.thread_time(), time.process_time())
                            for name, before, after in zip(
                                ("observed_query_window_ms", "query_thread_cpu_ms",
                                 "query_process_cpu_ms"),
                                started, ended,
                                strict=True,
                            ):
                                frame[name] = (after - before) * 1000
                except Exception:  # noqa: BLE001 - preserve the original query exception
                    pass
                raise

        replace(runtime.ReadSession, "check", check)
        original_session = runtime.read_session

        @contextlib.contextmanager
        @wraps(original_session)
        def read_session(*args, **kwargs):
            measurement = begin("read_session.setup")
            previous_clock = getattr(state, "query_clock", None)
            try:
                state.query_clock = None
                state.query_clock = (time.perf_counter(), time.thread_time(), time.process_time())
                frame = getattr(state, "active", None)
                if frame is not None:
                    frame["query_started_unix_ns"] = time.time_ns()
            except Exception:  # noqa: BLE001 - observation must not change execution
                pass
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
                with contextlib.suppress(Exception):
                    state.query_clock = previous_clock

        replace(runtime, "read_session", read_session)
        original_route = route.run

        def emit(frame, error, started):
            nonlocal emitted
            try:
                with emission_lock:
                    if emitted:
                        return
                    emitted = True  # One bounded failure record per observed child, even if I/O fails.
                    causes, cause = [], error
                    for _ in range(4):
                        if cause is None:
                            break
                        frames, trace = [], cause.__traceback__
                        while trace is not None and len(frames) < 8:
                            module = trace.tb_frame.f_globals.get("__name__", "")
                            # Fixed query, storage and crypto namespaces expose no paths or local values.
                            if module.startswith(("exomem.query_engine.", "exomem.collection_store.", "cryptography.fernet")):
                                frames.append({"module": module, "function": trace.tb_frame.f_code.co_name,
                                               "line": trace.tb_lineno})
                            trace = trace.tb_next
                        causes.append({"type": type(cause).__name__, "frames": frames})
                        cause = cause.__cause__ or cause.__context__
                    record = {"observer": "journey-query-timeout-v1",
                              "error": {"code": error.code, "type": type(error).__name__},
                              "exception_causes": causes,
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
                         "gc": {"elapsed_ms": 0.0, "thread_cpu_ms": 0.0, "collections": 0},
                         "query_deadline_elapsed_ms": None, "failure_phase_stack": None,
                         "observed_query_window_ms": None, "query_thread_cpu_ms": None,
                         "query_process_cpu_ms": None,
                         "query_started_unix_ns": None, "failure_check_caller": None,
                         "failure_check_parent": None, "largest_selected_values": None,
                         "body_returned": None, "body_return_deadline_elapsed_ms": None}
                started = time.perf_counter()
            except Exception:  # noqa: BLE001 - observation must not change execution
                return original_route(*args, **kwargs)
            state.active = frame
            try:
                return original_route(*args, **kwargs)
            except runtime.QueryError as error:
                # These are fixed query refusal codes, not classifications of user input.
                if error.code in ("QUERY_TIMEOUT", "QUERY_CURSOR_INVALID"):
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
