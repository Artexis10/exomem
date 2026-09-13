"""D4: benchmarks/reports/ must never transitively reach a network-capable or
provider-execution module.

Mirrors the AST-walking technique of scripts/generate_harness_modules.py:29-60
(`_imported_top_level_names`: parse a file, collect every import name at any
nesting depth) but answers a different question: not "does this test file
touch the harness at all", but "does the reports/ package's *own* module-level
import graph ever reach one of a fixed set of forbidden modules".

Two different rules for two different trust levels. Files INSIDE
benchmarks/reports/ are OUR code: every scope is scanned -- module, class,
function, async function, arbitrarily nested -- because a function-scoped
forbidden import in reports/ itself (even one no code path calls yet) is
exactly the kind of latent dependency this test exists to catch; a
module-level-only scan would let `def render_all(): import httpx` inside
render.py itself pass clean. Modules reached transitively OUTSIDE the
package (lme/report.py, protocol/offline.py, and whatever they import in
turn) keep the narrower, load-time-only rule: only imports that actually
execute when a module is *loaded* -- module- and class-scope statements --
are followed as graph edges and checked against the forbidden set there. A
`def`-nested import in one of THOSE external files is guarded exactly the
way benchmarks/membench/judge/backends.py:486 guards its own `import httpx`
("lazy: offline flows must not require the dependency"): it never executes
merely by importing the enclosing module, so it is not a real dependency of
that import and must not fail this test on ITS behalf. Using the exhaustive
rule everywhere would flag that lazy httpx import as "reached" even though
nothing outside backends.py itself ever touches it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_ROOT = REPO_ROOT / "benchmarks"
REPORTS_ROOT = BENCHMARKS_ROOT / "reports"

#: Forbidden dotted names/prefixes. Matched after stripping an optional
#: leading "benchmarks." (both spellings reach the same file via
#: tests/conftest.py's sys.path insert of benchmarks/ alongside the repo
#: root itself being on sys.path).
FORBIDDEN_PREFIXES = (
    "lme.providers",
    "lme.runner",
    "memorybench.export",
    "memorybench.recording_proxy",
    "memorybench.traffic",
    "httpx",
    "requests",
    "urllib.request",
    "openai",
    "anthropic",
    "aiohttp",
)
#: "socket" is forbidden everywhere in the closure except inside
#: protocol/offline.py, the one sanctioned network guard.
SOCKET_NAME = "socket"
SOCKET_EXCEPTION_FILE = BENCHMARKS_ROOT / "protocol" / "offline.py"


def _strip_benchmarks_prefix(name: str) -> str:
    if name == "benchmarks":
        return ""
    if name.startswith("benchmarks."):
        return name[len("benchmarks.") :]
    return name


def _load_level_imports(path: Path, *, full_scan: bool) -> list[tuple[str, int]]:
    """Every (dotted_name, ast_level) import reachable in `path`.

    `ast_level` is the ImportFrom relative-import level (0 for absolute).
    When `full_scan` is True (files under benchmarks/reports/ itself), every
    scope is scanned -- the tree is walked unconditionally, function and
    async-function bodies included, at any nesting depth: this is OUR code,
    so a function-scoped forbidden import inside it counts even if nothing
    calls that function today. When False (a module reached transitively
    OUTSIDE the package), only module- and class-scope statements are
    considered: a FunctionDef/AsyncFunctionDef subtree is never descended
    into there, matching real Python import-time semantics -- a `def`-nested
    import in an external module never executes merely by importing it.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if not full_scan and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # deferred: never executes merely by importing the module
            if isinstance(child, ast.Import):
                for alias in child.names:
                    found.append((alias.name, 0))
            elif isinstance(child, ast.ImportFrom):
                if child.module:
                    for alias in child.names:
                        found.append((f"{child.module}.{alias.name}", child.level))
                    found.append((child.module, child.level))
                else:
                    for alias in child.names:
                        found.append((alias.name, child.level))
            else:
                walk(child)

    walk(tree)
    return found


