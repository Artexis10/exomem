"""`python -m exomem` entry point.

Subcommands:
- (default) serve the MCP server — `python -m exomem [--transport ...]`
- `setup` — guided one-command local onboarding (scan → init → doctor → register → skill)
- `setup --remote` — guided remote-connector onboarding (tunnel → .env + GitHub OAuth
  → `doctor --profile remote --probe` gate → connector URL) for claude.ai / iOS access
- `init` — bootstrap a fresh Knowledge Base into a vault
- `install-skill` — install the Exomem skill into Claude Code
- `personalize` — scan a vault and generate a starter `_access.yaml` (readonly/excluded siblings)
- `install-hook` — wire the KB capture + retrieval hooks into Claude Code or Codex
- `demo` — the packaged 30-second proof: doctor → find → get → audit against a
  bundled sample vault, no clone/config/vault needed (`uvx exomem demo`)
- `studio` — print the local Review Studio URL; `--open` launches it explicitly
- `tui` — launch the interactive terminal UI over the same product commands
  (requires the optional `tui` extra; needs an interactive terminal)
- `doctor` — read-only local install/setup preflight
- `auth sessions|revoke` — operator-only durable MCP session administration
- `governance-schema status|plan-migration|stage-migration|commit-migration|restore-migration-backup|downmigrate` — offline schema control
- `status` — resource posture/residency diagnostics without loading models
- `warm` — pre-download/load the search models (bge, reranker, CLIP) so the first
  server start doesn't pay the download in the background; optional `--vault`
  also warms the lexical caches
- `backfill-media` — make pre-existing Evidence binaries searchable (sidecar + OCR/ASR/PDF + CLIP)
- `index` — build/refresh the semantic (bge) vector index incrementally; `--scope vault`
  (or EXOMEM_INDEX_SCOPE=vault) makes notes OUTSIDE Knowledge Base/ semantically searchable
- `enroll-speaker` / `list-speakers` / `remove-speaker` — manage named-speaker voice profiles
  for opt-in diarization (desk-side admin; never an MCP tool)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from .kbdir import kb_dirname, kb_prefix


def _module_available(name: str) -> bool:
    """Cheaply detect an optional local capability without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _configure_local_search_capabilities(action: str | None) -> tuple[str, ...]:
    """Keep lean direct CLI retrieval on its intentionally installed lanes.

    The managed service can carry model/media extras while PATH-visible uv-tool
    commands stay lean. A local ``ask``/legacy ``find`` must not import missing
    model stacks merely to discover that BM25 is its available backend. Return
    only the fallback variables introduced for this invocation so callers can
    restore the surrounding process environment without touching explicit user
    configuration.
    """
    if action not in {"ask", "ask_memory"}:
        return ()
    model_stack_available = _module_available("torch") and _module_available(
        "sentence_transformers"
    )
    introduced: list[str] = []
    if not model_stack_available:
        for name in (
            "EXOMEM_DISABLE_EMBEDDINGS",
            "EXOMEM_DISABLE_RANKING",
            "EXOMEM_DISABLE_CLIP",
        ):
            if name not in os.environ:
                os.environ[name] = "1"
                introduced.append(name)
    return tuple(introduced)


# Subcommands `_dispatch_main` routes explicitly, before falling through to
# `_serve_main`. Kept alongside `_is_cli_only_invocation` so a one-shot CLI
# command gets its own log file; `serve` configures server-role logging
# itself from `server.run()`, and `hosted` emits a JSON-only operator protocol,
# so they are deliberately excluded here.
_CLI_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "setup",
        "init",
        "reclaim-schema",
        "install-skill",
        "package-skills",
        "personalize",
        "install-hook",
        "demo",
        "studio",
        "tui",
        "doctor",
        "install-info",
        "auth",
        "status",
        "warm",
        "mode",
        "prominence",
        "backfill-media",
        "index",
        "enroll-speaker",
        "list-speakers",
        "remove-speaker",
        "trace",
        "logs",
        "lease",
        "governance-schema",
    }
)


def _is_cli_only_invocation(raw: list[str]) -> bool:
    """Whether `raw` routes to a one-shot CLI command rather than `serve`."""
    if not raw or raw[0].startswith("-"):
        return False
    if raw[0] in _CLI_ONLY_SUBCOMMANDS:
        return True
    if raw[0] in _core_op_names(expose_tier2=True):
        return True
    return raw[0] in _simple_cli_action_names()


def main(argv: list[str] | None = None) -> int:
    # Wrapped so *every* exit drains, including the early returns above
    # `_run_cli`. A graph rebuild no longer blocks the write that caused it, and
    # it runs on a daemon thread. That is right for the long-lived server and
    # wrong here: this process is about to exit and would take the rebuild with
    # it, so a CLI write would report `pending` and nothing would ever make it
    # true. The boundary is process lifetime, not the write path -- and a
    # boundary that only some exits honour is not one.
    try:
        return _run_cli(argv)
    finally:
        from . import graph_sync

        if not graph_sync.drain_active_rebuilds():
            print(
                "exomem: a graph rebuild did not finish before exit; the change is "
                "committed and `exomem reconcile` will bring the graph up to date",
                file=sys.stderr,
            )


