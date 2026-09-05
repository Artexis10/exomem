"""Governance fixtures must never publish into the real account's custody."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from exomem.governance import authorization_custody


def test_standalone_host_control_is_isolated_per_test(tmp_path: Path) -> None:
    root = authorization_custody._standalone_host_control_root()
    assert root.parent == tmp_path.parent
    assert root != tmp_path


def test_host_control_isolation_outlives_shared_monkeypatch(tmp_path: Path) -> None:
    """Exercise actual pytest teardown order, including an explicit mid-test undo."""
    probe = tmp_path / "test_host_control_probe.py"
    probe.write_text(textwrap.dedent('''
        from exomem.governance import authorization_custody

        def test_override(monkeypatch, tmp_path):
            isolated = authorization_custody._standalone_host_control_root()
            monkeypatch.setattr(
                authorization_custody, "_standalone_host_control_root",
                lambda: tmp_path / "fixture-custody",
            )
            monkeypatch.undo()
            assert authorization_custody._standalone_host_control_root() == isolated
            monkeypatch.setattr(
                authorization_custody, "_standalone_host_control_root",
                lambda: tmp_path / "second-fixture-custody",
            )

        def test_next_test_stays_isolated(tmp_path):
            root = authorization_custody._standalone_host_control_root()
            assert root.parent == tmp_path.parent
            assert root != tmp_path
    '''), encoding="utf-8")
    launcher = textwrap.dedent('''
        import importlib.util
        import sys
        from pathlib import Path
        import pytest
        from exomem.governance import authorization_custody

        original = authorization_custody._standalone_host_control_root
        original_root = original()
        sys.path.insert(0, str(Path(sys.argv[1]).parent))
        spec = importlib.util.spec_from_file_location("fixture_probe", sys.argv[1])
        root_fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(root_fixtures)

        class CheckRestoration:
            @pytest.hookimpl(tryfirst=True)
            def pytest_fixture_setup(self, fixturedef, request):
                if fixturedef.argname == "_isolate_authorization_host_control":
                    resolver = authorization_custody._standalone_host_control_root
                    assert resolver is original, "previous test retained an isolated resolver"
                    assert resolver() == original_root

        result = pytest.main(
            ["-q", "-c", sys.argv[3], "--confcutdir", sys.argv[4],
             "--basetemp", str(Path(sys.argv[4]) / "nested"), sys.argv[2]],
            plugins=[root_fixtures, CheckRestoration()],
        )
        assert authorization_custody._standalone_host_control_root is original
        raise SystemExit(result)
    ''')
    result = subprocess.run(
        [sys.executable, "-c", launcher, str(Path(__file__).with_name("conftest.py")),
         str(probe), os.devnull, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
