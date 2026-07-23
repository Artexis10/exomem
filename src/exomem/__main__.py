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
- `doctor` — read-only local install/setup preflight
- `auth sessions|revoke` — operator-only durable MCP session administration
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


def main(argv: list[str] | None = None) -> int:
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
    introduced = _configure_local_search_capabilities(raw[0] if raw else None)
    try:
        return _dispatch_main(raw)
    finally:
        for name in introduced:
            os.environ.pop(name, None)


def _dispatch_main(raw: list[str]) -> int:
    if raw and raw[0] == "hosted":
        from .hosted_operator import main as hosted_operator_main

        return hosted_operator_main(raw[1:])
    if raw and raw[0] == "setup":
        from .setup_wizard import setup_main

        return setup_main(raw[1:])
    if raw and raw[0] == "init":
        return _init_main(raw[1:])
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

        index_sync.clear_deferred_work(vault_root)
    print(json.dumps(stats))
    return 0


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
            print(f"  bulk_gpu:                {policy['bulk_gpu']}")
            print(f"  config: {policy['config_path']}")
        return 0

    try:
        path = mode_mod.write_mode(args.mode)
    except ValueError as e:
        print(f"mode: {e}", file=sys.stderr)
        return 2
    print(f"Compute mode set to '{mode_mod.normalize(args.mode)}'  ({path})")
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

    from . import embeddings

    failed = False
    missing_extra = False

    def _step(label: str, fn) -> None:
        nonlocal failed, missing_extra
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  {label}: ready ({time.perf_counter() - t0:.1f}s)")
        except ImportError as e:
            # A lean install has no ML stack — that's a missing extra, not a
            # download problem, and the remediation must say so.
            failed = True
            missing_extra = True
            print(f"  {label}: FAILED ({e})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — report every model, then exit non-zero
            failed = True
            print(f"  {label}: FAILED ({e})", file=sys.stderr)

    print("exomem warm — downloading/loading search models (first run can take minutes)")
    _step(f"embedding model {embeddings.MODEL_NAME}", embeddings.get_model)
    _step(f"reranker {embeddings.RERANKER_NAME}", embeddings.get_reranker)
    if embeddings.clip_enabled():
        _step(f"CLIP model {embeddings.CLIP_MODEL_NAME}", embeddings.get_clip_model)
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
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    args = parser.parse_args(argv)

    from . import install_hook as hook_module

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
    parser.add_argument("--source-type", default="other", help="source type for --as source")
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
            "--source-type",
            args.source_type,
            "--title",
            args.title,
        ]
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


def _simple_review_main(argv: list[str]) -> int:
    triage_actions = {"dismiss", "snooze", "reopen"}
    if argv and argv[0] in triage_actions:
        action = argv[0]
        parser = argparse.ArgumentParser(
            prog=f"exomem review {action}",
            description=f"{action.title()} one Epistemic Inbox item.",
        )
        parser.add_argument("ref", help="stable exomem://review/<id> reference")
        if action == "snooze":
            parser.add_argument("--until", required=True, help="snooze through YYYY-MM-DD")
        parser.add_argument("--why", help="optional review rationale")
        parser.add_argument("--json", action="store_true", help="emit the shared JSON envelope")
        args = parser.parse_args(argv[1:])
        core = ["triage_memory", args.ref, "--action", action]
        if action == "snooze":
            core.extend(["--until", args.until])
        if args.why:
            core.extend(["--why", args.why])
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
        choices=("open", "all", "snoozed", "dismissed"),
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

        for name in sorted(LEGACY_EDIT_FIELDS):
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
    for name in ("snoozed", "dismissed"):
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
    print(f"Review item {result.get('state', 'updated')}")
    if result.get("path"):
        print(f"  {result['path']}")
    if result.get("ref"):
        print(f"  {result['ref']}")


def _core_op_main(argv: list[str]) -> int:
    from . import capabilities, cli_ops
    from . import commands as commands_module
    from . import schema as schema_module
    from .vault import resolve_vault

    expose_tier2 = _expose_tier2()
    registered_commands = commands_module.product_commands_for("cli", expose_tier2=expose_tier2)
    cmds = {command.name: command for command in registered_commands}
    surface_descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="cli",
        profile="product",
        tier2_enabled=expose_tier2,
        product_commands=tuple(command.name for command in registered_commands),
        exported_aliases=commands_module.simple_action_names(),
    )

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
        _add_command_args(sp, cmd)

    args = parser.parse_args(argv)
    cmd = cmds[args.op]
    as_json = getattr(args, "json", False)

    try:
        raw = _collect_raw_args(cmd, args, parser)
        if cmd.name == "edit_memory":
            kwargs = _normalize_cli_edit(cmd, raw, cli_ops)
        else:
            kwargs = cli_ops.coerce(
                cmd.params, raw, guarded_fields=cmd.guarded_fields, tool=cmd.name, cli=True
            )
        vault_root = _resolve_core_op_vault(cmd.name, kwargs, resolve_vault)
        if cmd.needs_schema:
            injected = (vault_root, schema_module.load_source_schema(vault_root))
        else:
            injected = (vault_root,)
        from .writer_lease import invoke_command

        with capabilities.active_surface(surface_descriptor):
            result = invoke_command(
                cmd,
                *injected,
                idempotency_key=os.environ.get("EXOMEM_IDEMPOTENCY_KEY") or None,
                **kwargs,
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


def _resolve_core_op_vault(op: str, kwargs: dict, resolve_vault_func) -> Path:
    """Resolve the CLI vault root, allowing read-only first-run scans pre-init."""
    try:
        return resolve_vault_func()
    except RuntimeError:
        if not _core_op_allows_uninitialized_vault(op, kwargs):
            raise
        override = os.environ.get("EXOMEM_VAULT_PATH")
        if not override:
            raise
        path = Path(override)
        if not path.is_dir():
            raise
        return path


def _core_op_allows_uninitialized_vault(op: str, kwargs: dict) -> bool:
    if op == "browse_memory":
        return True
    if op == "adopt_vault":
        return (kwargs.get("mode") or "scan-only") == "scan-only"
    return False


if __name__ == "__main__":
    sys.exit(main())
