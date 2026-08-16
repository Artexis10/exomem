"""The governed-folder name inside a vault — configurable via ``EXOMEM_KB_DIRNAME``.

Defaults to ``"Knowledge Base"``. This is the single source of truth for the KB
subtree name so it isn't hardcoded across the codebase. Read from the environment on
each call (the same way ``vault.resolve_vault`` reads ``EXOMEM_VAULT_PATH``), so it is
per-process and test-overridable — set ``EXOMEM_KB_DIRNAME`` and the whole engine
resolves, indexes, and wikilinks against that folder name instead.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT = "Knowledge Base"


def kb_dirname() -> str:
    """The KB folder name, no slashes (``EXOMEM_KB_DIRNAME`` override, else default)."""
    name = os.environ.get("EXOMEM_KB_DIRNAME", "").strip().strip("/")
    return name or _DEFAULT


def kb_prefix() -> str:
    """The KB folder name with a trailing slash, for prefix ops (e.g. ``"Knowledge Base/"``)."""
    return kb_dirname() + "/"


def kb_relative_form(path: str) -> str:
    """The KB-relative rel-form a page write leaf will use for ``path``.

    The single source of truth for how caller text becomes a vault-relative
    page path: separators normalised, leading separators dropped, and the KB
    prefix supplied when the caller omitted it (which the tool descriptions
    explicitly invite). ``edit._resolve`` and ``replace._resolve_kb_path`` both
    call it, and so does the hosted protected-tree guard.

    That last caller is the reason this lives here rather than in either leaf.
    A guard that decides "is this target inside a protected tree?" and an
    executor that decides "which file do I open?" must derive the same
    rel-form, or the guard is answering about a path the executor never
    touches. Three separate bypasses of the hosted guard traced to exactly that
    disagreement, the last one because the guard joined at the *vault* root
    while this joins at the *KB* root. Copying these four lines into the guard
    would be the same defect with a longer fuse, so there is one function and
    every caller uses it.
    """

    rel = path.strip().replace("\\", "/").lstrip("/")
    if not rel.startswith(kb_prefix()):
        rel = kb_prefix() + rel
    return rel


def kb_page_target(vault_root: Path, path: str) -> tuple[Path, str]:
    """Return ``(absolute candidate, rel-form)`` for a caller-supplied page path.

    The Markdown suffix is supplied when absent, matching what the write leaves
    do before they open the file. No filesystem access and no validation: the
    caller still has to decide whether the result exists, escapes the KB, or is
    allowed. This answers only "which file does this text name?".
    """

    rel = kb_relative_form(path)
    if not rel.endswith(".md"):
        rel = rel + ".md"
    return Path(vault_root) / rel, rel
