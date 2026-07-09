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
import subprocess
from pathlib import Path

from . import adopt as adopt_module
from . import doctor as doctor_module
from . import init as init_module
from . import install_hook as hook_module
from . import install_skill as install_module
from . import knowledge_packs as knowledge_packs_module
from . import overview as overview_module
from . import personalize as personalize_module
from .kbdir import kb_dirname, kb_prefix

_SKILL_NAME_MARKER = "name: exomem"
_CLAUDE_SCOPES = ("user", "local", "project")


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


def _output_mentions_exomem(output: str) -> bool:
    for line in output.splitlines():
        stripped = line.strip().lower()
        if not stripped:
            continue
        if stripped.startswith("exomem") or '"exomem"' in stripped or "name: exomem" in stripped:
            return True
    return False


def _claude_registered_scopes(claude: str, run_fn, run_kwargs: dict) -> list[str]:
    scopes: list[str] = []
    for item in _CLAUDE_SCOPES:
        try:
            result = run_fn([claude, "mcp", "list", "--scope", item], **run_kwargs)
        except Exception:  # noqa: BLE001 - registration can still try the requested add
            continue
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and _output_mentions_exomem(output):
            scopes.append(item)
    return scopes


def _format_scopes(scopes: list[str]) -> str:
    return ", ".join(scopes) if scopes else "none"


def run_setup(
    *,
    vault: str | None,
    yes: bool = False,
    profile: str | None = None,
    with_hooks: bool | None = None,
    skip_claude_register: bool = False,
    scope: str = "user",
    input_fn=input,
    run_fn=subprocess.run,
    which_fn=shutil.which,
    home: Path | None = None,
    print_fn=print,
) -> int:
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
        env_default = os.environ.get("EXOMEM_VAULT_PATH", "")
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
        if yes or not has_embeddings:
            profile = "hybrid" if has_embeddings else "lean"
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

    # 6. Claude Code registration
    if skip_claude_register:
        report("register", "[skipped: --skip-claude-register]")
    else:
        env_args = ["--env", f"EXOMEM_VAULT_PATH={vault_path}"]
        env_dict = {"EXOMEM_VAULT_PATH": str(vault_path)}
        if profile == "lean":
            env_args += ["--env", "EXOMEM_DISABLE_EMBEDDINGS=1"]
            env_dict["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
        server_cmd = _server_command(which_fn)
        if server_cmd[0] == "uvx":
            print_fn(
                "  Note: exomem is not durably installed, so the server will be "
                "registered as `uvx exomem`. For a registration that never "
                "re-resolves, run `uv tool install exomem` first."
            )
        claude = which_fn("claude")
        if not claude:
            snippet = {
                "mcpServers": {
                    "exomem": {
                        "command": server_cmd[0],
                        "args": server_cmd[1:],
                        "env": env_dict,
                    }
                }
            }
            print_fn("  claude CLI not found — add this to .mcp.json or Claude Code settings:")
            print_fn(json.dumps(snippet, indent=2))
            report("register", "[skipped: no claude CLI — snippet printed above]")
        else:
            argv = [claude, "mcp", "add", "exomem", "--scope", scope, *env_args, "--", *server_cmd]
            # encoding pinned: Windows-native Python otherwise decodes pipes as
            # cp1252 and multibyte output crashes the reader thread
            run_kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
            existing_scopes = _claude_registered_scopes(claude, run_fn, run_kwargs)
            if existing_scopes:
                scope_text = _format_scopes(existing_scopes)
                if yes:
                    report("register", f"[skipped: already registered in {scope_text}]")
                elif _ask_yn(input_fn, f"exomem is already registered in {scope_text}. Replace it?", False):
                    for existing_scope in existing_scopes:
                        run_fn([claude, "mcp", "remove", "exomem", "--scope", existing_scope], **run_kwargs)
                    result = run_fn(argv, **run_kwargs)
                    if result.returncode == 0:
                        report("register", f"[done] re-registered with Claude Code (scope {scope})")
                    else:
                        report("register", f"[failed: {(result.stderr or '').strip()}]")
                else:
                    report("register", f"[skipped: already registered in {scope_text}]")
            else:
                result = run_fn(argv, **run_kwargs)
                output = (result.stderr or "") + (result.stdout or "")
                if result.returncode == 0:
                    report("register", f"[done] registered with Claude Code (scope {scope})")
                elif "already exists" in output:
                    if not yes and _ask_yn(input_fn, "exomem is already registered. Replace it?", False):
                        run_fn([claude, "mcp", "remove", "exomem", "--scope", scope], **run_kwargs)
                        result = run_fn(argv, **run_kwargs)
                        if result.returncode == 0:
                            report("register", "[done] re-registered")
                        else:
                            report("register", f"[failed: {(result.stderr or '').strip()}]")
                    else:
                        report("register", "[skipped: already registered]")
                else:
                    detail = (result.stderr or "").strip() or f"claude mcp add exited {result.returncode}"
                    report("register", f"[failed: {detail}]")

    # 7. skill — the brain; without it the tools sit unused
    skill_target = (home / "skills" / "exomem") if home else None
    try:
        install_module.install_skill(skill_target)
        report("skill", "[done] installed")
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
            install_module.install_skill(skill_target, force=True)
            report("skill", "[done] refreshed")
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
    else:
        report("hooks", "[skipped]")

    code = finish()
    print_fn("")
    print_fn("Next steps:")
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
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument("--with-hooks", action="store_const", const=True, dest="with_hooks",
                       help="Also install the capture/retrieval nudge hooks.")
    hooks.add_argument("--no-hooks", action="store_const", const=False, dest="with_hooks",
                       help="Skip the hooks step without asking.")
    parser.add_argument("--skip-claude-register", action="store_true",
                        help="Don't touch Claude Code's MCP registration.")
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
    )
