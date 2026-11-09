"""Filings of identical bytes, built by hand, because every storing door now reuses an earlier identical filing.

The duplicate tests need the vault as it stood before that reuse existed (or as an outside copy left it).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from pathlib import Path

from exomem import lexstore
from exomem import preserve as preserve_module


def evidence_filing(
    vault: Path, scope: str, category: str, filename: str, data: bytes, *, protected: bool = False, filed: dt.date | None = None
) -> str:
    """One Evidence page with its original beside it, through the low-level writer that never reuses. Returns the page path."""
    return preserve_module.preserve_bytes(
        vault, scope=scope, category=category, filename=filename, data=data, raw_protection=protected, today=filed
    ).sidecar_path


def source_filing(
    vault: Path,
    name: str,
    text: str,
    *,
    original: bytes | None = None,
    captured: str = "2026-01-01",
    url: str | None = None,
    imported_from: str | None = None,
) -> str:
    """A Source page of the shape `add` writes, with its original beside it when `original` is given. Returns the page path."""
    folder = vault / "Knowledge Base" / "Sources" / "Session"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"captured: {captured}"]
    if original is not None:
        (folder / f"{name}.bin").write_bytes(original)
        lines += [
            f"evidence_file: Knowledge Base/Sources/Session/{name}.bin",
            f"binary_sha256: {hashlib.sha256(original).hexdigest()}",
        ]
    if url is not None:
        lines.append(f"url: {url}")
    if imported_from is not None:
        lines.append(f"imported_from: {imported_from}")
    (folder / f"{name}.md").write_text(
        f"---\ntype: source\nexomem_id: {uuid.uuid4()}\ntitle: {name}\nsource_type: session\n"
        + "\n".join(lines)
        + f"\ningested_into: []\ntags: []\n---\n\n# {name}\n\n## Capture\n\n{text}\n",
        encoding="utf-8",
    )
    lexstore.record_payload_commit(vault, [f"Knowledge Base/Sources/Session/{name}.md"])
    return f"Knowledge Base/Sources/Session/{name}.md"
