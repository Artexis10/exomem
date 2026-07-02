"""Generate EXOMEM_UPLOAD_TOKEN and write it into .env (idempotent).

The /upload endpoint is OFF (returns 503) until this token is set — it's the
sole credential for out-of-band binary uploads. Run once:

    uv run python scripts/set-upload-token.py

Re-run with --force to rotate the token (replaces the existing line).
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
KEY = "EXOMEM_UPLOAD_TOKEN"


def _is_set(text: str) -> bool:
    for line in text.splitlines():
        if line.strip().startswith(f"{KEY}=") and line.split("=", 1)[1].strip():
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rotate even if already set")
    args = ap.parse_args()

    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    if _is_set(text) and not args.force:
        print(f"{KEY} already set; pass --force to rotate. No change made.")
        return

    token = secrets.token_hex(32)  # 256-bit, hex
    if _is_set(text):  # --force: replace in place
        lines = [
            f"{KEY}={token}" if line.strip().startswith(f"{KEY}=") else line
            for line in text.splitlines()
        ]
        ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:  # append on a clean line
        with ENV.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write(f"{KEY}={token}\n")

    # .env holds credentials — keep it owner-only. Real protection on POSIX
    # (friend/Linux deploys); a harmless near-no-op on single-user Windows.
    try:
        ENV.chmod(0o600)
    except OSError:
        pass

    print(f"set {KEY}={token[:6]}...{token[-4:]}  (restart the service to load it)")


if __name__ == "__main__":
    main()
