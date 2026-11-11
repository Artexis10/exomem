"""No module may call `os.sync`: it flushes every filesystem the host mounts.

One wedged mount — a stalled FUSE daemon, a hung network volume — then freezes the
caller forever in uninterruptible sleep, though none of its files is Exomem's.
Durability is scoped: `os.fsync` on the file's descriptor, then on the directory
that holds its name (`cloud_import.flush_to_disk` for a written tree).
"""
from __future__ import annotations

import ast
from pathlib import Path

import exomem

SOURCE = Path(exomem.__file__).resolve().parent


def _os_sync_lines(path: Path) -> list[int]:
    """Lines where `os.sync` is used under any spelling: `os.sync()`, `from os import sync`, `getattr(os, "sync")`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "sync"
                and isinstance(node.value, ast.Name) and node.value.id == "os"):
            lines.append(node.lineno)
        elif (isinstance(node, ast.ImportFrom) and node.module == "os"
                and any(alias.name == "sync" for alias in node.names)):
            lines.append(node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "os"
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == "sync"):
            lines.append(node.lineno)
    return lines


def test_no_module_calls_os_sync() -> None:
    """Catches a host-wide `os.sync` returning to `src/exomem` under any spelling."""
    offenders = {
        path.relative_to(SOURCE).as_posix(): lines
        for path in sorted(SOURCE.rglob("*.py"))
        if (lines := _os_sync_lines(path))
    }

    assert offenders == {}