def _run_cli(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw in (["--version"], ["--version", "--json"]):
        # Keep this before command-registry and optional capability imports.  A
        # lean uv-tool command must always be able to identify itself and the
        # separately managed service it is paired with.
        from .install_info import print_version

        return print_version(as_json="--json" in raw)
    if raw and raw[0] == "find":
        # `find` was the original friendly retrieval command.  Keep existing
        # scripts useful while the current product language calls it `ask`.
        raw[0] = "ask"
    if _is_cli_only_invocation(raw):
        from .logging_config import configure_logging, resolve_log_dir

        configure_logging(resolve_log_dir(), process="cli")
    introduced = _configure_local_search_capabilities(raw[0] if raw else None)
    try:
        return _dispatch_main(raw)
    finally:
        for name in introduced:
            os.environ.pop(name, None)


def _dispatch_main(raw: list[str]) -> int:
    if raw and raw[0] == "hosted-fingerprint":
        from .hosted_fingerprint import main as hosted_fingerprint_main

        return hosted_fingerprint_main(raw[1:])
    if raw and raw[0] == "hosted":
        from .hosted_operator import main as hosted_operator_main

        return hosted_operator_main(raw[1:])
    if raw and raw[0] == "setup":
        from .setup_wizard import setup_main

        return setup_main(raw[1:])
    if raw and raw[0] == "init":
        return _init_main(raw[1:])
    if raw and raw[0] == "reclaim-schema":
        return _reclaim_schema_main(raw[1:])
    if raw and raw[0] == "install-skill":
        return _install_skill_main(raw[1:])
    if raw and raw[0] == "package-skills":
        return _package_skills_main(raw[1:])
    if raw and raw[0] == "personalize":
        from .personalize import personalize_main

        return personalize_main(raw[1:])
    if raw and raw[0] == "install-hook":
        return _install_hook_main(raw[1:])
    if raw and raw[0] == "demo":
        from .demo import main as demo_main

        return demo_main(raw[1:])
    if raw and raw[0] == "studio":
        return _studio_main(raw[1:])
    if raw and raw[0] == "tui":
        return _tui_main(raw[1:])
    if raw and raw[0] == "doctor":
        return _doctor_main(raw[1:])
    if raw and raw[0] == "install-info":
        return _install_info_main(raw[1:])
    if raw and raw[0] == "auth":
        return _auth_main(raw[1:])
    if raw and raw[0] == "status":
        return _status_main(raw[1:])
    if raw and raw[0] == "warm":
        return _warm_main(raw[1:])
    if raw and raw[0] == "mode":
        return _mode_main(raw[1:])
    if raw and raw[0] == "prominence":
        return _prominence_main(raw[1:])
    if raw and raw[0] == "backfill-media":
        return _backfill_media_main(raw[1:])
    if raw and raw[0] == "index":
        return _index_main(raw[1:])
    if raw and raw[0] == "enroll-speaker":
        return _enroll_speaker_main(raw[1:])
    if raw and raw[0] == "list-speakers":
        return _list_speakers_main(raw[1:])
    if raw and raw[0] == "remove-speaker":
        return _remove_speaker_main(raw[1:])
    if raw and raw[0] == "trace":
        return _trace_main(raw[1:])
    if raw and raw[0] == "logs":
        return _logs_main(raw[1:])
    if raw and raw[0] == "lease":
        return _lease_main(raw[1:])
    if raw and raw[0] == "governance-schema":
        return _governance_schema_main(raw[1:])
    # Registry-driven product operations (reads + writes): `exomem ask_memory "..."`,
    # `exomem remember ...`, etc. Product commands take precedence over old
    # short aliases when a name overlaps.
    if raw and not raw[0].startswith("-") and raw[0] in _core_op_names():
        return _core_op_main(raw)
    if raw and not raw[0].startswith("-") and raw[0] in _simple_cli_action_names():
        return _simple_action_main(raw)
    # A real tier-2 op invoked while EXOMEM_DISABLE_TIER2 is set would otherwise fall
    # through to the serve parser and emit a confusing argparse error — name it instead.
    if (
        raw
        and not raw[0].startswith("-")
        and not _expose_tier2()
        and raw[0] in _core_op_names(expose_tier2=True)
    ):
        print(
            f"Error [UNAVAILABLE]: operation {raw[0]!r} is unavailable (tier-2 disabled)",
            file=sys.stderr,
        )
        return 2
    return _serve_main(raw)


def _build_auth_session_authority():
    """Load operator configuration and reuse the HTTP auth authority factory.

    Keeping this adapter tiny makes the CLI and HTTP paths share the exact same
    issuer, audience, storage namespace, and local-vs-HA selection without
    importing server auth during unrelated CLI startup.
    """
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=True)
    from . import env_compat

    env_compat.promote_legacy()
    base_url = os.environ.get("EXOMEM_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ValueError("EXOMEM_BASE_URL is required for session administration")
    from .server_auth import build_session_authority

    return build_session_authority(base_url=base_url)


def _session_metadata(record, *, current_generation: str) -> dict[str, object]:
    effective_status = record.status
    if effective_status == "active" and record.generation != current_generation:
        effective_status = "generation_revoked"
    return {
        "session_id": record.session_id,
        "client_id": record.client_id,
        "scopes": list(record.scopes),
        "github_login": record.github_login,
        "github_user_id": record.github_user_id,
        "issued_at": record.issued_at,
        "status": effective_status,
    }


def _auth_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem auth",
        description="Inspect and revoke durable MCP sessions (operator-only).",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    sessions = subcommands.add_parser("sessions", help="list non-secret session metadata")
    sessions.add_argument("--json", action="store_true", help="emit stable JSON")

    revoke = subcommands.add_parser("revoke", help="revoke one session or every session")
    revoke.add_argument("session_id", nargs="?", help="opaque session ID (not the bearer)")
    revoke.add_argument("--all", action="store_true", dest="revoke_all")
    revoke.add_argument("--reason", default=None, help="operator audit reason")
    revoke.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    if args.command == "revoke":
        if bool(args.session_id) == bool(args.revoke_all):
            parser.error("revoke requires exactly one session ID or --all")
        if args.reason is not None and not args.reason.strip():
            parser.error("--reason must not be empty")

    from .auth_sessions import SessionStoreUnavailable

    try:
        authority = _build_auth_session_authority()
    except (SessionStoreUnavailable, OSError):
        print(
            "session authority unavailable; check storage configuration and connectivity",
            file=sys.stderr,
        )
        return 1
    except (ValueError, RuntimeError) as error:
        print(f"auth configuration error: {error}", file=sys.stderr)
        return 2

    async def run() -> dict[str, object]:
        if args.command == "sessions":
            records = await authority.list_sessions()
            current_generation = await authority.current_generation()
            return {
                "sessions": [
                    _session_metadata(record, current_generation=current_generation)
                    for record in records
                ]
            }
        reason = (
            args.reason or ("operator-revoke-all" if args.revoke_all else "operator-revocation")
        ).strip()
        if args.revoke_all:
            await authority.replace_generation()
            return {"revoked_all": True}
        revoked = await authority.tombstone(args.session_id, reason=reason)
        return {"revoked": revoked, "session_id": args.session_id}

    try:
        result = asyncio.run(run())
    except SessionStoreUnavailable:
        print(
            "session authority unavailable; check storage configuration and connectivity",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "sessions":
        rows = result["sessions"]
        if not rows:
            print("No durable MCP sessions.")
        else:
            for row in rows:
                print(
                    f"{row['session_id']}  {row['status']}  {row['client_id']}  "
                    f"{row['github_login']}  {row['issued_at']}"
                )
    elif result.get("revoked_all"):
        print("Revoked all durable MCP sessions.")
    elif result.get("revoked"):
        print(f"Revoked session {result['session_id']}.")
    else:
        print(f"Session {result['session_id']} was not found.")
    return 0


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

    from . import server

    try:
        server.run(transport=args.transport, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 — top-level CLI guard: report and exit non-zero
        print(f"exomem failed: {e}", file=sys.stderr)
        return 1
    return 0


def _studio_main(argv: list[str]) -> int:
    """Print the Review Studio URL and open it only when explicitly requested."""
    import webbrowser
    from urllib.parse import urlsplit, urlunsplit

    parser = argparse.ArgumentParser(
        prog="exomem studio",
        description="Show the packaged Epistemic Review Studio entry URL.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("EXOMEM_BASE_URL", "http://127.0.0.1:8765"),
        help="Exomem service base URL (default: $EXOMEM_BASE_URL or http://127.0.0.1:8765)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the Studio in the system browser (never done by default)",
    )
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/studio"}
    ):
        parser.error("--url must be an http(s) origin or existing /studio/ URL without credentials")
    studio_url = urlunsplit((parsed.scheme, parsed.netloc, "/studio/", "", ""))
    print(studio_url)
    if args.open and not webbrowser.open(studio_url):
        print("Could not open the system browser; use the URL above.", file=sys.stderr)
        return 1
    return 0


def _tui_stdio_is_tty() -> bool:
    """Whether stdin AND stdout are interactive — the TUI needs both."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _tui_main(argv: list[str]) -> int:
    """`exomem tui` — the interactive terminal UI over the product commands.

    Guards run before any TUI import so a piped invocation or a lean install
    fails in milliseconds with one actionable line, never a traceback.
    """
    parser = argparse.ArgumentParser(
        prog="exomem tui",
        description=(
            "Launch the interactive terminal UI: capture, ask, review, adopt, "
            "packs, status, and settings over the same product commands as the "
            "CLI and MCP surfaces. Requires the optional `tui` extra."
        ),
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="vault root override for this session (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument(
        "--no-mouse",
        action="store_true",
        help=(
            "do not capture the mouse, so the terminal keeps its own click-drag "
            "selection (otherwise selecting text needs shift held down)"
        ),
    )
    args = parser.parse_args(argv)

    if not _tui_stdio_is_tty():
        print(
            "exomem tui needs an interactive terminal (stdin/stdout is not a TTY).",
            file=sys.stderr,
        )
        return 2
    if not _module_available("textual"):
        print(
            "exomem tui needs the optional TUI stack: run `uv sync --extra tui` "
            "(source checkout) or `pip install 'exomem[tui]'`, then retry.",
            file=sys.stderr,
        )
        return 1

    from . import tui as tui_package

    vault = str(Path(args.vault).expanduser()) if args.vault else None
    return tui_package.run(vault=vault, mouse=not args.no_mouse)


def _backfill_media_main(argv: list[str]) -> int:
    import logging

    parser = argparse.ArgumentParser(
        prog="exomem backfill-media",
        description="Make pre-existing Evidence binaries searchable: write a sidecar if "
        "missing, extract text (OCR/ASR/PDF), and CLIP-embed images. Idempotent; CPU or GPU.",
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change; write nothing",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="skip text extraction (sidecar + CLIP only)",
    )
    parser.add_argument("--no-clip", action="store_true", help="skip CLIP image embedding")
    parser.add_argument(
        "--rediarize",
        action="store_true",
        help="re-extract audio/video transcribed before diarization (extracted_by without "
        "'+diarized') so they gain speaker turns + a speakers: frontmatter list. Requires "
        "EXOMEM_DIARIZE set in this shell (the CLI does not read .env).",
    )
    parser.add_argument(
        "--retime",
        action="store_true",
        help="re-extract audio/video transcribed before timed transcripts (extracted_by "
        "without '+timed') so they gain per-segment [m:ss] lines — the substrate for "
        "semantic segments. Requires EXOMEM_SEMANTIC_SEGMENTS set in this shell (the CLI "
        "does not read .env). One re-extraction serves --retime and --rediarize together. "
        "Already-diarized recordings are SKIPPED unless EXOMEM_DIARIZE is also set, so "
        "re-timing never drops their speaker labels.",
    )
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
        rediarize=args.rediarize,
        retime=args.retime,
        dry_run=args.dry_run,
        log_fn=print,
    )
    return 0


def _index_main(argv: list[str]) -> int:
    import logging

    parser = argparse.ArgumentParser(
        prog="exomem index",
        description="Build/refresh the semantic (bge) vector index INCREMENTALLY: "
        "skip files already up to date, embed new/changed ones in batches, prune "
        "rows for files that are gone. Idempotent; unlike a full audit_fix rebuild "
        f"it never wipes the sidecar first. Covers {kb_prefix()} by default, or "
        f"the whole vault with --scope vault (so notes outside {kb_prefix()} "
        "become semantically searchable).",
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument(
        "--scope",
        choices=("kb", "vault"),
        default=None,
        help="index scope override; default reads EXOMEM_INDEX_SCOPE (else 'kb'). "
        f"'vault' indexes the whole vault, not just {kb_prefix()}.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="chunks per embedding batch (default: 256)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "gpu"),
        default=None,
        help="embedding device for this run. Default: GPU when a capable one is present "
        "and the mode isn't 'quiet' (this is a short-lived process, so it frees the CUDA "
        "context on exit — safe even on a CPU-default server). 'cpu' forces CPU; 'gpu'/"
        "'auto' opt in with the marginal-VRAM guard.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be (re)embedded and pruned; write nothing",
    )
    args = parser.parse_args(argv)
    if not args.vault:
        print("index: set --vault or EXOMEM_VAULT_PATH", file=sys.stderr)
        return 2
    # Scope override flows through the env var the whole stack reads, so a single
    # source of truth governs the walk, the drift check, and freshness.
    if args.scope:
        os.environ["EXOMEM_INDEX_SCOPE"] = args.scope

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from . import accel, embeddings

    # A real run needs the model; --dry-run only walks + reads the sidecar, so it
    # stays fast and works in stripped/embeddings-disabled environments.
    if not args.dry_run:
        if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
            print(
                "index: EXOMEM_DISABLE_EMBEDDINGS is set; nothing to embed",
                file=sys.stderr,
            )
            return 2
        # Pick the embedding device for THIS one-shot process (accel.bulk_device):
        # explicit --device wins; else GPU when capable and not quiet. A short-lived
        # CLI process frees the CUDA context on exit, so GPU here is safe even on a
        # normal-mode (CPU-default) server — this is how onboarding gets GPU speed
        # without leaving a resident context floor.
        bulk = accel.bulk_device(args.device)
        if bulk:
            os.environ["EXOMEM_EMBED_DEVICE"] = bulk
        logging.getLogger(__name__).info(
            "index: embedding on %s", accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
        )
        try:
            embeddings.get_model()
        except Exception as e:  # noqa: BLE001 — surface a clean CLI error
            print(
                f"index: embedding model unavailable ({e}); "
                "install the 'embeddings' extra (uv sync --extra embeddings)",
                file=sys.stderr,
            )
            return 1

    vault_root = Path(args.vault).expanduser()
    stats = embeddings.index_incremental(
        vault_root,
        batch_size=max(1, args.batch_size),
        dry_run=args.dry_run,
        log_fn=print,
    )
    if not args.dry_run:
        from . import index_sync

        index_sync.drain_deferred_work(vault_root)
        index_sync.clear_deferred_work(vault_root, paths=[], include_full=True)
    print(json.dumps(stats))
    return 0


def _prominence_main(argv: list[str]) -> int:
    """`exomem prominence [off|light|balanced|maximal]` — how much Exomem speaks up.

    Orthogonal to `exomem mode`, which governs machine footprint. Shares the same
    config file so one write can never drop the other's setting. Torch-free, so it
    stays instant.
    """
    from . import prominence as prom_mod

    parser = argparse.ArgumentParser(
        prog="exomem prominence",
        description="Show or set how much Exomem participates in a conversation: "
        "off (explicit invocation only) | light (recall when asked, capture on "
        "request) | balanced (recall on topic match, capture durable conclusions) | "
        "maximal (recall before every substantive turn, capture every stepping "
        "stone, and say so). Persisted to the same config file as `exomem mode`, "
        "read by BOTH the server and the CLI.",
    )
    parser.add_argument(
        "level",
        nargs="?",
        choices=tuple(prom_mod.CANON) + tuple(prom_mod._ALIASES),
        help="level to set; omit to show the current one",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON (status only)")
    parser.add_argument(
        "--hook-env",
        action="store_true",
        help="print the hook tunables this level implies, as shell exports",
    )
    args = parser.parse_args(argv)

    if args.level is None:
        policy = prom_mod.resolved()
        policy["config_path"] = str(mode_config_path())
        if args.json:
            print(json.dumps(policy))
        elif args.hook_env:
            for key, value in prom_mod.hook_env().items():
                print(f"unset {key}" if value == "" else f"export {key}={value}")
        else:
            contract = policy["contract"]
            print(f"prominence: {policy['level']}  (source: {policy['source']})")
            print(f"  {contract['summary']}")
            print(f"  recall:    {contract['recall']}")
            print(f"  capture:   {contract['capture']}")
            print(f"  narration: {contract['narration']}")
            print(f"  config: {policy['config_path']}")
            print(f"  levels: {', '.join(policy['levels'])}")
        return 0

    try:
        path = prom_mod.write_prominence(args.level)
    except ValueError as e:
        print(f"prominence: {e}", file=sys.stderr)
        return 2
    canonical = prom_mod.normalize(args.level)
    print(f"Prominence set to '{canonical}'  ({path})")
    print(f"  {prom_mod.CONTRACTS[canonical].summary}")
    print("A running exomem server serves it to new sessions via bootstrap().")
    print("Reinstall hooks (exomem install-hook) to apply the matching nudge cadence.")
    return 0


def mode_config_path():
    """Shared config path for the `mode`/`prominence` pair (kept torch-free)."""
    from . import mode as mode_mod

    return mode_mod.config_path()


def _mode_main(argv: list[str]) -> int:
    """`exomem mode [quiet|normal|performance]` — show or set the per-machine compute mode.

    Torch-free (no vault, no model import) so it stays instant. Writing persists to
    ~/.exomem/config.json, which the running server picks up live within ~10s and CLI
    ops read on their next run.
    """
    from . import mode as mode_mod

    parser = argparse.ArgumentParser(
        prog="exomem mode",
        description="Show or set the compute mode: quiet (CPU, low footprint) | normal "
        "(default, CPU steady-state) | performance (use the GPU; aliases gpu/turbo). "
        "Low-resource aliases resource-saver/low-resource map to quiet. Persisted to "
        "~/.exomem/config.json, read by BOTH the server and CLI ops.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=(
            "quiet",
            "normal",
            "performance",
            "gpu",
            "turbo",
            "resource-saver",
            "low-resource",
        ),
        help="mode to set; omit to show the current one",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON (status only)")
    args = parser.parse_args(argv)

    if args.mode is None:
        source = (
            "env"
            if os.environ.get("EXOMEM_MODE")
            else "config"
            if mode_mod.read_config().get("mode")
            else "quiet-alias"
            if os.environ.get("EXOMEM_QUIET_MODE")
            else "default"
        )
        policy = mode_mod.resolved()
        policy["source"] = source
        policy["config_path"] = str(mode_mod.config_path())
        if args.json:
            print(json.dumps(policy))
        else:
            print(f"mode: {policy['mode']}  (source: {source})")
            print(f"  preload_models:          {policy['preload_models']}")
            print(f"  preload_cpu_caches:      {policy['preload_cpu_caches']}")
            print(f"  retain_cpu_caches:       {policy['retain_cpu_caches']}")
            print(f"  defer_expensive_indexes: {policy['defer_expensive_indexes']}")
            print(f"  release_when_idle:       {policy['release_when_idle']}")
            print(f"  reap_models_when_idle:   {policy['reap_models_when_idle']}")
            print(f"  bulk_gpu:                {policy['bulk_gpu']}")
            print(f"  config: {policy['config_path']}")
        return 0

    try:
        path = mode_mod.write_mode(args.mode)
    except ValueError as e:
        print(f"mode: {e}", file=sys.stderr)
        return 2
    except OSError:
        print(
            f"mode: could not persist {mode_mod.config_path()}; grant Modify permission "
            "to the invoking account or change EXOMEM_CONFIG_PATH",
            file=sys.stderr,
        )
        return 1
    requested = mode_mod.normalize(args.mode)
    persisted = mode_mod.normalize(mode_mod.read_config().get("mode"))
    if persisted != requested:
        print(
            f"mode: {path} read back {persisted!r} instead of {requested!r}; "
            "stop concurrent config writers, verify the path permissions, and retry",
            file=sys.stderr,
        )
        return 1
    print(f"Compute mode set to '{persisted}'  ({path})")
    print("A running exomem server applies it live within ~10s (or restart to apply now).")
    print("CLI ops (exomem index / warm) use it on their next run.")
    return 0


def _status_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem status",
        description="Report resource mode, residency, deferred work, and CUDA accounting.",
    )
    parser.add_argument(
        "--resources",
        action="store_true",
        help="report resource posture and residency (default)",
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import resource_status

    vault_root = Path(args.vault).expanduser() if args.vault else None
    status = resource_status.collect(vault_root)
    if args.json:
        print(json.dumps(status))
    else:
        print(f"mode: {status['mode']}  (source: {status['source']})")
        print(f"  config: {status['config_path']}")
        print(f"  models: {status['models']}")
        print(f"  media: {status['media']}")
        print(f"  deferred_work: {status['deferred_work']}")
        print(f"  cuda: {status['cuda']}")
    return 0


def _install_info_main(argv: list[str]) -> int:
    """Report where this install came from.

    Answers "what version is deployed, and from which environment" without
    inspecting service-manager config. Unlike the `/health` route, this runs
    locally and so may include the interpreter path — the detail that identifies
    the real deploy target when a service venv sits apart from the checkout.

    Named `install-info` rather than `provenance` deliberately: in this codebase
    provenance already means note/source provenance (see `provenance.py`), and
    reusing the word for install origin would be genuinely ambiguous.
    """
    parser = argparse.ArgumentParser(
        prog="exomem install-info",
        description="Report install origin: version, source, revision, torch build, extras.",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import install_info

    report = install_info.report()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
        return 0

    print(f"version:          {report['version']}")
    print(f"install source:   {report['install_source']}")
    print(f"interpreter:      {report['python_executable']}")
    print(f"local profile:    {report['local_profile']}")
    print(f"effective route:  {report['effective_route']}")
    if report["managed_service_version"]:
        print(f"service version:  {report['managed_service_version']}")
        print(f"service profile:  {report['managed_service_profile']}")
        print(f"version match:    {str(report['version_match']).lower()}")
    else:
        print(f"managed manifest: {report['manifest_status']}")
    return 0


def _doctor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem doctor",
        description="Read-only local setup preflight for exomem installs.",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
    )
    parser.add_argument(
        "--profile",
        choices=("lean", "hybrid", "standard", "media", "remote", "ha"),
        default=None,
        help="capability profile to validate (default: infer from EXOMEM_PROFILE, else lean)",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="with --profile remote or ha: also verify live endpoints using read-only GETs. "
        "Default is fully offline.",
    )
    parser.add_argument(
        "--replica-url",
        action="append",
        default=None,
        help="with --profile ha --probe: replica origin to inspect; repeat once per replica",
    )
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=True)
    from . import env_compat

    env_compat.promote_legacy()
    from . import doctor as doctor_module

    report = doctor_module.doctor(
        vault=args.vault,
        profile=args.profile,
        probe=args.probe,
        replica_urls=args.replica_url,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, default=str))
    else:
        print(doctor_module.render_human(report))
        # GPU-discoverability: use a non-torch probe so doctor does not create
        # a CUDA context or import model dependencies just to print guidance.
        from . import mode as mode_mod
        from . import resource_status

        gpu = resource_status.gpu_headroom()
        if mode_mod.resolve_mode() != "performance" and gpu.get("usable") is True:
            print(
                "\nA capable idle GPU was detected. Normal mode still avoids "
                "steady-state CUDA residency. For faster explicit indexing, run:\n"
                "    exomem mode performance"
            )
        print(
            "\nResource controls: exomem mode quiet | normal | performance; "
            "inspect with exomem status --resources --json."
        )
    return 0 if report.success else 1


def _warm_main(argv: list[str]) -> int:
    """`exomem warm` — pre-download/load the search models on the user's terms.

    The server warms these in the background by default; this command exists
    so a user (or a deploy/provisioning script) can pay the GB-scale first
    download explicitly, with HF progress bars on the TTY, instead of having
    the first server start do it silently behind lexical-only results.
    """
    import logging
    import time

    parser = argparse.ArgumentParser(
        prog="exomem warm",
        description=(
            "Pre-download and load the search models (bge embedder, reranker, "
            "CLIP when enabled) into the local Hugging Face cache. Optional "
            "--vault also warms that vault's lexical caches. Run once after "
            "install; every later server start then warms from disk in seconds."
        ),
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="also warm this vault's lexical caches (default: models only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        print(
            "warm: EXOMEM_DISABLE_EMBEDDINGS is set — this install runs lexical-only, "
            "so there are no models to warm. Unset it (and `uv sync --extra embeddings`) "
            "for hybrid search."
        )
        return 0

    from . import embedding_backend, embeddings

    # Which models this install can actually load. The reranker and CLIP are
    # sentence-transformers models, so the `embeddings-onnx` lane withholds both
    # by design. Reporting their absence as a warm failure means a correct ONNX
    # install exits 1 on a successful run, and calls itself "lean" while holding
    # a working embedder.
    try:
        backend = embedding_backend.resolve_backend()
    except ValueError:
        backend = embedding_backend.TORCH
    lane_serves_torch_models = backend != embedding_backend.ONNX

    failed = False
    missing_extra = False
    optional_unavailable: list[str] = []

    def _step(label: str, fn, *, required: bool = True) -> None:
        nonlocal failed, missing_extra
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  {label}: ready ({time.perf_counter() - t0:.1f}s)")
        except ImportError as e:
            # A lean install has no ML stack — that's a missing extra, not a
            # download problem, and the remediation must say so. On a lane that
            # never carries this model, the same ImportError is the expected
            # state and must not fail the run.
            if not required:
                optional_unavailable.append(label)
                print(f"  {label}: unavailable on the {backend} lane (skipped)")
                return
            failed = True
            missing_extra = True
            print(f"  {label}: FAILED ({e})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — report every model, then exit non-zero
            failed = True
            print(f"  {label}: FAILED ({e})", file=sys.stderr)

    print("exomem warm — downloading/loading search models (first run can take minutes)")
    _step(f"embedding model {embeddings.MODEL_NAME}", embeddings.get_model)
    _step(
        f"reranker {embeddings.RERANKER_NAME}",
        embeddings.get_reranker,
        required=lane_serves_torch_models,
    )
    if embeddings.clip_enabled():
        _step(
            f"CLIP model {embeddings.CLIP_MODEL_NAME}",
            embeddings.get_clip_model,
            required=lane_serves_torch_models,
        )
    else:
        print("  CLIP: skipped (EXOMEM_DISABLE_CLIP)")

    if args.vault:
        from . import warmup

        t0 = time.perf_counter()
        warmup.warm_caches(Path(args.vault).expanduser())
        print(f"  lexical caches: warmed ({time.perf_counter() - t0:.1f}s)")

    if failed:
        if missing_extra:
            print(
                "warm: the ML stack is not installed — this is a lean install. "
                "Add it with `uv sync --extra embeddings` (source checkout) or "
                "`pip install 'exomem[embeddings]'`, then re-run `exomem warm`.",
                file=sys.stderr,
            )
            return 1
        print("warm: one or more models failed — check network/proxy and retry.", file=sys.stderr)
        return 1
    if optional_unavailable:
        print(
            f"warm: done on the {backend} lane. Not carried by this lane: "
            f"{', '.join(optional_unavailable)} — that is the documented "
            "`embeddings-onnx` trade-off, not a failure. Install the `embeddings` "
            "extra if you need reranking or image search."
        )
        return 0
    print("warm: done. Server starts will now warm from disk in seconds.")
    return 0


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
            "Example: exomem enroll-speaker --name Alice --self alice-sample.wav"
        ),
    )
    parser.add_argument("audio", help="path to an audio sample of the speaker's voice")
    parser.add_argument("--name", required=True, help="speaker name to attach to matched clusters")
    parser.add_argument(
        "--self",
        dest="is_self",
        action="store_true",
        help="mark this profile as the vault owner's own voice (is_self).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="per-profile cosine match threshold (default 0.40). Raise for confusable voices.",
    )
    parser.add_argument(
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
    )
    args = parser.parse_args(argv)

    from . import enroll_speaker as enroll_module
    from .voice_profiles import DEFAULT_THRESHOLD

    try:
        rec = enroll_module.enroll_speaker(
            args.audio,
            args.name,
            is_self=args.is_self,
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
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
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
        "--vault",
        default=os.environ.get("EXOMEM_VAULT_PATH"),
        help=f"vault root containing '{kb_prefix()}' (default: $EXOMEM_VAULT_PATH)",
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


def _trace_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem trace",
        description=(
            "Join the server log, the call ledger, queries/writes/reads.jsonl, "
            "and mutations.jsonl for one request id into a single time-ordered "
            "report."
        ),
    )
    parser.add_argument("request_id", help="the x-exomem-request-id to trace")
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import obs_cli

    records = obs_cli.trace(args.request_id)
    if args.json:
        print(json.dumps({"request_id": args.request_id, "records": records}, ensure_ascii=False))
        return 0
    if not records:
        print(f"No records found for request id {args.request_id!r}.")
        return 0
    for record in records:
        ts = record.get("ts_utc") or record.get("ts") or "?"
        source = record.get("_source", "?")
        event = record.get("event") or record.get("tool") or record.get("outcome") or ""
        print(f"[{ts}] ({source}) {event} {json.dumps(record, ensure_ascii=False)}")
    return 0


def _logs_main(argv: list[str]) -> int:
    from . import obs_cli

    # One list, so the CLI's `choices` cannot drift from what
    # `resolve_log_file` accepts.
    choices = obs_cli.file_aliases()
    parser = argparse.ArgumentParser(
        prog="exomem logs",
        description="Tail, grep, or verify the per-process JSONL log files.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    tail = subcommands.add_parser("tail", help="print the last N lines of a log file")
    tail.add_argument("--file", required=True, choices=choices)
    tail.add_argument("-n", "--lines", type=int, default=20)
    tail.add_argument("-f", "--follow", action="store_true", help="keep following new lines")

    grep = subcommands.add_parser("grep", help="print lines matching a pattern")
    grep.add_argument("--file", required=True, choices=choices)
    grep.add_argument("pattern", help="regular expression to match")

    subcommands.add_parser(
        "verify",
        help="check the call ledger's hash chain for dropped, reordered, or edited rows",
    )

    args = parser.parse_args(argv)

    if args.command == "verify":
        problems = obs_cli.verify_ledger()
        if not problems:
            print("call ledger intact: no dropped, reordered, or edited rows.")
            return 0
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    path = obs_cli.resolve_log_file(args.file)
    if args.command == "tail":
        for line in obs_cli.tail_lines(path, args.lines):
            print(line)
        if args.follow:
            try:
                for line in obs_cli.follow_lines(path):
                    print(line)
            except KeyboardInterrupt:
                pass
        return 0
    for line in obs_cli.grep_lines(path, args.pattern):
        print(line)
    return 0


def _lease_print_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(message, file=sys.stderr)


def _lease_status_main(config, *, as_json: bool) -> int:  # noqa: ANN001
    from . import writer_lease

    manager = writer_lease.LeaseManager(config)
    status = manager.status()
    if as_json:
        print(json.dumps(status, default=str))
        return 0
    print(f"role: {status['role']}")
    print(f"replica_id: {status['replica_id']}")
    print(f"holder: {status['holder']}")
    print(f"fencing_token: {status['fencing_token']}")
    print(f"coordinator_healthy: {status['coordinator_healthy']}")
    print(f"ttl_remaining_seconds: {status['ttl_remaining_seconds']}")
    print(f"renewer_alive: {status['renewer_alive']}")
    if status.get("last_coordinator_error"):
        print(f"last_coordinator_error: {status['last_coordinator_error']}")
    idempotency = status.get("idempotency") or {}
    print(
        "idempotency: "
        f"pending={idempotency.get('pending')} "
        f"abandoned={idempotency.get('abandoned')} "
        f"oldest_pending_age_seconds={idempotency.get('oldest_pending_age_seconds')}"
    )
    return 0


def _lease_release_main(config, *, confirmed: bool, as_json: bool) -> int:  # noqa: ANN001
    from . import writer_lease

    client = writer_lease.LeaseCoordinatorClient(config)
    try:
        record = client.status()
    except writer_lease.OpError as error:
        _lease_print_error(f"{error.code}: {error.message}", as_json=as_json)
        return 1

    if record.holder is None:
        if as_json:
            print(json.dumps({"released": False, "reason": "unheld"}))
        else:
            print("no lease is currently held; nothing to release.")
        return 0

    if not confirmed:
        if as_json:
            print(
                json.dumps(
                    {
                        "released": False,
                        "reason": "confirmation_required",
                        "holder": record.holder,
                        "fencing_token": record.fencing_token,
                        "expires_at": record.expires_at,
                    }
                )
            )
        else:
            print(
                f"lease is held by {record.holder!r} (fencing_token={record.fencing_token}, "
                f"expires_at={record.expires_at}); pass --yes to confirm release.",
                file=sys.stderr,
            )
        return 2

    try:
        result = client.release_holder(record.holder, record.fencing_token)
    except writer_lease.OpError as error:
        _lease_print_error(f"{error.code}: {error.message}", as_json=as_json)
        return 1

    if as_json:
        print(json.dumps({"released": bool(result.granted), "previous_holder": record.holder}))
        return 0
    if result.granted:
        print(f"released the lease previously held by {record.holder!r}.")
    else:
        print(
            "release did not take effect (the lease changed hands concurrently); "
            "rerun 'exomem lease status'."
        )
    return 0


def _lease_schema_admission_main(
    config, *, schema_version: int, as_json: bool  # noqa: ANN001
) -> int:
    from . import writer_lease

    operator_token = os.environ.get("EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN", "").strip()
    if not operator_token:
        _lease_print_error(
            "schema admission requires EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN.",
            as_json=as_json,
        )
        return 1
    try:
        admission = writer_lease.LeaseCoordinatorClient(
            replace(config, token=operator_token)
        ).schema_admission(schema_version)
    except writer_lease.OpError as error:
        _lease_print_error(f"{error.code}: {error.message}", as_json=as_json)
        return 1
    payload = admission.as_dict()
    if as_json:
        print(json.dumps(payload))
    elif admission.admitted:
        print(
            f"schema {schema_version} is admitted by external fence generation "
            f"{admission.schema_fence_generation}."
        )
    else:
        required = admission.required_schema_version
        print(
            f"schema {schema_version} is refused; external fence requires "
            f"{required if required is not None else 'an enrolled schema'}.",
            file=sys.stderr,
        )
    return 0 if admission.admitted else 1


def _lease_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem lease",
        description=(
            "Ops-only writer-lease inspection, schema admission, and manual release. "
            "Not an MCP or REST product command; 'steal'/'force-acquire' are deliberately "
            "absent — release plus preferred-writer reclaim already hands over within "
            "roughly one lease TTL."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status_parser = subcommands.add_parser(
        "status", help="report coordinator status, local boundary, and idempotency counts"
    )
    status_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    release_parser = subcommands.add_parser(
        "release", help="release the currently held writer lease"
    )
    release_parser.add_argument(
        "--yes", action="store_true", help="confirm the release without prompting"
    )
    release_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    admission_parser = subcommands.add_parser(
        "schema-admission",
        help="fail unless a release schema may join the externally fenced cell",
    )
    admission_parser.add_argument(
        "--schema-version",
        type=int,
        choices=(3, 4),
        required=True,
        help="schema contract declared by the release being admitted",
    )
    admission_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    args = parser.parse_args(argv)

    from . import writer_lease

    config = writer_lease.LeaseConfig.from_env()
    if not config.enabled:
        _lease_print_error(
            "writer-lease coordination is not configured (EXOMEM_WRITER_LEASE_URL unset).",
            as_json=args.json,
        )
        return 1

    if args.command == "status":
        return _lease_status_main(config, as_json=args.json)
    if args.command == "schema-admission":
        return _lease_schema_admission_main(
            config,
            schema_version=args.schema_version,
            as_json=args.json,
        )
    return _lease_release_main(config, confirmed=args.yes, as_json=args.json)


def _governance_schema_print_error(
    code: str,
    message: str,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(json.dumps({"error": code, "message": message}))
    else:
        print(f"{code}: {message}", file=sys.stderr)


def _governance_schema_status(vault: Path, *, now: int) -> dict[str, object]:
    from .governance import authorization_custody, store

    custody = authorization_custody.load_authorization_custody(vault, now=now)
    control = custody.control
    membership = custody.serving_membership
    replicas = [] if membership is None else [
        {
            "replica_id": item.replica_id,
            "state": item.state,
            "schema_version": item.schema_version,
            "issuance_stopped": item.issuance_stopped,
            "no_in_flight": item.no_in_flight,
        }
        for item in membership.replicas
    ]
    return {
        "schema_version": store.authorization_session_schema_version(vault),
        "governance_enrolled": control.governance_enrolled,
        "logical_vault_id": control.logical_vault_id,
        "activation_store_id": control.activation_store_id,
        "activation_epoch": control.activation_epoch,
        "activation_state_digest": control.activation_state_digest,
        "serving_membership_epoch": control.serving_membership_epoch,
        "replicas": replicas,
    }


def _governance_schema_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem governance-schema",
        description=(
            "Ops-only governance schema inspection, planning, and explicit rollback. "
            "This command is not exposed through MCP, REST, or Hosted agent surfaces."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status_parser = subcommands.add_parser(
        "status",
        help="show the content-free schema, activation, and drain basis",
    )
    status_parser.add_argument("--vault", required=True, help="explicit absolute vault root")
    status_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    plan_parser = subcommands.add_parser(
        "plan-migration",
        help="stage and review an inert exact-v3 to exact-v4 migration target",
    )
    plan_parser.add_argument("--vault", required=True, help="explicit absolute vault root")
    plan_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    stage_parser = subcommands.add_parser(
        "stage-migration",
        help="publish the exact reviewed immutable namespace without activating it",
    )
    stage_parser.add_argument("--vault", required=True, help="explicit absolute vault root")
    stage_parser.add_argument(
        "--expected-plan-digest",
        required=True,
        help="64-character digest copied from the reviewed migration plan",
    )
    stage_parser.add_argument(
        "--yes", action="store_true", help="confirm the reviewed inert publication"
    )
    stage_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    commit_parser = subcommands.add_parser(
        "commit-migration",
        help="back up exact v3 and commit the reviewed staged v4 target",
    )
    commit_parser.add_argument(
        "--vault", required=True, help="explicit absolute vault root"
    )
    commit_parser.add_argument(
        "--expected-plan-digest",
        required=True,
        help="64-character digest copied from the reviewed migration plan",
    )
    commit_parser.add_argument(
        "--yes", action="store_true", help="confirm the irreversible reviewed cutover"
    )
    commit_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    restore_parser = subcommands.add_parser(
        "restore-migration-backup",
        help="restore the exact immediate pre-migration v3 backup under a drained fence",
    )
    restore_parser.add_argument(
        "--vault", required=True, help="explicit absolute vault root"
    )
    restore_parser.add_argument(
        "--expected-plan-digest",
        required=True,
        help="64-character digest copied from the committed migration terminal",
    )
    restore_parser.add_argument(
        "--expected-backup-reference",
        required=True,
        help="private backup reference copied from the committed migration terminal",
    )
    restore_parser.add_argument(
        "--yes", action="store_true", help="confirm the reviewed predecessor restore"
    )
    restore_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    downmigrate_parser = subcommands.add_parser(
        "downmigrate",
        help="restore exact schema v3 after every v4 replica is drained",
    )
    downmigrate_parser.add_argument(
        "--vault", required=True, help="explicit absolute vault root"
    )
    downmigrate_parser.add_argument(
        "--expected-activation-state-digest",
        required=True,
        help="64-character digest copied from the reviewed status output",
    )
    downmigrate_parser.add_argument(
        "--yes", action="store_true", help="confirm the reviewed destructive rollback"
    )
    downmigrate_parser.add_argument("--json", action="store_true", help="emit stable JSON")

    args = parser.parse_args(argv)
    vault = Path(args.vault).expanduser()
    as_json = bool(args.json)
    if not vault.is_absolute() or not vault.is_dir():
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_VAULT_INVALID",
            "--vault must name an existing absolute vault root",
            as_json=as_json,
        )
        return 1

    from .governance import authorization_custody, schema_downmigration, schema_migration

    moment = int(time.time())
    if args.command == "plan-migration":
        try:
            plan = schema_migration.prepare_forward_migration(vault, now=moment)
            summary = schema_migration.plan_summary(plan)
        except schema_migration.ForwardMigrationUnavailable:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_MIGRATION_UNAVAILABLE",
                "the exact quiesced v3 policy and catalog could not be prepared",
                as_json=as_json,
            )
            return 1
        if as_json:
            print(json.dumps(summary))
        else:
            for key, value in summary.items():
                print(f"{key}: {value}")
        return 0

    if args.command == "stage-migration":
        expected_plan_digest = args.expected_plan_digest
        if not args.yes:
            confirmation = {
                "staged": False,
                "reason": "confirmation_required",
                "plan_digest": expected_plan_digest,
            }
            if as_json:
                print(json.dumps(confirmation))
            else:
                for key, value in confirmation.items():
                    print(f"{key}: {value}")
            return 2
        try:
            terminal = schema_migration.stage_forward_migration(
                vault,
                expected_plan_digest=expected_plan_digest,
                now=moment,
            )
        except schema_migration.ForwardMigrationPlanMismatch:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_PLAN_MISMATCH",
                "the reviewed migration plan changed; no activation was attempted",
                as_json=as_json,
            )
            return 1
        except schema_migration.ForwardMigrationUnavailable:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_MIGRATION_UNAVAILABLE",
                "the exact v3 migration namespace could not be staged",
                as_json=as_json,
            )
            return 1
        result = {
            "staged": True,
            "schema_version": 3,
            "plan_digest": terminal.plan_digest,
            "projection_namespace_id": terminal.projection_namespace_id,
            "projection_rows_digest": terminal.projection_rows_digest,
            "item_count": terminal.item_count,
        }
        if as_json:
            print(json.dumps(result))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        return 0

    if args.command == "commit-migration":
        expected_plan_digest = args.expected_plan_digest
        if not args.yes:
            confirmation = {
                "migrated": False,
                "reason": "confirmation_required",
                "plan_digest": expected_plan_digest,
            }
            if as_json:
                print(json.dumps(confirmation))
            else:
                print(
                    "migration cutover requires --yes after reviewing this exact plan: "
                    f"{expected_plan_digest}",
                    file=sys.stderr,
                )
            return 2
        try:
            result = schema_migration.commit_forward_migration(
                vault,
                expected_plan_digest=expected_plan_digest,
                now=moment,
            )
        except schema_migration.ForwardMigrationPlanMismatch:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_PLAN_MISMATCH",
                "the reviewed migration plan changed; no activation was attempted",
                as_json=as_json,
            )
            return 1
        except schema_migration.ForwardMigrationUnavailable:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_MIGRATION_UNAVAILABLE",
                "the verified backup and exact v3-to-v4 cutover could not complete",
                as_json=as_json,
            )
            return 1
        target = result.target
        terminal = {
            "migrated": True,
            "schema_version": result.schema_version,
            "logical_vault_id": target.logical_vault_id,
            "activation_store_id": target.activation_store_id,
            "activation_epoch": target.activation_epoch,
            "activation_state_digest": target.activation_state_digest,
            "policy_generation_id": target.policy_generation_id,
            "policy_fingerprint": target.policy_fingerprint,
            "projector_schema_version": target.projector_schema_version,
            "catalog_generation": target.catalog_generation,
            "projection_namespace_id": target.projection_namespace_id,
            "plan_digest": result.plan_digest,
            "source_store_digest": result.source_store_digest,
            "backup_reference": result.backup_reference,
            "replayed": result.replayed,
        }
        if as_json:
            print(json.dumps(terminal))
        else:
            delivery = "replayed" if result.replayed else "committed"
            print(
                f"schema v4 migration {delivery}; backup {result.backup_reference}"
            )
        return 0

    if args.command == "restore-migration-backup":
        expected_plan_digest = args.expected_plan_digest
        expected_backup_reference = args.expected_backup_reference
        if not args.yes:
            confirmation = {
                "restored": False,
                "reason": "confirmation_required",
                "plan_digest": expected_plan_digest,
                "backup_reference": expected_backup_reference,
            }
            if as_json:
                print(json.dumps(confirmation))
            else:
                print(
                    "pre-migration backup restore requires --yes after reviewing "
                    f"plan {expected_plan_digest} and backup {expected_backup_reference}",
                    file=sys.stderr,
                )
            return 2
        try:
            result = schema_migration.restore_forward_migration_backup(
                vault,
                expected_plan_digest=expected_plan_digest,
                expected_backup_reference=expected_backup_reference,
                now=moment,
            )
        except schema_migration.ForwardMigrationRestoreUnavailable:
            _governance_schema_print_error(
                "GOVERNANCE_SCHEMA_BACKUP_RESTORE_UNAVAILABLE",
                "the immediate predecessor backup could not be restored safely; "
                "use reviewed v4-to-v3 downmigration after later durable changes",
                as_json=as_json,
            )
            return 1
        terminal = {
            "restored": True,
            "schema_version": result.schema_version,
            "plan_digest": result.plan_digest,
            "source_store_digest": result.source_store_digest,
            "backup_reference": result.backup_reference,
            "recovery_event_id": result.recovery_event_id,
            "recovery_plan_digest": result.recovery_plan_digest,
            "replayed": result.replayed,
        }
        if as_json:
            print(json.dumps(terminal))
        else:
            delivery = "replayed" if result.replayed else "committed"
            print(
                f"schema v3 predecessor restore {delivery}; recovery event "
                f"{result.recovery_event_id}"
            )
        return 0

    try:
        status = _governance_schema_status(vault, now=moment)
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_UNAVAILABLE",
            "the external custody and local schema basis could not be verified",
            as_json=as_json,
        )
        return 1

    if args.command == "status":
        if as_json:
            print(json.dumps(status))
        else:
            for key in (
                "schema_version",
                "governance_enrolled",
                "logical_vault_id",
                "activation_store_id",
                "activation_epoch",
                "activation_state_digest",
                "serving_membership_epoch",
            ):
                print(f"{key}: {status[key]}")
            for replica in status["replicas"]:
                assert isinstance(replica, dict)
                print(
                    "replica: "
                    f"{replica['replica_id']} state={replica['state']} "
                    f"schema={replica['schema_version']} "
                    f"issuance_stopped={replica['issuance_stopped']} "
                    f"no_in_flight={replica['no_in_flight']}"
                )
        return 0

    expected_digest = args.expected_activation_state_digest
    if (
        len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
        or expected_digest != status["activation_state_digest"]
    ):
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_TARGET_MISMATCH",
            "the reviewed activation state changed; no downmigration was attempted",
            as_json=as_json,
        )
        return 1
    if status["schema_version"] != 4:
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_NOT_V4",
            "the reviewed vault is not an exact schema-v4 downmigration source",
            as_json=as_json,
        )
        return 1
    if not args.yes:
        payload = {
            "downmigrated": False,
            "reason": "confirmation_required",
            "schema_version": status["schema_version"],
            "logical_vault_id": status["logical_vault_id"],
            "activation_state_digest": status["activation_state_digest"],
        }
        if as_json:
            print(json.dumps(payload))
        else:
            print(
                "downmigration requires --yes after reviewing this exact activation "
                f"state: {status['activation_state_digest']}",
                file=sys.stderr,
            )
        return 2

    try:
        result = schema_downmigration.downmigrate_enrolled_v4_store(
            vault,
            now=moment,
        )
    except schema_downmigration.DownmigrationUnavailable:
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_DOWNMIGRATION_UNAVAILABLE",
            "the drained schema-v4 rollback basis did not verify; no unsafe recovery was attempted",
            as_json=as_json,
        )
        return 1
    active = result.active
    if (
        result.schema_version != 3
        or active.logical_vault_id != status["logical_vault_id"]
        or active.activation_store_id != status["activation_store_id"]
        or active.activation_epoch != status["activation_epoch"]
        or active.activation_state_digest != status["activation_state_digest"]
    ):
        _governance_schema_print_error(
            "GOVERNANCE_SCHEMA_DOWNMIGRATION_UNAVAILABLE",
            "the durable downmigration terminal did not match the reviewed activation state",
            as_json=as_json,
        )
        return 1
    terminal = {
        "downmigrated": True,
        "schema_version": result.schema_version,
        "logical_vault_id": active.logical_vault_id,
        "activation_store_id": active.activation_store_id,
        "activation_epoch": active.activation_epoch,
        "activation_state_digest": active.activation_state_digest,
        "recovery_event_id": result.recovery_event_id,
        "recovery_plan_digest": result.recovery_plan_digest,
        "recovery_target_digest": result.recovery_target_digest,
        "recovery_terminal_digest": result.recovery_terminal_digest,
        "replayed": result.replayed,
    }
    if as_json:
        print(json.dumps(terminal))
    else:
        delivery = "replayed" if result.replayed else "committed"
        print(
            f"schema v3 downmigration {delivery}; recovery terminal "
            f"{result.recovery_terminal_digest}"
        )
    return 0


def _reclaim_schema_main(argv: list[str]) -> int:
    """Remove the pre-#488 copy of the shipped contract from the note namespace.

    A separate command, and a preview by default, because the alternative is an
    upgrade that silently deletes a few hundred kilobytes from inside a user's
    Obsidian vault. The bytes are reproducible from the package, but the vault is
    the artifact the product promises the user owns, so a deletion inside it is
    something they ask for rather than something they discover.
    """
    parser = argparse.ArgumentParser(
        prog="exomem reclaim-schema",
        description=(
            f"Remove the superseded copy of the shipped schema from "
            f"{kb_prefix()}_Schema/ once it has been redeployed to .exomem/schema/."
        ),
    )
    parser.add_argument(
        "--vault",
        help="Vault root (default: $EXOMEM_VAULT_PATH, else current dir).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this the command reports what it would do.",
    )
    args = parser.parse_args(argv)

    from . import init as init_module

    vault = Path(args.vault or os.environ.get("EXOMEM_VAULT_PATH") or ".")
    report = init_module.reclaim_legacy_shipped_schema(vault, apply=args.apply)

    if not report["removed"] and not report["declined"]:
        print(f"Nothing to reclaim: {vault} has no superseded schema copy.")
        return 0

    verb = "Removed" if report["applied"] else "Would remove"
    print(f"{verb} {len(report['removed'])} file(s), {report['reclaimed_kb']} KB:")
    for relative in report["removed"]:
        print(f"  {kb_prefix()}_Schema/{relative}")
    if report["declined"]:
        print(f"Kept {len(report['declined'])} file(s):")
        for entry in report["declined"]:
            print(f"  {kb_prefix()}_Schema/{entry['path']} — {entry['reason']}")
    if not report["applied"]:
        print("Re-run with --apply to delete.")
    return 0


def _init_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem init",
        description=f"Bootstrap a fresh {kb_dirname()} scaffold into a vault.",
    )
    parser.add_argument(
        "--vault",
        help="Vault root to scaffold (default: $EXOMEM_VAULT_PATH, else current dir).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Overlay the scaffold even if {kb_prefix()} exists (existing files kept).",
    )
    args = parser.parse_args(argv)

    from . import init as init_module

    vault = args.vault or os.environ.get("EXOMEM_VAULT_PATH") or "."
    try:
        report = init_module.init_vault(Path(vault), force=args.force)
    except FileExistsError as e:
        print(f"exomem init: {e}", file=sys.stderr)
        return 1
    print(f"Initialized {kb_dirname()} at {report['kb']}")
    print(f"  {len(report['created'])} files created + the typed folder tree.")
    print("Next:")
    print("  1. Point Claude Code at this vault (see QUICKSTART.md).")
    print(
        f"  2. Install the Exomem {kb_dirname()} skill so Claude knows how to use it: "
        "python -m exomem install-skill"
    )
    print(f"  3. Adapt {kb_prefix()}_Schema/project-keys.yaml to your own projects.")
    print(
        "  4. For low-resource mode: exomem mode quiet; inspect with "
        "exomem status --resources --json."
    )
    return 0


def _install_skill_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem install-skill",
        description=(
            "Install the Exomem skills into every agent client on this machine. "
            "The MCP server is the hands; the skills are the brain that tells the agent "
            "when to capture and how to file — without them, the tools sit unused."
        ),
    )
    parser.add_argument(
        "--client",
        default="auto",
        help="Which client(s) to install into: auto (every client detected here), "
        "all (every supported client), claude, or codex. Default: auto.",
    )
    parser.add_argument(
        "--target",
        help="Install into one explicit folder instead (default: ~/.claude/skills/exomem). "
        "Overrides --client.",
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
        if target is not None:
            reports = {
                "claude": install_module.install_skill(target, force=args.force, link=args.link)
            }
        else:
            reports = install_module.install_skills(
                client=args.client, force=args.force, link=args.link
            )["clients"]
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"exomem install-skill: {e}", file=sys.stderr)
        return 1

    for client, report in reports.items():
        print(
            f"Installed the Exomem skills for {client} ({report['mode']}, {report['files']} files):"
        )
        print(f"  {report['target']}")
        if report.get("workflow_skills"):
            names = ", ".join(s["name"] for s in report["workflow_skills"])
            print(f"  Workflow skills: {names}")
    # Installing to the default location supersedes any pre-rename `knowledge-base`
    # skill; retire it so Claude Code doesn't load both.
    if target is None:
        removed = install_module.remove_legacy_skill()
        if removed is not None:
            print(f"  Removed the pre-rename skill at {removed}.")
    clients = ", ".join(reports)
    print(f"Restart {clients} to load them. Then just talk - it captures at")
    print('natural stopping points, or say "find my notes on X".')
    return 0


def _package_skills_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem package-skills",
        description=(
            "Build one uploadable .zip per skill for clients that have no filesystem "
            "install path (claude.ai, ChatGPT). Claude Code and Codex should use "
            "`exomem install-skill` instead."
        ),
    )
    parser.add_argument(
        "--out",
        help="Output directory for the archives (default: ./dist/skills).",
    )
    parser.add_argument(
        "--vault",
        help="Vault root whose real project-keys.yaml to overlay into the core skill. "
        "This is an explicit personalized build; omit for a generic, shareable archive. "
        "Personalized output defaults inside the supplied vault and cannot target public "
        "repository or release paths.",
    )
    parser.add_argument(
        "--plugin-root",
        help="Instead of archives, regenerate the Claude Code plugin tree at this path "
        "(maintainer task; the committed tree must mirror the packaged sources).",
    )
    args = parser.parse_args(argv)

    from . import package_skills as package_module

    if args.plugin_root:
        report = package_module.sync_plugin(Path(args.plugin_root))
        print(f"Synced plugin v{report['version']} at {report['plugin_root']}")
        print(f"  skills: {', '.join(report['skills'])}")
        return 0

    # Personalized packaging is explicit.  A configured runtime vault must never
    # silently turn the default public package command into a private build.
    vault = Path(args.vault) if args.vault else None
    try:
        report = package_module.package_skills(
            Path(args.out) if args.out else None,
            vault=vault,
        )
    except (FileNotFoundError, OSError, ValueError) as e:
        print(f"exomem package-skills: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {report['count']} skill archives to {report['out_dir']}:")
    for archive in report["archives"]:
        print(f"  {Path(archive['path']).name} ({archive['bytes'] // 1024} KB)")
    print()
    print("Upload these in the client's settings:")
    print("  claude.ai  -> Settings > Capabilities > Skills > upload")
    print("  ChatGPT    -> Settings > Skills > upload")
    print("Claude Code and Codex do not need these - run `exomem install-skill`.")
    return 0


def _install_hook_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem install-hook",
        description=(
            "Wire the KB capture, retrieval, and local continuation hooks into "
            "Claude Code or Codex: a "
            "Stop hook that captures conclusions at stepping-stones (write), and "
            "a UserPromptSubmit hook that reminds the agent to consult the KB before "
            "answering (read), plus structural PreCompact/SessionStart recovery. "
            "Claude also supports SessionEnd; pinned Codex 0.144.3 does not. "
            "Language-agnostic and cheap (gated + cooldown). "
            "Re-running is idempotent."
        ),
    )
    parser.add_argument(
        "--client",
        choices=("claude", "codex", "all"),
        default=None,
        help="client hook config to wire/check (default: claude for install; both for --check)",
    )
    parser.add_argument(
        "--hook-dir",
        help="Where to write the hook scripts (default: ~/.claude/hooks or ~/.codex/hooks).",
    )
    parser.add_argument(
        "--settings",
        help="hook config to wire (default: ~/.claude/settings.json or ~/.codex/hooks.json).",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Write the script but don't touch settings.json; print the snippet to add.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only health check for deployed Claude Code/Codex hooks; writes nothing.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "Remove the entries and scripts this installed, and nothing else; "
            "also edits any yadm alternate sources, which is what makes the "
            "removal survive the next alternate selection."
        ),
    )
    parser.add_argument(
        "--keep-scripts",
        action="store_true",
        help="With --uninstall: unwire the hook config but leave the scripts on disk.",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import install_hook as hook_module

    if args.uninstall:
        if args.check or args.print_only:
            parser.error("--uninstall cannot be combined with --check or --print-only")
        if args.client == "all":
            if args.hook_dir or args.settings:
                parser.error("--client all cannot be combined with --hook-dir or --settings")
            report = hook_module.uninstall_all_hooks(remove_scripts=not args.keep_scripts)
            if args.json:
                print(json.dumps(report))
            else:
                for row in report["clients"]:
                    if "result" in row:
                        print(hook_module.render_uninstall_human(row["result"]))
                    else:
                        print(
                            f"Failed to uninstall hooks for {row['client']}: {row['error']}",
                            file=sys.stderr,
                        )
            return 0 if report["success"] else 1
        try:
            report = hook_module.uninstall_hook(
                hook_dir=args.hook_dir,
                settings_path=args.settings,
                client=args.client or "claude",
                remove_scripts=not args.keep_scripts,
            )
        except (OSError, RuntimeError, ValueError) as e:
            print(f"exomem install-hook --uninstall: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report))
        else:
            print(hook_module.render_uninstall_human(report))
        return 0 if report["success"] else 1

    if args.check:
        if (args.hook_dir or args.settings) and args.client in {None, "all"}:
            parser.error("--hook-dir/--settings with --check require one explicit client")
        try:
            report = hook_module.check_hooks(
                clients=(
                    (args.client,)
                    if args.client not in {None, "all"}
                    else hook_module.SUPPORTED_CLIENTS
                ),
                hook_dir=Path(args.hook_dir) if args.hook_dir else None,
                settings_path=Path(args.settings) if args.settings else None,
            )
        except ValueError as e:
            print(f"exomem install-hook --check: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report))
        else:
            print(hook_module.render_check_human(report))
        return 0 if report["success"] else 1

    if args.client == "all":
        if args.hook_dir or args.settings:
            parser.error("--client all cannot be combined with --hook-dir or --settings")
        report = hook_module.install_all_hooks(wire=not args.print_only)
        if args.json:
            print(json.dumps(report))
        else:
            for row in report["clients"]:
                if row["success"]:
                    result = row["result"]
                    destination = result["settings"] or "print-only output"
                    print(f"Installed hooks for {row['client']} into {destination}.")
                else:
                    print(
                        f"Failed to install hooks for {row['client']}: {row['error']}",
                        file=sys.stderr,
                    )
        return 0 if report["success"] else 1

    try:
        report = hook_module.install_hook(
            hook_dir=args.hook_dir,
            settings_path=args.settings,
            wire=not args.print_only,
            client=args.client or "claude",
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as e:
        print(f"exomem install-hook: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report))
        return 0

    client_label = "Codex" if report["client"] == "codex" else "Claude Code"
    print(f"Installed the KB hook scripts for {client_label}:")
    for item in report["installed"]:
        print(f"  {item['event']:<16} {item['script']}")
    if report["wired"]:
        print(f"Wired into {report['settings']}.")
        print(f"Restart {client_label} to activate. Triggers log to:")
        home = "~/.codex" if report["client"] == "codex" else "~/.claude"
        print(f"  {home}/exomem-capture-nudge.log   (write / capture)")
        print(f"  {home}/exomem-retrieve-nudge.log  (read / retrieval)")
    else:
        print("Add this to your hook config (merge into hooks):")
        print(hook_module.snippet(report["installed"]))
    return 0


# --------------------------------------------------------------------------- #
# Simple product actions (friendly CLI aliases over canonical registry commands)
# --------------------------------------------------------------------------- #
def _simple_cli_action_names() -> frozenset[str]:
    from . import commands as commands_module

    return frozenset(commands_module.simple_action_names())


def _with_json(argv: list[str], enabled: bool) -> list[str]:
    return argv + (["--json"] if enabled else [])


def _append_repeated(argv: list[str], flag: str, values: list[str] | None) -> None:
    for value in values or []:
        argv.extend([flag, value])


def _field(argv: list[str], name: str, value: object | None) -> None:
    if value is None:
        return
    if isinstance(value, list):
        if not value:
            return
        value = ",".join(str(item) for item in value)
    argv.extend(["--field", f"{name}={value}"])


def _simple_action_main(argv: list[str]) -> int:
    action = argv[0]
    rest = argv[1:]
    if action == "ask":
        return _simple_ask_main(rest)
    if action == "remember":
        return _simple_remember_main(rest)
    if action == "capture":
        return _simple_capture_main(rest)
    if action == "review":
        return _simple_review_main(rest)
    if action == "connect":
        return _simple_connect_main(rest)
    if action == "adopt":
        return _simple_adopt_main(rest)
    if action == "maintain":
        return _simple_maintain_main(rest)
    raise AssertionError(f"unhandled simple action: {action}")


def _simple_ask_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem ask",
        description=(
            "Ask Exomem what it knows. Thin alias over ask_memory with compact recall defaults."
        ),
    )
    parser.add_argument("query", help="question or search phrase")
    parser.add_argument("--deep", action="store_true", help="include a packed reasoning context")
    parser.add_argument(
        "--graph-enrich",
        action="store_true",
        help="with --deep, include typed graph neighborhood data when available",
    )
    parser.add_argument("--limit", type=int, default=15, help="maximum hits to return")
    parser.add_argument(
        "--scope",
        choices=("kb", "vault", "kb-only"),
        default="kb",
        help="search scope (default: kb, which can auto-widen)",
    )
    parser.add_argument(
        "--type",
        dest="types",
        action="append",
        default=None,
        help="page type filter (repeatable)",
    )
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        default=None,
        help="project filter (repeatable)",
    )
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=None,
        help="tag filter (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    core = [
        "ask_memory",
        args.query,
        "--detail",
        "compact",
        "--no-rerank",
        "--limit",
        str(args.limit),
        "--scope",
        args.scope,
    ]
    _append_repeated(core, "--types", args.types)
    _append_repeated(core, "--projects", args.projects)
    _append_repeated(core, "--tags", args.tags)
    if args.deep or args.graph_enrich:
        core.append("--deep")
    if args.graph_enrich:
        core.append("--graph-enrich")
    return _core_op_main(_with_json(core, args.json))


def _simple_remember_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem remember",
        description="Remember a durable conclusion. Thin alias over remember.",
    )
    parser.add_argument("content", help="compiled note body markdown")
    parser.add_argument("--title", required=True, help="note title")
    parser.add_argument(
        "--type",
        dest="note_type",
        default="insight",
        help="note type (default: insight)",
    )
    parser.add_argument("--project", help="research-note project key")
    parser.add_argument(
        "--project-ref",
        dest="projects",
        action="append",
        default=None,
        help="projects list entry (repeatable)",
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        default=None,
        help="source/evidence path (repeatable)",
    )
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=None,
        help="tag (repeatable)",
    )
    parser.add_argument("--status", help="status override")
    parser.add_argument("--severity", help="failure severity")
    parser.add_argument("--pattern-type", help="pattern subtype")
    parser.add_argument("--domain", help="experiment domain")
    parser.add_argument("--started", help="experiment start date")
    parser.add_argument("--duration", help="experiment duration")
    parser.add_argument("--medium", help="production-log medium")
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    core = [
        "remember",
        "--content",
        args.content,
        "--note-type",
        args.note_type,
        "--title",
        args.title,
    ]
    _field(core, "project", args.project)
    _field(core, "projects", args.projects)
    _field(core, "sources", args.sources)
    _field(core, "tags", args.tags)
    _field(core, "status", args.status)
    _field(core, "severity", args.severity)
    _field(core, "pattern_type", args.pattern_type)
    _field(core, "domain", args.domain)
    _field(core, "started", args.started)
    _field(core, "duration", args.duration)
    _field(core, "medium", args.medium)
    return _core_op_main(_with_json(core, args.json))


def _simple_capture_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem capture",
        description="Capture raw source or proof-bearing text. Thin alias over add/preserve.",
    )
    parser.add_argument("content", help="raw text to capture")
    parser.add_argument(
        "--as",
        dest="capture_kind",
        choices=("source", "evidence"),
        default="source",
    )
    parser.add_argument("--title", help="source title (required for --as source)")
    # No default. Defaulting to the fallback here taught every CLI capture to
    # file clearly classifiable material as unclassified; absent means
    # unclassified, which the product resolves, rather than a claim.
    parser.add_argument(
        "--source-type", help="what the artifact IS; open vocabulary, use the label you mean"
    )
    parser.add_argument(
        "--source-kind", help="preferred name for the same axis as --source-type"
    )
    parser.add_argument(
        "--domain", help="what the artifact is ABOUT, independent of its kind"
    )
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        default=None,
        help="project key this source serves (repeatable)",
    )
    parser.add_argument("--url", help="source URL")
    parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=None,
        help="tag (repeatable)",
    )
    parser.add_argument("--why-captured", help="why this source is worth keeping")
    parser.add_argument("--scope", help="evidence scope for --as evidence")
    parser.add_argument("--category", help="evidence category for --as evidence")
    parser.add_argument("--filename", help="evidence filename for --as evidence")
    parser.add_argument("--description", help="evidence sidecar description")
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    if args.capture_kind == "source":
        if not args.title:
            parser.error("--title is required when --as source")
        core = [
            "capture_source",
            "--content",
            args.content,
            "--title",
            args.title,
        ]
        if args.source_type:
            core.extend(["--source-type", args.source_type])
        if args.source_kind:
            core.extend(["--source-kind", args.source_kind])
        if args.domain:
            core.extend(["--domain", args.domain])
        _append_repeated(core, "--projects", args.projects)
        if args.url:
            core.extend(["--url", args.url])
        _append_repeated(core, "--tags", args.tags)
        if args.why_captured:
            core.extend(["--why-captured", args.why_captured])
    else:
        missing = [name for name in ("scope", "category", "filename") if not getattr(args, name)]
        if missing:
            parser.error("--as evidence requires " + ", ".join(f"--{name}" for name in missing))
        core = [
            "preserve_evidence",
            "--scope",
            args.scope,
            "--category",
            args.category,
            "--filename",
            args.filename,
            "--content",
            args.content,
        ]
        if args.description:
            core.extend(["--description", args.description])
    return _core_op_main(_with_json(core, args.json))


def _compose_review_why(reason: str | None, why: str | None) -> str | None:
    """Compose `--reason` onto `--why` as the leading colon token the store parses.

    The reason code travels inside the existing free-text `why` rather than as a
    parameter of its own, so the pinned tool input schema does not move and a
    client that only ever sends `why` keeps working unchanged.
    """
    text = (why or "").strip()
    if not reason:
        return text or None
    return f"{reason}: {text}" if text else f"{reason}:"


def _simple_review_main(argv: list[str]) -> int:
    triage_actions = {"dismiss", "snooze", "reopen", "competing"}
    disposition_actions = {"quiet", "off", "normal"}
    if argv and argv[0] in triage_actions | disposition_actions:
        from .review_state import DEFAULT_REASON, REASON_CODES, family_ref

        action = argv[0]
        family = action in disposition_actions
        parser = argparse.ArgumentParser(
            prog=f"exomem review {action}",
            description=(
                f"Set one signal family's disposition to `{action}`."
                if family
                else f"{action.title()} one Epistemic Inbox item."
            ),
        )
        parser.add_argument(
            "ref",
            help=(
                "signal family name, or its exomem://review/family/<name> reference"
                if family
                else "stable exomem://review/<id> reference"
            ),
        )
        if action == "snooze":
            parser.add_argument("--until", required=True, help="snooze through YYYY-MM-DD")
        # `quiet` and `off` refuse `unspecified` in the store, so the CLI must
        # refuse it at the parser. Accepting a value and then failing on it a
        # layer down turns a spelling mistake into a runtime error with a
        # different message, and hides the real vocabulary from `--help`.
        reason_choices = (
            tuple(code for code in REASON_CODES if code != DEFAULT_REASON)
            if action in {"quiet", "off"}
            else REASON_CODES
        )
        parser.add_argument(
            "--reason",
            choices=reason_choices,
            required=action in {"quiet", "off"},
            help=(
                "closed reason code (required)"
                if action in {"quiet", "off"}
                else "closed reason code"
            ),
        )
        parser.add_argument("--why", help="optional review rationale")
        parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
        args = parser.parse_args(argv[1:])
        ref = family_ref(args.ref) if family and "://" not in args.ref else args.ref
        core = ["triage_memory", ref, "--action", action]
        if action == "snooze":
            core.extend(["--until", args.until])
        why = _compose_review_why(args.reason, args.why)
        if why:
            core.extend(["--why", why])
        return _core_op_main(_with_json(core, args.json))

    parser = argparse.ArgumentParser(
        prog="exomem review",
        description="Review the Epistemic Inbox or run the full vault audit.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="run the full audit report instead of attention queue",
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        default=None,
        help="review/audit category (repeatable)",
    )
    parser.add_argument("--limit", type=int, default=25, help="attention item cap")
    parser.add_argument(
        "--state",
        choices=("open", "all", "snoozed", "dismissed", "competing"),
        default="open",
        help="review state view",
    )
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    core = (
        ["review_memory", "--mode", "audit"]
        if args.audit
        else [
            "review_memory",
            "--mode",
            "attention",
            "--limit",
            str(args.limit),
            "--state",
            args.state,
        ]
    )
    _append_repeated(core, "--categories", args.categories)
    return _core_op_main(_with_json(core, args.json))


def _simple_connect_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem connect",
        description="Suggest links or typed graph relations. Proposal-only by default.",
    )
    parser.add_argument("--path", help="existing page path")
    parser.add_argument("--draft-title", help="draft title")
    parser.add_argument("--draft-body", help="draft body")
    parser.add_argument(
        "--relations",
        action="store_true",
        help="suggest typed graph relations instead of wikilinks",
    )
    parser.add_argument(
        "--model-suggestions",
        action="store_true",
        help="opt into model-backed relation suggestions",
    )
    parser.add_argument("--limit", type=int, default=8, help="candidate cap")
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    operation = "suggest-relations" if args.relations else "suggest-links"
    core = ["connect_memory", "--operation", operation]
    if args.path:
        core.extend(["--path", args.path])
    if args.draft_title:
        core.extend(["--draft-title", args.draft_title])
    if args.draft_body:
        core.extend(["--draft-body", args.draft_body])
    core.extend(["--limit", str(args.limit)])
    if args.relations and args.model_suggestions:
        core.append("--include-model-suggestions")
    return _core_op_main(_with_json(core, args.json))


def _simple_adopt_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem adopt",
        description="Adopt an existing vault safely. Thin alias over adopt_vault.",
    )
    parser.add_argument("path", nargs="?", help="vault subtree to scan")
    parser.add_argument(
        "--mode",
        choices=("scan-only", "save-manifest", "copy-as-sources", "compile-selected"),
        default="scan-only",
        help="adoption mode (default: scan-only)",
    )
    parser.add_argument("--max-depth", type=int, help="folder tree depth cap")
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="include hidden files/directories",
    )
    parser.add_argument("--samples", type=int, help="filename sample count per folder")
    parser.add_argument("--pack-limit", type=int, help="maximum suggested knowledge packs")
    parser.add_argument("--manifest-path", help="optional adoption manifest destination")
    parser.add_argument(
        "--selected-path",
        dest="selected_paths",
        action="append",
        default=None,
        help="legacy file to copy/compile (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    core = ["adopt_vault", "--mode", args.mode]
    if args.path:
        core.append(args.path)
    if args.max_depth is not None:
        core.extend(["--max-depth", str(args.max_depth)])
    if args.include_hidden:
        core.append("--include-hidden")
    if args.samples is not None:
        core.extend(["--samples", str(args.samples)])
    if args.pack_limit is not None:
        core.extend(["--pack-limit", str(args.pack_limit)])
    if args.manifest_path:
        core.extend(["--manifest-path", args.manifest_path])
    _append_repeated(core, "--selected-paths", args.selected_paths)
    return _core_op_main(_with_json(core, args.json))


def _simple_maintain_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem maintain",
        description="Check vault health; write-capable fixes require explicit flags.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="run audit_fix instead of read-only audit",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="run reconcile instead of read-only audit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --fix/--reconcile, report without writing",
    )
    parser.add_argument(
        "--rebuild-embeddings",
        action="store_true",
        help="with --fix, rebuild text embeddings",
    )
    parser.add_argument(
        "--rebuild-graph",
        action="store_true",
        help="with --reconcile, quarantine unavailable graph lineage and rebuild it",
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        default=None,
        help="audit category (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
    args = parser.parse_args(argv)

    if args.fix and args.reconcile:
        parser.error("choose only one of --fix or --reconcile")
    if args.rebuild_graph and not args.reconcile:
        parser.error("--rebuild-graph requires --reconcile")
    if args.fix:
        core = ["maintain_memory", "--mode", "fix"]
        if args.dry_run:
            core.append("--dry-run")
        if args.rebuild_embeddings:
            core.append("--rebuild-embeddings")
    elif args.reconcile:
        core = ["maintain_memory", "--mode", "reconcile"]
        if args.dry_run:
            core.append("--dry-run")
        if args.rebuild_graph:
            core.append("--rebuild-graph")
    else:
        core = ["maintain_memory", "--mode", "audit"]
        _append_repeated(core, "--categories", args.categories)
    return _core_op_main(_with_json(core, args.json))


# --------------------------------------------------------------------------- #
# Registry-driven core operations (reads + writes)
# --------------------------------------------------------------------------- #
# `note`/`replace` carry a wide, type-specific signature; rather than dozens of
# flags, their REQUIRED params stay flags and everything else is reachable via a
# repeatable `--field key=value`, so the CLI stays clean.
_FIELD_ESCAPE = frozenset({"remember", "replace_memory"})
_FIELD_ESCAPE_VISIBLE_PARAMS = frozenset({"slug", "response_detail"})
_LEGACY_EDIT_BOOL_FIELDS = frozenset({"replace_all", "overwrite", "allow_curated", "validate_only"})


def _expose_tier2() -> bool:
    return not os.environ.get("EXOMEM_DISABLE_TIER2")


def _core_op_names(*, expose_tier2: bool | None = None) -> frozenset[str]:
    from . import commands as commands_module

    if expose_tier2 is None:
        expose_tier2 = _expose_tier2()
    return frozenset(
        c.name for c in commands_module.product_commands_for("cli", expose_tier2=expose_tier2)
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
        if field_escape and not p.required and p.name not in _FIELD_ESCAPE_VISIBLE_PARAMS:
            continue  # reachable via --field
        if p.cli_positional:
            sp.add_argument(
                p.name,
                nargs=None if p.required else "?",
                default=None,
                metavar="{" + ",".join(p.choices) + "}" if p.choices else None,
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
                required=(
                    p.required
                    and not p.cli_positional
                    and not (cmd.name == "edit_memory" and p.name == "operation")
                ),
                metavar="{" + ",".join(p.choices) + "}" if p.choices else None,
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
    if cmd.name == "edit_memory":
        from .edit_operations import LEGACY_EDIT_FIELDS

        primary_names = {p.name for p in cmd.params}
        for name in sorted(LEGACY_EDIT_FIELDS):
            if name in primary_names:
                # Already registered above as a real top-level param (e.g.
                # `validate_only`, promoted out of the legacy-only set) —
                # re-adding it here would collide on the same option string.
                continue
            if name in _LEGACY_EDIT_BOOL_FIELDS:
                sp.add_argument(
                    _flag(name),
                    dest=name,
                    action=argparse.BooleanOptionalAction,
                    default=None,
                    help=argparse.SUPPRESS,
                )
            elif name == "tags":
                sp.add_argument(
                    _flag(name),
                    dest=name,
                    action="append",
                    default=None,
                    help=argparse.SUPPRESS,
                )
            else:
                sp.add_argument(
                    _flag(name),
                    dest=name,
                    default=None,
                    help=argparse.SUPPRESS,
                )


def _collect_raw_args(cmd, args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
    field_escape = cmd.name in _FIELD_ESCAPE
    raw: dict = {}
    for p in cmd.params:
        if field_escape and not p.required and p.name not in _FIELD_ESCAPE_VISIBLE_PARAMS:
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
    if cmd.name == "edit_memory":
        from .edit_operations import LEGACY_EDIT_FIELDS

        for name in LEGACY_EDIT_FIELDS:
            value = getattr(args, name, None)
            if value is not None:
                raw[name] = value
    return raw


def _normalize_cli_edit(cmd, raw: dict, cli_ops) -> dict:  # noqa: ANN001
    from . import edit_operations

    primary_names = {parameter.name for parameter in cmd.params}
    primary_raw = {name: value for name, value in raw.items() if name in primary_names}
    legacy = {name: value for name, value in raw.items() if name not in primary_names}
    primary = cli_ops.coerce(
        cmd.params,
        primary_raw,
        guarded_fields=cmd.guarded_fields,
        tool=cmd.name,
        cli=True,
    )
    if isinstance(legacy.get("edits"), str):
        try:
            legacy["edits"] = json.loads(legacy["edits"])
        except json.JSONDecodeError as error:
            raise cli_ops.OpError("BAD_JSON", f"`edits` must be valid JSON: {error}") from None
    if isinstance(legacy.get("value"), str):
        try:
            legacy["value"] = json.loads(legacy["value"])
        except json.JSONDecodeError:
            pass
    normalized = edit_operations.normalize_edit_surface_arguments({**primary, **legacy})
    return cli_ops.coerce(
        cmd.params,
        normalized,
        guarded_fields=cmd.guarded_fields,
        tool=cmd.name,
        cli=True,
    )


def _print_adopt_human(result: dict) -> None:
    summary = result.get("summary") or {}
    totals = summary.get("totals") or {}
    governance = result.get("governance") or {}

    print("Adoption report")
    print(f"  Mode: {result.get('mode', 'scan-only')}")
    print(
        "  Scan: "
        f"{totals.get('files', 0)} files, "
        f"{totals.get('markdown', 0)} markdown, "
        f"{totals.get('dirs', 0)} folders"
    )
    if governance.get("kb_present"):
        print(f"  Governed layer: {governance.get('governed_path') or kb_prefix()}")
    else:
        print("  Governed layer: not initialized yet")
    print("  Originals: untouched; non-KB files stay read-only input.")

    packs = result.get("pack_suggestions") or []
    print("\nLikely packs")
    if packs:
        for pack in packs[:6]:
            name = pack.get("name") or pack.get("id") or "unknown"
            score = int(pack.get("score") or 0)
            signals = ", ".join(pack.get("matched_signals") or [])
            suffix = f" - {signals}" if signals else " - default starting pack"
            print(f"  - {name} ({score} signal{'s' if score != 1 else ''}){suffix}")
    else:
        print("  - None suggested by the structural scan")

    actions = result.get("next_actions") or []
    print("\nSafe next actions")
    for action in actions:
        print(f"  - {action.get('action')} [{action.get('status')}]: {action.get('description')}")

    if manifest := result.get("manifest"):
        print(f"\nSaved manifest: {manifest.get('path')}")
    if copy := result.get("copy"):
        copied = copy.get("copied_sources") or []
        skipped = copy.get("skipped") or []
        print(f"\nCopied sources: {len(copied)} copied, {len(skipped)} skipped")
        for item in copied[:10]:
            print(f"  - {item.get('original_path')} -> {item.get('source_path')}")

    if plan := result.get("compile_plan"):
        sources = plan.get("sources") or []
        skipped = plan.get("skipped") or []
        status = plan.get("status", "unknown")
        print(f"\nCompile plan: {status} ({len(sources)} source(s), {len(skipped)} skipped)")
        proposal = plan.get("proposal") or {}
        if proposal.get("suggested_title"):
            print(f"  Suggested title: {proposal.get('suggested_title')}")
        if proposal.get("suggested_note_type"):
            print(f"  Suggested type: {proposal.get('suggested_note_type')}")
        if plan.get("proposal_ref"):
            print(f"  Ref: {plan.get('proposal_ref')}")


def _print_human(result, *, op: str | None = None) -> None:
    specialized_op = op in {"adopt", "adopt_vault", "review_memory", "triage_memory"}
    if (
        specialized_op
        and isinstance(result, dict)
        and result.get("ok") is True
        and result.get("status") == "committed"
        and result.get("mutated") is True
    ):
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict):
            result = diagnostics
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return
    if op in {"adopt", "adopt_vault"} and isinstance(result, dict):
        _print_adopt_human(result)
        return
    if op == "review_memory" and isinstance(result, dict) and "items" in result:
        _print_review_human(result)
        return
    if op == "triage_memory" and isinstance(result, dict):
        _print_triage_human(result)
        return
    if (
        isinstance(result, dict)
        and isinstance(result.get("hits"), list)
        and isinstance(result.get("referents"), dict)
        and not (
            set(result)
            - {"hits", "referents", "timings", "pack", "warming", "degraded"}
        )
    ):
        _print_human(result["hits"], op=op)
        referents = result["referents"]
        resolved = ", ".join(
            str(item.get("title") or item.get("path") or "")
            for item in referents.get("resolved") or []
            if isinstance(item, dict)
        )
        unresolved = referents.get("unresolved_count")
        if not isinstance(unresolved, int):
            unresolved = 0
        print(
            f"referents: {referents.get('status', 'unresolved')}; "
            f"resolved: {resolved or '(none)'}; unresolved: {unresolved}"
        )
        return
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


def _print_review_human(result: dict) -> None:
    items = result.get("items") or []
    states = result.get("state_summary") or {}
    all_total = result.get("all_total", result.get("total", len(items)))
    print("Epistemic Inbox")
    print(
        "  "
        f"{result.get('shown', len(items))} shown, {result.get('total', len(items))} in view, "
        f"{all_total} total"
    )
    hidden = []
    for name in ("snoozed", "dismissed", "competing"):
        if states.get(name):
            hidden.append(f"{states[name]} {name}")
    if hidden:
        print(f"  Hidden: {', '.join(hidden)}")
    if not items:
        print("\nNothing needs attention in this view.")
        return

    for index, item in enumerate(items, start=1):
        categories = ", ".join(
            str(category).replace("_", " ") for category in item.get("categories") or []
        )
        severity = str(item.get("severity") or "info").upper()
        print(f"\n{index}. [{severity}] {categories}")
        print(f"   {item.get('path')}")
        reasons = item.get("reasons") or []
        if reasons:
            print(f"   {reasons[0].get('detail', '')}")
            if len(reasons) > 1:
                print(f"   + {len(reasons) - 1} additional reason(s)")
        print(f"   {item.get('ref')}")
    if result.get("note"):
        print(f"\n{result['note']}")


def _print_triage_human(result: dict) -> None:
    if result.get("family"):
        print(f"Signal family {result['family']} set to {result.get('disposition')}")
        if result.get("reason"):
            print(f"  reason: {result['reason']}")
        if result.get("why"):
            print(f"  {result['why']}")
        if result.get("ref"):
            print(f"  {result['ref']}")
        return
    print(f"Review item {result.get('state', 'updated')}")
    if result.get("path"):
        print(f"  {result['path']}")
    if result.get("ref"):
        print(f"  {result['ref']}")


def _core_op_main(argv: list[str]) -> int:
    from . import cli_ops, product_invoke
    from . import commands as commands_module

    if "EXOMEM_AUTHORIZATION_SESSION_CREDENTIAL" in os.environ or any(
        item == "--authorization-session-credential"
        or item.startswith("--authorization-session-credential=")
        for item in argv
    ):
        error = {
            "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
            "message": "authorization session is unavailable",
            "remediation": None,
        }
        if "--json" in argv:
            print(json.dumps(cli_ops.envelope(False, error=error)))
        else:
            print(
                "Error [AUTHORIZATION_SESSION_UNAVAILABLE]: "
                "authorization session is unavailable",
                file=sys.stderr,
            )
        return 1

    from .governance import authorization_transport

    fd_values: list[str] = []
    for index, item in enumerate(argv):
        if item == "--authorization-session-fd":
            if index + 1 >= len(argv):
                fd_values.append("")
            else:
                fd_values.append(argv[index + 1])
        elif item.startswith("--authorization-session-fd="):
            fd_values.append(item.partition("=")[2])
    if not fd_values:
        authorization_carrier = authorization_transport.CredentialCarrier.absent()
    elif len(fd_values) != 1:
        authorization_carrier = authorization_transport.CredentialCarrier.invalid()
    else:
        authorization_carrier = authorization_transport.read_cli_authorization_fd(
            fd_values[0]
        )
    if authorization_carrier.is_invalid:
        error = {
            "code": "AUTHORIZATION_SESSION_UNAVAILABLE",
            "message": "authorization session is unavailable",
            "remediation": None,
        }
        if "--json" in argv:
            print(json.dumps(cli_ops.envelope(False, error=error)))
        else:
            print(
                "Error [AUTHORIZATION_SESSION_UNAVAILABLE]: "
                "authorization session is unavailable",
                file=sys.stderr,
            )
        return 1

    expose_tier2 = _expose_tier2()
    registered_commands = commands_module.product_commands_for("cli", expose_tier2=expose_tier2)
    cmds = {command.name: command for command in registered_commands}

    preverified_root = None
    preverified_admission = None
    if not authorization_carrier.is_absent and argv and argv[0] in cmds:
        try:
            preverified_root, preverified_admission = (
                product_invoke.verify_local_authorization_transport(
                    cmds[argv[0]],
                    raw_for_vault={},
                    surface="cli",
                    authorization_carrier=authorization_carrier,
                )
            )
        except (ValueError, TypeError, RuntimeError) as error:
            err = cli_ops.error_dict(error)
            if "--json" in argv:
                print(json.dumps(cli_ops.envelope(False, error=err), default=str))
            else:
                print(f"Error [{err['code']}]: {err['message']}", file=sys.stderr)
            return 1

    parser = _CLIParser(prog="kb", description=f"Query and write the local {kb_dirname()}.")
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
        sp.add_argument(
            "--authorization-session-fd",
            dest="authorization_session_fd",
            default=None,
            metavar="FD|-",
            help=(
                "read one authorization-session bearer from an already-open "
                "protected descriptor or stdin (-)"
            ),
        )
        _add_command_args(sp, cmd)

    args = parser.parse_args(argv)
    cmd = cmds[args.op]
    as_json = getattr(args, "json", False)

    try:
        raw = _collect_raw_args(cmd, args, parser)
        if preverified_admission is None:
            root, principal = product_invoke.prepare_local_authorization(
                cmd,
                raw,
                surface="cli",
                authorization_session_fd=getattr(
                    args, "authorization_session_fd", None
                ),
                authorization_carrier=authorization_carrier,
            )
        else:
            root = product_invoke.resolve_vault_for(
                cmd.name,
                raw,
                preverified_root,
            )
            principal = product_invoke.enforce_local_authorization_route(
                cmd,
                raw,
                preverified_admission,
            )
        if cmd.name == "edit_memory":
            kwargs = _normalize_cli_edit(cmd, raw, cli_ops)
        else:
            kwargs = cli_ops.coerce(
                cmd.params, raw, guarded_fields=cmd.guarded_fields, tool=cmd.name, cli=True
            )
        # The invocation itself — coercion aside — is the shared CLI-family
        # seam (`product_invoke`), the same code path the terminal UI drives.
        result = product_invoke.invoke_prepared(
            cmd,
            kwargs,
            vault_root=root,
            principal=principal,
            expose_tier2=expose_tier2,
            idempotency_key=os.environ.get("EXOMEM_IDEMPOTENCY_KEY") or None,
        )
    except (cli_ops.OpError, ValueError, TypeError, RuntimeError) as e:
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
        _print_human(result, op=cmd.name)
    return 1 if isinstance(result, dict) and result.get("strict_failed") else 0


if __name__ == "__main__":
    sys.exit(main())
