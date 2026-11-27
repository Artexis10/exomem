"""The content-free cause of a Cloud Job's failure, for its termination report.

cellctl logs this before it deletes the Job, so the cause outlives the pod. It holds code identifiers and
an error number, never exception text, a file name or a URL.
"""

from __future__ import annotations

from types import CodeType


def failure_diagnostic(error: Exception, *, not_the_cause: frozenset[CodeType] = frozenset()) -> dict:
    """The class of `error`, the function it came from, and the OSError class and number behind it."""
    detail = {"type": type(error).__name__}
    frame = error.__traceback__
    while frame is not None:
        # A raise helper is never the cause; its caller is the check that refused.
        if frame.tb_frame.f_code not in not_the_cause:
            detail["function"] = frame.tb_frame.f_code.co_name
        frame = frame.tb_next
    cause = error
    if not isinstance(error, OSError):
        # A refusal raised from or while handling an OSError keeps that error's class and number,
        # unless the raise suppressed its context (`from None`).
        cause = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__)
        if isinstance(cause, OSError):
            detail["cause"] = type(cause).__name__
    # OSError accepts any first argument; only an integer is an error number, never a path.
    if isinstance(cause, OSError) and isinstance(cause.errno, int):
        detail["errno"] = cause.errno
    return detail
