"""Write judge-batch scenarios for a multi-turn case folder (`scenarios/follow-up`).

`replay_cases.write_scenarios` serves one turn. A follow-up case has several, and the judge needs all of them to read the last
reply, so this writer folds each case's turns into one transcript prompt and reuses `replay_cases.scenario_for`. Only
`judge-prepare --suite <out>/scenarios` reads the result; no actor runs from it.

    PYTHONPATH=benchmarks python -m import_guidance.followup_judge_scenarios --case-dir <cases> --out <outside every repository>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from epistemic.journeys.f27_replay import JourneySetupError, refuse_unsafe_out_dir

from . import replay_cases
from .session_close import load_cases


def transcript(turns: tuple[str, ...]) -> str:
    earlier = "".join(f"Turn {n}: {turn}\n" for n, turn in enumerate(turns[:-1], start=1))
    return f"{earlier}Final turn (the reply to judge answers this one): {turns[-1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        out = refuse_unsafe_out_dir(args.out)
    except JourneySetupError as error:
        raise SystemExit(str(error)) from error
    rubric = replay_cases.read_rubric(args.case_dir)
    folder = out / "scenarios"
    folder.mkdir(parents=True, exist_ok=True)
    for case in load_cases("all", args.case_dir):
        folded = dataclasses.replace(case, turns=(transcript(case.turns),))
        scenario = replay_cases.scenario_for(folded, replay_cases.read_marks(case), rubric)
        (folder / f"{case.id}.json").write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(list(folder.glob('*.json')))} scenarios under {folder}")


if __name__ == "__main__":
    main()
