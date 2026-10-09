"""Temporary evidence for the unresolved CI hook fallback; remove after attribution."""

from __future__ import annotations

import ast
from contextlib import contextmanager

_OBSERVER = r'''
def _hook_diag_emit(record):
    try:
        with open(_HOOK_DIAG_PATH, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Observation must not change the hook's result.


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
    try:
        result = function(*args, **kwargs)
    except BaseException as error:
        _hook_diag_emit({
            "phase": phase, "event": "exception",
            "elapsed": time.monotonic() - began,
            "exception": type(error).__name__,
            "message_prefix": _hook_diag_text(error),
            "stdout_prefix": _hook_diag_text(getattr(error, "stdout", None)),
            "stderr_tail": _hook_diag_text(getattr(error, "stderr", None), tail=True),
        })
        raise
    record = {"phase": phase, "event": "return", "elapsed": time.monotonic() - began}
    if phase == "cli":
        record.update(returncode=result.returncode,
                      stdout_prefix=_hook_diag_text(result.stdout),
                      stderr_tail=_hook_diag_text(result.stderr, tail=True))
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


@contextmanager
def observe_hook_cli(home, evidence):
    """Observe the private hook copy without changing its calls or deadlines."""
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
    try:
        hook.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        yield evidence
    finally:
        hook.write_bytes(original)
