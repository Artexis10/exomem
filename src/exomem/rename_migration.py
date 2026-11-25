"""`evermatter setup --migrate`: move a local install from the `exomem` agent names to `evermatter`.

Every reader accepts both names, so an install made before the rename keeps working and
nothing here runs unasked. The user runs it to adopt the new names. It changes only what
setup wrote: the server key in Claude Code and Codex, the managed instruction block and the
explicit checkpoint bindings. A rule or hook the user wrote stays as it is and is listed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import client_config, prominence, setup_wizard
from . import install_hook as hook_module
from . import install_skill as install_module

_BACKUP_SUFFIX = ".exomem-bak"  # the suffix client_config._write_user_file gives its backups


def _backup(path: Path) -> str | None:
    """Copy ``path`` beside itself before a client's own CLI rewrites it."""
    if not path.is_file():
        return None
    backup = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    return str(backup)


def _migrate_claude_servers(
    *, config_path: Path, project_dir: Path, which_fn, run_fn, print_fn, report
) -> None:
    try:
        found = setup_wizard._claude_registrations(config_path=config_path, project_dir=project_dir)
    except ValueError as exc:
        report("claude", f"[failed: {exc}]")
        return
    legacy = {
        scope: entry
        for scope, (key, entry) in found.items()
        if key == client_config.LEGACY_SERVER_NAME
    }
    if not legacy:
        report("claude", f"[skipped: no `{client_config.LEGACY_SERVER_NAME}` server in Claude Code]")
        return
    claude = which_fn("claude")
    if not claude:
        report(
            "claude",
            f"[skipped: no claude CLI; rename the `{client_config.LEGACY_SERVER_NAME}` key under "
            f"mcpServers to `{client_config.SERVER_NAME}` by hand]",
        )
        return
    run_kwargs = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(project_dir)
    )
    project_file = project_dir / ".mcp.json"
    for scope, entry in legacy.items():
        file = project_file if scope == "project" else config_path
        snapshots = setup_wizard._snapshot_files((config_path, project_file))
        backup = _backup(file)
        payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        failure = ""
        for argv in (
            [claude, "mcp", "remove", client_config.LEGACY_SERVER_NAME, "--scope", scope],
            [claude, "mcp", "add-json", "--scope", scope, client_config.SERVER_NAME, payload],
        ):
            try:
                result = run_fn(argv, **run_kwargs)
            except Exception as exc:  # noqa: BLE001 - restore is the safety boundary
                failure = str(exc)
                break
            if result.returncode != 0:
                failure = (result.stderr or "").strip() or f"claude mcp exited {result.returncode}"
                break
        if failure:
            try:
                setup_wizard._restore_files(snapshots)
            except OSError as restore_exc:
                failure += f"; restoring the previous registration also failed: {restore_exc}"
                report(f"claude-{scope}", f"[failed: {failure}]")
            else:
                report(f"claude-{scope}", f"[failed: {failure}; restored the previous registration]")
            continue
        if backup:
            print_fn(f"  backup: {backup}")
        report(f"claude-{scope}", f"[done] renamed to `{client_config.SERVER_NAME}` in {file.name}")
        if scope == "project":
            print_fn("  Claude Code asks you to approve the renamed project server again.")


def _migrate_codex_server(*, codex_home: Path, which_fn, print_fn, report) -> None:
    if not setup_wizard._codex_present(which_fn, codex_home):
        report("codex", "[skipped: Codex not detected on this machine]")
        return
    path = setup_wizard._resolve_codex_home(codex_home) / "config.toml"
    try:
        outcome = client_config.rename_codex_mcp_server(path=path)
    except (ValueError, OSError) as exc:
        report("codex", f"[failed: {exc}]")
        return
    skipped = {
        "current": f"no `{client_config.LEGACY_SERVER_NAME}` server in {path.name}",
        "conflict": f"{path.name} holds both `{client_config.LEGACY_SERVER_NAME}` and "
        f"`{client_config.SERVER_NAME}`; remove one by hand",
        "manual": f"the `{client_config.LEGACY_SERVER_NAME}` server in {path.name} is not a plain "
        "table; rename it by hand",
    }
    if outcome["action"] in skipped:
        report("codex", f"[skipped: {skipped[outcome['action']]}]")
        return
    setup_wizard._report_file_outcome("codex", outcome, path=path, print_fn=print_fn, report=report)


