"""The conftest hook that reports an absent custody capability as a skip."""

from __future__ import annotations

from conftest import PROC_FD_DIRECTORY_CUSTODY, _declares_custody_unsupported


class CustodyUnsupported(Exception):
    """Same name as `protocol.custody`'s, deliberately not the same class.

    The predicate matches by qualified name rather than by importing that
    module, because on Windows the import is itself one of the things that
    fails -- a check that needs the module in order to decide whether the
    module is usable would be no check at all. This stand-in is what proves
    the matching is by name.
    """


class CustodyError(Exception):
    pass


def test_the_refusal_itself_is_recognised() -> None:
    assert _declares_custody_unsupported(CustodyUnsupported("no proc-fd"))


def test_an_unrelated_failure_is_not_recognised() -> None:
    assert not _declares_custody_unsupported(CustodyError("something else"))
    assert not _declares_custody_unsupported(AssertionError("assert 1 == 2"))
    assert not _declares_custody_unsupported(None)


def test_a_refusal_wrapped_by_raise_from_is_recognised() -> None:
    """`custody.py` re-raises through `from exc` in several places."""
    try:
        try:
            raise CustodyUnsupported("no proc-fd")
        except CustodyUnsupported as exc:
            raise RuntimeError("runner failed") from exc
    except RuntimeError as outer:
        assert _declares_custody_unsupported(outer)


def test_a_refusal_wrapped_by_implicit_chaining_is_recognised() -> None:
    """A handler that raises while the refusal is live chains via __context__."""
    try:
        try:
            raise CustodyUnsupported("no proc-fd")
        except CustodyUnsupported:
            raise RuntimeError("cleanup failed")
    except RuntimeError as outer:
        assert _declares_custody_unsupported(outer)


def test_a_self_referential_chain_terminates() -> None:
    """Never loop: a chain can be made to point at itself."""
    error = CustodyError("loop")
    error.__cause__ = error
    assert not _declares_custody_unsupported(error)


def test_the_capability_flag_matches_this_platform() -> None:
    """The gate is the capability itself, not a platform name."""
    import os
    from pathlib import Path

    assert PROC_FD_DIRECTORY_CUSTODY == (
        os.name == "posix" and Path("/proc/self/fd").is_dir()
    )


def test_an_absent_posix_uid_api_is_recognised() -> None:
    """A hosted cell checks its own effective uid; Windows has no such call."""
    from conftest import _needs_an_absent_posix_api

    assert _needs_an_absent_posix_api(
        AttributeError("module 'os' has no attribute 'geteuid'")
    )
    assert _needs_an_absent_posix_api(
        AttributeError("module 'os' has no attribute 'getuid'")
    )
    assert _needs_an_absent_posix_api(
        AttributeError("<module 'os' (frozen)> has no attribute 'geteuid'")
    )
    assert _needs_an_absent_posix_api(ModuleNotFoundError("No module named 'fcntl'"))


def test_an_ordinary_attribute_error_is_not_a_missing_platform_api() -> None:
    """The narrowness is the point: a typo must not become a skip."""
    from conftest import _needs_an_absent_posix_api

    assert not _needs_an_absent_posix_api(
        AttributeError("module 'os' has no attribute 'getcwdd'")
    )
    assert not _needs_an_absent_posix_api(
        AttributeError("'HostedCell' object has no attribute 'geteuid_result'")
    )
    assert not _needs_an_absent_posix_api(ModuleNotFoundError("No module named 'numpy'"))
    assert not _needs_an_absent_posix_api(RuntimeError("module 'os' has no attribute 'geteuid'"))
    assert not _needs_an_absent_posix_api(None)


def test_a_wrapped_posix_api_error_is_recognised() -> None:
    """Hosted bootstrap wraps failures; the cause still names the platform gap."""
    from conftest import _needs_an_absent_posix_api

    try:
        try:
            raise AttributeError("module 'os' has no attribute 'geteuid'")
        except AttributeError as exc:
            raise RuntimeError("hosted runtime bootstrap failed") from exc
    except RuntimeError as outer:
        assert _needs_an_absent_posix_api(outer)


