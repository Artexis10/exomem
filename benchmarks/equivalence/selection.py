"""Hash-ordered, stratified pre-result subset selection."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping


def select_question_ids(rows: Iterable[Mapping[str, object]], dataset_sha256: str) -> dict[str, object]:
    buckets: dict[str, list[str]] = defaultdict(list)
    abstention: list[str] = []
    for row in rows:
        question_id = row.get("question_id")
        if not isinstance(question_id, str):
            raise ValueError("selection rows require question_id")
        if question_id.endswith("_abs"):
            abstention.append(question_id)
            continue
        question_type = row.get("question_type")
        if not isinstance(question_type, str):
            raise ValueError("answerable selection rows require question_type")
        buckets[question_type].append(question_id)
    ordered = {key: sorted(values, key=lambda value: hashlib.sha256((value + dataset_sha256).encode()).hexdigest()) for key, values in buckets.items()}
    selected = [item for key in sorted(ordered) for item in ordered[key][:3]]
    queue = deque(sorted(abstention, key=lambda value: hashlib.sha256((value + dataset_sha256).encode()).hexdigest()))
    selected.extend(queue.popleft() for _ in range(min(7, len(queue))))
    return {"selection": {"algorithm": "sha256(question_id + dataset_sha256), stratified 3-per-type + 7-abstention", "dataset_sha256": dataset_sha256, "strata": {**{key: min(3, len(values)) for key, values in ordered.items()}, "abstention": min(7, len(abstention))}}, "question_ids": selected}
