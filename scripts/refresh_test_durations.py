"""Refresh `.test_durations.json` (pytest-split timings) from real CI evidence.

The durations file drives pytest-split's `least_duration` sharding in
`.github/workflows/ci.yml`. A stale file skews the shards: when 42% of cases
were unrecorded, pytest-split predicted 388 s for a shard that ran 970 s.

Two refresh routes, both from evidence rather than local runs:

`from-junit` — rebuild the file from the junit XML artifacts a CI run uploads
(`core-*/harness-*-junit`, previously `lean-*-junit`)::

    uv run python scripts/refresh_test_durations.py from-junit path/to/*.xml

`apply` — merge one or more downloaded durations artifacts (the nightly full
run stores per-shard durations with `--store-durations` and uploads each as a
`test-durations-*` artifact)::

    gh run download <run-id> --pattern 'test-durations-*' --dir durations/
    uv run python scripts/refresh_test_durations.py apply durations/**/*.json

Neither route runs in CI against the repository: durations refresh is a
reviewed local change, never an auto-commit from a workflow.

junit → node-id mapping: pytest writes `classname="tests.test_x[.Class...]"`
and `name="test_y[param]"`. The node id is `tests/test_x.py::Class::test_y[param]`,
recovered by walking the dotted classname against the filesystem to find the
module file (so `tests.scripts.test_startup_benchmark` maps to
`tests/scripts/test_startup_benchmark.py`, and anything after the module file
is a class path). Parametrized ids pass through byte-for-byte in `name`;
xfail/skipped cases keep their recorded time. Collection-level module skips
(`classname=""`) have no per-test node id and are dropped — pytest-split never
sees those items either.

Privacy: a handful of parametrized node ids embed path-shaped *test data*
(drive-letter paths, raw media byte strings) that the public-repository
privacy gate (`scripts/validate-public-artifacts.py`) refuses in any checked-in
file. Those entries are dropped on write, using the gate's own rules rather
than a restated copy; pytest-split hands the affected cases the average
recorded duration, which is noise at their measured size.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DURATIONS_PATH = REPO_ROOT / ".test_durations.json"

_SRC_PATH = REPO_ROOT / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

from exomem.public_artifact_privacy import _scan_text  # noqa: E402


class MappingError(RuntimeError):
    """A junit case id could not be mapped unambiguously to a pytest node id."""


def _node_id(classname: str, name: str, repo_root: Path) -> str | None:
    """Map a junit (classname, name) pair to a pytest node id.

    Returns None for entries that are not test cases (module-level collection
    skips carry an empty classname). Raises MappingError when no module file
    on disk matches the dotted classname.
    """
    if not classname:
        return None
    parts = classname.split(".")
    for split in range(len(parts), 0, -1):
        module = repo_root.joinpath(*parts[:split]).with_suffix(".py")
        if module.is_file():
            rel = module.relative_to(repo_root).as_posix()
            class_path = parts[split:]
            return "::".join([rel, *class_path, name])
    raise MappingError(f"no module file found for junit classname {classname!r}")


def durations_from_junit(paths: list[Path], repo_root: Path = REPO_ROOT) -> dict[str, float]:
    durations: dict[str, float] = {}
    dropped = 0
    for path in paths:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            node = _node_id(case.get("classname", ""), case.get("name", ""), repo_root)
            if node is None:
                dropped += 1
                continue
            durations[node] = durations.get(node, 0.0) + float(case.get("time", "0") or 0.0)
    if dropped:
        print(
            f"note: dropped {dropped} module-level collection-skip entries "
            "(no per-test node id exists for those)",
            file=sys.stderr,
        )
    return durations


def _load(path: Path) -> dict[str, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(k, str) and isinstance(v, (int, float)) for k, v in payload.items()
    ):
        raise SystemExit(f"{path} is not a pytest-split durations mapping")
    return {k: float(v) for k, v in payload.items()}


def _privacy_clean(durations: dict[str, float]) -> dict[str, float]:
    """Drop entries whose serialized node id the privacy gate would refuse.

    Judged against the exact text that would land in the checked-in file
    (`json.dumps` escaping included), with the gate's own content rules, so
    this cannot drift from what `validate-public-artifacts.py --repository`
    enforces.
    """
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for node, seconds in durations.items():
        if _scan_text(json.dumps(node), ".test_durations.json"):
            dropped.append(node)
        else:
            kept[node] = seconds
    if dropped:
        print(
            f"note: dropped {len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} "
            "whose parametrized node id trips the public-artifact privacy gate; "
            "pytest-split will use the average duration for those cases",
            file=sys.stderr,
        )
    return kept


def _write(durations: dict[str, float]) -> None:
    durations = _privacy_clean(durations)
    DURATIONS_PATH.write_text(
        json.dumps(dict(sorted(durations.items())), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(durations)} durations to {DURATIONS_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    junit = sub.add_parser("from-junit", help="rebuild durations from junit XML artifacts")
    junit.add_argument("paths", nargs="+", type=Path, help="junit XML files")
    junit.add_argument(
        "--merge",
        action="store_true",
        help="merge over the existing file instead of replacing it",
    )

    apply_cmd = sub.add_parser("apply", help="merge downloaded durations artifacts")
    apply_cmd.add_argument("paths", nargs="+", type=Path, help="durations JSON files")
    apply_cmd.add_argument(
        "--prune",
        action="store_true",
        help="drop node ids absent from every provided artifact",
    )

    args = parser.parse_args(argv)

    if args.command == "from-junit":
        fresh = durations_from_junit(args.paths)
        if not fresh:
            raise SystemExit("no test cases found in the provided junit files")
        if args.merge:
            merged = _load(DURATIONS_PATH) if DURATIONS_PATH.is_file() else {}
            merged.update(fresh)
            _write(merged)
        else:
            _write(fresh)
        return 0

    if args.command == "apply":
        union: dict[str, float] = {}
        for path in args.paths:
            union.update(_load(path))
        if not union:
            raise SystemExit("the provided durations artifacts are empty")
        if args.prune:
            _write(union)
        else:
            merged = _load(DURATIONS_PATH) if DURATIONS_PATH.is_file() else {}
            merged.update(union)
            _write(merged)
        return 0

    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
