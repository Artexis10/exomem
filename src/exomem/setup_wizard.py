"""`exomem setup` — one-command guided local onboarding.

Collapses the manual QUICKSTART steps (init → profile → doctor → Claude Code
registration → skill → hooks) into a single interactive, idempotent wizard.
Every step is a converger: it detects the current state and reports `[done]`,
`[skipped: …]`, or `[failed: …]`, so re-running is always safe.

Before `init` touches anything, the wizard scans the vault with the `overview`
core (which needs no initialized KB) and states the write contract out loud —
a vault full of pre-existing notes stays untouched, read-only, searchable.

CLI-only by design: it mutates host config (`~/.claude`), spawns subprocesses,
and prompts — none of which belongs on the MCP/REST registry. All side-effect
seams (`input_fn`, `run_fn`, `which_fn`, `home`, `print_fn`) are injectable so
tests never touch the real home directory or spawn a real `claude`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import adopt as adopt_module
from . import client_config, install_info
from . import doctor as doctor_module
from . import init as init_module
from . import install_hook as hook_module
from . import install_skill as install_module
from . import knowledge_packs as knowledge_packs_module
from . import overview as overview_module
from . import personalize as personalize_module
from .kbdir import kb_dirname, kb_prefix

_SKILL_NAME_MARKER = "name: exomem"
def _format_pack_suggestions(packs: list[dict], *, limit: int = 3) -> str:
    shown = []
    for pack in packs[:limit]:
        name = pack.get("name") or pack.get("id") or "unknown"
        score = int(pack.get("score") or 0)
        if score > 0:
            shown.append(f"{name} ({score} signal{'s' if score != 1 else ''})")
        else:
            shown.append(f"{name} (default)")
    return ", ".join(shown)


def _default_pack_ids(suggested: list[dict]) -> list[str]:
    ids: list[str] = []
    for pack in suggested:
        pack_id = str(pack.get("id") or "").strip()
        if pack_id and pack_id not in ids:
            ids.append(pack_id)
    return ids or [knowledge_packs_module.DEFAULT_PACK_ID]


def _format_selected_pack_names(selection: dict) -> str:
    names = [pack.get("name") or pack.get("id") for pack in selection.get("packs") or []]
    return ", ".join(str(name) for name in names if name) or knowledge_packs_module.DEFAULT_PACK_ID


def _choose_pack_ids(input_fn, print_fn, *, available: list[dict], suggested: list[dict], yes: bool) -> list[str]:
    default_ids = _default_pack_ids(suggested)
    if yes:
        return default_ids

    suggested_set = set(default_ids)
    print_lines = ["  Choose starter knowledge packs (guidance only; no folders are created):"]
    for index, pack in enumerate(available, start=1):
        marker = "*" if pack.get("id") in suggested_set else " "
        desc = pack.get("beginner_description") or pack.get("description") or ""
        print_lines.append(f"    {index}. [{marker}] {pack.get('name')} - {desc}")
    print_lines.append("  Press Enter to accept the marked packs, or enter numbers/IDs separated by commas.")
    for line in print_lines:
        print_fn(line)

    answer = input_fn("Packs: ").strip()
    if not answer:
        return default_ids
    by_number = {str(index): str(pack.get("id")) for index, pack in enumerate(available, start=1)}
    by_id = {str(pack.get("id")): str(pack.get("id")) for pack in available}
    selected: list[str] = []
    unknown: list[str] = []
    for raw in answer.replace(";", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        pack_id = by_number.get(token) or by_id.get(token)
        if not pack_id:
            unknown.append(token)
            continue
        if pack_id not in selected:
            selected.append(pack_id)
    if unknown:
        raise knowledge_packs_module.PackSelectionError(
            "UNKNOWN_PACK",
            f"unknown pack selection(s): {unknown}",
        )
    return selected or default_ids


def _ask_yn(input_fn, prompt: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input_fn(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _server_command(which_fn) -> list[str]:
    """How Claude Code should launch the server, most-durable first: uv in a
    repo checkout; the `exomem` console script for pip/`uv tool` installs;
    `uvx exomem` as the transient-install escape hatch.

    Never `sys.executable -m exomem` for wheel installs: under `uvx exomem
    setup`, sys.executable points into uvx's ephemeral cache env, so the
    registered server silently breaks when that cache is pruned.
    """
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").is_file() and which_fn("uv"):
        return [
            "uv", "--directory", str(repo_root),
            "run", "python", "-m", "exomem", "--transport", "stdio",
        ]
    console_script = which_fn("exomem")
    if console_script:
        return [console_script, "--transport", "stdio"]
    return ["uvx", "exomem", "--transport", "stdio"]


def _read_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot inspect Claude MCP registrations in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"cannot inspect Claude MCP registrations in {path}: expected an object")
    return value


def _project_entry(config: dict, project_dir: Path) -> dict:
    target = project_dir.resolve()
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    for raw_path, value in projects.items():
        if not isinstance(raw_path, str) or not isinstance(value, dict):
            continue
        try:
            if Path(raw_path).expanduser().resolve() == target:
                return value
        except OSError:
            continue
    return {}


def _registered_server(config: dict) -> dict | None:
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    value = servers.get("exomem")
    return value if isinstance(value, dict) else None


def _claude_registrations(*, config_path: Path, project_dir: Path) -> dict[str, dict]:
    """Inventory registrations without `claude mcp list/get` health checks."""
    user_config = _read_json_object(config_path)
    project_config = _read_json_object(project_dir / ".mcp.json")
    found: dict[str, dict] = {}
    candidates = (
        ("local", _registered_server(_project_entry(user_config, project_dir))),
        ("project", _registered_server(project_config)),
        ("user", _registered_server(user_config)),
    )
    for scope, server in candidates:
        if server is not None:
            found[scope] = server
    return found


def _registration_matches(route: client_config.McpRoute, server: dict) -> bool:
    if route.transport == "http":
        return server.get("url") == route.url and server.get("type", "http") == "http"
    desired = route.as_claude_config()
    return all(server.get(key) == value for key, value in desired.items())


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes | None
    mode: int | None


def _snapshot_files(paths: tuple[Path, ...]) -> dict[Path, _FileSnapshot]:
    snapshots: dict[Path, _FileSnapshot] = {}
    for path in paths:
        if path.is_file():
            snapshots[path] = _FileSnapshot(
                content=path.read_bytes(),
                mode=stat.S_IMODE(path.stat().st_mode),
            )
        else:
            snapshots[path] = _FileSnapshot(content=None, mode=None)
    return snapshots


def _restore_files(snapshots: dict[Path, _FileSnapshot]) -> None:
    for path, snapshot in snapshots.items():
        if snapshot.content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".exomem-restore")
        temporary.write_bytes(snapshot.content)
        if snapshot.mode is not None:
            os.chmod(temporary, snapshot.mode)
        os.replace(temporary, path)


def _legacy_stdio_plugins(claude: str, run_fn, run_kwargs: dict) -> list[str]:
    """Return enabled Exomem plugin IDs that still declare a stdio core."""
    try:
        result = run_fn([claude, "plugin", "list", "--json"], **run_kwargs)
    except Exception as exc:  # noqa: BLE001 - an unknown plugin state must fail closed
        raise ValueError(f"could not verify Claude plugin state: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise ValueError(f"could not verify Claude plugin state: {detail}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("could not verify Claude plugin state: invalid JSON") from exc
    plugins = payload.get("plugins", []) if isinstance(payload, dict) else payload
    if not isinstance(plugins, list):
        raise ValueError("could not verify Claude plugin state: unexpected inventory shape")
    legacy: list[str] = []
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("enabled") is not True:
            continue
        plugin_id = str(plugin.get("id") or plugin.get("name") or "")
        if "exomem" not in plugin_id.casefold():
            continue
        servers = plugin.get("mcpServers")
        server = servers.get("exomem") if isinstance(servers, dict) else None
        if not isinstance(server, dict):
            raise ValueError(
                f"could not verify Claude plugin state for {plugin_id}: "
                "Exomem MCP inventory is missing"
            )
        if server.get("command") or server.get("args"):
            legacy.append(plugin_id)
            continue
        if server.get("type") != "http" or not isinstance(server.get("url"), str):
            raise ValueError(
                f"could not verify Claude plugin state for {plugin_id}: "
                "Exomem MCP declaration is incomplete"
            )
    return legacy


def _configured_mcp_url(
    *,
    mcp_url: str | None,
    force_stdio: bool,
    cwd: Path,
    environ: dict[str, str],
) -> str | None:
    if force_stdio:
        return None
    configured = mcp_url
    env_path = cwd / ".env"
    if configured is None and env_path.is_file():
        from .remote_setup_wizard import parse_env

        parsed = parse_env(env_path.read_text(encoding="utf-8"))
        if "EXOMEM_BASE_URL" in parsed:
            configured = parsed["EXOMEM_BASE_URL"]
    if configured is None and "EXOMEM_BASE_URL" in environ:
        configured = environ["EXOMEM_BASE_URL"]
    return client_config.normalize_mcp_url(configured) if configured is not None else None


def _default_claude_config_path(environ: dict[str, str]) -> Path:
    """Return Claude's user-state path, including configured relocation."""
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def _resolve_client_route(
    *,
    mcp_url: str | None,
    force_stdio: bool,
    cwd: Path,
    environ: dict[str, str],
    which_fn,
    vault_path: Path,
    profile: str,
) -> client_config.McpRoute:
    env = {"EXOMEM_VAULT_PATH": str(vault_path)}
    if profile == "lean":
        env["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    if force_stdio:
        return client_config.McpRoute.stdio(_server_command(which_fn), env)

    configured = _configured_mcp_url(
        mcp_url=mcp_url,
        force_stdio=force_stdio,
        cwd=cwd,
        environ=environ,
    )
    if configured is not None:
        return client_config.McpRoute.http(configured)
    return client_config.McpRoute.stdio(_server_command(which_fn), env)


def _format_scopes(scopes: list[str]) -> str:
    return ", ".join(scopes) if scopes else "none"


def _register_claude(
    *,
    claude: str,
    route: client_config.McpRoute,
    scope: str,
    config_path: Path,
    project_dir: Path,
    replace_client_registration: bool,
    yes: bool,
    input_fn,
    run_fn,
    run_kwargs: dict,
    report,
) -> None:
    try:
        registrations = _claude_registrations(
            config_path=config_path,
            project_dir=project_dir,
        )
    except ValueError as exc:
        report("register", f"[failed: {exc}]")
        return

    existing_scopes = list(registrations)
    scope_text = _format_scopes(existing_scopes)
    desired_is_active = (
        existing_scopes == [scope]
        and _registration_matches(route, registrations[scope])
    )
    if desired_is_active:
        report("register", f"[skipped: already registered in {scope}]")
        return

    replace = replace_client_registration and bool(existing_scopes)
    if existing_scopes and not replace:
        if yes or not _ask_yn(
            input_fn,
            f"exomem is already registered in {scope_text}. Replace every explicit route?",
            False,
        ):
            report("register", f"[skipped: already registered in {scope_text}]")
            return
        replace = True

    argv = route.claude_add_argv(claude, scope=scope)
    if replace:
        paths = (config_path, project_dir / ".mcp.json")
        try:
            snapshots = _snapshot_files(paths)
        except OSError as exc:
            report("register", f"[failed: could not snapshot Claude configuration: {exc}]")
            return
        failure = ""
        for existing_scope in existing_scopes:
            try:
                result = run_fn(
                    [claude, "mcp", "remove", "exomem", "--scope", existing_scope],
                    **run_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - restore is the safety boundary
                failure = str(exc)
                break
            if result.returncode != 0:
                failure = (result.stderr or "").strip() or (
                    f"claude mcp remove ({existing_scope}) exited {result.returncode}"
                )
                break
        if not failure:
            try:
                result = run_fn(argv, **run_kwargs)
            except Exception as exc:  # noqa: BLE001 - restore is the safety boundary
                failure = str(exc)
            else:
                if result.returncode != 0:
                    failure = (result.stderr or "").strip() or (
                        f"claude mcp add exited {result.returncode}"
                    )
        if failure:
            try:
                _restore_files(snapshots)
            except OSError as restore_exc:
                report(
                    "register",
                    f"[failed: {failure}; restoring previous registration also failed: "
                    f"{restore_exc}]",
                )
                return
            report("register", f"[failed: {failure}; restored previous registration]")
            return
        report(
            "register",
            f"[done] replaced {scope_text} with Claude Code scope {scope}",
        )
        return

    try:
        result = run_fn(argv, **run_kwargs)
    except Exception as exc:  # noqa: BLE001 - report client invocation failures
        report("register", f"[failed: {exc}]")
        return
    output = (result.stderr or "") + (result.stdout or "")
    if result.returncode == 0:
        report("register", f"[done] registered with Claude Code (scope {scope})")
    elif "already exists" in output.casefold():
        report("register", "[skipped: already registered]")
    else:
        detail = (result.stderr or "").strip() or f"claude mcp add exited {result.returncode}"
        report("register", f"[failed: {detail}]")


def _resolve_codex_home(codex_home: Path | None) -> Path:
    """Codex's config home, injectable so tests never reach the real one."""
    from . import client_config

    return Path(codex_home) if codex_home is not None else client_config.codex_home()


def _codex_present(which_fn, codex_home: Path | None) -> bool:
    """True when Codex is installed here: the CLI on PATH, or its config home."""
    return bool(which_fn("codex")) or _resolve_codex_home(codex_home).is_dir()


def _register_codex(
    *,
    route: client_config.McpRoute,
    which_fn,
    run_fn,
    input_fn,
    print_fn,
    report,
    yes: bool,
    replace_client_registration: bool,
    codex_home: Path | None = None,
) -> None:
    """Register the MCP server with Codex, preferring its CLI over file surgery."""
    block = client_config.render_codex_block(route)
    path = _resolve_codex_home(codex_home) / "config.toml"
    try:
        existing = client_config.codex_mcp_exists(path)
    except ValueError as exc:
        report("register-codex", f"[failed: {exc}]")
        return
    replace = replace_client_registration and existing
    if existing and not replace:
        if yes or not _ask_yn(input_fn, f"exomem is already in {path}. Replace it?", False):
            report("register-codex", "[skipped: already in config.toml]")
            return
        replace = True

    codex = which_fn("codex")
    run_kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
    if replace and codex:
        try:
            snapshot = _snapshot_files((path,))
        except OSError as exc:
            report("register-codex", f"[failed: could not snapshot Codex configuration: {exc}]")
            return
        failure = ""
        try:
            removed = run_fn([codex, "mcp", "remove", "exomem"], **run_kwargs)
            if removed.returncode != 0:
                detail = (removed.stderr or "").strip() or (
                    f"codex mcp remove exited {removed.returncode}"
                )
                failure = detail
            else:
                added = run_fn(route.codex_add_argv(codex), **run_kwargs)
                if added.returncode != 0:
                    failure = (added.stderr or "").strip() or (
                        f"codex mcp add exited {added.returncode}"
                    )
        except Exception as exc:  # noqa: BLE001 - restore is the safety boundary
            failure = str(exc)
        if failure:
            try:
                _restore_files(snapshot)
            except OSError as restore_exc:
                report(
                    "register-codex",
                    f"[failed: {failure}; restoring previous Codex registration also failed: "
                    f"{restore_exc}]",
                )
                return
            report(
                "register-codex",
                f"[failed: {failure}; restored previous Codex registration]",
            )
            return
        report("register-codex", "[done] replaced registration with Codex")
        return

    if replace:
        try:
            outcome = client_config.merge_codex_mcp(block, path=path, replace=True)
        except (ValueError, OSError) as e:
            report("register-codex", f"[failed: {e}]")
            return
        _report_codex_file_outcome(outcome, path=path, print_fn=print_fn, report=report)
        return

    if codex:
        argv = route.codex_add_argv(codex)
        try:
            result = run_fn(argv, **run_kwargs)
        except Exception as exc:  # noqa: BLE001 - fall back to the config file
            print_fn(f"  codex mcp add unavailable ({exc}); writing config.toml directly.")
            result = None
        if result is not None and result.returncode == 0:
            report("register-codex", "[done] registered with Codex")
            return
        # Older Codex builds have no `mcp add`; fall through to the config file
        # rather than leaving the user unregistered.
        if result is not None:
            print_fn("  codex mcp add unavailable; writing config.toml directly.")

    try:
        outcome = client_config.merge_codex_mcp(block, path=path)
        if outcome["action"] == "exists":
            if yes:
                report("register-codex", "[skipped: already in config.toml]")
                return
            if not _ask_yn(input_fn, f"exomem is already in {path}. Replace it?", False):
                report("register-codex", "[skipped: already in config.toml]")
                return
            outcome = client_config.merge_codex_mcp(block, path=path, replace=True)
    except (ValueError, OSError) as e:
        report("register-codex", f"[failed: {e}]")
        return

    _report_codex_file_outcome(outcome, path=path, print_fn=print_fn, report=report)


def _report_codex_file_outcome(outcome: dict, *, path: Path, print_fn, report) -> None:
    if outcome["diff"]:
        print_fn(f"  {path}:")
        for line in outcome["diff"].splitlines():
            print_fn(f"    {line}")
    if outcome["backup"]:
        print_fn(f"  backup: {outcome['backup']}")
    report("register-codex", f"[done] {outcome['action']} in config.toml")


def run_setup(
    *,
    vault: str | None,
    yes: bool = False,
    profile: str | None = None,
    with_hooks: bool | None = None,
    skip_claude_register: bool = False,
    scope: str = "user",
    mcp_url: str | None = None,
    force_stdio: bool = False,
    replace_client_registration: bool = False,
    input_fn=input,
    run_fn=subprocess.run,
    which_fn=shutil.which,
    home: Path | None = None,
    codex_home: Path | None = None,
    claude_config_path: Path | None = None,
    project_dir: Path | None = None,
    environ: dict[str, str] | None = None,
    persist_profile_fn=None,
    print_fn=print,
) -> int:
    environ = dict(os.environ if environ is None else environ)
    project_dir = Path.cwd() if project_dir is None else Path(project_dir)
    claude_config_path = (
        _default_claude_config_path(environ)
        if claude_config_path is None
        else Path(claude_config_path)
    )
    if persist_profile_fn is None:
        persist_profile_fn = install_info.persist_local_profile
    try:
        configured_mcp_url = _configured_mcp_url(
            mcp_url=mcp_url,
            force_stdio=force_stdio,
            cwd=project_dir,
            environ=environ,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print_fn(f"setup: {exc}")
        return 2
    steps: list[tuple[str, str]] = []

    def report(name: str, status: str) -> None:
        steps.append((name, status))
        print_fn(f"  {name}: {status}")

    def finish() -> int:
        print_fn("")
        print_fn("Summary:")
        for name, status in steps:
            print_fn(f"  {name:<10} {status}")
        return 1 if any("[failed" in status for _, status in steps) else 0

    print_fn("exomem setup")
    print_fn("")

    # 1. vault path
    if not vault:
        if yes:
            print_fn("setup: --yes requires --vault.")
            return 2
        env_default = environ.get("EXOMEM_VAULT_PATH", "")
        raw = input_fn(f"Vault folder [{env_default}]: ").strip()
        vault = raw or env_default
        if not vault:
            print_fn("setup: a vault path is required.")
            return 2
    vault_path = Path(vault).expanduser()
    if not vault_path.exists():
        if yes or _ask_yn(input_fn, f"{vault_path} does not exist. Create it?", True):
            vault_path.mkdir(parents=True, exist_ok=True)
            report("vault", f"[done] created {vault_path}")
        else:
            print_fn("setup: aborted.")
            return 1
    else:
        report("vault", f"[done] {vault_path}")

    # 2. pre-init scan — the "you already have notes here" moment
    try:
        adoption = adopt_module.adopt(vault_path)
        scan = adoption["overview"]
    except adopt_module.AdoptError as e:
        report("scan", f"[failed: {e}]")
        return finish()
    totals = scan["totals"]
    print_fn("")
    print_fn(
        f"  Scanned: {totals['files']} files ({totals['markdown']} markdown) "
        f"in {totals['dirs']} folders."
    )
    busiest = sorted(
        (e for e in scan["tree"] if e["path"]),
        key=lambda e: -e["files_recursive"],
    )[:3]
    for entry in busiest:
        print_fn(f"    {entry['path']}/  ({entry['files_recursive']} files)")
    junk_total = sum(scan["junk"]["counts"].values())
    if junk_total:
        print_fn(f"    {junk_total} junk candidate(s) — zero-byte or sync-conflict copies.")
    kb_state = "already present" if scan["kb"]["present"] else "not present yet"
    print_fn(f"    {kb_prefix()}: {kb_state}")
    packs = adoption.get("pack_suggestions") or []
    if packs:
        print_fn(f"    Likely packs: {_format_pack_suggestions(packs)}")
    print_fn("")
    print_fn(f"  Contract: {overview_module.SCOPE_NOTE}")
    print_fn("  Adoption: run `exomem adopt` anytime for manifest review, source copy, and compile planning.")
    print_fn("")
    report("scan", "[done]")

    # 3. init — never forced from the wizard
    try:
        init_module.init_vault(vault_path)
        report("init", f"[done] {kb_prefix()} scaffold created")
    except FileExistsError:
        report("init", f"[skipped: {kb_prefix()} already exists]")

    # 3b. packs — product guidance for fresh vaults and suggested routes for existing vaults
    try:
        selected_ids = _choose_pack_ids(
            input_fn,
            print_fn,
            available=adoption.get("available_packs") or knowledge_packs_module.list_builtin_packs(),
            suggested=packs,
            yes=yes,
        )
        selection = knowledge_packs_module.write_selected_packs(
            vault_path,
            selected_ids,
            source="setup",
        )
        print_fn(f"    Selected packs: {_format_selected_pack_names(selection)}")
        print_fn("    Pack selection is guidance only; no folders or notes were created.")
        report("packs", f"[done] {', '.join(selection['selected_pack_ids'])}")
    except knowledge_packs_module.PackSelectionError as e:
        report("packs", f"[failed: {e}]")
        return finish()

    # 3c. personalize — propose per-subtree access governance for sibling folders
    try:
        prep = personalize_module.scan_and_classify(vault_path)
    except personalize_module.PersonalizeError as e:
        report("personalize", f"[failed: {e}]")
        prep = None
    if prep is not None:
        if not prep.needs_write:
            report("personalize", "[skipped: no sibling folders need governing]")
        else:
            for p in prep.proposals:
                if p.already_configured is None and p.classification != personalize_module.CLASS_UNMANAGED:
                    print_fn(f"    {p.folder}/  -> {p.classification}  ({p.reason})")
            if yes or _ask_yn(input_fn, "Write these entries to _access.yaml?", True):
                done = personalize_module.write_access_yaml(prep)
                report(
                    "personalize",
                    f"[done] +{len(done.add_readonly)} readonly, +{len(done.add_excluded)} excluded",
                )
            else:
                report("personalize", "[skipped: declined]")

    # 4. profile
    if profile is None:
        has_embeddings = importlib.util.find_spec("sentence_transformers") is not None
        has_media = importlib.util.find_spec("faster_whisper") is not None
        if yes or not has_embeddings:
            profile = "standard" if has_embeddings and has_media else (
                "hybrid" if has_embeddings else "lean"
            )
            if not has_embeddings:
                print_fn(
                    "  Lean profile (keyword/BM25 search). For semantic search later: "
                    "uv sync --extra embeddings."
                )
        else:
            profile = (
                "hybrid"
                if _ask_yn(input_fn, "Semantic embeddings are installed — use hybrid search?", True)
                else "lean"
            )
    report("profile", f"[done] {profile}")
    try:
        persist_profile_fn(profile)
    except (OSError, ValueError) as exc:
        print_fn(f"  Warning: could not persist the selected doctor profile: {exc}")

    # 5. doctor preflight — hard gate in non-interactive mode
    doctor_report = doctor_module.doctor(vault=str(vault_path), profile=profile)
    if doctor_report.success:
        report("doctor", "[done] preflight passed")
    else:
        print_fn(doctor_module.render_human(doctor_report))
        report("doctor", "[failed: preflight reported failures]")
        if yes or not _ask_yn(input_fn, "Doctor reported failures. Continue anyway?", False):
            return finish()

    # 5b. GPU discoverability — offer performance mode when a capable idle GPU is present.
    # Interactive only (never blocks --yes automation), and only when embeddings are on
    # (a lean install has no models to accelerate). CPU stays the safe default otherwise.
    if not yes and profile != "lean":
        from . import mode as mode_mod
        from . import resource_status

        gpu = resource_status.gpu_headroom()
        if mode_mod.resolve_mode() != "performance" and gpu.get("usable") is True:
            if _ask_yn(
                input_fn,
                "\nA capable idle GPU was detected. Use performance mode for "
                "faster explicit indexing? Normal mode avoids steady-state CUDA "
                "residency. (change anytime with `exomem mode`)",
                False,
            ):
                mode_mod.write_mode("performance")
                report("gpu", "[done] performance mode enabled")
            else:
                report("gpu", "[skipped] staying on CPU (normal mode)")

    try:
        route = _resolve_client_route(
            mcp_url=configured_mcp_url,
            force_stdio=force_stdio,
            cwd=project_dir,
            environ={},
            which_fn=which_fn,
            vault_path=vault_path,
            profile=profile,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report("route", f"[failed: {exc}]")
        return finish()

    # 6. Claude Code registration
    if skip_claude_register:
        report("register", "[skipped: --skip-claude-register]")
    else:
        if route.transport == "stdio" and route.command[0] == "uvx":
            print_fn(
                "  Note: exomem is not durably installed, so the server will be "
                "registered as `uvx exomem`. For a registration that never "
                "re-resolves, run `uv tool install exomem` first."
            )
        claude = which_fn("claude")
        if not claude:
            snippet = {"mcpServers": {"exomem": route.as_claude_config()}}
            print_fn("  claude CLI not found — add this to .mcp.json or Claude Code settings:")
            print_fn(json.dumps(snippet, indent=2))
            report("register", "[skipped: no claude CLI — snippet printed above]")
        else:
            # encoding pinned: Windows-native Python otherwise decodes pipes as
            # cp1252 and multibyte output crashes the reader thread
            run_kwargs = dict(
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(project_dir),
            )
            legacy_plugins: list[str] | None = None
            try:
                legacy_plugins = _legacy_stdio_plugins(claude, run_fn, run_kwargs)
            except ValueError as exc:
                report("register", f"[failed: {exc}]")
            if legacy_plugins:
                plugin_id = legacy_plugins[0]
                print_fn(
                    "  The enabled Exomem plugin still launches a full stdio core. "
                    f"Run `claude plugin update {plugin_id}`, then `/reload-plugins` "
                    "(or restart Claude Code) before rerunning setup."
                )
                report("register", "[failed: enabled plugin still declares stdio]")
            elif legacy_plugins is not None:
                _register_claude(
                    claude=claude,
                    route=route,
                    scope=scope,
                    config_path=claude_config_path,
                    project_dir=project_dir,
                    replace_client_registration=replace_client_registration,
                    yes=yes,
                    input_fn=input_fn,
                    run_fn=run_fn,
                    run_kwargs=run_kwargs,
                    report=report,
                )

    # 6b. Codex registration — symmetric with Claude Code above. Codex reads
    # skills from disk and speaks MCP just like Claude Code does, so leaving it
    # as a printed snippet was the reason Codex users ended up with tools and no
    # working registration.
    if skip_claude_register:
        report("register-codex", "[skipped: --skip-claude-register]")
    elif not _codex_present(which_fn, codex_home):
        report("register-codex", "[skipped: Codex not detected on this machine]")
    else:
        _register_codex(
            codex_home=codex_home,
            route=route,
            which_fn=which_fn,
            run_fn=run_fn,
            input_fn=input_fn,
            print_fn=print_fn,
            report=report,
            yes=yes,
            replace_client_registration=replace_client_registration,
        )

    # 7. skill — the brain; without it the tools sit unused
    skill_target = (home / "skills" / "exomem") if home else None
    try:
        if skill_target is not None:
            install_module.install_skill(skill_target)
            report("skill", "[done] installed")
        else:
            # No explicit target: install into every client present on this
            # machine, so a Codex user gets the brain too rather than just tools.
            installed = install_module.install_skills(client="auto")["installed"]
            report("skill", f"[done] installed for {', '.join(installed)}")
    except FileExistsError:
        target = skill_target if skill_target is not None else install_module.DEFAULT_TARGET
        skill_md = target / "SKILL.md"
        try:
            head = skill_md.read_text(encoding="utf-8", errors="replace")[:2048]
        except OSError:
            head = ""
        if _SKILL_NAME_MARKER not in head:
            report("skill", f"[skipped: {target} exists and is not the bundled skill — not overwriting]")
        elif not yes and _ask_yn(input_fn, "Skill already installed. Refresh it from this repo?", False):
            if skill_target is not None:
                install_module.install_skill(skill_target, force=True)
                report("skill", "[done] refreshed")
            else:
                installed = install_module.install_skills(client="auto", force=True)["installed"]
                report("skill", f"[done] refreshed for {', '.join(installed)}")
        else:
            report("skill", "[skipped: already installed]")
    except FileNotFoundError as e:
        report("skill", f"[failed: {e}]")

    # 7b. migrate: a pre-rename `knowledge-base` install lingers as a stale duplicate
    # skill now that the skill is `exomem`; retire it, but only when it's ours.
    legacy_dir = (home / "skills" / "knowledge-base") if home else None
    removed = install_module.remove_legacy_skill(legacy_dir)
    if removed is not None:
        report("migrate", f"[done] removed stale {removed}")

    # 8. hooks — optional reliability nudges
    do_hooks = with_hooks
    if do_hooks is None:
        do_hooks = False if yes else _ask_yn(
            input_fn, "Install the optional capture/retrieval nudge hooks?", False
        )
    if do_hooks:
        try:
            hook_module.install_hook(
                hook_dir=str(home / "hooks") if home else None,
                settings_path=str(home / "settings.json") if home else None,
                wire=True,
            )
            report("hooks", "[done] installed + wired")
        except FileNotFoundError as e:
            report("hooks", f"[failed: {e}]")
        except OSError as e:
            # The trusted-directory guard raises PermissionError, which is an
            # OSError — uncaught, it escaped as a raw traceback after register,
            # register-codex and skill had already succeeded. scripts/install.sh
            # holds the line that a user never sees a stack trace; the wizard
            # has to hold it too, and say that the earlier steps stuck.
            report("hooks", f"[failed: {e}]")
            print_fn("")
            print_fn(
                "  Setup is idempotent — fix the above and re-run the same command; "
                "the completed steps are skipped."
            )
    else:
        report("hooks", "[skipped]")

    code = finish()
    print_fn("")
    print_fn("Next steps:")
    if route.transport == "http":
        print_fn("  1. In Claude Code, use /mcp to authenticate the claude mcp connection.")
        print_fn("     For Codex, run: codex mcp login exomem")
    else:
        print_fn("  1. Restart Claude Code so it loads the exomem server and skill.")
    print_fn('  2. Try: "what does this vault look like" or "find my notes on X".')
    print_fn("  3. Optional, for direct CLI use (`kb find ...`): set EXOMEM_VAULT_PATH.")
    print_fn(
        "  4. For foreground work/gaming: exomem mode quiet; inspect with "
        "exomem status --resources --json."
    )
    return code


def setup_main(argv: list[str]) -> int:
    # `exomem setup --remote` is a distinct wizard (tunnel + GitHub OAuth + .env
    # + live probe) with its own flags; route to it before the local parser so
    # the two flag sets never collide.
    if "--remote" in argv:
        from .remote_setup_wizard import remote_setup_main

        return remote_setup_main([a for a in argv if a != "--remote"])

    parser = argparse.ArgumentParser(
        prog="exomem setup",
        description=(
            "Guided local setup: scan the vault, init the "
            f"{kb_dirname()}, pick a "
            "search profile, run doctor, register with Claude Code, and install "
            "the skill — one idempotent command. Existing vault content is never "
            f"touched; exomem writes only under {kb_prefix()}. For remote "
            "connector setup (claude.ai / iOS), use `exomem setup --remote`."
        ),
    )
    parser.add_argument("--vault", help="Vault root (default: prompt, or $EXOMEM_VAULT_PATH).")
    parser.add_argument("--yes", action="store_true", help="Non-interactive; requires --vault.")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--lean", action="store_const", const="lean", dest="profile",
                         help="Keyword/BM25 search only (no embeddings).")
    profile.add_argument("--hybrid", action="store_const", const="hybrid", dest="profile",
                         help="Hybrid semantic search (needs the embeddings extra).")
    profile.add_argument("--standard", action="store_const", const="standard", dest="profile",
                         help="Default multimodal profile (embeddings + media extras).")
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument("--with-hooks", action="store_const", const=True, dest="with_hooks",
                       help="Also install the capture/retrieval nudge hooks.")
    hooks.add_argument("--no-hooks", action="store_const", const=False, dest="with_hooks",
                       help="Skip the hooks step without asking.")
    parser.add_argument("--skip-claude-register", action="store_true",
                        help="Don't touch Claude Code's MCP registration.")
    route = parser.add_mutually_exclusive_group()
    route.add_argument(
        "--mcp-url",
        help="Shared Exomem service origin or exact /mcp URL (HTTPS, or loopback HTTP).",
    )
    route.add_argument(
        "--stdio",
        action="store_true",
        dest="force_stdio",
        help="Register a separate local stdio server even when a service URL is configured.",
    )
    parser.add_argument(
        "--replace-client-registration",
        action="store_true",
        help="Replace existing Exomem MCP registrations across client scopes.",
    )
    parser.add_argument("--scope", choices=("user", "local", "project"), default="user",
                        help="claude mcp add scope (default: user — available in every project).")
    args = parser.parse_args(argv)
    if args.yes and not args.vault:
        parser.error("--yes requires --vault")
    return run_setup(
        vault=args.vault,
        yes=args.yes,
        profile=args.profile,
        with_hooks=args.with_hooks,
        skip_claude_register=args.skip_claude_register,
        scope=args.scope,
        mcp_url=args.mcp_url,
        force_stdio=args.force_stdio,
        replace_client_registration=args.replace_client_registration,
    )
