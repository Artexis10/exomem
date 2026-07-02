"""Set a single KEY=VALUE line in .env, idempotently.

A tiny helper for flipping non-secret feature flags (e.g. EXOMEM_VISION_CAPTION)
without hand-editing .env. Replaces the line in place if the key already exists,
otherwise appends it on a clean line. Restart the service to load the change.

    uv run --no-sync python scripts/set-env.py EXOMEM_VISION_CAPTION 1

For generated secrets use the dedicated helpers (set-rest-key.py,
set-upload-token.py) instead — this one writes the literal value you pass.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", help="env var name, e.g. EXOMEM_VISION_CAPTION")
    ap.add_argument("value", help="value to set, e.g. 1")
    args = ap.parse_args()

    key = args.key.strip()
    if not _KEY_RE.match(key):
        raise SystemExit(f"refusing: {key!r} is not a valid env-var name (A-Z, 0-9, _).")
    value = args.value.strip()

    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    prefix = f"{key}="
    present = any(line.strip().startswith(prefix) for line in text.splitlines())

    if present:  # replace in place
        lines = [
            f"{key}={value}" if line.strip().startswith(prefix) else line
            for line in text.splitlines()
        ]
        ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
        action = "updated"
    else:  # append on a clean line
        with ENV.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write(f"{key}={value}\n")
        action = "set"

    try:
        ENV.chmod(0o600)
    except OSError:
        pass

    print(f"{action} {key}={value}  (restart the service to load it)")


if __name__ == "__main__":
    main()
