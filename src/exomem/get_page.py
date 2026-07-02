"""The `get` MCP tool: read a full vault file by path.

Read-only. The ergonomic counterpart to `find` (which returns excerpts) —
when Claude finds a page via `find` and wants to read/cite/build on it,
`get` returns the full frontmatter + body.

Path is vault-relative. Reads anywhere under the vault root:
- `Knowledge Base/...` — the compiled KB layer
- sibling top-level folders (e.g. `Reference/...`) — curated, hand-authored
  material that compiled notes link to, kept read-only via `_access.yaml`.
  `get` honors that by only reading.

The trailing `.md` is optional. Bare-name shortcuts (`Notes/Insights/foo`)
auto-prepend `Knowledge Base/` if the literal path doesn't exist — back-
compat with how this tool worked before the broadening.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from .vault import content_hash


log = logging.getLogger(__name__)


@dataclass
class GetResult:
    path: str           # vault-relative, with .md, normalized
    frontmatter: dict
    body: str           # markdown body without the frontmatter delimiters
    content: str        # full raw file (frontmatter delimiters + body)
    content_hash: str   # sha256 of `content` — echo to edit(expected_hash=...)
    mtime: float        # file mtime (advisory; hash is the real guard)

    def as_dict(self, include_raw: bool = False) -> dict:
        """Wire shape. `content` (the raw file text) ships only on request —
        it duplicates `body` + `frontmatter`, roughly doubling the payload of
        every read for a field the edit drift-guard never needs: the server
        always computes `content_hash` over the raw bytes here, so callers
        echo the hash without ever reconstructing the hashed text."""
        out = {
            "path": self.path,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "content_hash": self.content_hash,
            "mtime": self.mtime,
        }
        if include_raw:
            out["content"] = self.content
        return out


@dataclass
class GetError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def get_page(vault_root: Path, *, path: str) -> GetResult:
    """Read any markdown file under the vault root.

    Accepts any vault-relative path. Examples:
    - `Knowledge Base/Notes/Insights/foo.md`
    - `Notes/Insights/foo` (auto-prepends `Knowledge Base/`, auto-adds `.md`)
    - `Reference/Strategy.md`
    - `Reference/AI Systems & Architecture.md`
    """
    if not path or not path.strip():
        raise GetError(code="INVALID_PATH", reason="path is empty")

    rel = path.strip().replace("\\", "/").lstrip("/")
    # Only auto-append .md if the path has NO extension. Previously this
    # appended unconditionally, which made e.g. `foo.meta.json` resolve to
    # `foo.meta.json.md` and 404 — surfaced when trying to inspect trash
    # sidecars via `get`.
    last_segment = rel.rsplit("/", 1)[-1]
    if "." not in last_segment:
        rel = rel + ".md"

    candidate = vault_root / rel

    # Back-compat shortcut: if the literal path doesn't exist but the same
    # path under Knowledge Base/ does, use that. Lets callers write
    # `Notes/Insights/foo` without the leading prefix.
    if not candidate.exists() and not rel.startswith("Knowledge Base/"):
        kb_rel = "Knowledge Base/" + rel
        kb_candidate = vault_root / kb_rel
        if kb_candidate.exists():
            candidate = kb_candidate
            rel = kb_rel

    # Path-escape guard: resolved path must be under vault_root.
    try:
        resolved = candidate.resolve()
        resolved.relative_to(vault_root.resolve())
    except (ValueError, OSError) as e:
        raise GetError(
            code="INVALID_PATH",
            reason=f"path escapes vault or is unreadable: {e}",
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

    parsed = find_module._parse_page(candidate, mtime, vault_root)
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
        content_hash=content_hash(content),
        mtime=mtime,
    )
