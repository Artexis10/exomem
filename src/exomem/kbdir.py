"""The governed-folder name inside a vault — configurable via ``EXOMEM_KB_DIRNAME``.

Defaults to ``"Knowledge Base"``. This is the single source of truth for the KB
subtree name so it isn't hardcoded across the codebase. Read from the environment on
each call (the same way ``vault.resolve_vault`` reads ``EXOMEM_VAULT_PATH``), so it is
per-process and test-overridable — set ``EXOMEM_KB_DIRNAME`` and the whole engine
resolves, indexes, and wikilinks against that folder name instead.
"""

from __future__ import annotations

import os

_DEFAULT = "Knowledge Base"


def kb_dirname() -> str:
    """The KB folder name, no slashes (``EXOMEM_KB_DIRNAME`` override, else default)."""
    name = os.environ.get("EXOMEM_KB_DIRNAME", "").strip().strip("/")
    return name or _DEFAULT


def kb_prefix() -> str:
    """The KB folder name with a trailing slash, for prefix ops (e.g. ``"Knowledge Base/"``)."""
    return kb_dirname() + "/"
