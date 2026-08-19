r"""The Windows machine-wide base is derived in one place, or in a pinned mirror.

`public_artifact_privacy`'s `absolute_local_path` rule forbids a contiguous
drive-absolute literal in any repository input or shipped wheel member --
docstrings included, because they travel inside the wheel. The Windows
machine-wide fallback genuinely needs `ProgramData`, so the final tier is
written as a concatenation (`"C:" + r"\ProgramData"`), which keeps the literal
from ever appearing whole.

That constraint lived only as an example. Nothing imported it, nothing refused
a violation, and read quickly the split looks like odd formatting -- so the
#552 lane, reproducing "the %PROGRAMDATA% fallback" faithfully in prose, a
PowerShell tier and twelve test sites, reintroduced it in every one: 16
findings and six red checks on a branch that had already passed two
independent clean reviews (#574). Neither review could have caught it; the gate
only ran on Linux CI.

So the constraint is encoded here instead of exemplified. `mode` owns the one
derivation, callers import it, and the standalone hook scripts -- which run as
bare files under whatever interpreter the client provides and therefore cannot
import anything from `exomem` -- carry mirrors this file pins by behaviour.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from exomem import mode

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "exomem"

#: Files allowed to spell the fallback chain themselves. `mode` is the single
#: source; the rest are standalone hook scripts that cannot import it.
DERIVERS = {
    "mode.py",
    "_hooks/exomem_capture_nudge.py",
    "_hooks/exomem_retrieve_nudge.py",
}

TIERS = ("PROGRAMDATA", "ALLUSERSPROFILE")


def _reads_both_tiers(tree: ast.AST) -> bool:
    """Whether this module reads the machine-wide environment tiers itself."""
    named = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    return all(tier in named for tier in TIERS)


def test_only_the_named_files_derive_the_machine_wide_base() -> None:
    """A new caller must import `windows_machine_wide_root`, not copy it."""
    derivers = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _reads_both_tiers(tree):
            derivers.add(path.relative_to(SOURCE_ROOT).as_posix())

    assert derivers == DERIVERS, (
        "import exomem.mode.windows_machine_wide_root instead of re-deriving the "
        "%PROGRAMDATA% chain; copying it drops the constraint that keeps the "
        "drive-absolute literal from ever appearing contiguously"
    )


def test_no_source_file_spells_the_literal_contiguously() -> None:
    """The rule the split exists to satisfy, asserted where authors will see it.

    `public_artifact_privacy` already refuses this, but only on Linux CI and
    only over the whole repository. Naming it here means a Windows contributor
    running the ordinary suite finds it in the lane.
    """
    contiguous = "C:" + chr(92) + "ProgramData"
    offenders = [
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if contiguous.casefold() in path.read_text(encoding="utf-8").casefold()
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"PROGRAMDATA": "P:" + chr(92) + "Data"}, "P:" + chr(92) + "Data"),
        (
            {"ALLUSERSPROFILE": "Q:" + chr(92) + "All"},
            "Q:" + chr(92) + "All",
        ),
        ({}, "C:" + chr(92) + "ProgramData"),
    ],
    ids=["programdata", "allusersprofile", "hardcoded"],
)
def test_each_tier_of_the_chain_answers(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], expected: str
) -> None:
    for tier in TIERS:
        monkeypatch.delenv(tier, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert mode.windows_machine_wide_root() == Path(expected)


@pytest.mark.skipif(os.name != "nt", reason="the shared base is a Windows path")
def test_config_and_log_paths_share_the_one_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reuse the helper exists for, asserted rather than assumed.

    Windows-only rather than platform-faked. Both callers branch on
    `os.name` / `sys.platform`, and monkeypatching those is not a local act:
    they are read by the whole process, including pytest's own reporting
    hooks. Faking them here made `shutil.which` take its win32 branch on a
    Linux runner and brought the session down with an INTERNALERROR, which
    is a far worse failure than the one it was checking for.
    """
    from exomem import logging_config

    monkeypatch.setenv("PROGRAMDATA", "P:" + chr(92) + "Data")
    monkeypatch.delenv("ALLUSERSPROFILE", raising=False)
    monkeypatch.delenv("EXOMEM_CONFIG_PATH", raising=False)

    base = mode.windows_machine_wide_root()

    assert mode.config_path() == base / "exomem" / "config.json"
    assert logging_config._user_log_dir() == base / "exomem" / "logs"


@pytest.mark.parametrize(
    "relative",
    ["_hooks/exomem_capture_nudge.py", "_hooks/exomem_retrieve_nudge.py"],
)
def test_the_standalone_hook_mirrors_keep_the_split(relative: str) -> None:
    """Mirrors are allowed; a mirror that drops the split is not.

    These files are copied verbatim into `plugins/claude-code/hooks/` and run
    outside the package, so they cannot import the helper. What they can do is
    stay spelled the same way.
    """
    source = (SOURCE_ROOT / relative).read_text(encoding="utf-8")

    assert '"C:" + r"' + chr(92) + 'ProgramData"' in source
    assert all(tier in source for tier in TIERS)
