"""The outer terminal is written after the mutation, so a cut can lose it.

`invoke` persists the terminal row that makes a same-key retry a replay only
once the leaf has returned. Between those two points the work has happened and
nothing records that it has, which is the one window where a retry could run
the leaf a second time.

The cut is taken by a child interpreter so the window is real: no unwinding, no
`finally`, nothing flushed that was not already durable. The parent then
retries the same idempotency key against the same state directory and counts
how many times the leaf actually ran.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CRASH_EXIT = 97
KEY = "outer-terminal-cut"
SCOPE = "principal:alice"

_CHILD = """
import os, sys
from pathlib import Path
from types import SimpleNamespace

from exomem import writer_lease

state = Path({state!r})
vault = Path({vault!r})
marker = Path({marker!r})


def leaf(*_args, **kwargs):
    # Durable, outside the lease's own bookkeeping: this is what stands in for
    # the canonical effect, and it is what proves whether a retry redid it.
    with marker.open("a", encoding="utf-8") as handle:
        handle.write("ran\\n")
    return {{"value": kwargs["value"]}}


def crash(reached):
    if reached == {point!r}:
        os._exit({code})


writer_lease._crash_point = crash
manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state))
manager.invoke(
    SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False),
    (vault,),
    {{"value": 7}},
    idempotency_key={key!r},
    idempotency_principal_scope={scope!r},
)
print("child completed without reaching the cut", file=sys.stderr)
sys.exit(1)
"""


def _child(tmp_path: Path, point: str) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / f"terminal-cut-{point}.py"
    driver.write_text(
        _CHILD.format(
            state=str(tmp_path / "state"),
            vault=str(tmp_path / "vault"),
            marker=str(tmp_path / "leaf-runs"),
            point=point,
            code=CRASH_EXIT,
            key=KEY,
            scope=SCOPE,
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        timeout=180,
        env=dict(os.environ),
        check=False,
    )


def _runs(tmp_path: Path) -> int:
    marker = tmp_path / "leaf-runs"
    return len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "vault" / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    return tmp_path / "vault"


def test_a_cut_before_the_outer_terminal_never_runs_the_leaf_twice(tmp_path: Path) -> None:
    """The window exists, and the retry does not pay for it with a second effect.

    The child's leaf ran and its terminal was never written, so the retry
    cannot replay -- there is nothing to replay from. What it must not do is
    quietly perform the work again and call that success.
    """

    from types import SimpleNamespace

    from exomem import writer_lease

    vault = _vault(tmp_path)
    marker = tmp_path / "leaf-runs"

    result = _child(tmp_path, "before-outer-terminal-persistence")
    assert result.returncode == CRASH_EXIT, result.stderr
    assert _runs(tmp_path) == 1

    def leaf(*_args: object, **kwargs: object) -> dict[str, object]:
        with marker.open("a", encoding="utf-8") as handle:
            handle.write("ran\n")
        return {"value": kwargs["value"]}

    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    command = SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False)

    outcome = manager.invoke(
        command,
        (vault,),
        {"value": 7},
        idempotency_key=KEY,
        idempotency_principal_scope=SCOPE,
    )

    # Measured, and better than "does not duplicate": the retry resolves to the
    # original result. The terminal row is missing, so this cannot be a replay
    # of it -- the canonical row written before the leaf returned is the retry
    # anchor, and it survives the window on its own.
    assert outcome == {"value": 7}
    assert _runs(tmp_path) == 1

    # And it is stable: asking a third time changes nothing.
    assert (
        manager.invoke(
            command,
            (vault,),
            {"value": 7},
            idempotency_key=KEY,
            idempotency_principal_scope=SCOPE,
        )
        == {"value": 7}
    )
    assert _runs(tmp_path) == 1


def test_the_child_really_dies_at_the_barrier_and_not_somewhere_convenient(
    tmp_path: Path,
) -> None:
    """Guard the instrument."""

    _vault(tmp_path)

    result = _child(tmp_path, "a-barrier-that-does-not-exist")

    assert result.returncode != CRASH_EXIT
    assert "child completed without reaching the cut" in result.stderr
