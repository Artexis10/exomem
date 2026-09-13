"""Artifact-only, offline consolidation of one or more completed runs.

    python -m benchmarks.reports.consolidate --run <dir> [--run <dir>...] --out <dir>

Before rendering anything, re-reads and re-hashes every pre-registration
receipt through ``protocol.contracts.derive_preregistration_identity`` --
never trusting a run's own claimed identity -- and writes it (sha256 plus the
ordered amendment chain) into ``consolidated.json``. Refuses any non-terminal
manifest or unknown ``schema_version`` (via ``reports.render.render_all``),
runs entirely under ``offline_guard``, and writes ``consolidated.json`` then
``report.md`` into ``--out`` on success only: exit 0 (identity first, so a
failure between the two writes can never leave a report standing without the
identity it was rendered against). On refusal it prints one line to stderr,
exits non-zero, and writes nothing. It never mutates a run directory.

Each ``--run`` directory is classified independently for two purposes that
are not mutually exclusive: its LME manifest or Epistemic cohort (at most
one of the two -- carrying both is refused as ambiguous) feeds
``render_all``'s per-ability / per-scenario sections, and a
``memorybench-export.v1.json`` alongside either of those (or on its own)
feeds a ``ProviderLatency`` observation into ``render_all``'s ``latency``
parameter -- the wiring 9.1's cross-provider latency refusal needs to ever
run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ``python -m benchmarks.reports.consolidate`` starts with the repository
# root on sys.path, while the reused lane renderers are intentionally
# importable as ``protocol``/``lme``/``epistemic`` from the benchmarks
# directory (tests/conftest.py already installs this same path for pytest;
# this entrypoint does it explicitly for a standalone run). Mirrors
# benchmarks/lme/cli.py's identical sys.path insert.
_BENCHMARKS_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_ROOT))

from protocol.contracts import derive_preregistration_identity  # noqa: E402
from protocol.models import MemoryBenchExport  # noqa: E402
from protocol.offline import offline_guard  # noqa: E402

from .latency import ProviderLatency  # noqa: E402
from .render import EpistemicCohort, LmeRun, ReportInput, render_all  # noqa: E402

_REPO_ROOT = _BENCHMARKS_ROOT.parent

#: The stored, validated cohort artifact name persist_validated_cohort writes
#: (epistemic.report._load_stored_cohort_without_trusting_claims's sole input
#: shape) -- used only to recognize which lane a --run directory belongs to.
_EPISTEMIC_COHORT_NAME = "validated-cohort.v1.json"

#: The MemoryBench export artifact name memorybench/export.py's real writer
#: uses (benchmarks/memorybench/export.py:1693: `write(output_root /
#: "memorybench-export.v1.json", ...)`).
_MEMORYBENCH_EXPORT_NAME = "memorybench-export.v1.json"


def _classify(run_dir: Path) -> ReportInput | None:
    """Which lane renders `run_dir`'s own section, or None for a directory
    that carries no LME/Epistemic artifact of its own -- a MemoryBench
    export sitting alone, latency-only, handled by `_latency_observation`."""

    has_manifest = (run_dir / "manifest.json").is_file()
    cohort_path = run_dir / _EPISTEMIC_COHORT_NAME
    has_cohort = cohort_path.is_file()
    if has_manifest and has_cohort:
        raise ValueError(
            f"{run_dir}: carries both an LME manifest and an epistemic cohort -- ambiguous"
        )
    if has_manifest:
        return LmeRun(run_dir=run_dir)
    if has_cohort:
        return EpistemicCohort(cohort_path=cohort_path, run_root=run_dir)
    if (run_dir / _MEMORYBENCH_EXPORT_NAME).is_file():
        return None
    raise ValueError(f"{run_dir}: not a recognized run artifact directory")


def _latency_observation(run_dir: Path) -> ProviderLatency | None:
    """Read `run_dir`'s MemoryBench export latency flag, if it carries one.

    Validates the WHOLE export through the real `MemoryBenchExport` model --
    not just its `latency` sub-object -- so an export with an unknown
    `schema_version`, a wrong `artifact_type`, or any other malformed shape
    is refused exactly like a non-terminal/unknown-schema_version LME
    manifest is (benchmark-protocol/spec.md:63-90; D3): a pydantic
    `ValidationError` propagates as a refusal (consolidate() writes nothing,
    main() exits non-zero), never swallowed into a raw `KeyError` that could
    let a malformed export render a latency row.

    The export never carries a millisecond figure (protocol.models.
    MemoryBenchExport has no such field -- membench's own numbers live in a
    lane this package does not read), so `value_ms` is always None from this
    source; see reports/latency.py::ProviderLatency's docstring.
    """

    export_path = run_dir / _MEMORYBENCH_EXPORT_NAME
    if not export_path.is_file():
        return None
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    export = MemoryBenchExport.model_validate(payload)
    return ProviderLatency(provider=export.provider, latency=export.latency)


def consolidate(run_dirs: list[Path], out_dir: Path, *, repo_root: Path | None = None) -> None:
    """Render `run_dirs` and write consolidated.json + report.md into `out_dir`.

    Raises (writing nothing) on any refusal -- a non-terminal manifest, an
    unknown schema_version, an unrecognized or ambiguous run directory, or a
    pre-registration identity that fails to re-derive. `out_dir` is touched
    only after every input has rendered and the identity has re-derived
    successfully; consolidated.json is written before report.md.
    """

    root = repo_root or _REPO_ROOT
    inputs: list[ReportInput] = []
    latency: list[ProviderLatency] = []
    for run_dir in run_dirs:
        classified = _classify(run_dir)
        if classified is not None:
            inputs.append(classified)
        observation = _latency_observation(run_dir)
        if observation is not None:
            latency.append(observation)
    with offline_guard():
        report = render_all(inputs, latency=latency)
        identity = derive_preregistration_identity(root)
    payload = {
        "runs": [str(run_dir) for run_dir in run_dirs],
        "preregistration_identity": identity.model_dump(mode="json"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "consolidated.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--run", action="append", dest="runs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        consolidate(list(args.runs), args.out)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: one-line refusal reason
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
