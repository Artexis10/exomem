"""``python -m recall_parity``: sample, capture, packet, score."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import capture, packet, private, sampling, scoring


def _sample(args: argparse.Namespace) -> int:
    root = private.private_root(args.private_dir)
    cases, manifest = sampling.build_sample(
        [Path(path) for path in args.queries],
        seed=args.seed,
        size=args.size,
        tuning_fraction=args.tuning_fraction,
        min_chars=args.min_chars,
    )
    private.write_jsonl(root / "sample" / "cases.jsonl", cases)
    private.write_json(root / "sample" / "sample-manifest.json", manifest)
    print(json.dumps({"counts": manifest["counts"], "split_sizes": manifest["split_sizes"]}))
    return 0


def _capture(args: argparse.Namespace) -> int:
    root = private.private_root(args.private_dir)
    arms = [capture.parse_arm(*triple) for triple in args.arm]
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise capture.CaptureError("--arguments must be a JSON object")
    # The SDK logs endpoint URLs and remote error text; this run keeps only its own records.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        manifest = capture.run_capture(
            root,
            args.run,
            split=args.split,
            arms=arms,
            tool=args.tool,
            arguments=arguments,
            timeout_s=args.timeout,
            warmup=args.warmup,
        )
    finally:
        logging.disable(previous)
    print(json.dumps({"cases": len(manifest["case_ids"]), "outcomes": manifest["outcomes"]}))
    return 0


def _packet(args: argparse.Namespace) -> int:
    root = private.private_root(args.private_dir)
    print(json.dumps(packet.build_packet(root, args.run, k=args.k)))
    return 0


def _score(args: argparse.Namespace) -> int:
    root = private.private_root(args.private_dir)
    report = scoring.score_run(
        root,
        args.run,
        Path(args.judgments),
        baseline=args.baseline,
        candidate=args.candidate,
        resamples=args.resamples,
    )
    out = root / "runs" / args.run / f"score-{report['judgements_sha256'][:10]}.json"
    private.write_json(out, report, overwrite=True)
    summary = {key: report[key] for key in ("difference", "coverage", "headline", "latency")}
    summary["excluding_cases_with_arm_error"] = report["excluding_cases_with_arm_error"]
    print(json.dumps(summary, indent=2))
    print(f"report: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall_parity", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--private-dir", required=True, help="directory for every artifact")

    sample = commands.add_parser("sample", help="draw a seeded tuning/held-out sample")
    common(sample)
    sample.add_argument("--queries", nargs="+", required=True, help="queries.jsonl file(s)")
    sample.add_argument("--seed", type=int, required=True)
    sample.add_argument("--size", type=int, default=200)
    sample.add_argument("--tuning-fraction", type=float, default=0.3)
    sample.add_argument("--min-chars", type=int, default=sampling.DEFAULT_MIN_CHARS)
    sample.set_defaults(handler=_sample)

    run = commands.add_parser("capture", help="send the sample to both arms over MCP/HTTP")
    common(run)
    run.add_argument("--run", required=True, help="run name; a new directory under runs/")
    run.add_argument("--split", choices=(*sampling.SPLITS, "all"), required=True)
    run.add_argument(
        "--arm",
        nargs=3,
        action="append",
        required=True,
        metavar=("NAME", "URL", "TOKEN_ENV"),
        help="an endpoint and the NAME of the environment variable that holds its bearer token",
    )
    run.add_argument("--tool", default=capture.DEFAULT_TOOL)
    run.add_argument(
        "--arguments",
        default=json.dumps(capture.DEFAULT_ARGUMENTS),
        help="fixed tool arguments as JSON, without 'query'",
    )
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--warmup", type=int, default=1, help="discarded calls per arm before timing")
    run.set_defaults(handler=_capture)

    pool = commands.add_parser("packet", help="write the blinded judge packet and the arm key")
    common(pool)
    pool.add_argument("--run", required=True)
    pool.add_argument("--k", type=int, default=packet.DEFAULT_K)
    pool.set_defaults(handler=_packet)

    score = commands.add_parser("score", help="score judgements: paired nDCG, CI, latency")
    common(score)
    score.add_argument("--run", required=True)
    score.add_argument("--judgments", required=True, help="JSONL of {pair_id, grade 0|1|2}")
    score.add_argument("--baseline", required=True, help="arm name, e.g. the personal server")
    score.add_argument("--candidate", required=True, help="arm name, e.g. the Cloud cell")
    score.add_argument("--resamples", type=int, default=scoring.DEFAULT_RESAMPLES)
    score.set_defaults(handler=_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (capture.CaptureError, private.PrivateDirError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
