"""Safety boundaries for the one-time wikilink migration scripts."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "normalize_vault_wikilinks.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "normalize_vault_wikilinks_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


normalize_script = _load_script_module()


def test_walk_only_returns_writable_compiled_markdown(vault: Path) -> None:
    kb = vault / "Knowledge Base"
    protected = {
        kb / "Sources" / "Articles" / "protected-source.md",
        kb / "Evidence" / "Case" / "protected-evidence.md",
        kb / "Products" / "protected-curated.md",
    }
    writable = kb / "Notes" / "Insights" / "writable-note.md"
    for path in protected | {writable}:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
    (kb / "_access.yaml").write_text(
        "readonly:\n  - Products\nexcluded: []\n", encoding="utf-8"
    )

    walked = set(normalize_script.walk_kb_md(kb))

    assert writable in walked
    assert walked.isdisjoint(protected)
