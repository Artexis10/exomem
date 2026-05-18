"""The `get` MCP tool: read a full vault file by path.

Read-only. The ergonomic counterpart to `find` (which returns excerpts) —
when Claude finds a page via `find` and wants to read/cite/build on it,
`get` returns the full frontmatter + body.

Path is vault-relative. The leading `Knowledge Base/` and trailing `.md`
are both optional (tolerated either way).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from .vault import kb_root


log = logging.getLogger(__name__)


@dataclass
class GetResult:
    path: str           # vault-relative, with .md, normalized
    frontmatter: dict
    body: str           # markdown body without the frontmatter delimiters
    content: str        # full raw file (frontmatter delimiters + body)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "content": self.content,
        }


@dataclass
class GetError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def get_page(vault_root: Path, *, path: str) -> GetResult:
    """Read a file in the Knowledge Base.

    Accepts `"Knowledge Base/Notes/Insights/foo.md"`, `"Notes/Insights/foo.md"`,
    or the same with the `.md` stripped.
    """
    if not path or not path.strip():
        raise GetError(code="INVALID_PATH", reason="path is empty")

    rel = path.strip().replace("\\", "/").lstrip("/")
    if not rel.startswith("Knowledge Base/"):
        rel = "Knowledge Base/" + rel
    if not rel.endswith(".md"):
        rel = rel + ".md"

    candidate = vault_root / rel
    # Path-escape guard: resolved path must be under vault_root.
    try:
        resolved = candidate.resolve()
        resolved.relative_to(vault_root.resolve())
    except (ValueError, OSError) as e:
        raise GetError(
            code="INVALID_PATH",
            reason=f"path escapes vault or is unreadable: {e}",
        ) from None

    # And must be under Knowledge Base/ specifically (no peeking at sibling trees).
    try:
        resolved.relative_to(kb_root(vault_root).resolve())
    except ValueError:
        raise GetError(
            code="INVALID_PATH",
            reason=(
                f"path {path!r} resolves outside Knowledge Base/. "
                "Only files under Knowledge Base/ are readable."
            ),
        ) from None

    if not candidate.exists() or not candidate.is_file():
        raise GetError(
            code="NOT_FOUND",
            reason=f"file does not exist: {rel}",
        )

    try:
        mtime = candidate.stat().st_mtime
    except OSError as e:
        raise GetError(code="UNREADABLE", reason=str(e)) from e

    parsed = find_module._parse_page(candidate, mtime)
    if parsed is None:
        raise GetError(
            code="UNREADABLE",
            reason=f"could not parse {rel} as a markdown file with frontmatter",
        )

    content = candidate.read_text(encoding="utf-8")
    return GetResult(
        path=rel,
        frontmatter=parsed.frontmatter,
        body=parsed.body,
        content=content,
    )