def test_a_missing_proc_path_is_recognised_as_an_absent_procfs() -> None:
    """The custody tests that read `/proc` directly never reach a declaration."""
    from conftest import _needs_an_absent_procfs

    assert _needs_an_absent_procfs(
        FileNotFoundError(2, "No such file or directory", "/proc/self/fd")
    )
    assert _needs_an_absent_procfs(
        FileNotFoundError(2, "No such file or directory", "/proc/1270/fd/48/observation.json")
    )


def test_a_missing_file_outside_proc_is_still_a_failure() -> None:
    """A real missing artefact must not be laundered into a skip."""
    from conftest import _needs_an_absent_procfs

    assert not _needs_an_absent_procfs(
        FileNotFoundError(2, "No such file or directory", "/tmp/evidence/observation.json")
    )
    assert not _needs_an_absent_procfs(
        FileNotFoundError(2, "No such file or directory", "processed/report.json")
    )
    assert not _needs_an_absent_procfs(FileNotFoundError("no filename recorded"))
    assert not _needs_an_absent_procfs(None)


def test_a_lifecycle_wrapped_proc_failure_is_recognised() -> None:
    """`run_provider_lifecycle` re-raises what it caught; the cause survives."""
    from conftest import _needs_an_absent_procfs

    try:
        try:
            raise FileNotFoundError(
                2, "No such file or directory", "/proc/1270/fd/48/observation.json"
            )
        except FileNotFoundError as exc:
            raise RuntimeError("direct provider lifecycle failed") from exc
    except RuntimeError as outer:
        assert _needs_an_absent_procfs(outer)


def test_the_declaration_compared_as_a_value_is_recognised() -> None:
    """`assert <refusal> is <expected>` reports the refusal as text, not as a raise."""
    from conftest import _needs_an_absent_procfs

    refusal = Exception("POSIX proc-fd directory capabilities are unavailable")
    assert _needs_an_absent_procfs(
        AssertionError(f"assert {refusal!r} is BaseException('control')")
    )
    assert not _needs_an_absent_procfs(AssertionError("assert 1 == 2"))


class HostedRuntimeTempUnavailable(Exception):
    """Same name as the hosted cell's, deliberately not the same class."""


class HostedSecurityStateInvalid(Exception):
    pass


def test_hosted_posix_ownership_refusals_are_recognised() -> None:
    from conftest import _needs_posix_ownership_semantics

    assert _needs_posix_ownership_semantics(HostedRuntimeTempUnavailable())
    assert _needs_posix_ownership_semantics(HostedSecurityStateInvalid())


def test_a_wrapped_hosted_ownership_refusal_is_recognised() -> None:
    """`server_hosted` re-raises the temp refusal as a gateway error."""
    from conftest import _needs_posix_ownership_semantics

    try:
        try:
            raise HostedRuntimeTempUnavailable()
        except HostedRuntimeTempUnavailable as exc:
            raise RuntimeError(
                "HOSTED_TRANSFER_UNAVAILABLE: hosted runtime temp is unavailable"
            ) from exc
    except RuntimeError as outer:
        assert _needs_posix_ownership_semantics(outer)


def test_an_unrelated_hosted_failure_is_still_a_failure() -> None:
    """The gate must not absorb ordinary hosted defects into skips."""
    from conftest import _needs_posix_ownership_semantics

    assert not _needs_posix_ownership_semantics(AssertionError("assert 200 == 403"))
    assert not _needs_posix_ownership_semantics(OSError("permission denied"))
    assert not _needs_posix_ownership_semantics(
        RuntimeError("HOSTED_TRANSFER_DENIED: policy refused the download")
    )
    assert not _needs_posix_ownership_semantics(None)
