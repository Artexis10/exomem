#!/usr/bin/env python3
"""One-time SessionStart notice that the plugin's server was renamed and needs a new sign-in.

Claude Code stores an HTTP server's sign-in under the plugin and server names, so the rename to
`evermatter` signs each user of the plugin's URL out once. This hook says so once per install.

It cannot learn the sign-in state: Claude Code keeps it in a per-OS credential store that a hook
should not read, and a network probe would put a call on the session-start path. So the marker
is the proof that it has spoken, and it speaks only when the plugin has a URL to sign in to.

It depends only on the Python standard library and makes no network call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: The step Claude Code offers for a server that needs a sign-in. `client_config.SIGN_IN_STEPS`
#: holds the same text for `evermatter setup --migrate`; this hook cannot import the package, and
#: `tests/test_plugin_sync.py` fails when the two differ.
SIGN_IN_STEP = "run /mcp in Claude Code and choose evermatter"
MARKER = "sign-in-notice-shown"
# One text for a renamed install, whose sign-in the rename dropped, and a new one, which never had it.
MESSAGE = (
    "Evermatter's server in this plugin is now named `evermatter`. "
    f"If Evermatter shows as not connected, {SIGN_IN_STEP}, then sign in."
)


def notice(environ: dict[str, str]) -> str | None:
    """Return the notice to show now, or None. Writes the marker when it returns the notice."""
    # Claude Code reserves the `sdk` prefix for its SDK entry points (`sdk-cli` is `-p`), so a prefix test is sound.
    # Wrong firing costs little: an SDK-built interface never shows this notice, but `/mcp` still says it needs sign-in.
    if environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-"):
        return None  # No one reads a headless run's message, and it must not use up the marker.
    if not environ.get("CLAUDE_PLUGIN_OPTION_MCP_URL", "").strip():
        return None  # No URL: the plugin's server is inert and has no sign-in to renew.
    data = environ.get("CLAUDE_PLUGIN_DATA", "")
    if not data:
        return None  # No place to remember it, and a notice that repeats every session is worse.
    marker = Path(data) / MARKER
    if marker.exists():
        return None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown\n", encoding="utf-8")
    except OSError:
        return None
    return MESSAGE


def main() -> int:
    try:
        message = notice(dict(os.environ))
        if message:
            sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    except Exception:  # noqa: BLE001 - a notice must never break session start
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