def _migrate_instruction_blocks(*, claude_home: Path, codex_home: Path, print_fn, report) -> None:
    for step, client, home in (
        ("instruction", "claude", claude_home),
        ("instruction-codex", "codex", codex_home),
    ):
        path = client_config.instruction_file(client, home)
        try:
            outcome = client_config.migrate_learning_instruction(
                path, prominence.LEARNING_INSTRUCTION
            )
        except (OSError, ValueError) as exc:
            report(step, f"[failed: {exc}]")
            continue
        if outcome["action"] == "migrated":
            setup_wizard._report_file_outcome(step, outcome, path=path, print_fn=print_fn, report=report)
        elif outcome["action"] == "current":
            report(step, f"[skipped: already current in {path.name}]")
        else:
            report(step, f"[skipped: no managed instruction block in {path.name}]")


def _migrate_bindings(*, client: str, home: Path, print_fn, report) -> None:
    """Add the new tool prefix beside each old one in the explicit checkpoint bindings."""
    from ._hooks import exomem_retrieve_nudge as retrieve

    step = f"bindings-{client}"
    hook_dir = home / "hooks"
    prior, status = retrieve.refresh_metadata(hook_dir, client, home)
    if prior is None:
        reason = "no explicit checkpoint bindings" if status == "missing" else f"bindings {status}"
        report(step, f"[skipped: {reason}]")
        return
    bindings = list(prior["bindings"])
    for binding in prior["bindings"]:
        for old, new in client_config.LEGACY_TOOL_PREFIXES.items():
            if binding.startswith(old) and (added := new + binding[len(old) :]) not in bindings:
                bindings.append(added)
    if len(bindings) == len(prior["bindings"]):
        report(step, "[skipped: no binding names the old prefix, or the new one is already bound]")
        return
    try:
        result = hook_module.install_hook(
            hook_dir=hook_dir,
            settings_path=home / ("hooks.json" if client == "codex" else "settings.json"),
            client=client,
            activation_mode="mcp",
            refresh_bindings=tuple(bindings),
            refresh_max_operations=prior["limits"]["operations"],
            refresh_max_activation_chars=prior["limits"]["activation_chars"],
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        report(step, f"[failed: {exc}]")
        return
    if result.get("backup"):
        print_fn(f"  backup: {result['backup']}")
    report(step, f"[done] bound {', '.join(bindings[len(prior['bindings']) :])}")


def migrate(
    *,
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    claude_config_path: Path | None = None,
    project_dir: Path | None = None,
    which_fn=shutil.which,
    run_fn=subprocess.run,
    print_fn=print,
) -> int:
    """Run the migration. Returns 1 when any step failed, otherwise 0."""
    claude_home = Path(claude_home) if claude_home else install_module.client_home("claude")
    codex_home = setup_wizard._resolve_codex_home(codex_home)
    claude_config_path = (
        Path(claude_config_path)
        if claude_config_path
        else setup_wizard._default_claude_config_path(dict(os.environ))
    )
    project_dir = Path.cwd() if project_dir is None else Path(project_dir)
    steps: list[tuple[str, str]] = []

    def report(name: str, status: str) -> None:
        steps.append((name, status))
        print_fn(f"  {name}: {status}")

    print_fn("evermatter setup --migrate")
    print_fn("")
    _migrate_claude_servers(
        config_path=claude_config_path,
        project_dir=project_dir,
        which_fn=which_fn,
        run_fn=run_fn,
        print_fn=print_fn,
        report=report,
    )
    _migrate_codex_server(codex_home=codex_home, which_fn=which_fn, print_fn=print_fn, report=report)
    _migrate_instruction_blocks(
        claude_home=claude_home, codex_home=codex_home, print_fn=print_fn, report=report
    )
    _migrate_bindings(client="claude", home=claude_home, print_fn=print_fn, report=report)
    _migrate_bindings(client="codex", home=codex_home, print_fn=print_fn, report=report)

    references = client_config.user_references(
        claude_home=claude_home, codex_home=codex_home, project_dir=project_dir
    )
    if references:
        print_fn("")
        print_fn(
            "These permission rules and hooks are yours, so setup left them as they are. They name "
            "the old `exomem` tool prefix, which no longer matches once the server key is "
            f"`{client_config.SERVER_NAME}`. Change each to the `mcp__{client_config.SERVER_NAME}__` "
            "prefix:"
        )
        for reference in references:
            print_fn(f"  {reference.path}: {reference.kind} {reference.name}")
    print_fn("")
    print_fn("To move the skills too, run `evermatter setup`; it retires the old `exomem` skills.")
    print_fn("Restart Claude Code and Codex to load the renamed server.")
    print_fn("")
    print_fn("Summary:")
    for name, status in steps:
        print_fn(f"  {name:<18} {status}")
    return 1 if any("[failed" in status for _, status in steps) else 0
