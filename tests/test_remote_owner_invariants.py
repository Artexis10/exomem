"""The owner binding can only come from the host's own service environment.

`EXOMEM_OWNER_OAUTH_SUBJECT` is read from the process environment, which the
service fills from its unit's environment file and the working directory's
`.env` -- never from the vault. So two things must stay true for no remote or
synced writer to promote itself: no product command, REST route or transfer
route can write an environment file, and no environment file is loaded from
the vault root. Both are pinned here structurally, the second behaviourally too.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from exomem import server_runtime

SRC = Path(__file__).resolve().parents[1] / "src" / "exomem"
_WRITE_CALLS = {"write_text", "write_bytes", "open", "replace", "rename", "write", "touch"}
_ENV_MARKERS = (".env", "env_path", "service_env", "environment_file", "dotenv")
_LOADERS = {"load_dotenv", "load_dotenv_func", "dotenv_values"}


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(SRC)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _importers(modules: list[tuple[str, ast.Module]], target: str) -> set[str]:
    found: set[str] = set()
    for name, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = [node.module or ""] + [
                    f"{node.module}.{alias.name}" if node.module else alias.name
                    for alias in node.names
                ]
            elif isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            else:
                continue
            if any(item.split(".")[-1] == target for item in imported):
                found.add(name)
    return found


def test_only_the_operator_setup_wizard_writes_an_environment_file() -> None:
    modules = _modules()
    writers: set[str] = set()
    for name, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in _WRITE_CALLS:
                text = ast.unparse(node).casefold()
                if any(marker in text for marker in _ENV_MARKERS):
                    writers.add(name)
    assert writers == {"remote_setup_wizard.py"}

    # ...and that writer is reachable only from the operator's CLI, never from
    # a product command, a REST route, a transfer route or the MCP server.
    assert _importers(modules, "remote_setup_wizard") <= {"setup_wizard.py", "__main__.py"}
    assert _importers(modules, "setup_wizard") <= {"__main__.py"}


def test_every_environment_file_the_process_loads_is_the_working_directory_s() -> None:
    offenders: list[str] = []
    for name, tree in _modules():
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and _call_name(node) in _LOADERS
            ]
            if not calls:
                continue
            if (name, function.name) == ("remote_setup_wizard.py", "_load_env"):
                # Reloads the file the operator's setup command just wrote.
                continue
            if "Path.cwd() / '.env'" not in ast.unparse(function):
                offenders.append(f"{name}:{function.name}")
    assert offenders == []


def test_the_service_never_loads_an_environment_file_from_the_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (vault / ".env").write_text("EXOMEM_OWNER_OAUTH_SUBJECT=github:4242\n", encoding="utf-8")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    monkeypatch.chdir(service_root)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    loaded: list[Path] = []

    class _Stop(Exception):
        pass

    def recording_loader(*, dotenv_path: Path, override: bool) -> None:
        from dotenv import load_dotenv

        loaded.append(Path(dotenv_path))
        load_dotenv(dotenv_path=dotenv_path, override=override)
        raise _Stop

    with pytest.raises(_Stop):
        server_runtime.initialize_runtime(load_dotenv_func=recording_loader)

    assert loaded == [service_root / ".env"]
    assert not loaded[0].resolve().is_relative_to(vault.resolve())
    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ
