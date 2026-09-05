"""The managed-recall admission a served process has, for a measured phase.

Both benchmark entry points need the same thing: the retrieval admission the
product grants itself at boot, standing for the duration of a sampling run, so
the numbers describe managed recall rather than the offline source-walk
fallback a bare caller gets.

They need it to be the *same* thing, which is why this module exists rather
than a second implementation next to each script. `semantic_write_latency.py`
once shipped an abbreviation of this that opened and closed the warm window
without publishing the catalogue proof, and had to be repaired in place.
`live_write_acceptance.py` was written later, carried its own abbreviation of
the same shape, and reproduced the identical defect on its first live run:
managed recall `unavailable`, every read after a write answering `warming`, and
an edit median seven times the product's real one because each write rebuilt
corpus context from cold. Two copies that agree today are how one of them
drifts. There is one now, imported by both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exomem import lexstore, readiness  # noqa: E402


def enter_managed_recall(vault_root: Path) -> dict[str, Any]:
    """Stand up the admission a served process has, for this phase only.

    The warm window is not the admission. `begin_warm`/`finish_warm` only open
    and close the window; what actually admits retrieval is the *catalogue
    proof* published inside it -- `readiness.admit_retrieval_proof`, which is
    the sole writer of the `retrieval_catalog` event that
    `readiness.retrieval_admission` reads. An earlier version of this function
    opened and closed the window without ever publishing that proof, so it left
    `_warm_finished` set with the event unset, which is precisely the
    `unavailable` state, and the gate died at the assertion below within a
    minute of starting.

    So this delegates to `warmup.warm_retrieval_catalog`, the function the
    served process itself calls, rather than restating an abbreviation of it.
    That keeps the rebaseline, the live-projection requirement, the maintained
    versus reference index distinction and the proof CAS in one place, and it
    means this warm-up cannot drift away from the product's.

    `EXOMEM_EAGER_BOOT` is set for the call because a benchmark needs the
    synchronous contract: without it, an incomplete catalogue is delegated to
    the background repair worker and the function returns False, leaving the
    sampling loop to race a rebuild. With it, the repair is awaited and proven,
    and a failure to converge raises here instead of quietly measuring the
    wrong thing.

    Returns the granted admission, so a caller that reports its state does not
    have to ask a second time and risk reporting a different answer than the
    one that was asserted. The caller owns `readiness.unmanage_runtime()`: the
    runtime is left managed on purpose, because the phase being measured is the
    one that needs it.
    """
    from exomem import warmup

    lexstore.ensure_fresh(vault_root)
    readiness.manage_runtime()
    previous_eager = os.environ.get("EXOMEM_EAGER_BOOT")
    os.environ["EXOMEM_EAGER_BOOT"] = "1"
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(vault_root)
    finally:
        readiness.finish_warm()
        if previous_eager is None:
            os.environ.pop("EXOMEM_EAGER_BOOT", None)
        else:
            os.environ["EXOMEM_EAGER_BOOT"] = previous_eager

    admission = readiness.retrieval_admission(vault_root)
    if not admission.get("admitted"):
        # A benchmark that silently measured the offline walk instead of
        # managed recall would report the wrong capability entirely.
        raise RuntimeError(
            f"managed recall admission was not granted: {admission}"
        )
    return admission
