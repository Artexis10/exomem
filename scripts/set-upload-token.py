"""Generate EVERMATTER_UPLOAD_TOKEN and write it into .env (idempotent).

The /upload endpoint is OFF (returns 503) until this token is set — it's the
sole credential for out-of-band binary uploads. Run once:

    uv run python scripts/set-upload-token.py

Re-run with --force to rotate the token (replaces the existing line). The line is
written as `EVERMATTER_UPLOAD_TOKEN`; an `EXOMEM_UPLOAD_TOKEN` line is replaced by it.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from exomem import env_compat
from exomem.remote_setup_wizard import parse_env, patch_env

ENV = Path(__file__).resolve().parents[1] / ".env"
KEY = "EXOMEM_UPLOAD_TOKEN"  # the internal name; patch_env writes it as EVERMATTER_UPLOAD_TOKEN


def _is_set(text: str) -> bool:
    return bool(env_compat.rename_settings(parse_env(text))[0].get(KEY, "").strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rotate even if already set")
    args = ap.parse_args()

    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    written = env_compat.emitted_name(KEY)
    if _is_set(text) and not args.force:
        print(f"{written} already set; pass --force to rotate. No change made.")
        return

    token = secrets.token_hex(32)  # 256-bit, hex
    ENV.write_text(patch_env(text, {KEY: token}), encoding="utf-8")

    # .env holds credentials — keep it owner-only. Real protection on POSIX
    # (friend/Linux deploys); a harmless near-no-op on single-user Windows.
    try:
        ENV.chmod(0o600)
    except OSError:
        pass

    print(f"set {written}={token[:6]}...{token[-4:]}  (restart the service to load it)")


if __name__ == "__main__":
    main()
