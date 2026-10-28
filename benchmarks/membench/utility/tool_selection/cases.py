"""Cases: a template plus a seed, the committed dev set and the held-out generator.

A case is ``(template, seed)``. Its parameters come from ``random.Random`` seeded
by the template id and the seed, so a case rebuilds byte for byte; the dev file
also stores the parameters, so a change to a template's draw shows as a diff and
a test, never as a silent change of the measured cases.

The held-out generator draws fresh seeds from a master seed, refuses any seed
the dev set uses, and spreads the cases evenly over the templates. The dev
round's results must be known before it runs (design: generated and frozen after
the dev round); nothing in this package generates or stores held-out cases.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .templates import TEMPLATES, Key, SeedStep, Template

DEV_FILE = Path(__file__).parent / "dev_cases.json"

#: The dev set's plan: ``(template, seeds)``. Four templates carry three cases and
#: four carry two, so the 20 cases span the 8 groups the comparison flips.
DEV_PLAN: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("records-log-entry", (101, 102, 103)),
    ("records-update-row", (101, 102, 103)),
    ("records-range-total", (101, 102, 103)),
    ("records-create-collection", (101, 102)),
    ("plan-add-item", (101, 102)),
    ("plan-triage-item", (101, 102, 103)),
    ("plan-committed-week", (101, 103)),
    ("plan-item-history", (103, 108)),
)

#: Held-out seeds start here, above every dev seed.
HELDOUT_SEED_FLOOR = 10_000


@dataclass(frozen=True)
class Case:
    case_id: str
    template_id: str
    seed: int
    params: dict
    #: Cases that share invented source bytes share a sampling unit. Legacy data keeps its template group.
    fixture_cluster: str | None = None

    @property
    def template(self) -> Template:
        if self.template_id == "source-derived":
            from .development import source_template

            return source_template(self.params)
        return TEMPLATES[self.template_id]

    @property
    def cluster_id(self) -> str:
        return self.fixture_cluster or self.template_id

    @property
    def kind(self) -> str:
        return self.template.kind

    @property
    def prompt(self) -> str:
        return self.template.prompt(self.params)

    @property
    def key(self) -> Key:
        return self.template.key(self.params)

    def seed_steps(self) -> list[SeedStep]:
        return self.template.seed_steps(self.params)

    def as_dict(self) -> dict:
        result = {
            "case_id": self.case_id,
            "template": self.template_id,
            "seed": self.seed,
            "params": self.params,
        }
        if self.fixture_cluster is not None:
            result["fixture_cluster"] = self.fixture_cluster
        return result


def case_id_for(template_id: str, seed: int) -> str:
    return f"{template_id}-s{seed}"


def build_case(template_id: str, seed: int) -> Case:
    template = TEMPLATES[template_id]
    params = template.draw(random.Random(f"{template_id}:{seed}"))
    # A JSON round trip is the form the case is stored in, so params compare equal.
    return Case(case_id_for(template_id, seed), template_id, seed, json.loads(json.dumps(params)))


def dev_cases_from_plan() -> list[Case]:
    return [build_case(template, seed) for template, seeds in DEV_PLAN for seed in seeds]


def load_cases(path: Path) -> list[Case]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        document = rows
        rows = [
            {**row, "params": {**row["params"],
                               "fixture": document["fixtures"][row["params"]["fixture_id"]],
                               "source_reference_sha256": document["source_reference_sha256"],
                               "source_commit": document["source_commit"]}}
            for row in document["cases"]
        ]
    return [Case(row["case_id"], row["template"], row["seed"], row["params"], row.get("fixture_cluster")) for row in rows]


def dev_cases() -> list[Case]:
    return load_cases(DEV_FILE)


def write_cases(cases: Sequence[Case], path: Path) -> None:
    Path(path).write_text(
        json.dumps([case.as_dict() for case in cases], indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate_heldout(count: int, master_seed: int) -> list[Case]:
    """``count`` cases spread over the templates, from seeds above every dev seed.

    The master seed is the generator's only input, so one value regenerates the
    same set; the caller records it with the file it writes.
    """

    if count < len(TEMPLATES):
        raise ValueError(f"need at least one case per template ({len(TEMPLATES)})")
    rng = random.Random(f"heldout:{master_seed}")
    ids = sorted(TEMPLATES)
    cases: list[Case] = []
    taken: set[tuple[str, int]] = set()
    for index in range(count):
        template_id = ids[index % len(ids)]
        while True:
            seed = HELDOUT_SEED_FLOOR + rng.randrange(1_000_000)
            if (template_id, seed) not in taken:
                break
        taken.add((template_id, seed))
        cases.append(build_case(template_id, seed))
    return cases
