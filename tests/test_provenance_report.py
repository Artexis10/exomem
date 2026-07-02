"""provenance_report: read-only scan of `<!-- key:value -->` tags in bodies.

Answers "show all conv:-derived takes" / "what's flagged add-to-imdb" without
grep. On-demand body scan — no index, no sidecar. The provenance tags stay
exactly where they are (HTML comments in the markdown); this only reads them.
"""

from __future__ import annotations

from pathlib import Path

from exomem import provenance as provenance_module


def _make_page(vault: Path, stem: str, body: str) -> str:
    rel = f"Knowledge Base/Notes/Research/Taste/{stem}.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n" + body,
        encoding="utf-8",
    )
    return rel


def test_provenance_by_key(vault: Path) -> None:
    rel = _make_page(
        vault,
        "p1",
        "# P\n\n"
        "- A — [take: x]  <!-- platform:imdb -->\n"
        "- B — [take: y]  <!-- platform:letterboxd -->\n"
        "- C — [take: z]\n",
    )
    findings = provenance_module.scan_provenance(vault, key="platform", path=rel)
    assert len(findings) == 2
    assert all("platform" in f.tags for f in findings)
    assert findings[0].tags["platform"] == "imdb"
    assert findings[0].line_number == 3  # body-relative, 1-based
    assert "A —" in findings[0].row_text


def test_provenance_by_key_value(vault: Path) -> None:
    rel = _make_page(
        vault,
        "p2",
        "# P\n\n- A <!-- platform:imdb -->\n- B <!-- platform:letterboxd -->\n",
    )
    only = provenance_module.scan_provenance(vault, key="platform", value="imdb", path=rel)
    assert len(only) == 1
    assert only[0].tags["platform"] == "imdb"
    assert provenance_module.scan_provenance(vault, key="platform", value="nope", path=rel) == []


def test_provenance_tag_shorthand(vault: Path) -> None:
    rel = _make_page(vault, "p3", "# P\n\n- A <!-- platform:imdb -->\n")
    via_tag = provenance_module.scan_provenance(vault, tag="platform:imdb", path=rel)
    via_kv = provenance_module.scan_provenance(vault, key="platform", value="imdb", path=rel)
    assert [f.as_dict() for f in via_tag] == [f.as_dict() for f in via_kv]
    key_only = provenance_module.scan_provenance(vault, tag="platform", path=rel)
    assert len(key_only) == 1


def test_provenance_empty_result(vault: Path) -> None:
    rel = _make_page(vault, "p4", "# P\n\n- A <!-- platform:imdb -->\n")
    assert provenance_module.scan_provenance(vault, key="nonexistent", path=rel) == []


def test_provenance_ignores_code_fence(vault: Path) -> None:
    rel = _make_page(
        vault,
        "p5",
        "# P\n\n```\n<!-- platform:imdb -->\n```\n\n- real <!-- platform:letterboxd -->\n",
    )
    findings = provenance_module.scan_provenance(vault, key="platform", path=rel)
    assert len(findings) == 1
    assert findings[0].tags["platform"] == "letterboxd"


def test_provenance_multiple_tags_one_comment(vault: Path) -> None:
    rel = _make_page(vault, "p6", "# P\n\n- A <!-- platform:imdb conv:2026-06-01 -->\n")
    findings = provenance_module.scan_provenance(vault, path=rel)
    assert len(findings) == 1
    assert findings[0].tags == {"platform": "imdb", "conv": "2026-06-01"}


def test_provenance_multiple_comments_one_line(vault: Path) -> None:
    rel = _make_page(
        vault, "p7", "# P\n\n- A <!-- platform:imdb --> <!-- conv:2026-06-01 -->\n"
    )
    findings = provenance_module.scan_provenance(vault, path=rel)
    assert len(findings) == 1
    assert findings[0].tags == {"platform": "imdb", "conv": "2026-06-01"}


def test_provenance_path_filter_scopes_to_one_file(vault: Path) -> None:
    rel1 = _make_page(vault, "pa", "# A\n\n- x <!-- xfeed:alpha -->\n")
    rel2 = _make_page(vault, "pb", "# B\n\n- y <!-- xfeed:beta -->\n")
    all_x = provenance_module.scan_provenance(vault, key="xfeed")
    paths = {f.path for f in all_x}
    assert rel1 in paths and rel2 in paths  # global walk finds both
    only1 = provenance_module.scan_provenance(vault, key="xfeed", path=rel1)
    assert {f.path for f in only1} == {rel1}  # path filter narrows
