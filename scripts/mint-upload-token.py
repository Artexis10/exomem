"""Mint a short-lived /upload token to paste into a claude.ai web chat.

The long-lived EXOMEM_UPLOAD_TOKEN should never land in a chat transcript. This
derives a token from it that expires in ~15 min, so the claude.ai web sandbox can
curl on-disk files to /upload without seeing the real secret. The secret is read
but NEVER printed — only the short-lived token is.

    uv run python scripts/mint-upload-token.py             # 15 min
    uv run python scripts/mint-upload-token.py --ttl 1800  # 30 min
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from exomem import upload_tokens

ENV = Path(__file__).resolve().parents[1] / ".env"
KEY = "EXOMEM_UPLOAD_TOKEN"


def _secret() -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{KEY}="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise SystemExit(f"{KEY} not set in .env — run scripts/set-upload-token.py first")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl", type=int, default=upload_tokens.DEFAULT_TTL, help="lifetime in seconds (default 900)")
    args = ap.parse_args()
    token = upload_tokens.mint(_secret(), ttl=args.ttl)
    expires = dt.datetime.now() + dt.timedelta(seconds=args.ttl)
    print(token)
    print(
        f"# expires ~{expires.strftime('%H:%M:%S')} ({args.ttl}s). Paste the line above "
        "as the upload bearer token; it can't be reused after it expires."
    )


if __name__ == "__main__":
    main()
