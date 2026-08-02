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
    p_run.add_argument("--top-k", type=int, default=10)
    p_run.add_argument("--runs-root", default=str(_BENCH_ROOT / "runs"))
    p_run.add_argument("--label", default=None)
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
