"""Answer the requests a hook's credential lane sends, without a server."""

from __future__ import annotations

import urllib.request

import pytest


def answer_requests(monkeypatch: pytest.MonkeyPatch, fake) -> None:  # noqa: ANN001
    """Answer every request the credential module's opener sends with `fake(request, timeout=...)`.

    The opener class is patched because each hook call reloads the credential module.
    """
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda _opener, request, data=None, timeout=None: fake(request, timeout=timeout),
    )
