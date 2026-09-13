"""A promoted standby's first ten writes are incremental (phase 2, task 2.1).

Phase 1 proved a *cold* replacement can adopt the snapshot it inherited during
its own warm-up. This is the handoff the supervisor actually performs: the
candidate warms as a standby beside the worker that is still serving, adopts the
published snapshot there, and is promoted only after the old worker has exited.
The assertion phase 1 deferred to this task is that the ten governed writes
after that promotion schedule no whole-vault rebuild at all -- not one, which is
what a replacement without adoption still costs.

Run in a fresh interpreter, which is the point: `freshness`'s registry and the
watcher's self-attribution table are process-local, so a promoted worker starts
with neither, exactly as the spawned child of a managed upgrade does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_graph_post_handoff_writes import (
    NOTE_COUNT,
    WRITE_COUNT,
    _assert_incremental_latency,
    _build_vault,
    _run_child,
)


@pytest.fixture
def handoff_vault(vault: Path) -> Iterator[Path]:
    """The same seeded vault a replacement worker inherits, built by phase 1's
    helper so both suites measure the identical corpus."""
    yield from _build_vault(vault, NOTE_COUNT)


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Managed worker handoff is Linux-only"
)

_STANDBY_CHILD = '''
import json, sys, time
from pathlib import Path

sys.path.insert(0, {tests_dir!r})
from test_graph_post_handoff_writes import (  # noqa: E402
    GENERATED,
    WRITE_COUNT,
    _governed_write,
)

from exomem import graph_sync, service_standby  # noqa: E402
from exomem.epistemic_graph import EpistemicGraphIndex  # noqa: E402

root = Path(sys.argv[1])
generated = root / GENERATED

passes = []
real = EpistemicGraphIndex._rebuild_all_off_boundary


def counted(self, **kwargs):
    started = time.monotonic()
    try:
        return real(self, **kwargs)
    finally:
        passes.append(time.monotonic() - started)


EpistemicGraphIndex._rebuild_all_off_boundary = counted

# D7: warm as a standby. No lease, no publication, no scheduler, no watcher --
# the old worker is still the one serving and still owns every repair.
service_standby.enter_standby()
service_standby.warm(root)
readiness = service_standby.readiness_payload()
warm_passes = len(passes)

# D8: the previous worker and its descendants have exited; take ownership.
promotion = service_standby.promote(root, migrated=False)

acknowledgements = []
per_write_rebuilds = []
for index in range(WRITE_COUNT):
    before = len(passes)
    acknowledgements.append(
        _governed_write(root, generated / f"generated-note-{{index:04d}}.md", "promoted")
    )
    per_write_rebuilds.append(len(passes) - before)

graph_sync.drain_active_rebuilds(timeout=60.0)
print(
    json.dumps(
        {{
            "readiness": readiness,
            "promotion": promotion,
            "warm_passes": warm_passes,
            "rebuilds": len(passes),
            "per_write_rebuilds": per_write_rebuilds,
            "acknowledgements": acknowledgements,
            "available": EpistemicGraphIndex(root).available(),
        }}
    )
)
'''


def _run_standby(vault: Path, tmp_path: Path) -> dict:
    script = tmp_path / "standby_child.py"
    script.write_text(
        _STANDBY_CHILD.format(tests_dir=str(Path(__file__).parent)), encoding="utf-8"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, str(script), str(vault)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_a_promoted_standby_serves_its_first_ten_writes_incrementally(
    handoff_vault: Path, tmp_path: Path
) -> None:
    """The whole-vault pass a replacement used to pay is gone after promotion."""
    _run_child(handoff_vault, tmp_path, ["outgoing"])
    report = _run_standby(handoff_vault, tmp_path)

    cutover = report["readiness"]
    assert cutover["components"]["graph_snapshot"] == "ready", cutover
    assert cutover["adoption"]["reason"] == "adopted", cutover
    assert report["warm_passes"] == 0, "the standby warm must not rebuild anything"
    assert report["promotion"]["snapshot"] == "current", report["promotion"]
    assert report["per_write_rebuilds"] == [0] * WRITE_COUNT, (
        "every write after a promotion must be incremental; rebuilds per write "
        f"were {report['per_write_rebuilds']} for {report['acknowledgements']}"
    )
    assert report["rebuilds"] == 0
    assert report["available"] is True
    _assert_incremental_latency(report["acknowledgements"])


def test_a_promotion_after_a_deferred_write_is_still_incremental(
    handoff_vault: Path, tmp_path: Path
) -> None:
    """The state a real mid-traffic handoff leaves: a page the graph never took.

    The outgoing worker defers a write, which withdraws the availability marker,
    and the background repair does not republish it before that worker stops.
    The standby adopts that residue rather than refusing it, and the promoted
    process still owes nothing to a whole-vault pass.
    """
    _run_child(handoff_vault, tmp_path, ["outgoing_fenced"])
    report = _run_standby(handoff_vault, tmp_path)

    cutover = report["readiness"]
    assert cutover["components"]["graph_snapshot"] == "ready", cutover
    assert cutover["adoption"]["residue"] >= 1, (
        "the outgoing process left a deferred page; the cutover block must say so"
    )
    assert cutover["adoption"]["reason"] == "adopted", cutover
    assert report["per_write_rebuilds"] == [0] * WRITE_COUNT, (
        f"a residue adoption must stay incremental: {report['per_write_rebuilds']}"
    )
    assert report["available"] is True
