"""Temporary evidence for the unresolved CI hook fallback; remove after attribution."""

from __future__ import annotations

import ast
import shlex
import sys
from contextlib import contextmanager

_EMITTER = r'''
def _hook_diag_emit(record):
    try:
        with open(_HOOK_DIAG_PATH, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Observation must not change the hook's result.
'''

_OBSERVER = _EMITTER + r'''

def _hook_diag_output(value):
    if value is None:
        return {"stdout_bytes": None, "stdout_json_valid": None, "stdout_success": None}
    size = len(value if isinstance(value, bytes) else value.encode("utf-8"))
    try:
        payload = json.loads(value)
    except (ValueError, UnicodeError):
        return {"stdout_bytes": size, "stdout_json_valid": False, "stdout_success": None}
    success = payload.get("success") if isinstance(payload, dict) else None
    return {"stdout_bytes": size, "stdout_json_valid": True,
            "stdout_success": success if isinstance(success, bool) else None}


def _hook_diag_text(value, *, tail=False):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value or "")
    return text[-256:] if tail else text[:256]


def _hook_diag_call(phase, function, *args, **kwargs):
    start = {"phase": phase, "event": "start", "at": time.monotonic()}
    if phase == "cli":
        start["timeout"] = kwargs.get("timeout")
        start["executable_and_command"] = [str(part) for part in args[0][:2]]
    _hook_diag_emit(start)
    began = time.monotonic()
    original_popen = None
    if phase == "cli":
        original_popen = subprocess.Popen

        class ObservedPopen(original_popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                _hook_diag_emit({"phase": "process", "event": "start", "pid": self.pid,
                                 "at": time.monotonic()})

            def kill(self):
                _hook_diag_emit({"phase": "process", "event": "before_kill", "pid": self.pid,
                                 "at": time.monotonic(), "poll": self.poll()})
                return super().kill()

        subprocess.Popen = ObservedPopen
    try:
        result = function(*args, **kwargs)
    except BaseException as error:
        elapsed = time.monotonic() - began
        _hook_diag_emit({
            "phase": phase, "event": "exception",
            "elapsed": elapsed,
            "exception": type(error).__name__,
            "message_prefix": _hook_diag_text(error),
            "stdout_prefix": _hook_diag_text(getattr(error, "stdout", None)),
            "stderr_tail": _hook_diag_text(getattr(error, "stderr", None), tail=True),
            **_hook_diag_output(getattr(error, "stdout", None)),
        })
        raise
    finally:
        if original_popen is not None:
            subprocess.Popen = original_popen
    record = {"phase": phase, "event": "return", "elapsed": time.monotonic() - began}
    if phase == "cli":
        record.update(returncode=result.returncode,
                      stdout_prefix=_hook_diag_text(result.stdout),
                      stderr_tail=_hook_diag_text(result.stderr, tail=True),
                      **_hook_diag_output(result.stdout))
    elif phase == "json":
        record["payload_type"] = type(result).__name__
    else:
        record["outcome"] = "unusable" if result is None else "usable"
        record["hit_count"] = len(result) if isinstance(result, list) else None
    _hook_diag_emit(record)
    return result


def _hook_diag_rung(function):
    def observed(*args, **kwargs):
        return _hook_diag_call("rung", function, *args, **kwargs)
    return observed
'''

