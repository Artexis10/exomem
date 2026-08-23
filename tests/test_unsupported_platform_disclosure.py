"""A platform with no held-filesystem backend says so once, up front.

`6587ad8c` made every governed write acquire a reserved-path root through
`held_fs`, and `_held_fs_posix` serves Linux only -- darwin has no backend at
all. The result was not a clear refusal but roughly a third of the suite
failing per macOS shard, each one describing a permanent platform fact as
"could not acquire the vault" or "held filesystem route is unavailable", plus
a graph-epoch cascade behind them.

The package meanwhile advertised `Operating System :: OS Independent`.

These tests pin the disclosure rather than the port: the substrate still has
no darwin implementation, and nothing here pretends otherwise.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from exomem import held_fs

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the body as though this host were darwin."""
    monkeypatch.setattr(sys, "platform", "darwin")
    held_fs.reset_capability_cache_for_tests()
    yield
    held_fs.reset_capability_cache_for_tests()


# --- the host question, asked without a root -------------------------------


def test_this_host_has_a_backend() -> None:
    """The suite runs where exomem is served; if this fails, the rest is noise."""
    assert held_fs.platform_support().supported


def test_an_unsupported_host_names_itself_and_the_supported_ones(unsupported) -> None:
    support = held_fs.platform_support()

    assert not support.supported
    assert "darwin" in support.reason
    assert "Linux" in support.reason and "Windows" in support.reason


def test_the_reason_is_empty_when_supported() -> None:
    assert held_fs.platform_support().reason == ""


def test_the_probe_and_the_host_question_share_one_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_probe` must not carry a second, drifting copy of the platform rule."""
    from exomem import _held_fs_posix

    if sys.platform.startswith("win"):
        pytest.skip("the POSIX backend is not the one selected here")
    monkeypatch.setattr(_held_fs_posix, "platform_supported", lambda: False)
    held_fs.reset_capability_cache_for_tests()

    capability = _held_fs_posix._probe(tmp_path)

    assert not capability.relative_operations
    assert capability.reason == _held_fs_posix._UNSUPPORTED_PLATFORM_REASON
    held_fs.reset_capability_cache_for_tests()


def test_acquire_refuses_rather_than_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fallback: a host with no backend gets a refusal, never a weaker route."""
    from exomem import _held_fs_posix

    if sys.platform.startswith("win"):
        pytest.skip("the POSIX backend is not the one selected here")
    monkeypatch.setattr(_held_fs_posix, "platform_supported", lambda: False)
    held_fs.reset_capability_cache_for_tests()

    result = held_fs.acquire(tmp_path)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CAPABILITY_UNAVAILABLE"
    held_fs.reset_capability_cache_for_tests()


# --- the CLI refuses once, and stays answerable ----------------------------


def test_a_vault_command_refuses_with_one_message(unsupported) -> None:
    from exomem import __main__ as entry

    refusal = entry._unsupported_platform_refusal(["ask"])

    assert refusal is not None
    assert "cannot run here" in refusal
    assert "darwin" in refusal
    assert "exomem doctor" in refusal


@pytest.mark.parametrize(
    "argv", [["doctor"], ["install-info"], ["--version"], ["--help"], []]
)
def test_diagnosis_and_identification_stay_reachable(argv, unsupported) -> None:
    """A user on an unsupported platform must be able to ask what is wrong."""
    from exomem import __main__ as entry

    assert entry._unsupported_platform_refusal(argv) is None


def test_a_supported_host_is_never_refused() -> None:
    from exomem import __main__ as entry

    assert entry._unsupported_platform_refusal(["ask"]) is None
    assert entry._unsupported_platform_refusal([]) is None


# --- doctor explains it ----------------------------------------------------


def test_doctor_reports_the_missing_backend_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vault is not degraded here, it is unusable, so this is not a warning.

    Patched at `platform_support` rather than at `sys.platform`: doctor reaches
    `urllib.request`, which on a faked `darwin` tries to import the macOS-only
    `_scproxy` and dies for a reason that has nothing to do with this check.
    """
    from exomem import doctor

    monkeypatch.setattr(
        held_fs,
        "platform_support",
        lambda: held_fs.PlatformSupport(
            False,
            "exomem has no held-filesystem backend for 'darwin'; "
            "Linux and Windows are the platforms it can serve today",
        ),
    )

    check = doctor._check_held_filesystem_platform()

    assert check.id == "platform.held_filesystem"
    assert check.status == "fail"
    assert "darwin" in check.message
    assert check.remediation is not None
    assert "Linux or Windows" in check.remediation


def test_doctor_passes_where_a_backend_exists() -> None:
    from exomem import doctor

    check = doctor._check_held_filesystem_platform()

    assert check.status == "pass"
    assert check.remediation is None


# --- the test-suite skip is narrow -----------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "reserved identity catalogue could not acquire the vault",
        "private SQLite target cannot acquire the vault",
        "SQLite identity publication cannot acquire the vault",
        "private byte publication cannot acquire the vault",
        "private byte read cannot acquire the vault",
        "private move cannot acquire the vault",
        "PATH_GUARD_UNSAFE: held filesystem route is unavailable",
    ],
)
def test_every_substrate_refusal_is_recognised(message: str) -> None:
    """Each string is one taken verbatim from a red macOS shard."""
    from benchmark_capabilities import declares_absent_held_filesystem

    assert declares_absent_held_filesystem(RuntimeError(message))


def test_the_graph_epoch_cascade_is_not_matched() -> None:
    """The downstream symptom stays a failure; only the named refusal skips.

    A platform with no backend leaves the graph epoch genuinely incoherent, and
    that error looks identical to the lineage defects this repository has had.
    Matching it would mask them.
    """
    from benchmark_capabilities import declares_absent_held_filesystem

    cascade = RuntimeError(
        "graph floor/checkpoint/ack epoch is not coherent for publication: "
        "GRAPH_SYNC_LINEAGE_CONFLICT: Reconcile the graph epoch before retrying"
    )

    assert not declares_absent_held_filesystem(cascade)


def test_a_bare_failure_is_not_matched() -> None:
    from benchmark_capabilities import declares_absent_held_filesystem

    assert not declares_absent_held_filesystem(OSError("permission denied"))
    assert not declares_absent_held_filesystem(None)


def test_the_refusal_is_found_through_an_exception_chain() -> None:
    from benchmark_capabilities import declares_absent_held_filesystem

    cause = RuntimeError("private SQLite target cannot acquire the vault")
    outer = AssertionError("did not raise the expected contract error")
    outer.__context__ = cause

    assert declares_absent_held_filesystem(outer)


# --- the package stops claiming what it cannot serve -----------------------


def test_the_package_does_not_advertise_os_independence() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = data["project"]["classifiers"]

    assert "Operating System :: OS Independent" not in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Operating System :: Microsoft :: Windows" in classifiers
