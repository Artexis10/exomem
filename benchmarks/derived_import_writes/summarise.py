"""Summarise one probe run: bytes written per row, their trend, and the cancel arm.

Usage: python summarise.py RUN.jsonl [RUN.jsonl ...]; prints one JSON object per run.
"""

from __future__ import annotations

import json
import sys

WINDOWS = 8


def summarise(path: str) -> dict:
    samples = [json.loads(line) for line in open(path)]
    config = next(s for s in samples if s["phase"] == "config")
    imports = [s for s in samples if s["phase"] == "import"]
    done = next(s for s in samples if s["phase"] == "import_done")
    first = imports[0]
    rows = done["rows"]["imported"] - first["rows"]["imported"]
    result = {"run": path, "shape": config["shape"], "pragmas": config["pragmas"], "rows": rows,
              "seconds": round(done["t"] - first["t"], 1),
              "bytes_per_row": round((done["written"] - first["written"]) / rows),
              "derived_bytes": done["derived_bytes"]}
    step = max(1, len(imports) // WINDOWS)
    windows = []
    for start in range(0, len(imports) - step, step):
        a, b = imports[start], imports[start + step]
        added = b["rows"]["imported"] - a["rows"]["imported"]
        if added > 0:
            windows.append(round((b["written"] - a["written"]) / added))
    result["window_bytes_per_row"] = windows
    # The first window includes setup writes, so the trend compares the first full window with the last.
    if len(windows) >= 3:
        result["last_over_first_full"] = round(windows[-1] / windows[1], 3)
    cancelled = next((s for s in samples if s["phase"] == "cancelled"), None)
    settled = next((s for s in samples if s["phase"] == "settled"), None)
    if cancelled and settled:
        result["cancel"] = {"staged_rows": cancelled["staged_rows"],
                            "bytes": settled["written"] - cancelled["written"],
                            "seconds": round(settled["t"] - cancelled["t"], 1),
                            "digest_equal": settled["digest_before"] == settled["digest_after"]}
    return result


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        print(json.dumps(summarise(argument)))
