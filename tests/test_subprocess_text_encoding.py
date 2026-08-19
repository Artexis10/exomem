"""Every captured subprocess pipe must name its encoding.

`subprocess.run(..., text=True)` without `encoding=` decodes the child's
output with `locale.getencoding()`. On a Windows-native Python that is the
active code page -- cp1252 on a Western install, cp932/cp949/cp936 elsewhere --
so any byte the child emits outside that page raises `UnicodeDecodeError`
*inside the reader*, not at the call site. The traceback names `subprocess`,
never the caller, and the caller's `except OSError` does not catch it.

The bytes that trigger it are ordinary: a repository path under a user name
that is not Latin-1, a git branch or commit message with a typographic quote, a
worker's traceback quoting a page title. `git` and every Python child speak
UTF-8, so the encoding is not in doubt -- it was simply left unstated, and the
platform filled it in with the wrong answer.

Asserted over the source rather than by provoking a decode, because the failure
depends on the host's active code page: a check that only fires under cp1252
proves nothing on the Linux runners where most of CI lives, and `errors=` is a
policy this repo makes deliberately (`replace`, so a hook degrades instead of
crashing) rather than something a runtime probe can observe.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "exomem"


def _captures_text(call: ast.Call) -> bool:
    """Whether this call decodes a child's output into `str`."""
    keywords = {k.arg: k.value for k in call.keywords if k.arg}
    if not isinstance(keywords.get("text"), ast.Constant):
        return False
    if keywords["text"].value is not True:
        return False
    # `encoding=` implies text mode on its own, so a call that already names one
    # is compliant whatever `text=` says.
    return "encoding" not in keywords


def _subprocess_calls(tree: ast.AST) -> list[ast.Call]:
    """Calls into `subprocess`'s process-spawning surface."""
    spawners = {"run", "Popen", "check_output", "call", "check_call"}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr in spawners:
            value = function.value
            if isinstance(value, ast.Name) and value.id == "subprocess":
                found.append(node)
    return found


def _offenders() -> list[str]:
    offending = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _subprocess_calls(tree):
            if _captures_text(call):
                offending.append(f"{path.relative_to(SOURCE_ROOT).as_posix()}:{call.lineno}")
    return offending


def test_every_text_mode_subprocess_pins_its_encoding() -> None:
    assert _offenders() == [], (
        "these calls decode a child's output with the host's active code page; "
        'add encoding="utf-8" (and an explicit errors= policy)'
    )


def test_the_guard_recognises_an_unpinned_call() -> None:
    """A source-shape assertion is only worth its ability to fail."""
    unpinned = ast.parse(
        "import subprocess\n"
        "subprocess.run(['git', 'status'], capture_output=True, text=True)\n"
    )
    pinned = ast.parse(
        "import subprocess\n"
        "subprocess.run(\n"
        "    ['git', 'status'], capture_output=True, text=True, encoding='utf-8'\n"
        ")\n"
    )

    assert [_captures_text(call) for call in _subprocess_calls(unpinned)] == [True]
    assert [_captures_text(call) for call in _subprocess_calls(pinned)] == [False]


@pytest.mark.parametrize(
    "relative",
    [
        "_hooks/exomem_continuation_checkpoint.py",
        "deploy_provenance.py",
        "extract.py",
        "package_skills.py",
        "resource_status.py",
    ],
)
def test_the_repaired_callers_still_name_utf8(relative: str) -> None:
    """Name them, so a later edit that drops the encoding is a named regression."""
    source = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
    assert 'encoding="utf-8"' in source
