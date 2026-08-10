from __future__ import annotations

from pathlib import Path

from record_fixtures import copy_x3_fixture


def test_x3_fixture_preserves_crlf_and_no_final_newline_variant(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)

    variant = fixture / "Training Log CRLF no final newline.md"

    assert b"\r\n" in variant.read_bytes()
    assert not variant.read_bytes().endswith((b"\n", b"\r"))
