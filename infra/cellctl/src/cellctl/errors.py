"""Content-private error descriptions for logs, shared by cellctl and the key custodian."""

from __future__ import annotations

import traceback
from pathlib import Path

import httpx
from kubernetes.client.rest import ApiException

_PACKAGE_DIR = Path(__file__).parent


def describe_error(error: BaseException) -> str:
    """D4: logs are content-private. An error is logged by class, plus status
    and reason for a Kubernetes ApiException (status for a provider's HTTP error),
    and the innermost frame. Never its str or a traceback that renders it: an
    ApiException's str carries the response headers and body, and a cell Secret's
    apply response carries that cell's credentials."""

    described = type(error).__name__
    if isinstance(error, ApiException):
        described += f" status={error.status} reason={error.reason}"
    elif isinstance(error, httpx.HTTPStatusError):
        described += f" status={error.response.status_code}"
    frames = traceback.extract_tb(error.__traceback__)
    if frames:
        described += f" at {Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}"
        # The innermost frame is usually inside the Kubernetes client, and the
        # innermost cellctl one is its request wrapper; the cellctl frames
        # around it name the call that failed.
        own = [frame for frame in frames if Path(frame.filename).parent == _PACKAGE_DIR][-3:]
        if own and own[-1] is not frames[-1]:
            described += " via " + " < ".join(
                f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in reversed(own)
            )
    return described
