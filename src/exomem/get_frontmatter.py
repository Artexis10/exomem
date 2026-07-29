"""The `get_frontmatter` Tier 2 op: read just the frontmatter of a file.

Lightweight counterpart to `get` for scans — load N pages' frontmatter
without dragging in their bodies. Reads anywhere under vault root.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .get_page import GetError, PreparedPageRead, prepare_page_read
from .vault import parse_frontmatter

log = logging.getLogger(__name__)


@dataclass
class GetFrontmatterResult:
    path: str
    frontmatter: dict[str, Any]
    has_frontmatter: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "frontmatter": self.frontmatter,
            "has_frontmatter": self.has_frontmatter,
        }


@dataclass
class GetFrontmatterError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def get_frontmatter(
    vault_root: Path,
    *,
    path: str,
    _prepared: PreparedPageRead | None = None,
) -> GetFrontmatterResult:
    try:
        prepared = _prepared or prepare_page_read(vault_root, path=path)
    except GetError as e:
        raise GetFrontmatterError(code=e.code, reason=e.reason) from e

    try:
        text = prepared.target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise GetFrontmatterError(
            code="UNREADABLE", reason=f"could not read {prepared.path}: {e}"
        ) from e

    fm, _body, fm_text = parse_frontmatter(text)
    return GetFrontmatterResult(
        path=prepared.path,
        frontmatter=fm,
        has_frontmatter=fm_text is not None,
    )
