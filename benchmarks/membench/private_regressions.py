"""Private founder-regression fixture format (LOCAL-ONLY DATA).

The DATA lives in ``benchmarks/private/founder-regressions.jsonl`` which is
gitignored; only this format module is committed. Records are excluded from
git, CI, telemetry, published artifacts, and any model upload by default, and
are never aggregated into published numbers. A record may graduate into the
public corpus only through a deliberate synthetic rewrite (set
``converted_to`` to the public query id once done).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from membench.schema import StrictModel

PRIVATE_DIR_NAME = "private"
FIXTURES_BASENAME = "founder-regressions.jsonl"


class FounderRegression(StrictModel):
    id: str
    recorded_on: str  # ISO date, author-supplied (no wall-clock in tooling)
    natural_prompt: str
    should_activate: bool
    relevant_sources: list[str] = Field(default_factory=list)  # vault-relative
    expected_durable_conclusion: str | None = None
    observed_result: str
    explicit_prompt_needed: bool | None = None
    correction_writeback_success: bool | None = None
    privacy_class: Literal["P0", "P1", "P2", "P3"]  # P0 = most sensitive
    synthetic_convertible: bool = False
    converted_to: str | None = None  # public corpus query id after rewrite
    notes: str | None = None


def fixtures_path(bench_root: Path) -> Path:
    return Path(bench_root) / PRIVATE_DIR_NAME / FIXTURES_BASENAME


def load_regressions(bench_root: Path) -> list[FounderRegression]:
    """Load local fixtures; an absent file is a normal empty state."""

    path = fixtures_path(bench_root)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(FounderRegression.model_validate_json(line))
    return records


def replay_activation_cases(bench_root: Path) -> list[dict]:
    """Stub replayer: pairs each activation case with the Track C driver.

    Full replay requires the natural-prompt driver (network/user-run); this
    stub returns the work items so the driver can consume them, and exists so
    the format has an executable consumer from day one.
    """

    return [
        {
            "id": record.id,
            "prompt": record.natural_prompt,
            "should_activate": record.should_activate,
            "privacy_class": record.privacy_class,
        }
        for record in load_regressions(bench_root)
        if record.privacy_class != "P0"  # P0 never leaves the local store
    ]
