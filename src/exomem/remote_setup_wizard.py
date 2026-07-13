"""`exomem setup --remote` — one-command guided remote-connector onboarding.

Collapses the six sharp manual steps of docs/remote-quickstart.md (pick a
tunnel, generate a signing key, populate `.env`, create the GitHub OAuth App,
verify the live triple, add the connector) into a single interactive,
idempotent wizard, so a non-engineer can expose exomem for claude.ai / iOS.

Every step is a converger: existing `.env` values are read back as defaults and
an already-set `EXOMEM_JWT_SIGNING_KEY` is preserved (rotating it would orphan
the OAuth token store and force every client to re-authorize — see
server.build_server). Re-running is always safe.

The env-var contract is the source of truth in `server.build_server`
(require_auth branch): EXOMEM_BASE_URL, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
EXOMEM_GITHUB_USERNAME, EXOMEM_GITHUB_USER_ID, EXOMEM_JWT_SIGNING_KEY, plus
EXOMEM_VAULT_PATH and matching coordinator credentials when HA is configured.

CLI-only by design: it writes `.env` (secrets), reloads the environment, and
runs doctor's live probes. All side-effect seams (`input_fn`, `print_fn`,
`doctor_fn`, `load_env_fn`, `env_path`) are injectable so tests never write a
real `.env` or touch the network.
"""

from __future__ import annotations

import argparse
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import doctor as doctor_module

# The keys this wizard owns in `.env`. Written/patched in place; every other
# line (comments, unrelated keys) is preserved verbatim by `patch_env`.
_OAUTH_KEYS = (
    "EXOMEM_BASE_URL",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "EXOMEM_GITHUB_USERNAME",
    "EXOMEM_GITHUB_USER_ID",
    "EXOMEM_JWT_SIGNING_KEY",
    "EXOMEM_LEASE_COORDINATOR_TOKEN",
    "EXOMEM_WRITER_LEASE_TOKEN",
    "EXOMEM_OAUTH_STORAGE_TOKEN",
    "EXOMEM_VAULT_PATH",
)

_TUNNELS = ("ngrok", "cloudflare")


# --------------------------------------------------------------------------- #
# Pure helpers — unit-tested directly (no I/O, no network).
# --------------------------------------------------------------------------- #


class BaseUrlError(ValueError):
    """EXOMEM_BASE_URL failed validation (empty / no scheme / trailing slash / /mcp)."""


def generate_signing_key() -> str:
    """A fresh, stable JWT signing key — `secrets.token_urlsafe(48)` (64 chars)."""
    return secrets.token_urlsafe(48)


def generate_storage_credential() -> str:
    """Generate one shared bearer for coordinator, lease, and OAuth state."""
    return secrets.token_urlsafe(48)


def resolve_github_user(username: str) -> dict[str, Any]:
    """Resolve GitHub's immutable numeric user ID for a login."""
    import httpx

    response = httpx.get(
        f"https://api.github.com/users/{username}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=10.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned an invalid user response")
    return payload


def _positive_user_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("GitHub user ID must be a positive integer")
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("GitHub user ID must be a positive integer") from None
    if user_id <= 0:
        raise ValueError("GitHub user ID must be a positive integer")
    return user_id


def validate_base_url(raw: str) -> str:
    """Return `raw` trimmed, or raise BaseUrlError.

    The connector's OAuth discovery and the `resource` metadata are built by
    concatenating `/mcp`, `/auth/callback`, and `/.well-known/...` onto this
    value, so it must be the BARE public origin: an http(s) scheme, no trailing
    slash, and no `/mcp` suffix. A trailing slash or a pasted `/mcp` endpoint
    is the single most common cause of `mcp_registration_failed`.
    """
    url = (raw or "").strip()
    if not url:
        raise BaseUrlError("EXOMEM_BASE_URL is empty — set the public HTTPS origin.")
    if not (url.startswith("https://") or url.startswith("http://")):
        raise BaseUrlError(
            f"EXOMEM_BASE_URL must include the scheme, e.g. https://{url}"
        )
    if url.endswith("/"):
        raise BaseUrlError(
            f"EXOMEM_BASE_URL must not end with a trailing slash: {url!r}. "
            f"Use {url.rstrip('/')!r}."
        )
    if url.endswith("/mcp"):
        raise BaseUrlError(
            f"EXOMEM_BASE_URL must be the bare origin, not the /mcp endpoint: "
            f"{url!r}. Use {url[: -len('/mcp')]!r} (the wizard appends /mcp itself)."
        )
    return url


