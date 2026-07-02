"""dedupe-get-payload (BREAKING): get's default response drops raw
`content` (it duplicated frontmatter+body); `include_raw=true` restores it;
the content_hash drift-guard loop is untouched."""

from __future__ import annotations

from pathlib import Path

from exomem import commands
from exomem.vault import content_hash


def _page(vault: Path) -> str:
    p = vault / "Knowledge Base" / "Notes" / "get-payload-probe.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: research-note\nproject: project-alpha\n---\n\n"
        "# Get payload probe\n\nbody text here\n",
        encoding="utf-8",
    )
    return "Knowledge Base/Notes/get-payload-probe.md"


def test_default_get_has_no_content_key(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel)
    assert "content" not in out
    assert set(out) >= {"path", "frontmatter", "body", "content_hash", "mtime"}
    assert out["body"].startswith("# Get payload probe")


def test_include_raw_returns_disk_bytes(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, include_raw=True)
    assert out["content"] == (vault / rel).read_text(encoding="utf-8")
    assert out["content_hash"] == content_hash(out["content"])


def test_drift_guard_roundtrip_without_content(vault: Path) -> None:
    """edit(expected_hash=get().content_hash) still works — the hash is
    server-computed over raw bytes; callers never need `content`."""
    rel = _page(vault)
    got = commands.op_get(vault, path=rel)
    edited = commands.op_edit(
        vault, path=rel,
        new_body=got["body"] + "\nappended line\n",
        expected_hash=got["content_hash"],
        why="payload dedup roundtrip test",
    )
    assert "appended line" in (vault / rel).read_text(encoding="utf-8")
    assert edited


def test_frontmatter_only_unaffected(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, frontmatter_only=True)
    assert "content" not in out
    assert out["frontmatter"]["type"] == "research-note"


def test_history_and_links_compose(vault: Path) -> None:
    rel = _page(vault)
    out = commands.op_get(vault, path=rel, include_history=True, links=True)
    assert "content" not in out
    assert "history" in out and "links" in out
