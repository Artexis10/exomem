"""Track D journey registry + JSON-able report assembly.

Mirrors scripts/product_flow_benchmark.py's run/report shape (per-flow dicts +
summary) for the two workflow journeys. Vaults are per-journey temp dirs under
the provided root — never a real vault; ``EXOMEM_VAULT_PATH`` is always set by
``journeys.journey_env``.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from membench.trackd.journeys import (
    REPO_ROOT,
    JourneyResult,
    normalize_instant,
    run_j1_longitudinal,
    run_j2_correction,
    run_j3_weekly_review,
)

JOURNEYS: dict[str, Callable[..., JourneyResult]] = {
    "j1_longitudinal": run_j1_longitudinal,
    "j2_correction": run_j2_correction,
    "j3_weekly_review": run_j3_weekly_review,
}
JOURNEY_ORDER = tuple(JOURNEYS)


def default_tmp_root() -> Path:
    root = REPO_ROOT / ".pytest-tmp" / "membench-trackd"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_journeys(
    journey_ids: tuple[str, ...] | None = None,
    *,
    tmp_root: Path | None = None,
    keep_tmp: bool = False,
    instant: dt.datetime | None = None,
) -> dict:
    """Run the selected journeys, each against its own fresh isolated vault."""
    pinned_instant = normalize_instant(instant)
    selected = journey_ids or JOURNEY_ORDER
    unknown = set(selected) - set(JOURNEYS)
    if unknown:
        raise ValueError(f"unknown journey id(s): {sorted(unknown)}")
    base = Path(tempfile.mkdtemp(prefix="run-", dir=tmp_root or default_tmp_root()))
    started = time.perf_counter()
    results: list[JourneyResult] = []
    try:
        for journey_id in JOURNEY_ORDER:
            if journey_id not in selected:
                continue
            workdir = base / journey_id
            workdir.mkdir(parents=True, exist_ok=True)
            results.append(JOURNEYS[journey_id](workdir, instant=pinned_instant))
    finally:
        if not keep_tmp:
            import shutil

            shutil.rmtree(base, ignore_errors=True)
    return {
        "generated_at": pinned_instant.isoformat(timespec="seconds"),
        "repo": str(REPO_ROOT),
        "journeys": [r.as_dict() for r in results],
        "summary": {
            "total": len(results),
            "passed": [r.id for r in results if r.ok],
            "failed": [r.id for r in results if not r.ok],
            "manual_interventions": sum(r.manual_interventions for r in results),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def render_text_report(report: dict) -> str:
    lines = ["# membench Track D workflow journeys", ""]
    for journey in report["journeys"]:
        lines.append(
            f"- {journey['id']}: {'pass' if journey['ok'] else 'FAIL'} "
            f"({journey['steps_count']} steps, {journey['elapsed_seconds']:.2f}s, "
            f"manual_interventions={journey['manual_interventions']})"
        )
        for check in journey["checks"]:
            marker = "OK" if check["ok"] else "FAIL"
            lines.append(f"  - {marker}: {check['name']} - {check['detail']}")
    summary = report["summary"]
    lines.extend(["", f"passed={summary['passed']} failed={summary['failed']}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--journey",
        action="append",
        choices=JOURNEY_ORDER,
        help="journey id to run; repeatable (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--keep-tmp", action="store_true", help="keep journey vaults")
    parser.add_argument(
        "--instant",
        help="pinned ISO-8601 instant for reproducible journey timestamps",
    )
    args = parser.parse_args(argv)
    instant = dt.datetime.fromisoformat(args.instant) if args.instant else None
    report = run_journeys(
        tuple(args.journey) if args.journey else None,
        keep_tmp=args.keep_tmp,
        instant=instant,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text_report(report))
    return 0 if not report["summary"]["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
