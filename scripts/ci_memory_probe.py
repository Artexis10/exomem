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

TARGET = "test_a_governed_session_previews_and_starts_without_loading_the_source"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    with zipfile.ZipFile(os.environ["PROBE_JUNIT_ZIP"]) as archive:
        xml_files = [name for name in archive.namelist() if name.endswith(".xml")]
        if len(xml_files) != 1:
            raise RuntimeError("The probe requires one original JUnit report")
        report = ElementTree.fromstring(archive.read(xml_files[0]))
    wanted = []
    for case in report.iter("testcase"):
        if case.get("classname"):
            wanted.append((case.get("classname"), case.get("name")))
        if case.get("name") == TARGET:
            break
    else:
        raise RuntimeError("The original report does not contain the target")
    by_id = {}
    for item in items:
        # Pytest node IDs use ::; JUnit writes module and class names with dots.
        parts = item.nodeid.split("::")
        module = parts[0].removesuffix(".py").replace("/", ".")
        classname = ".".join([module, *parts[1:-1]])
        by_id[(classname, parts[-1])] = item
    missing = [pair for pair in wanted if pair not in by_id]
    if missing:
        raise RuntimeError(f"CI prefix cases absent from this exact source: {missing}")
    selected = [by_id[pair] for pair in wanted]
    selected_set = set(selected)
    config.hook.pytest_deselected(items=[item for item in items if item not in selected_set])
    items[:] = selected
    print("CI_PREFIX", json.dumps({"named_cases": len(selected), "last": selected[-1].name}))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.name != TARGET:
        yield
        return
    original_start, original_stop = tracemalloc.start, tracemalloc.stop
    original_main, original_threads = sys.getprofile(), threading.getprofile()
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
              "python": sys.version.split()[0], "threads": [t.name for t in threading.enumerate()]}))
        original_start(*args, **kwargs)
        threading.setprofile_all_threads(profile)

    def restore_profiles():
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