def connector_url(base_url: str) -> str:
    """The exact URL to paste into claude.ai's Add-custom-connector field."""
    return f"{base_url}/mcp"


def callback_url(base_url: str) -> str:
    """The GitHub OAuth App's Authorization callback URL."""
    return f"{base_url}/auth/callback"


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` text into a {KEY: value} dict (last write wins).

    Ignores blank lines, comments, and lines without `=`. Values are taken
    verbatim after the first `=` (no quote stripping — the wizard never quotes).
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def patch_env(existing: str, updates: dict[str, str]) -> str:
    """Merge `updates` into `.env` text, preserving every unrelated line.

    An existing `KEY=` line is replaced IN PLACE (position, and any surrounding
    comments/blank lines, preserved); a new key is appended. Comments and keys
    the wizard doesn't own are never touched. Always returns text ending in a
    single newline (empty input with no updates returns "").
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in existing.splitlines():
        stripped = line.strip()
        key = (
            stripped.partition("=")[0].strip()
            if stripped and not stripped.startswith("#") and "=" in stripped
            else None
        )
        if key is not None and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    # Append any keys not already present, in the caller's insertion order.
    for key, value in updates.items():
        if key in remaining:
            out.append(f"{key}={value}")
            del remaining[key]
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def render_oauth_fields(base_url: str) -> str:
    """The exact GitHub OAuth App fields to create, callback derived from base_url."""
    lines = [
        "Create a GitHub OAuth App — https://github.com/settings/developers",
        "  → OAuth Apps → New OAuth App — with these EXACT fields:",
        "",
        "    Application name             exomem",
        f"    Homepage URL                 {base_url}",
        f"    Authorization callback URL   {callback_url(base_url)}",
        "",
        "  Register it, then 'Generate a new client secret', and copy the",
        "  Client ID + Client Secret below.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _default_env_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".env"


def _ask(input_fn, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input_fn(f"{prompt}{suffix}: ").strip()
    return answer or default


def _prompt_tunnel(input_fn, print_fn) -> str:
    print_fn("Which public ingress will front this host?")
    print_fn("  1) ngrok       — no domain needed (free static dev domain)")
    print_fn("  2) cloudflare  — recommended if you own a domain")
    while True:
        choice = input_fn("Tunnel [1/2 or ngrok/cloudflare]: ").strip().lower()
        if choice in ("1", "ngrok", "n"):
            return "ngrok"
        if choice in ("2", "cloudflare", "cloudflared", "c"):
            return "cloudflare"
        print_fn("  Please answer 1/ngrok or 2/cloudflare.")


def _prompt_base_url(input_fn, print_fn, default: str) -> str:
    while True:
        raw = _ask(input_fn, "Public HTTPS base URL (no trailing slash, no /mcp)", default)
        try:
            return validate_base_url(raw)
        except BaseUrlError as e:
            print_fn(f"  {e}")


def _load_env(env_path: Path) -> None:
    """Reload `.env` into os.environ so doctor's remote checks see fresh values."""
    from dotenv import load_dotenv

    load_dotenv(env_path, override=True)


def run_remote_setup(
    *,
    vault: str | None = None,
    base_url: str | None = None,
    tunnel: str | None = None,
    github_client_id: str | None = None,
    github_client_secret: str | None = None,
    github_username: str | None = None,
    github_user_id: str | int | None = None,
    yes: bool = False,
    probe: bool = True,
    env_path: Path | None = None,
    input_fn=input,
    print_fn=print,
    doctor_fn=None,
    load_env_fn=None,
    github_user_resolver: Callable[[str], dict[str, Any]] | None = None,
) -> int:
    doctor_fn = doctor_fn or doctor_module.doctor
    load_env_fn = load_env_fn or _load_env
    github_user_resolver = github_user_resolver or resolve_github_user
    env_path = env_path or _default_env_path()

    steps: list[tuple[str, str]] = []

    def report(name: str, status: str) -> None:
        steps.append((name, status))
        print_fn(f"  {name}: {status}")

    def finish() -> int:
        print_fn("")
        print_fn("Summary:")
        for name, status in steps:
            print_fn(f"  {name:<12} {status}")
        return 1 if any("[failed" in s for _, s in steps) else 0

    print_fn("exomem setup --remote")
    print_fn("")

    existing_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    existing = parse_env(existing_text)

    def _existing(key: str) -> str:
        return existing.get(key) or os.environ.get(key, "")

    # 1. Tunnel choice (guidance only — informs the closing next-steps).
    if tunnel is None:
        if yes:
            print_fn("setup --remote: --yes requires --tunnel ngrok|cloudflare.")
            return 2
        tunnel = _prompt_tunnel(input_fn, print_fn)
    tunnel = tunnel.strip().lower()
    if tunnel not in _TUNNELS:
        print_fn(f"setup --remote: unknown tunnel {tunnel!r} (expected ngrok|cloudflare).")
        return 2
    report("tunnel", f"[done] {tunnel}")

    # 2. Vault path — the remote service reads it from .env, nothing else.
    if not vault:
        if yes:
            print_fn("setup --remote: --yes requires --vault.")
            return 2
        vault = _ask(input_fn, "Vault folder", _existing("EXOMEM_VAULT_PATH"))
    if not vault:
        print_fn("setup --remote: a vault path is required.")
        return 2
    vault_path = str(Path(vault).expanduser())
    report("vault", f"[done] {vault_path}")

    # 3. Public base URL — validated (no trailing slash, no /mcp).
    if base_url is None:
        if yes:
            print_fn("setup --remote: --yes requires --base-url.")
            return 2
        base_url = _prompt_base_url(input_fn, print_fn, _existing("EXOMEM_BASE_URL"))
    else:
        try:
            base_url = validate_base_url(base_url)
        except BaseUrlError as e:
            print_fn(f"setup --remote: {e}")
            return 2
    report("base_url", f"[done] {base_url}")

    # 4. Print the exact GitHub OAuth App fields, then collect the credentials.
    print_fn("")
    print_fn(render_oauth_fields(base_url))
    print_fn("")
    if not github_client_id:
        if yes:
            print_fn("setup --remote: --yes requires --github-client-id.")
            return 2
        github_client_id = _ask(input_fn, "GitHub Client ID", _existing("GITHUB_CLIENT_ID"))
    if not github_client_secret:
        if yes:
            print_fn("setup --remote: --yes requires --github-client-secret.")
            return 2
        github_client_secret = _ask(
            input_fn, "GitHub Client Secret", _existing("GITHUB_CLIENT_SECRET")
        )
    if not github_username:
        if yes:
            print_fn("setup --remote: --yes requires --github-username.")
            return 2
        github_username = _ask(
            input_fn, "GitHub username allowed to sign in", _existing("EXOMEM_GITHUB_USERNAME")
        )
    if not (github_client_id and github_client_secret and github_username):
        print_fn("setup --remote: client id, client secret, and GitHub username are all required.")
        return 2
    report("oauth_app", "[done] fields printed + credentials captured")

    # GitHub login names are mutable; pin authorization to GitHub's immutable
    # positive numeric subject. Preserve a prior ID unless an explicit flag
    # confirms the same value. Otherwise resolve it once from GitHub.
    existing_user_id = _existing("EXOMEM_GITHUB_USER_ID").strip()
    try:
        if existing_user_id:
            resolved_user_id = _positive_user_id(existing_user_id)
            if github_user_id is not None and _positive_user_id(github_user_id) != resolved_user_id:
                raise ValueError(
                    "--github-user-id conflicts with existing EXOMEM_GITHUB_USER_ID"
                )
            report("github_id", "[skipped: already set — kept stable]")
        elif github_user_id is not None:
            resolved_user_id = _positive_user_id(github_user_id)
            report("github_id", "[done] supplied explicitly")
        else:
            identity = github_user_resolver(github_username)
            resolved_login = str(identity.get("login") or "").strip().casefold()
            if resolved_login != github_username.strip().casefold():
                raise ValueError(
                    "GitHub resolved a different login; use the canonical login or "
                    "provide --github-user-id for offline setup"
                )
            resolved_user_id = _positive_user_id(identity.get("id"))
            report("github_id", "[done] resolved immutable ID")
    except Exception as error:  # noqa: BLE001 - setup boundary, before any write
        print_fn(f"setup --remote: could not establish GitHub identity: {error}")
        return 2

    # 5. JWT signing key — generate ONCE and keep it (rotating orphans the store).
    signing_key = _existing("EXOMEM_JWT_SIGNING_KEY")
    if signing_key:
        report("signing_key", "[skipped: already set — kept stable]")
    else:
        signing_key = generate_signing_key()
        report("signing_key", "[done] generated")

    # Active/passive auth state and the writer lease share one authenticated
    # coordinator. If HA is configured, converge all three credential views to
    # one value. Reject conflicts before touching the environment file.
    ha_enabled = bool(
        _existing("EXOMEM_WRITER_LEASE_URL").strip()
        or _existing("EXOMEM_OAUTH_STORAGE_URL").strip()
    )
    storage_credential: str | None = None
    if ha_enabled:
        token_keys = (
            "EXOMEM_LEASE_COORDINATOR_TOKEN",
            "EXOMEM_WRITER_LEASE_TOKEN",
            "EXOMEM_OAUTH_STORAGE_TOKEN",
        )
        configured = {value for key in token_keys if (value := _existing(key).strip())}
        if len(configured) > 1:
            print_fn(
                "setup --remote: HA credential conflict; coordinator, writer lease, "
                "and OAuth storage tokens must match. No changes were written."
            )
            return 2
        storage_credential = next(iter(configured), None) or generate_storage_credential()
        report(
            "ha_auth",
            "[skipped: existing matching credential preserved]"
            if configured
            else "[done] generated matching coordinator credential",
        )

    # 6. Patch .env in place, preserving every other line.
    updates = {
        "EXOMEM_VAULT_PATH": vault_path,
        "EXOMEM_BASE_URL": base_url,
        "GITHUB_CLIENT_ID": github_client_id,
        "GITHUB_CLIENT_SECRET": github_client_secret,
        "EXOMEM_GITHUB_USERNAME": github_username,
        "EXOMEM_GITHUB_USER_ID": str(resolved_user_id),
        "EXOMEM_JWT_SIGNING_KEY": signing_key,
    }
    if storage_credential is not None:
        updates.update(
            {
                "EXOMEM_LEASE_COORDINATOR_TOKEN": storage_credential,
                "EXOMEM_WRITER_LEASE_TOKEN": storage_credential,
                "EXOMEM_OAUTH_STORAGE_TOKEN": storage_credential,
            }
        )
    env_path.write_text(patch_env(existing_text, updates), encoding="utf-8")
    try:
        env_path.chmod(0o600)  # secrets — owner-only on POSIX; near-no-op on Windows
    except OSError:
        pass
    report("env", f"[done] wrote {env_path}")

    # 7. Reload the freshly-written .env, then run doctor as a HARD gate.
    load_env_fn(env_path)
    dr = doctor_fn(vault=vault_path, profile="remote", probe=probe)
    if dr.success:
        report("doctor", f"[done] remote preflight passed{'' if probe else ' (offline — no --probe)'}")
    else:
        print_fn("")
        print_fn(doctor_module.render_human(dr))
        report("doctor", "[failed: remote preflight reported failures]")
        print_fn("")
        print_fn(
            "Fix the FAIL rows above and re-run `exomem setup --remote`. The most "
            "common causes: the server isn't running (`exomem --transport http`), or "
            "the tunnel isn't up / forwarding to 127.0.0.1:8765 yet."
        )
        return finish()

    # 8. Success — print the connector URL and tunnel-specific next steps.
    print_fn("")
    print_fn("Ready. Add the connector on claude.ai:")
    print_fn("  Settings → Connectors → Add custom connector →")
    print_fn(f"      {connector_url(base_url)}")
    print_fn(f"  then sign in with the GitHub account: {github_username}")
    print_fn("")
    if tunnel == "ngrok":
        print_fn("ngrok notes:")
        print_fn(
            "  - Free tier caps at ~120 requests/minute. claude.ai's connector "
            "registration sends a burst; if the add stalls, check the ngrok "
            "dashboard for 429s and retry after a minute (or use Cloudflare)."
        )
        print_fn(
            "  - UNVERIFIED: whether that 120 req/min cap survives the OAuth "
            "registration burst has not been load-tested. It is the same failure "
            "class that broke Tailscale Funnel for no-domain users — verify live."
        )
        print_fn(
            "  - A one-time browser interstitial fires on a fresh ngrok session; "
            "open the URL once and click through it before adding the connector."
        )
    else:
        print_fn("Cloudflare notes:")
        print_fn(
            "  - Bring the named tunnel up (scripts/setup-cloudflared.ps1 or "
            "`docker compose --profile cloudflared up -d`) and confirm it forwards "
            "to 127.0.0.1:8765."
        )
    return finish()


def remote_setup_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="exomem setup --remote",
        description=(
            "Guided remote-connector setup: pick a tunnel, generate a stable JWT "
            "signing key, write/patch .env (validating EXOMEM_BASE_URL), print the "
            "exact GitHub OAuth App fields, gate on `doctor --profile remote "
            "--probe`, and print the connector URL. Idempotent; existing .env keys "
            "and an existing signing key are preserved."
        ),
    )
    parser.add_argument("--vault", help="Vault root (default: prompt, or existing .env / $EXOMEM_VAULT_PATH).")
    parser.add_argument("--base-url", dest="base_url", help="Public HTTPS origin, no trailing slash, no /mcp.")
    parser.add_argument("--tunnel", choices=_TUNNELS, help="Ingress you'll front the host with.")
    parser.add_argument("--github-client-id", dest="github_client_id", help="GitHub OAuth App Client ID.")
    parser.add_argument("--github-client-secret", dest="github_client_secret", help="GitHub OAuth App Client Secret.")
    parser.add_argument("--github-username", dest="github_username", help="The single GitHub login allowed to sign in.")
    parser.add_argument(
        "--github-user-id",
        dest="github_user_id",
        help="Immutable positive numeric GitHub user ID (avoids the setup lookup).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Non-interactive; requires --vault, --base-url, --tunnel, and the three --github-* flags.",
    )
    parser.add_argument(
        "--no-probe", dest="probe", action="store_false",
        help="Run doctor's remote env checks WITHOUT the live endpoint probes "
        "(use before the server/tunnel are up; the probe gate is on by default).",
    )
    parser.set_defaults(probe=True)
    args = parser.parse_args(argv)

    missing_for_yes = [
        flag for flag, val in (
            ("--vault", args.vault),
            ("--base-url", args.base_url),
            ("--tunnel", args.tunnel),
            ("--github-client-id", args.github_client_id),
            ("--github-client-secret", args.github_client_secret),
            ("--github-username", args.github_username),
        ) if not val
    ]
    if args.yes and missing_for_yes:
        parser.error(f"--yes requires {', '.join(missing_for_yes)}")

    return run_remote_setup(
        vault=args.vault,
        base_url=args.base_url,
        tunnel=args.tunnel,
        github_client_id=args.github_client_id,
        github_client_secret=args.github_client_secret,
        github_username=args.github_username,
        github_user_id=args.github_user_id,
        yes=args.yes,
        probe=args.probe,
    )