def _resolve_internal(path: Path, dotted: str, level: int) -> Path | None:
    """Resolve a load-time import to a file under benchmarks/, if it is one."""

    if level > 0:
        base = path.parent
        for _ in range(level - 1):
            base = base.parent
        parts = dotted.split(".") if dotted else []
    else:
        name = _strip_benchmarks_prefix(dotted)
        if not name:
            return None
        base = BENCHMARKS_ROOT
        parts = name.split(".")

    candidate = base.joinpath(*parts)
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_init = candidate / "__init__.py"
    if package_init.is_file():
        return package_init
    # Falling one segment short is still informative for ImportFrom's
    # doubled (module, module.name) entries -- try the parent too.
    if len(parts) > 1:
        parent_file = base.joinpath(*parts[:-1]).with_suffix(".py")
        if parent_file.is_file():
            return parent_file
    return None


def _is_under_reports_root(path: Path) -> bool:
    try:
        path.relative_to(REPORTS_ROOT)
    except ValueError:
        return False
    return True


def reports_import_closure() -> tuple[set[str], set[Path]]:
    """(forbidden-checkable dotted names, files visited) reachable from reports/.

    `rglob`, not `glob`: files under REPORTS_ROOT are scanned at every scope
    regardless of nesting depth within the package (see
    `_is_under_reports_root` / `_load_level_imports`'s `full_scan`).
    """

    visited: set[Path] = set()
    names: set[str] = set()
    queue = sorted(REPORTS_ROOT.rglob("*.py"))
    while queue:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        full_scan = _is_under_reports_root(path)
        for dotted, level in _load_level_imports(path, full_scan=full_scan):
            if dotted != SOCKET_NAME or path != SOCKET_EXCEPTION_FILE:
                names.add(_strip_benchmarks_prefix(dotted) if level == 0 else dotted)
            resolved = _resolve_internal(path, dotted, level)
            if resolved is not None and resolved not in visited:
                queue.append(resolved)
    return names, visited


def _forbidden_hit(names: set[str]) -> str | None:
    for name in names:
        if name == SOCKET_NAME:
            return name
        for prefix in FORBIDDEN_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return name
    return None


def test_reports_closure_reaches_no_forbidden_module() -> None:
    names, visited = reports_import_closure()
    assert visited, "closure walk must visit at least the reports/ modules themselves"
    hit = _forbidden_hit(names)
    assert hit is None, f"forbidden import reachable from benchmarks/reports/: {hit}"


def test_reports_closure_does_reach_protocol_offline() -> None:
    """Sanity: the walker actually resolves internal edges, not vacuously empty."""

    _names, visited = reports_import_closure()
    assert SOCKET_EXCEPTION_FILE in visited


@pytest.mark.parametrize(
    "forbidden_snippet",
    [
        "import httpx\n",
        "from lme.runner import execute_run\n",
        "from memorybench.export import build_export\n",
        # H1: a function-scoped import INSIDE a reports/ file must still be
        # caught -- the FunctionDef skip is for files reached OUTSIDE the
        # package, never for reports/ itself.
        "def _sneaky_helper():\n    import httpx\n\n",
    ],
    ids=["module-httpx", "module-lme.runner", "module-memorybench.export", "function-scoped-httpx"],
)
def test_closure_goes_red_when_a_forbidden_import_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbidden_snippet: str
) -> None:
    """R6: mutate a scratch copy of render.py, prove this test would catch it."""

    import shutil

    scratch_benchmarks = tmp_path / "benchmarks"
    shutil.copytree(BENCHMARKS_ROOT / "reports", scratch_benchmarks / "reports")
    shutil.copytree(BENCHMARKS_ROOT / "protocol", scratch_benchmarks / "protocol")
    render_path = scratch_benchmarks / "reports" / "render.py"
    render_path.write_text(
        forbidden_snippet + render_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "BENCHMARKS_ROOT", scratch_benchmarks)
    monkeypatch.setattr(this_module, "REPORTS_ROOT", scratch_benchmarks / "reports")
    monkeypatch.setattr(
        this_module, "SOCKET_EXCEPTION_FILE", scratch_benchmarks / "protocol" / "offline.py"
    )
    names, _visited = reports_import_closure()
    assert _forbidden_hit(names) is not None
