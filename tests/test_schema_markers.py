"""The GENERIC marker logic feeds both schema derivations, in opposite directions:
genericize-schema.py keeps the generic replacement (repo scaffold); rebuild-schema-zip.py
keeps the real content (claude.ai zip). Both must drop the marker scaffolding.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import exomem

REPO = Path(exomem.__file__).resolve().parent.parent.parent  # src/exomem -> repo root


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = (
    "before\n"
    "<!-- GENERIC-START\n"
    "GENERIC LINE\n"
    "GENERIC-REPLACE -->\n"
    "REAL LINE ONE\n"
    "REAL LINE TWO\n"
    "<!-- GENERIC-END -->\n"
    "after\n"
)


def test_generic_side_keeps_replacement_drops_real() -> None:
    gen = _load("genericize_schema", "genericize-schema.py")
    out = gen.strip_markers(SAMPLE)
    assert out == "before\nGENERIC LINE\nafter\n"
    assert "REAL LINE" not in out
    assert "GENERIC-START" not in out


def test_personal_side_keeps_real_drops_replacement() -> None:
    zip_mod = _load("rebuild_schema_zip", "rebuild-schema-zip.py")
    out = zip_mod.strip_markers_keep_real(SAMPLE)
    assert out == "before\nREAL LINE ONE\nREAL LINE TWO\nafter\n"
    assert "GENERIC LINE" not in out
    assert "GENERIC-START" not in out


def test_empty_replacement_omits_block_generic() -> None:
    gen = _load("genericize_schema", "genericize-schema.py")
    text = "a\n<!-- GENERIC-START\nGENERIC-REPLACE -->\nsecret\n<!-- GENERIC-END -->\nb\n"
    assert gen.strip_markers(text) == "a\nb\n"


def test_unbalanced_markers_raise() -> None:
    import pytest

    zip_mod = _load("rebuild_schema_zip", "rebuild-schema-zip.py")
    with pytest.raises(SystemExit):
        zip_mod.strip_markers_keep_real("x\n<!-- GENERIC-START\nonly start\n")
