"""Command line entrypoint for the LongMemEval-S evaluation lane."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ``python -m benchmarks.lme.cli`` starts with the repository root on
# sys.path, while the reused benchmark package is intentionally importable as
# ``membench`` from the benchmarks directory. Tests already install this same
# path in conftest; the module entrypoint does it explicitly for user runs.
_BENCHMARKS_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_ROOT))

from .judge_io import ingest_judge_labels  # noqa: E402
from .report import render_run_report  # noqa: E402
from .runner import RunConfig, execute_run  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run retrieval, reader, and both bounds")
    run.add_argument("--dataset", required=True, type=Path)
    run.add_argument("--reader", required=True, choices=("stub", "openai", "claude"))
    run.add_argument("--out", required=True, type=Path, help="root for a new immutable run")
    run.add_argument("--run-id")
    run.add_argument("--dataset-sha256")
    run.add_argument("--dataset-revision")
    run.add_argument("--metered-approval")
    run.add_argument("--pilot-evidence", type=Path)
    run.add_argument("--full-run-approval")
    run.add_argument("--reader-model", default="gpt-4o")
    run.add_argument("--openai-base-url", default="https://api.openai.com/v1")
    run.add_argument("--openai-api-key-env", default="OPENAI_API_KEY")
    run.add_argument("--claude-binary", default="claude")
    run.add_argument("--top-k", type=int, default=10)
    run.add_argument("--provider", choices=("exomem-source-only", "hybrid-rag-control", "no-memory"))
    run.add_argument(
        "--budget-cap-usd", type=float, default=float(os.environ.get("PROTOCOL_BUDGET_CAP_USD", "0") or 0),
        help="cap written into the run's immutable budget ledger (env: PROTOCOL_BUDGET_CAP_USD)",
    )
    run.add_argument(
        "--pilot",
        type=int,
        help="run a deterministic per-ability round-robin pilot of N questions",
    )
    run.add_argument(
        "--canonical-selection", action="store_true",
        help="use the repository-owned frozen LongMemEval-S 25-case cohort",
    )

    labels = commands.add_parser(
        "ingest-judge", help="preserve official evaluate_qa.py labels and refresh report"
    )
    labels.add_argument("--run-dir", required=True, type=Path)
    labels.add_argument("--labels", required=True, type=Path)
    labels.add_argument("--lane", choices=("main", "ceiling", "floor"), default="main")
    report = commands.add_parser("report", help="regenerate a terminal run's artifact-only report")
    report.add_argument("--run-dir", required=True, type=Path)
    report.add_argument("--offline", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "ingest-judge":
        destination = ingest_judge_labels(args.run_dir, args.labels, lane=args.lane)
        print(destination)
        return 0
    if args.command == "report":
        print(render_run_report(args.run_dir, offline=args.offline))
        return 0
    result = execute_run(
        RunConfig(
            dataset=args.dataset,
            out=args.out,
            reader_name=args.reader,
            run_id=args.run_id,
            dataset_sha256=args.dataset_sha256,
            dataset_revision=args.dataset_revision,
            metered_approval=args.metered_approval,
            pilot_evidence=args.pilot_evidence,
            full_run_approval=args.full_run_approval,
            reader_model=args.reader_model,
            openai_base_url=args.openai_base_url,
            openai_api_key_env=args.openai_api_key_env,
            claude_binary=args.claude_binary,
            top_k=args.top_k,
            pilot=args.pilot,
            canonical_selection=args.canonical_selection,
            provider=args.provider,
            budget_cap_usd=args.budget_cap_usd,
        )
    )
    print(result.run_dir)
    if result.failure_count:
        print(f"completed with {result.failure_count} question failure(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
