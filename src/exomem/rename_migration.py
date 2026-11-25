"""`evermatter setup --migrate`: move a local install from the `exomem` agent names to `evermatter`.

Every reader accepts both names, so an install made before the rename keeps working and
nothing here runs unasked. The user runs it to adopt the new names. It changes only what
setup wrote: the server key in Claude Code and Codex, the managed instruction block and the
explicit checkpoint bindings. Both clients store a URL (HTTP) server's sign-in under its key, so
renaming one can sign the user out once; the command prints the sign-in step for each. A URL that
points at Evermatter Cloud keeps its key, because the Cloud plugin's address changes in a later
release and one rename now would sign that user in twice. A rule or hook the user wrote stays as
it is and is listed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import client_config, cloud_address, prominence, setup_wizard
from . import install_hook as hook_module
from . import install_skill as install_module

#: Said for each Cloud URL registration that this command leaves under its old key.
_CLOUD_KEPT = (
    "kept `{old}` for {url}: it moves with the Cloud plugin's address change, "
    "so you sign in once"
)


def _sign_in_line(client: str) -> str:
    """The step for a renamed URL server. Its sign-in is OAuth only when the server asks for one,
    which the command cannot tell, so the line is conditional."""
    return f"  If `{client_config.SERVER_NAME}` shows as not connected, {client_config.SIGN_IN_STEPS[client]}."


def _migrate_claude_servers(
    *, config_path: Path, project_dir: Path, which_fn, run_fn, print_fn, report, backup
) -> None:
    project_file = project_dir / ".mcp.json"
    try:
        user_config = client_config.read_json_object(config_path)
        project_config = client_config.read_json_object(project_file)
    except ValueError as exc:
        report("claude", f"[failed: {exc}]")
        return
    scopes = {
        "local": client_config.project_entry(user_config, project_dir).get("mcpServers"),
        "project": project_config.get("mcpServers"),
        "user": user_config.get("mcpServers"),
    }
    old_key, new_key = client_config.LEGACY_SERVER_NAME, client_config.SERVER_NAME
    to_rename: dict[str, dict] = {}
    for scope, servers in scopes.items():
        old = servers.get(old_key) if isinstance(servers, dict) else None
        if not isinstance(old, dict):
            continue
        if isinstance(servers.get(new_key), dict):
            report(
                f"claude-{scope}",
                f"[skipped: holds both `{old_key}` and `{new_key}`; remove the one you do not use]",
            )
        elif "url" in old and cloud_address.is_cloud_url(str(old["url"])):
            report(f"claude-{scope}", f"[skipped: {_CLOUD_KEPT.format(old=old_key, url=old['url'])}]")
        else:
            to_rename[scope] = old
    others = _other_project_registrations(user_config, project_dir)
    if others:
        report(
            "claude-other-projects",
            f"[skipped: {len(others)} other project(s) keep a local `{old_key}` registration; "
            "this command renames the one for the current directory, so run it in each]",
        )
        for other in others:
            print_fn(f"    {other}")
    if not to_rename:
        if not any(isinstance(s, dict) and old_key in s for s in scopes.values()) and not others:
            report("claude", f"[skipped: no `{old_key}` server in Claude Code]")
        return
    claude = which_fn("claude")
    if not claude:
        report(
            "claude",
            f"[skipped: no claude CLI; rename the `{old_key}` key under mcpServers to "
            f"`{new_key}` by hand]",
        )
        return
    run_kwargs = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(project_dir)
    )
    for scope, entry in to_rename.items():
        file = project_file if scope == "project" else config_path
        snapshots = setup_wizard._snapshot_files((config_path, project_file))
        backup(file)
        payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        failure = ""
        # Add before remove: `claude mcp remove` deletes an http or sse server's stored sign-in, and
        # restoring the two files cannot bring that back. A refused add then removes nothing.
        for argv in (
            [claude, "mcp", "add-json", "--scope", scope, new_key, payload],
            [claude, "mcp", "remove", old_key, "--scope", scope],
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
        report(f"claude-{scope}", f"[done] renamed to `{new_key}` in {file.name}")
        if "url" in entry:
            print_fn(_sign_in_line("claude"))
        if scope == "project":
            print_fn("  Claude Code asks you to approve the renamed project server again.")


def _other_project_registrations(user_config: dict, project_dir: Path) -> list[str]:
    """The other projects in Claude Code's config that hold a local legacy registration."""
    projects = user_config.get("projects")
    if not isinstance(projects, dict):
        return []
    here = project_dir.resolve()
    found = []
    for raw_path, value in projects.items():
        servers = value.get("mcpServers") if isinstance(value, dict) else None
        if not isinstance(servers, dict) or not isinstance(
            servers.get(client_config.LEGACY_SERVER_NAME), dict
        ):
            continue
        try:
            if Path(raw_path).expanduser().resolve() == here:
                continue
        except OSError:
            pass
        found.append(str(raw_path))
    return found


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
        "kept": _CLOUD_KEPT.format(old=client_config.LEGACY_SERVER_NAME, url=outcome.get("url")),
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
    if outcome.get("url"):
        print_fn(_sign_in_line("codex"))


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
    """Add each new tool prefix, plugin prefix included, beside its old one in the checkpoint bindings."""
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
        for old, new in client_config.TOOL_PREFIX_RENAMES.items():
            if binding.startswith(old) and (added := new + binding[len(old) :]) not in bindings:
                bindings.append(added)
    if len(bindings) == len(prior["bindings"]):
        report(step, "[skipped: no binding names an old prefix, or its new one is already bound]")
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
        else client_config.default_claude_config_path(dict(os.environ))
    )
    project_dir = Path.cwd() if project_dir is None else Path(project_dir)
    steps: list[tuple[str, str]] = []
    backed_up: set[Path] = set()

    def report(name: str, status: str) -> None:
        steps.append((name, status))
        print_fn(f"  {name}: {status}")

    def backup(path: Path) -> None:
        """Copy ``path`` beside itself once per run, before the claude CLI first rewrites it.

        Two scopes can share one file, and a second copy would hold the first scope's change.
        """
        if path in backed_up or not path.is_file():
            return
        backed_up.add(path)
        copy = path.with_suffix(path.suffix + client_config.BACKUP_SUFFIX)
        shutil.copy2(path, copy)
        print_fn(f"  backup: {copy}")

    print_fn("evermatter setup --migrate")
    print_fn("")
    _migrate_claude_servers(
        config_path=claude_config_path,
        project_dir=project_dir,
        which_fn=which_fn,
        run_fn=run_fn,
        print_fn=print_fn,
        report=report,
        backup=backup,
    )
    _migrate_codex_server(codex_home=codex_home, which_fn=which_fn, print_fn=print_fn, report=report)
    _migrate_instruction_blocks(
        claude_home=claude_home, codex_home=codex_home, print_fn=print_fn, report=report
    )
    _migrate_bindings(client="claude", home=claude_home, print_fn=print_fn, report=report)
    _migrate_bindings(client="codex", home=codex_home, print_fn=print_fn, report=report)

    try:
        references, _read = client_config.dead_references(
            claude_home=claude_home,
            codex_home=codex_home,
            claude_config_path=claude_config_path,
            project_dir=project_dir,
        )
    except ValueError as exc:
        references = []
        print_fn("")
        print_fn(f"Could not check your permission rules and hooks: {exc}")
    if references:
        print_fn("")
        print_fn(
            "These permission rules and hooks are yours, so setup left them as they are. Each "
            "names an old tool prefix that no server answers now. Change it to the prefix shown:"
        )
        for reference in references:
            print_fn(
                f"  {reference.path}: {reference.kind} {reference.name} "
                f"(change `{reference.prefix}` to `{reference.replacement}`)"
            )
    print_fn("")
    print_fn(
        "To move the skills too, run `evermatter install-skill`; it moves the old `exomem` "
        "skills to a backup folder."
    )
    if any(status.startswith("[done]") for _, status in steps):
        print_fn("Restart Claude Code and Codex to load the changes.")
    print_fn("")
    print_fn("Summary:")
    for name, status in steps:
        print_fn(f"  {name:<18} {status}")
    return 1 if any("[failed" in status for _, status in steps) else 0
