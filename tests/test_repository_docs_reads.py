"""The docs-read guard must fire, or docs-only pull requests skip real proof.

Docs-only pull requests run only `reads_repository_docs` tests. A guard that
stopped seeing reads would turn every unmarked docs reader into a silent skip
on exactly the change that can break it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DOC = TESTS.parent / "docs" / "release.md"
PACKAGED_README = TESTS.parent / "README.md"

# The guard's subprocess reads a real docs file, so a docs-only change can break this module.
pytestmark = pytest.mark.reads_repository_docs


def _run_guarded(
    tmp_path: Path, test_module: str, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    """Run one test module under the guard alone, outside this suite's conftest."""
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    reads_repository_docs: reads repository docs\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(TESTS)!r})
            from repository_docs_reads import RepositoryDocsReads

            def pytest_configure(config):
                config.pluginmanager.register(RepositoryDocsReads(), "repository-docs-reads")
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_reader.py").write_text(textwrap.dedent(test_module), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path),
            str(tmp_path / "test_reader.py"), *args,
        ],
        cwd=cwd or tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_unmarked_test_that_reads_docs_fails_by_name(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        f"""
        def test_reads_release_doc():
            open({str(DOC)!r}, encoding="utf-8").read()
        """,
    )

    assert result.returncode == 1, result.stdout
    assert "read docs/release.md but is not marked" in result.stdout


def test_a_relative_docs_read_from_the_repository_root_fails_by_name(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        """
        from pathlib import Path

        def test_reads_release_doc_relatively():
            Path("docs/release.md").read_text(encoding="utf-8")
        """,
        cwd=TESTS.parent,
    )

    assert result.returncode == 1, result.stdout
    assert "read docs/release.md but is not marked" in result.stdout


def test_a_marked_test_may_read_docs(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        f"""
        import pytest

        @pytest.mark.reads_repository_docs
        def test_reads_release_doc():
            open({str(DOC)!r}, encoding="utf-8").read()
        """,
    )

    assert result.returncode == 0, result.stdout


def test_the_packaged_readme_is_not_a_docs_read(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        f"""
        def test_reads_readme():
            open({str(PACKAGED_README)!r}, encoding="utf-8").read()
        """,
    )

    assert result.returncode == 0, result.stdout


def test_a_module_that_reads_docs_at_import_fails_collection(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        f"""
        RELEASE_DOC = open({str(DOC)!r}, encoding="utf-8").read()

        def test_uses_release_doc():
            assert RELEASE_DOC
        """,
    )

    assert result.returncode != 0
    assert "read docs/release.md but is not marked" in result.stdout


def test_a_docs_only_shard_with_no_marked_test_passes(tmp_path: Path) -> None:
    result = _run_guarded(
        tmp_path,
        """
        def test_reads_nothing():
            pass
        """,
        "-m",
        "reads_repository_docs",
    )

    assert result.returncode == 0, result.stdout
