"""membench CLI: generate corpora, export schemas, execute runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BENCH_ROOT = Path(__file__).resolve().parents[1]


def _cmd_generate(args: argparse.Namespace) -> int:
    from membench.generate import generate_corpus

    manifest = generate_corpus(
        args.seed,
        Path(args.out),
        template_ids=args.template or None,
        force=args.force,
    )
    print(json.dumps(manifest.counts, sort_keys=True))
    return 0


def _cmd_export_schemas(args: argparse.Namespace) -> int:
    from membench.schema import export_json_schemas

    written = export_json_schemas(Path(args.out))
    print("\n".join(str(p) for p in written))
    return 0


def _cmd_catalog(args: argparse.Namespace) -> int:
    from membench.templates import registry

    for template_id, template in sorted(registry().items()):
        print(f"{template_id}\t{template.family}\tx{template.variants}\t{template.summary}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from membench.adapters import create_adapter
    from membench.adapters.exomem_local import embeddings_profile, lexical_profile
    from membench.runner import RunSpec, execute_run

    profile = embeddings_profile() if args.profile == "embeddings" else lexical_profile()
    adapter_kwargs: dict[str, object] = {
        "mode": args.mode,
        "search_style": args.search_style,
        "altitude": args.altitude,
    }
    if args.governance != "off":
        # Governance wiring is an exomem-local seam; other providers must
        # never receive an unknown kwarg — and a requested wiring must never
        # silently degrade to a default-open run.
        if args.provider != "exomem-local":
            print(
                f"--governance {args.governance} is only supported by the "
                "exomem-local provider",
                file=sys.stderr,
            )
            return 2
        adapter_kwargs["governance"] = args.governance
    if args.answer_mode != "harness":
        if args.provider != "exomem-local":
            print(
                f"--answer-mode {args.answer_mode} is only supported by the "
                "exomem-local provider",
                file=sys.stderr,
            )
            return 2
        adapter_kwargs["answer_mode"] = args.answer_mode
    adapter = create_adapter(args.provider, **adapter_kwargs)
    result = execute_run(
        RunSpec(
            corpus_dir=Path(args.corpus),
            adapter=adapter,
            profile=profile,
            runs_root=Path(args.runs_root),
            top_k=args.top_k,
            label=args.label,
        )
    )
    print(f"run_dir={result.run_dir}")
    print(f"invalid={result.invalid}" + (f" reason={result.invalid_reason}" if result.invalid else ""))
    for dim, counts in sorted(result.dimensions.items()):
        print(f"{dim}: {json.dumps(counts, sort_keys=True)}")
    return 2 if result.invalid else 0


def _cmd_utility_run(args: argparse.Namespace) -> int:
    import asyncio

    from membench.utility.runner import UtilityRunnerError, run_utility
    from protocol.contracts import ContractIdentityError

    try:
        report = asyncio.run(run_utility(
            Path(args.output), args.seed,
            product_root=Path(args.product_root), python=Path(args.python),
            tokenizer_path=Path(args.tokenizer_path),
            model_cache=Path(args.model_cache) if args.model_cache else None,
            clip_model_cache=Path(args.clip_model_cache) if args.clip_model_cache else None,
            profile=args.profile, cap_usd=args.cap_usd, paid=args.paid,
            approval_token=args.approval_token, phase_seconds=args.phase_seconds,
        ))
    except (UtilityRunnerError, ContractIdentityError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"halted": report["halted"], "attempted_variants": report["attempted_variants"]}, sort_keys=True))
    return 0


def _cmd_utility_read(args: argparse.Namespace) -> int:
    from membench.utility.runner import UtilityReportError, load_report
    import membench.utility.runner as utility_runner

    try:
        loaded = load_report(
            Path(args.run), Path(args.product_root), allow_synthetic=args.allow_synthetic,
            identity_validator=utility_runner._default_identity_validator,
            family_gate=utility_runner._default_report_family_gate,
            fixture_gate=utility_runner._default_fixture_gate,
        )
    except (UtilityReportError, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "synthetic": loaded["synthetic"],
        "seed": loaded["manifest"]["scenario"]["seed"],
        "scores": loaded["recomputed"]["scores"],
        "spend": loaded["report"].get("spend"),
    }, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="membench")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate a corpus from a seed")
    p_gen.add_argument("--seed", type=int, required=True)
    p_gen.add_argument("--out", required=True)
    p_gen.add_argument("--template", action="append", help="restrict to template id(s)")
    p_gen.add_argument("--force", action="store_true")
    p_gen.set_defaults(func=_cmd_generate)

    p_schemas = sub.add_parser("export-schemas", help="write JSON-Schemas")
    p_schemas.add_argument("--out", default=str(_BENCH_ROOT / "corpus" / "schema"))
    p_schemas.set_defaults(func=_cmd_export_schemas)

    p_catalog = sub.add_parser("catalog", help="list registered templates")
    p_catalog.set_defaults(func=_cmd_catalog)

    p_run = sub.add_parser("run", help="execute a provider run over a corpus")
    p_run.add_argument("--corpus", required=True)
    p_run.add_argument("--provider", default="exomem-local")
    p_run.add_argument("--mode", default="leaf", choices=["leaf", "wire"])
    p_run.add_argument(
        "--search-style", default="neutral", choices=["neutral", "product-default"]
    )
    p_run.add_argument("--profile", default="lexical", choices=["lexical", "embeddings"])
    p_run.add_argument(
        "--governance",
        default="off",
        choices=["off", "wired"],
        help=(
            "exomem-local only: translate the corpus policy set into the "
            "vault's opt-in _Governance/ policy and thread query personas "
            "(three-state reporting: wired / default_open / unsupported)"
        ),
    )
    p_run.add_argument(
        "--answer-mode",
        default="harness",
        choices=["harness", "native"],
        help=(
            "exomem-local only: 'harness' scores the shared extractive answerer; "
            "'native' scores the contender's own answer, citations and abstention. "
            "Decides whether provenance/abstention/calibration measure the product "
            "or the harness, so runs of different modes are not comparable on those"
        ),
    )
    p_run.add_argument(
        "--altitude",
        default="raw_source",
        choices=["raw_source", "compiled"],
        help=(
            "the layer to measure at. 'raw_source' loads documents verbatim; "
            "'compiled' also authors the corpus's conclusions through each "
            "product's own surfaces, which is what gives provenance and "
            "contradiction something to score. An adapter that cannot honour "
            "the tier refuses rather than being measured at another one"
        ),
    )
    p_run.add_argument("--top-k", type=int, default=10)
    p_run.add_argument("--runs-root", default=str(_BENCH_ROOT / "runs"))
    p_run.add_argument("--label", default=None)
    p_run.set_defaults(func=_cmd_run)

    p_utility = sub.add_parser("utility", help="epistemic-utility action instrument")
    utility_sub = p_utility.add_subparsers(dest="utility_command", required=True)

    p_utility_run = utility_sub.add_parser(
        "run", help="run the paired control/memory smoke for one seed (opt-in paid)"
    )
    p_utility_run.add_argument("--output", required=True, help="new run output directory")
    p_utility_run.add_argument("--seed", type=int, required=True)
    p_utility_run.add_argument("--product-root", required=True)
    p_utility_run.add_argument("--python", required=True, help="interpreter used for the native cell/worker")
    p_utility_run.add_argument("--tokenizer-path", required=True, help="pinned local tokenizer artifact")
    p_utility_run.add_argument("--model-cache", default=None)
    p_utility_run.add_argument("--clip-model-cache", default=None)
    p_utility_run.add_argument("--profile", default="semantic", choices=["semantic", "fixture"])
    p_utility_run.add_argument("--cap-usd", type=float, default=2.0)
    p_utility_run.add_argument(
        "--paid", action="store_true",
        help="required to spend anything; --help and generation-only paths never spend without it",
    )
    p_utility_run.add_argument(
        "--approval-token", default="",
        help="operator approval marker; never an API key (read only from its environment variable)",
    )
    p_utility_run.add_argument("--phase-seconds", type=float, default=180.0)
    p_utility_run.set_defaults(func=_cmd_utility_run)

    p_utility_read = utility_sub.add_parser(
        "read", help="read a saved utility run: protocol gates, digests, independent regrade"
    )
    p_utility_read.add_argument("--run", required=True, help="a completed run output directory")
    p_utility_read.add_argument("--product-root", required=True)
    p_utility_read.add_argument(
        "--allow-synthetic", action="store_true",
        help="read a run whose actor, cell or gates were injected; such a run can never "
             "support a comparative product claim",
    )
    p_utility_read.set_defaults(func=_cmd_utility_read)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
