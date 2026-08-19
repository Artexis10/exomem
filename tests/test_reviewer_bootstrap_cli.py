"""The promotion harness CLI must define every option its dispatcher reads.

`--openai-redirect` was documented in `--openai-connector`'s help text and in
`chatgpt_cimd_identity`'s docstring, and read by `main()`, but never added to the
parser. `run` therefore raised `AttributeError` before doing any work -- and `run`
is the command that spends a one-shot promotion window, so the failure could only
be discovered at the one moment it was most expensive.

These tests read the parser and the dispatcher rather than a hand-kept list, so a
future option that is consumed but not declared fails here instead of live.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "reviewer_bootstrap.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reviewer_bootstrap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _main_function() -> ast.FunctionDef:
    tree = ast.parse(SCRIPT.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("scripts/reviewer_bootstrap.py has no main()")


def _attributes_read_from_args(main: ast.FunctionDef) -> set[str]:
    return {
        node.attr
        for node in ast.walk(main)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    }


def _declared_destinations(main: ast.FunctionDef) -> set[str]:
    declared: set[str] = set()
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        dest = next(
            (kw.value.value for kw in node.keywords if kw.arg == "dest"),
            None,
        )
        if isinstance(dest, str):
            declared.add(dest)
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        long = next((f for f in flags if f.startswith("--")), None)
        if long is not None:
            declared.add(long[2:].replace("-", "_"))
        elif flags:
            declared.add(flags[0].replace("-", "_"))
    return declared


def test_every_option_main_reads_is_declared() -> None:
    main = _main_function()
    missing = _attributes_read_from_args(main) - _declared_destinations(main)
    assert not missing, (
        "main() reads argparse attributes that no add_argument() declares: "
        f"{sorted(missing)}. The command would raise AttributeError at runtime."
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["preflight", "--candidate-id", "c", "--state-dir", "/tmp/s"],
        ["prepare", "--candidate-id", "c", "--state-dir", "/tmp/s", "--email", "a@b.c"],
        [
            "run",
            "--candidate-id",
            "c",
            "--state-dir",
            "/tmp/s",
            "--token",
            "t",
            "--openai-connector",
            "6UNqc_HaufBZ",
        ],
    ],
    ids=("preflight", "prepare", "run"),
)
def test_each_command_parses_and_exposes_every_attribute(argv: list[str], monkeypatch) -> None:
    module = _load_module()
    read = _attributes_read_from_args(_main_function())

    captured: dict[str, object] = {}

    class _StopAfterParse(Exception):
        pass

    original = module.ControlPlane

    class _Probe(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            raise _StopAfterParse

    monkeypatch.setattr(module, "ControlPlane", _Probe)
    monkeypatch.setattr(sys, "argv", ["reviewer_bootstrap.py", *argv])
    monkeypatch.setenv("EXOMEM_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("EXOMEM_ADMIN_TOKEN", "token")

    real_parse = module.argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        namespace = real_parse(self, *a, **k)
        captured["ns"] = namespace
        return namespace

    monkeypatch.setattr(module.argparse.ArgumentParser, "parse_args", _capture)

    with pytest.raises(_StopAfterParse):
        module.main()

    namespace = captured["ns"]
    for attribute in sorted(read):
        assert hasattr(namespace, attribute), (
            f"`{' '.join(argv[:1])}` parses without an `args.{attribute}`, which main() reads"
        )


def test_openai_redirect_is_declared_and_documented() -> None:
    assert "openai_redirect" in _declared_destinations(_main_function())

    help_text = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--openai-redirect" in help_text, (
        "the operator is told to use this flag when the connector document is "
        "unreadable from their network; it has to appear in --help"
    )
