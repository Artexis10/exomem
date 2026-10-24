"""Destination diagnostics with independent truth and intact long-form seeds.

Give capture actors only ``actor_view`` turns and the seeded prior notes.
Give the later question to a fresh session after publication. Keep evaluator
truth outside both actor inputs. Judge semantic outcomes with that truth;
``Frozen`` binds the evidence and does not supply a semantic scorer.
Standing user instructions still determine ordinary-initiation eligibility.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import observation as obs
from .contract import sha256_json

FIXTURE_SET_ID = "memory-loop-destination-cohesion-v1"
FIXTURES = Path(__file__).with_name("fixtures")
CASES_PATH = FIXTURES / "destination-cohesion-cases.json"
TRUTH_PATH = FIXTURES / "destination-cohesion-truth.json"


def source_case(case_id: str) -> dict[str, Any]:
    """Read one independent source case without loading evaluator truth."""
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    return next(case for case in cases if case["case_id"] == case_id)


def actor_view(case_id: str) -> dict[str, Any]:
    return {"episode_id": case_id, "turns": [source_case(case_id)["original_turn"]]}


def frozen(
    case_id: str, *, shipped_prompts: Mapping[str, frozenset[str]] | None = None
) -> obs.Frozen:
    """Bind a run to source truth; archived arms supply their own prompt digests."""
    case = source_case(case_id)
    return obs.Frozen(
        fixture_id=f"{FIXTURE_SET_ID}/{case_id}",
        actor_sha256=sha256_json(actor_view(case_id)),
        pre_capture_sha256=sha256_json(case["prior_notes"]),
        evaluator_sha256=hashlib.sha256(TRUTH_PATH.read_bytes()).hexdigest(),
        turns_sha256=obs.turns_sha256([case["original_turn"]]),
        later_turn_sha256=obs.text_sha256(case["later_ordinary_question"]),
        shipped_prompts=obs.shipped_prompts() if shipped_prompts is None else shipped_prompts,
    )
