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


#: Loaders whose file comes from `server_runtime._working_directory_dotenv`,
#: which is `<cwd>/.env` unless the working directory is inside a vault.
_GUARDED_LOADERS = {
    ("server_runtime.py", "_working_directory_dotenv"),
    ("server_runtime.py", "initialize_runtime"),
}


def test_every_env_file_loader_reads_only_the_working_directory_env() -> None:
    """Structural pin: each `.env` loader in the package names `<cwd>/.env`
    (directly or through the service's vault guard), or is the setup wizard
    reloading the file it just wrote. It does not by itself prove the working
    directory is outside the vault; the startup tests below do that."""
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
            source = ast.unparse(function)
            if "Path.cwd() / '.env'" in source:
                continue
            if (name, function.name) in _GUARDED_LOADERS:
                continue
            offenders.append(f"{name}:{function.name}")
    assert offenders == []
    # The guarded service loader still derives its file from the working
    # directory, and service startup loads only what that guard returns.
    guard = next(
        node
        for module, tree in _modules()
        if module == "server_runtime.py"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_working_directory_dotenv"
    )
    assert "Path.cwd().resolve()" in ast.unparse(guard)
    assert "cwd / '.env'" in ast.unparse(guard)


class _Stop(Exception):
    pass


def _start_until_dotenv(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Run local startup up to the `.env` step; return what the loader was given."""
    loaded: list[Path] = []

    def recording_loader(*, dotenv_path: Path, override: bool) -> None:
        from dotenv import load_dotenv

        loaded.append(Path(dotenv_path))
        load_dotenv(dotenv_path=dotenv_path, override=override)

    def stop() -> list[str]:
        raise _Stop

    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    # Register the planted key with monkeypatch first, so anything a loader sets
    # is undone at teardown instead of leaking into later tests.
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "")
    monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")
    monkeypatch.setattr(server_runtime.env_compat, "promote_legacy", stop)
    with pytest.raises(_Stop):
        server_runtime.initialize_runtime(load_dotenv_func=recording_loader)
    return loaded


def _plant(directory: Path) -> None:
    (directory / ".env").write_text(
        "EXOMEM_OWNER_OAUTH_SUBJECT=github:4242\n", encoding="utf-8"
    )


def test_startup_loads_the_working_directory_env_file_and_not_the_vault_s(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a service root outside the vault, only `<cwd>/.env` is loaded; a
    `.env` planted at the vault root is never read."""
    _plant(vault)
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    monkeypatch.chdir(service_root)

    assert _start_until_dotenv(monkeypatch) == [service_root / ".env"]
    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ


@pytest.mark.parametrize("where", ["vault-root", "vault-subdirectory"])
def test_startup_refuses_a_working_directory_env_file_inside_the_vault(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    """A `.env` a remote writer planted in the vault must not become the service's
    configuration just because the service was started from there."""
    cwd = vault if where == "vault-root" else vault / "Knowledge Base"
    _plant(cwd)
    monkeypatch.chdir(cwd)

    with caplog.at_level("WARNING", logger=server_runtime.log.name):
        assert _start_until_dotenv(monkeypatch) == []

    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ
    refusals = [r.getMessage() for r in caplog.records if "dotenv_refused" in r.getMessage()]
    assert len(refusals) == 1
    assert str(cwd.resolve() / ".env") in refusals[0]


def test_startup_refuses_when_the_env_file_itself_names_the_enclosing_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no vault in the process environment, the `.env` that would configure
    it is still refused when it sits inside the vault it names."""
    (vault / ".env").write_text(
        f"EXOMEM_VAULT_PATH={vault}\nEXOMEM_OWNER_OAUTH_SUBJECT=github:4242\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXOMEM_VAULT_PATH")
    monkeypatch.chdir(vault)

    assert _start_until_dotenv(monkeypatch) == []
    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ


def test_startup_refuses_a_working_directory_inside_any_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured vault may be elsewhere; a directory that is itself a vault
    is still content that remote writers can reach."""
    other = tmp_path / "configured-elsewhere"
    other.mkdir()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(other))
    _plant(vault)
    monkeypatch.chdir(vault)

    assert _start_until_dotenv(monkeypatch) == []
    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ


def test_startup_refuses_an_env_file_that_is_a_symlink_into_the_vault(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The working directory is outside the vault, but its `.env` resolves to a
    file a remote writer can plant inside it: the file, not just the directory,
    decides."""
    planted = vault / "Knowledge Base" / ".env"
    planted.write_text("EXOMEM_OWNER_OAUTH_SUBJECT=github:4242\n", encoding="utf-8")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    (service_root / ".env").symlink_to(planted)
    monkeypatch.chdir(service_root)

    with caplog.at_level("WARNING", logger=server_runtime.log.name):
        assert _start_until_dotenv(monkeypatch) == []

    assert "EXOMEM_OWNER_OAUTH_SUBJECT" not in os.environ
    refusals = [r.getMessage() for r in caplog.records if "dotenv_refused" in r.getMessage()]
    assert len(refusals) == 1


def test_the_refusal_line_names_the_remedy(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused `.env` can stop startup when it held required settings, so the
    one line an operator sees must say what to do."""
    _plant(vault)
    monkeypatch.chdir(vault)
    with caplog.at_level("WARNING", logger=server_runtime.log.name):
        _start_until_dotenv(monkeypatch)
    refusals = [r.getMessage() for r in caplog.records if "dotenv_refused" in r.getMessage()]
    assert len(refusals) == 1
    assert "move the .env out of the vault, or put the settings in service.env" in refusals[0]
