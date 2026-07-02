"""`python -m exomem` entry point.

Subcommands:
- (default) serve the MCP server — `python -m exomem [--transport ...]`
- `init` — bootstrap a fresh Knowledge Base into a vault
- `install-skill` — install the Exomem knowledge-base skill into Claude Code
- `install-hook` — wire the KB capture + retrieval hooks into Claude Code
- `doctor` — read-only local install/setup preflight
- `backfill-media` — make pre-existing Evidence binaries searchable (sidecar + OCR/ASR/PDF + CLIP)
- `enroll-speaker` / `list-speakers` / `remove-speaker` — manage named-speaker voice profiles
  for opt-in diarization (desk-side admin; never an MCP tool)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import server


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "init":
        return _init_main(raw[1:])
    if raw and raw[0] == "install-skill":
        return _install_skill_main(raw[1:])
    if raw and raw[0] == "install-hook":
        return _install_hook_main(raw[1:])
    if raw and raw[0] == "doctor":
        return _doctor_main(raw[1:])
    if raw and raw[0] == "backfill-media":
        return _backfill_media_main(raw[1:])
    if raw and raw[0] == "enroll-speaker":
        return _enroll_speaker_main(raw[1:])
    if raw and raw[0] == "list-speakers":
        return _list_speakers_main(raw[1:])
    if raw and raw[0] == "remove-speaker":
        return _remove_speaker_main(raw[1:])
    # Registry-driven core operations (reads + writes): `kb find "…"`, `kb get …`,
    # `kb note …`, etc. — every command on the `cli` surface.
    if raw and raw[0] in _core_op_names():
        return _core_op_main(raw)
    # A real tier-2 op invoked while EXOMEM_DISABLE_TIER2 is set would otherwise fall
    # through to the serve parser and emit a confusing argparse error — name it instead.
    if raw and not _expose_tier2() and raw[0] in _core_op_names(expose_tier2=True):
        print(
            f"Error [UNAVAILABLE]: operation {raw[0]!r} is unavailable (tier-2 disabled)",
            file=sys.stderr,
        )
        return 2
    return _serve_main(raw)


def _serve_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="exomem")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default="http",
        help="MCP transport to serve (default: http). stdio for local Claude Code use.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for HTTP transports (default: $EXOMEM_HOST, else 127.0.0.1; "
        "fronted by Cloudflare Tunnel). Set 0.0.0.0 to also serve a direct Tailscale/LAN route.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for HTTP transports (default: 8765).",
    )
    args = parser.parse_args(argv)

    try:
        server.run(transport=args.transport, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 — top-level CLI guard: report and exit non-zero
        print(f"exomem failed: {e}", file=sys.stderr)
        return 1
    return 0


def _backfill_media_main(argv: list[str]) -> int:
    import logging

    parser = argparse.ArgumentParser(
        prog="exomem backfill-media",
        description="Make pre-existing Evidence binaries searchable: write a sidecar if "
        "missing, extract text (OCR/ASR/PDF), and CLIP-embed images. Idempotent; CPU or GPU.",
    )
    parser.add_argument(
        "--vault", default=os.environ.get("EXOMEM_VAULT_PATH"),
        help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    parser.add_argument("--no-ocr", action="store_true", help="skip text extraction (sidecar + CLIP only)")
    parser.add_argument("--no-clip", action="store_true", help="skip CLIP image embedding")
    args = parser.parse_args(argv)
    if not args.vault:
        print("backfill-media: set --vault or EXOMEM_VAULT_PATH", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from . import backfill

    backfill.backfill_media(
        Path(args.vault).expanduser(),
        do_ocr=not args.no_ocr,
        do_clip=not args.no_clip,
        dry_run=args.dry_run,
        log_fn=print,
    )
    return 0


def _doctor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem doctor",
        description="Read-only local setup preflight for exomem installs.",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument(
        "--profile",
        choices=("lean", "hybrid", "media", "remote"),
        default="lean",
        help="capability profile to validate (default: lean)",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import doctor as doctor_module

    report = doctor_module.doctor(vault=args.vault, profile=args.profile)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, default=str))
    else:
        print(doctor_module.render_human(report))
    return 0 if report.success else 1


def _speaker_vault(args) -> Path | None:
    """Vault root for the voice-profile store: --vault, else $EXOMEM_VAULT_PATH, else resolve."""
    if args.vault:
        return Path(args.vault).expanduser()
    return None  # enroll_speaker resolves via EXOMEM_VAULT_PATH


def _enroll_speaker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem enroll-speaker",
        description=(
            "Enroll (or extend) a named voice profile from an audio sample for opt-in "
            "diarization. The sample is embedded into a 192-dim ECAPA voiceprint and stored in "
            "the per-machine profile store beside the embedding sidecar — desk-side admin, never "
            "an MCP tool. Re-enrolling the same name running-averages the centroid over samples. "
            'Example: exomem enroll-speaker --name Alice --self alice-sample.wav'
        ),
    )
    parser.add_argument("audio", help="path to an audio sample of the speaker's voice")
    parser.add_argument("--name", required=True, help="speaker name to attach to matched clusters")
    parser.add_argument(
        "--self", dest="is_self", action="store_true",
        help="mark this profile as the vault owner's own voice (is_self).",
    )
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="per-profile cosine match threshold (default 0.40). Raise for confusable voices.",
    )
    parser.add_argument(
        "--vault", default=os.environ.get("EXOMEM_VAULT_PATH"),
        help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)",
    )
    args = parser.parse_args(argv)

    from . import enroll_speaker as enroll_module
    from .voice_profiles import DEFAULT_THRESHOLD

    try:
        rec = enroll_module.enroll_speaker(
            args.audio, args.name, is_self=args.is_self,
            threshold=args.threshold if args.threshold is not None else DEFAULT_THRESHOLD,
            vault_root=_speaker_vault(args),
        )
    except (enroll_module.EnrollmentError, RuntimeError) as e:
        print(f"exomem enroll-speaker: {e}", file=sys.stderr)
        return 1
    print(
        f"Enrolled {args.name!r} ({rec['samples']} sample(s), "
        f"threshold {rec['threshold']}, is_self={rec['is_self']})."
    )
    return 0


def _list_speakers_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem list-speakers",
        description="List the enrolled voice profiles used for named diarization.",
    )
    parser.add_argument(
        "--vault", default=os.environ.get("EXOMEM_VAULT_PATH"),
        help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)",
    )
    args = parser.parse_args(argv)

    from . import enroll_speaker as enroll_module

    try:
        profiles = enroll_module.list_speakers(_speaker_vault(args))
    except RuntimeError as e:
        print(f"exomem list-speakers: {e}", file=sys.stderr)
        return 1
    if not profiles:
        print("No voice profiles enrolled.")
        return 0
    for p in profiles:
        flag = " (self)" if p["is_self"] else ""
        print(f"  {p['name']}{flag}: {p['samples']} sample(s), threshold {p['threshold']}")
    return 0


def _remove_speaker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem remove-speaker",
        description="Delete an enrolled voice profile; that voice then labels anonymously again.",
    )
    parser.add_argument("--name", required=True, help="profile name to remove")
    parser.add_argument(
        "--vault", default=os.environ.get("EXOMEM_VAULT_PATH"),
        help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)",
    )
    args = parser.parse_args(argv)

    from . import enroll_speaker as enroll_module

    try:
        removed = enroll_module.remove_speaker(args.name, _speaker_vault(args))
    except RuntimeError as e:
        print(f"exomem remove-speaker: {e}", file=sys.stderr)
        return 1
    print(f"Removed {args.name!r}." if removed else f"No profile named {args.name!r}.")
    return 0


def _init_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem init",
        description="Bootstrap a fresh Knowledge Base scaffold into a vault.",
    )
    parser.add_argument(
        "--vault",
        help="Vault root to scaffold (default: $EXOMEM_VAULT_PATH, else current dir).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overlay the scaffold even if Knowledge Base/ exists (existing files kept).",
    )
    args = parser.parse_args(argv)

    from . import init as init_module

    vault = args.vault or os.environ.get("EXOMEM_VAULT_PATH") or "."
    try:
        report = init_module.init_vault(Path(vault), force=args.force)
    except FileExistsError as e:
        print(f"exomem init: {e}", file=sys.stderr)
        return 1
    print(f"Initialized Knowledge Base at {report['kb']}")
    print(f"  {len(report['created'])} files created + the typed folder tree.")
    print("Next:")
    print("  1. Point Claude Code at this vault (see SETUP-LOCAL.md).")
    print("  2. Install the Exomem Knowledge Base skill so Claude knows how to use it: python -m exomem install-skill")
    print("  3. Adapt Knowledge Base/_Schema/project-keys.yaml to your own projects.")
    return 0


def _install_skill_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem install-skill",
        description=(
            "Install the Exomem knowledge-base skill into Claude Code's skills folder. "
            "The MCP server is the hands; the skill is the brain that tells Claude "
            "when to capture and how to file — without it, the tools sit unused."
        ),
    )
    parser.add_argument(
        "--target",
        help="Skill folder to install into (default: ~/.claude/skills/knowledge-base).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing install at the target.",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="Symlink instead of copy, so the install tracks repo updates "
        "(falls back to copy if the OS refuses the symlink).",
    )
    args = parser.parse_args(argv)

    from . import install_skill as install_module

    target = Path(args.target) if args.target else None
    try:
        report = install_module.install_skill(target, force=args.force, link=args.link)
    except (FileExistsError, FileNotFoundError) as e:
        print(f"exomem install-skill: {e}", file=sys.stderr)
        return 1
    print(
        f"Installed the Exomem knowledge-base skill ({report['mode']}, "
        f"{report['files']} files):"
    )
    print(f"  {report['target']}")
    print("Restart Claude Code to load it. Then just talk - it captures at")
    print('natural stopping points, or say "find my notes on X".')
    return 0


def _install_hook_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem install-hook",
        description=(
            "Wire the KB capture + retrieval hooks into Claude Code: a Stop hook "
            "that captures conclusions at stepping-stones (write), and a "
            "UserPromptSubmit hook that reminds Claude to consult the KB before "
            "answering (read). Language-agnostic and cheap (gated + cooldown). "
            "Re-running is idempotent."
        ),
    )
    parser.add_argument(
        "--hook-dir",
        help="Where to write the hook script (default: ~/.claude/hooks).",
    )
    parser.add_argument(
        "--settings",
        help="settings.json to wire (default: ~/.claude/settings.json).",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Write the script but don't touch settings.json; print the snippet to add.",
    )
    args = parser.parse_args(argv)

    from . import install_hook as hook_module

    try:
        report = hook_module.install_hook(
            hook_dir=args.hook_dir,
            settings_path=args.settings,
            wire=not args.print_only,
        )
    except FileNotFoundError as e:
        print(f"exomem install-hook: {e}", file=sys.stderr)
        return 1

    print("Installed the KB hook scripts:")
    for item in report["installed"]:
        print(f"  {item['event']:<16} {item['script']}")
    if report["wired"]:
        print(f"Wired into {report['settings']}.")
        print("Restart Claude Code to activate. Triggers log to:")
        print("  ~/.claude/kb-capture-nudge.log   (write / capture)")
        print("  ~/.claude/kb-retrieve-nudge.log  (read / retrieval)")
    else:
        print("Add this to your settings.json (merge into hooks):")
        print(hook_module.snippet(report["installed"]))
    return 0


# --------------------------------------------------------------------------- #
# Registry-driven core operations (reads + writes)
# --------------------------------------------------------------------------- #
# `note`/`replace` carry a wide, type-specific signature; rather than dozens of
# flags, their REQUIRED params stay flags and everything else is reachable via a
# repeatable `--field key=value`, so the CLI stays clean.
_FIELD_ESCAPE = frozenset({"note", "replace"})


def _expose_tier2() -> bool:
    return not os.environ.get("EXOMEM_DISABLE_TIER2")


def _core_op_names(*, expose_tier2: bool | None = None) -> frozenset[str]:
    from . import commands as commands_module

    if expose_tier2 is None:
        expose_tier2 = _expose_tier2()
    return frozenset(
        c.name for c in commands_module.commands_for("cli", expose_tier2=expose_tier2)
    )


class _CLIParser(argparse.ArgumentParser):
    """argparse parser that emits `Error [USAGE]: …` and exits 2 on usage errors."""

    def error(self, message: str):  # noqa: ANN201 — argparse signature
        self.exit(2, f"Error [USAGE]: {message}\n")


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _add_command_args(sp: argparse.ArgumentParser, cmd) -> None:
    field_escape = cmd.name in _FIELD_ESCAPE
    for p in cmd.params:
        if field_escape and not p.required:
            continue  # reachable via --field
        if p.cli_positional:
            sp.add_argument(
                p.name,
                nargs=None if p.required else "?",
                default=None,
                help=p.help or None,
            )
        elif p.type == "bool":
            sp.add_argument(
                _flag(p.name),
                dest=p.name,
                action=argparse.BooleanOptionalAction,
                default=None,
                help=p.help or None,
            )
        elif p.type == "list[str]":
            sp.add_argument(
                _flag(p.name),
                dest=p.name,
                action="append",
                default=None,
                metavar="VALUE",
                help=(p.help or "") + " (repeatable)",
            )
        else:
            sp.add_argument(
                _flag(p.name),
                dest=p.name,
                default=None,
                required=p.required and not p.cli_positional,
                help=p.help or None,
            )
    if field_escape:
        sp.add_argument(
            "--field",
            action="append",
            default=None,
            metavar="KEY=VALUE",
            help="set any other parameter (repeatable), e.g. --field severity=critical",
        )


def _collect_raw_args(
    cmd, args: argparse.Namespace, parser: argparse.ArgumentParser
) -> dict:
    field_escape = cmd.name in _FIELD_ESCAPE
    raw: dict = {}
    for p in cmd.params:
        if field_escape and not p.required:
            continue
        val = getattr(args, p.name, None)
        if val is not None:
            raw[p.name] = val
    if field_escape:
        for item in getattr(args, "field", None) or []:
            key, sep, value = item.partition("=")
            if not sep:
                # Route through argparse's error path → exit 2, consistent with
                # every other usage error (a bare `raise SystemExit(str)` is exit 1).
                parser.error(f"--field expects KEY=VALUE, got {item!r}")
            raw[key.strip()] = value
    return raw


def _print_human(result) -> None:
    if isinstance(result, list):
        if not result:
            print("(no results)")
            return
        for item in result:
            if isinstance(item, dict) and "path" in item:
                title = item.get("title") or ""
                print(f"{item['path']}  {title}".rstrip())
            else:
                print(json.dumps(item, ensure_ascii=False, default=str))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _core_op_main(argv: list[str]) -> int:
    from . import cli_ops
    from . import commands as commands_module
    from . import schema as schema_module
    from .vault import resolve_vault

    cmds = {
        c.name: c
        for c in commands_module.commands_for("cli", expose_tier2=_expose_tier2())
    }

    parser = _CLIParser(prog="kb", description="Query and write the local Knowledge Base.")
    sub = parser.add_subparsers(dest="op", required=True, parser_class=_CLIParser)
    for name in sorted(cmds):
        cmd = cmds[name]
        summary = (cmd.description or name).strip().splitlines()[0]
        sp = sub.add_parser(name, help=summary, description=summary)
        sp.add_argument(
            "--json",
            action="store_true",
            help="emit the shared {success, data|error} JSON envelope",
        )
        _add_command_args(sp, cmd)

    args = parser.parse_args(argv)
    cmd = cmds[args.op]
    as_json = getattr(args, "json", False)

    try:
        vault_root = resolve_vault()
        raw = _collect_raw_args(cmd, args, parser)
        kwargs = cli_ops.coerce(
            cmd.params, raw, guarded_fields=cmd.guarded_fields, tool=cmd.name, cli=True
        )
        if cmd.needs_schema:
            injected = (vault_root, schema_module.load_source_schema(vault_root))
        else:
            injected = (vault_root,)
        result = cmd.leaf(*injected, **kwargs)
    except (cli_ops.OpError, ValueError, TypeError) as e:
        err = cli_ops.error_dict(e)
        if as_json:
            print(json.dumps(cli_ops.envelope(False, error=err), default=str))
        else:
            print(f"Error [{err['code']}]: {err['message']}", file=sys.stderr)
            if err.get("remediation"):
                print(err["remediation"], file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(cli_ops.envelope(True, data=result), ensure_ascii=False, default=str))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
