"""Canonical response snapshots on switch_eval's existing transport.

Snapshot work runs before the response reaches the actor. Recorded timing is
instrumented timing, not product latency. No command executes from this reader.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from membench.utility.switch_eval import tap

from .vault import read_end_state


class CanonicalLog(tap.TapLog):
    def write(self, event: str, **fields) -> None:
        if ((event == "ready_finished" and fields.get("outcome") == "ready")
                or (event == "call" and fields.get("outcome") == "served")):
            try:
                fields["canonical_state"] = read_end_state(Path(os.environ["EXOMEM_VAULT_PATH"]))
            except Exception as error:  # noqa: BLE001 - preserve an unresolved instrument read
                fields["canonical_state"] = {"unknown": [{"error": type(error).__name__}]}
            fields["timing_scope"] = "instrumented"
        super().write(event, **fields)


def main(argv: list[str] | None = None) -> int:
    # This isolated adapter changes the log sink only; shared transport and shutdown own the process.
    tap.TapLog = CanonicalLog
    return tap.main(argv)


if __name__ == "__main__":
    sys.exit(main())
