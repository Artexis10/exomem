"""Temporary allocation probe; remove after the CI-only failure is explained."""

import json
import os
import sys
import threading
import tracemalloc
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from _pytest.junitxml import bin_xml_escape, mangle_test_address

TARGET = "test_a_governed_session_previews_and_starts_without_loading_the_source"


def pytest_collection_finish(session):
    items = session.items
    if session.testsfailed:
        print("CI_SHARD", json.dumps({"retained_named": len(items),
              "selection_matches_original": None, "collection_failed": True}))
        return
    with zipfile.ZipFile(os.environ["PROBE_JUNIT_ZIP"]) as archive:
        xml_files = [name for name in archive.namelist() if name.endswith(".xml")]
        if len(xml_files) != 1:
            raise RuntimeError("The probe requires one original JUnit report")
        report = ElementTree.fromstring(archive.read(xml_files[0]))
    wanted = [(case.get("classname"), case.get("name"))
              for case in report.iter("testcase") if case.get("classname")]
    actual = []
    for item in items:
        # Reuse the pinned pytest writer so parameter IDs retain its exact JUnit form.
        names = mangle_test_address(item.nodeid)
        actual.append((".".join(names[:-1]), bin_xml_escape(names[-1])))
    if actual != wanted:
        raise RuntimeError("The selected shard or its order differs from the original report")
    targets = [index for index, item in enumerate(items) if item.name == TARGET]
    if len(targets) != 1:
        raise RuntimeError("The selected shard must contain the target once")
    print("CI_SHARD", json.dumps({"retained_named": len(items),
          "prefix_named": targets[0] + 1, "selection_matches_original": True}))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    result = yield
    if item.name == TARGET:
        # Stop after the complete protocol so teardown reports and timeout cleanup remain intact.
        session = item.session
        status = pytest.ExitCode.OK
        if session.testsfailed or session.shouldfail:
            status = pytest.ExitCode.TESTS_FAILED
        elif session.shouldstop:
            status = pytest.ExitCode.INTERRUPTED
        print("CI_STOP", json.dumps({"phase": "after_target_protocol",
              "retained_named": len(session.items), "exitstatus": int(status)}))
        pytest.exit("Diagnostic completed through target teardown", returncode=int(status))
    return result


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.name != TARGET:
        yield
        return
    original_start, original_stop = tracemalloc.start, tracemalloc.stop
    original_main, original_threads = sys.getprofile(), threading.getprofile()
    # The dispatch defines this two-state diagnostic switch; it changes no product setting.
    trace_enabled = os.environ.get("PROBE_TRACE", "1") == "1"
    records = []
    last_peak = 0

    def profile(frame, event, arg):
        nonlocal last_peak
        current, peak = tracemalloc.get_traced_memory()
        if peak <= last_peak + 500_000:
            return
        last_peak = peak
        stack = []
        while frame is not None and len(stack) < 16:
            stack.append([Path(frame.f_code.co_filename).name, frame.f_lineno, frame.f_code.co_name])
            frame = frame.f_back
        records.append({"current": current, "peak": peak, "event": event,
                        "thread": threading.current_thread().name, "stack": stack})

    def start(*args, **kwargs):
        print("CI_TRACE_BEFORE", json.dumps({"tracing": tracemalloc.is_tracing(),
              "profile_enabled": trace_enabled,
              "python": sys.version.split()[0], "threads": [t.name for t in threading.enumerate()]}))
        original_start(*args, **kwargs)
        if trace_enabled:
            threading.setprofile_all_threads(profile)

    def restore_profiles():
        if trace_enabled:
            threading.setprofile_all_threads(original_threads)
            sys.setprofile(original_main)

    def stop():
        restore_profiles()
        final = tracemalloc.get_traced_memory()
        original_stop()
        print("CI_PEAK_TRACE", json.dumps({"final": final, "records": records}))

    tracemalloc.start, tracemalloc.stop = start, stop
    try:
        yield
    finally:
        restore_profiles()
        tracemalloc.start, tracemalloc.stop = original_start, original_stop
