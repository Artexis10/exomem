from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_import_probe(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
    env["EXOMEM_DISABLE_MEDIA_EXTRACTION"] = "1"
    env["EXOMEM_DISABLE_RELEVANCE_CHECK"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_commands_import_does_not_load_media_numpy_stack() -> None:
    result = _run_import_probe(
        """
import sys
from exomem import commands
assert "numpy" not in sys.modules
assert "exomem.preserve" not in sys.modules
assert "exomem.extract" not in sys.modules
assert "exomem.video_frames" not in sys.modules
"""
    )
    assert result.returncode == 0, result.stderr


def test_server_import_does_not_load_media_numpy_stack() -> None:
    result = _run_import_probe(
        """
import sys
import exomem.server
assert "numpy" not in sys.modules
assert "exomem.preserve" not in sys.modules
assert "exomem.extract" not in sys.modules
assert "exomem.video_frames" not in sys.modules
"""
    )
    assert result.returncode == 0, result.stderr


def test_extract_import_and_media_type_check_do_not_load_numpy() -> None:
    result = _run_import_probe(
        """
import sys
from exomem import extract
assert extract.media_type_for("demo.pdf") == "pdf"
assert "numpy" not in sys.modules
assert "exomem.semantic_segments" not in sys.modules
"""
    )
    assert result.returncode == 0, result.stderr
