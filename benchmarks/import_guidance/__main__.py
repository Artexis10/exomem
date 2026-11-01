"""`python -m benchmarks.import_guidance run ...` from the repository root."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# The reused harnesses (`epistemic`, `membench`) are top-level packages under benchmarks/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from import_guidance import runner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import_guidance")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run scenarios through a real headless agent")
    run.add_argument("--model", required=True)
    run.add_argument("--judge-model", help="default: --model")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument(
        "--scenario", default="all", help="a scenario id, an id prefix such as 03, or all"
    )
    run.add_argument(
        "--out", type=Path, required=True, help="outside every repository, e.g. under /tmp"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="check files, tools and logging; start no model"
    )
    args = parser.parse_args(argv)

    scenarios = runner.load_scenarios(args.scenario)
    if args.dry_run:
        failed = 0
        with tempfile.TemporaryDirectory(prefix="ig-dry-") as scratch:
            for scenario in scenarios:
                problems = runner.dry_run(scenario, Path(scratch))
                print(
                    f"{scenario.id}: {'ok' if not problems else 'PROBLEMS ' + '; '.join(problems)}"
                )
                if not problems:
                    argv_preview = runner.agent_argv(
                        Path("claude"),
                        scenario,
                        mcp=Path("mcp.json"),
                        skill=Path("skill.md"),
                        model=args.model,
                    )
                    print(
                        "  would run:",
                        " ".join(argv_preview[:2] + [repr(argv_preview[2])] + argv_preview[3:]),
                    )
                failed += bool(problems)
        return 1 if failed else 0
    rows = runner.run_all(
        scenarios,
        model=args.model,
        judge_model=args.judge_model or args.model,
        repeats=args.repeats,
        out=args.out,
    )
    print(runner.summary_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
