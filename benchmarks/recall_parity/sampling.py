"""Draw a seeded tuning and held-out sample of distinct queries from query logs.

Input is ``queries.jsonl`` as ``exomem.query_log.log_find_call`` writes it; the
only fields read are ``query`` and ``outcome``. Every rule that drops a record
is structural, never a judgement about what a query means:

- the record is not a JSON object with a string ``query`` (``malformed``);
- ``outcome`` is present and is not ``"success"`` (``outcome_not_success``);
- the query has no alphanumeric character (``no_alphanumeric``);
- the query is shorter than ``min_chars`` once whitespace is collapsed
  (``too_short``);
- the query equals an earlier one after whitespace collapse and case folding
  (``duplicates``). Each distinct query then weighs the same, however often it
  was asked.

The draw and the split order queries by a SHA-256 of the seed and the query
text, so they do not depend on file order or on rotation of the log.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA = "recall-parity/sample/1"
SPLITS = ("tuning", "heldout")
#: Real one-word queries stay in the decision population; only a lone character is dropped.
DEFAULT_MIN_CHARS = 2


def normalize(query: str) -> str:
    """Collapse runs of whitespace; case is kept for sending and folded for identity."""

    return " ".join(query.split())


def case_id(query: str) -> str:
    """A stable opaque id for one distinct query, independent of the seed."""

    key = normalize(query).casefold()
    return "q-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _order_key(seed: int, purpose: str, query: str) -> str:
    material = f"{seed}\0{purpose}\0{normalize(query).casefold()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def read_distinct_queries(
    paths: Sequence[Path], *, min_chars: int
) -> tuple[list[str], dict[str, Any]]:
    """Distinct eligible queries in first-seen order, and what was read and dropped."""

    dropped: Counter[str] = Counter()
    inputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    kept: list[str] = []
    for path in paths:
        raw = Path(path).read_bytes()
        records = 0
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            records += 1
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            query = record.get("query") if isinstance(record, dict) else None
            if not isinstance(query, str):
                dropped["malformed"] += 1
                continue
            if record.get("outcome", "success") != "success":
                dropped["outcome_not_success"] += 1
                continue
            text = normalize(query)
            if not any(char.isalnum() for char in text):
                dropped["no_alphanumeric"] += 1
            elif len(text) < min_chars:
                dropped["too_short"] += 1
            elif text.casefold() in seen:
                dropped["duplicates"] += 1
            else:
                seen.add(text.casefold())
                kept.append(text)
        inputs.append({"sha256": hashlib.sha256(raw).hexdigest(), "records": records})
    counts = {
        "records_read": sum(item["records"] for item in inputs),
        "malformed": dropped["malformed"],
        "outcome_not_success": dropped["outcome_not_success"],
        "no_alphanumeric": dropped["no_alphanumeric"],
        "too_short": dropped["too_short"],
        "duplicates": dropped["duplicates"],
        "distinct_eligible": len(kept),
    }
    return kept, {"inputs": inputs, "counts": counts}


def draw_sample(
    queries: Sequence[str], *, seed: int, size: int, tuning_fraction: float
) -> list[dict[str, str]]:
    """Take ``size`` queries and split them, deterministically in ``seed``.

    Returns cases in draw order, each ``{"case_id", "split", "query"}``.
    """

    if not 0 < tuning_fraction < 1:
        raise ValueError("tuning_fraction must be strictly between 0 and 1")
    drawn = sorted(queries, key=lambda query: _order_key(seed, "draw", query))[:size]
    by_split = sorted(drawn, key=lambda query: _order_key(seed, "split", query))
    tuning = set(by_split[: round(len(drawn) * tuning_fraction)])
    if not 0 < len(tuning) < len(drawn):
        raise ValueError("the sample is too small to leave both a tuning and a held-out set")
    return [
        {
            "case_id": case_id(query),
            "split": "tuning" if query in tuning else "heldout",
            "query": query,
        }
        for query in drawn
    ]


def build_sample(
    paths: Sequence[Path], *, seed: int, size: int, tuning_fraction: float, min_chars: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Cases and the manifest that describes how they were drawn (no query text)."""

    queries, read = read_distinct_queries(paths, min_chars=min_chars)
    cases = draw_sample(queries, seed=seed, size=size, tuning_fraction=tuning_fraction)
    sizes = Counter(case["split"] for case in cases)
    manifest = {
        "schema": SCHEMA,
        "seed": seed,
        "requested_size": size,
        "tuning_fraction": tuning_fraction,
        "min_chars": min_chars,
        "inputs": read["inputs"],
        "counts": read["counts"],
        "sampled": len(cases),
        "split_sizes": {name: sizes[name] for name in SPLITS},
    }
    return cases, manifest
