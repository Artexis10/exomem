"""A self-managed temporary vault that no graph rebuild outlives.

`tempfile.TemporaryDirectory` removes its tree inside the test body, before any
fixture teardown runs — so the autouse rebuild quiesce in `conftest.py` is too
late for a test that manages its own vault directory rather than taking
`tmp_path`.

Since a write stopped joining its own graph rebuild (#576), a rebuild is
routinely still running at that point. On Windows the removal then fails
outright, because the rebuild holds its `.graph-rebuild-*.sqlite` open; on POSIX
it succeeds and leaves a rebuild writing into a deleted tree, which is worse for
being quiet.

Draining first is the same rule the CLI applies at process exit and the suite
applies at teardown: the boundary is the lifetime of the thing being built
against, and a vault about to be deleted is the end of one.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from exomem import graph_sync


@contextmanager
def temporary_vault() -> Iterator[Path]:
    """Yield a temporary directory, draining graph rebuilds before removing it."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            yield Path(directory)
        finally:
            assert graph_sync.drain_active_rebuilds(timeout=60.0), (
                "a graph rebuild outlived the vault it was building; removing the "
                "vault under it would fail on Windows and corrupt quietly elsewhere"
            )