_CLI_OBSERVER = '''
import atexit
import json
import os
import sys
import threading
import time
''' + _EMITTER + r'''

def _hook_diag_child(phase, event, **extra):
    _hook_diag_emit({"phase": phase, "event": event, "pid": os.getpid(),
                     "at": time.monotonic(), **extra})

def _hook_diag_wrap(phase, function, *, watch_drain=False):
    def observed(*args, **kwargs):
        extra = {}
        if phase == "graph_drain":
            extra["threads"] = [{"name": t.name, "daemon": t.daemon}
                                for t in threading.enumerate()]
        _hook_diag_child(phase, "start", **extra)
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            _hook_diag_child(phase, "exception", exception=type(error).__name__)
            raise
        else:
            _hook_diag_child(phase, "return")
            return result
        finally:
            if watch_drain:
                module = sys.modules.get("exomem.graph_sync")
                if module is None:
                    _hook_diag_child("graph_drain", "observer_unavailable")
                else:
                    module.drain_active_rebuilds = _hook_diag_wrap(
                        "graph_drain", module.drain_active_rebuilds)
    return observed

_hook_diag_child("launcher", "start", interpreter=sys.executable, version=sys.version)
# This early callback is not proof that interpreter finalization completed.
atexit.register(_hook_diag_child, "atexit", "early_callback")
from exomem import __main__ as cli
sys.argv[0] = cli.__file__
# These are the native CLI's fixed execution boundaries, not inferred operation names.
cli._core_op_main = _hook_diag_wrap("core_operation", cli._core_op_main)
cli._run_cli = _hook_diag_wrap("run_cli", cli._run_cli, watch_drain=True)
try:
    result = _hook_diag_wrap("main", cli.main)()
finally:
    atexit.register(_hook_diag_child, "atexit", "late_callback")
sys.exit(result)
'''


@contextmanager
def observe_hook_cli(home, evidence):
    """Observe native CLI calls in the fixture; launcher overhead is diagnostic."""
    from membench.trackc import injection_ladder

    hook = home.hooks_dir / "exomem_retrieve_nudge.py"
    original = hook.read_bytes()
    tree = ast.parse(original, filename=str(hook))
    targets = [node for node in tree.body
               if isinstance(node, ast.FunctionDef) and node.name == "_fetch_via_cli"]
    assert len(targets) == 1, "CLI observation target changed"
    target = targets[0]
    observed = []

    class Calls(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            callee = node.func
            phase = None
            if isinstance(callee, ast.Name) and callee.id == "_parse_hits":
                phase = "hits"
            elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                # These are the fixed Python call sites whose failure the hook suppresses.
                phase = {("subprocess", "run"): "cli", ("json", "loads"): "json"}.get(
                    (callee.value.id, callee.attr))
            if phase is None:
                return node
            observed.append(phase)
            return ast.copy_location(ast.Call(
                func=ast.Name(id="_hook_diag_call", ctx=ast.Load()),
                args=[ast.Constant(phase), callee, *node.args],
                keywords=node.keywords,
            ), node)

    Calls().visit(target)
    assert sorted(observed) == ["cli", "hits", "json"], "CLI observation boundaries changed"
    target.decorator_list.append(ast.Name(id="_hook_diag_rung", ctx=ast.Load()))
    observer = ast.parse(_OBSERVER).body
    observer.insert(0, ast.Assign(
        targets=[ast.Name(id="_HOOK_DIAG_PATH", ctx=ast.Store())],
        value=ast.Constant(str(evidence)),
    ))
    position = tree.body.index(target)
    tree.body[position:position] = observer
    ast.fix_missing_locations(tree)
    evidence.write_text("", encoding="utf-8")
    original_factory = injection_ladder.make_exomem_shim

    def observed_factory(bin_dir):
        shim = original_factory(bin_dir)
        source = shim.read_text(encoding="utf-8")
        # The maintained fixture fixes this launcher protocol; do not parse arbitrary shell text.
        native_exec = f'exec "{sys.executable}" -m exomem "$@"\n'
        if source.count(native_exec) != 1:
            with evidence.open("a", encoding="utf-8") as stream:
                stream.write('{"phase":"launcher","event":"observer_unavailable"}\n')
            return shim
        launcher = bin_dir / "hook-cli-observer.py"
        launcher.write_text("_HOOK_DIAG_PATH = " + repr(str(evidence)) + "\n" + _CLI_OBSERVER,
                            encoding="utf-8")
        # Native functions and argv stay intact; importing the CLI changes module entry mechanics.
        observed_exec = f'exec {shlex.quote(sys.executable)} {shlex.quote(str(launcher))} "$@"\n'
        shim.write_text(source.replace(native_exec, observed_exec, 1), encoding="utf-8")
        return shim

    try:
        hook.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        injection_ladder.make_exomem_shim = observed_factory
        yield evidence
    finally:
        injection_ladder.make_exomem_shim = original_factory
        hook.write_bytes(original)
