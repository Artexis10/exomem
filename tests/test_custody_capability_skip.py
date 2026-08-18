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
