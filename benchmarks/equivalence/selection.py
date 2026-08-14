"""Hash-ordered, stratified pre-result subset selection."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from pathlib import Path

from protocol.models import LmeSelection

from lme.dataset import QUESTION_TYPES


QUESTION_TYPE_ORDER = QUESTION_TYPES
CANONICAL_LME_S_SOURCE: dict[str, object] = {
    "repository": "xiaowu0162/longmemeval-cleaned",
    "revision": "98d7416c24c778c2fee6e6f3006e7a073259d48f",
    "filename": "longmemeval_s_cleaned.json",
    "sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
    "byte_count": 277383467,
    "row_count": 500,
    "type_census": {
        "knowledge-update": 78,
        "multi-session": 133,
        "single-session-assistant": 56,
        "single-session-preference": 30,
        "single-session-user": 70,
        "temporal-reasoning": 133,
    },
    "abstention_count": 30,
}
CANONICAL_LME_S_25_ARTIFACT_PATH = Path(__file__).with_name("subsets") / "lme-s-25.json"
CANONICAL_LME_S_25_ARTIFACT_SHA256 = "7c46b689758901f73fe365d861d0998ecc64ec0435392df745d63d7da0ccc901"
LME_S_25_ALGORITHM_VERSION = "lme-s-25.sha256-v1"
LME_S_25_ALGORITHM = "sha256(utf8(question_id + dataset_sha256)); sort (digest_hex, question_id)"
LME_S_25_QUOTAS = {**{question_type: 3 for question_type in QUESTION_TYPE_ORDER}, "abstention": 7}


def load_frozen_lme_selection() -> tuple[dict[str, object], bytes]:
    """Read the canonical artifact once, without following a replaceable path."""
    path = CANONICAL_LME_S_25_ARTIFACT_PATH
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError("canonical selection requires a no-follow regular artifact") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("canonical selection requires a no-follow regular artifact")
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ValueError("canonical selection changed during stable read") from exc
    if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
        raise ValueError("canonical selection changed during stable read")
    if hashlib.sha256(raw).hexdigest() != CANONICAL_LME_S_25_ARTIFACT_SHA256:
        raise ValueError("canonical selection frozen bytes differ")
    try:
        artifact = LmeSelection.model_validate_json(raw).model_dump(mode="json")
        canonical = json.dumps(artifact, indent=2, sort_keys=True).encode() + b"\n"
    except Exception as exc:
        raise ValueError("canonical selection artifact is invalid") from exc
    if raw != canonical:
        raise ValueError("canonical selection frozen bytes are not canonical JSON")
    return artifact, raw


def _digest(question_id: str, dataset_sha256: str) -> str:
    return hashlib.sha256((question_id + dataset_sha256).encode("utf-8")).hexdigest()


def _ordered(question_ids: Iterable[str], dataset_sha256: str) -> list[str]:
    return sorted(question_ids, key=lambda question_id: (_digest(question_id, dataset_sha256), question_id))


def select_question_ids(rows: Iterable[Mapping[str, object]], dataset_sha256: str) -> dict[str, object]:
    """Generic fixture selector; deliberately permits partial strata."""

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
    ordered = {key: _ordered(values, dataset_sha256) for key, values in buckets.items()}
    selected = [item for key in sorted(ordered) for item in ordered[key][:3]]
    queue = deque(_ordered(abstention, dataset_sha256))
    selected.extend(queue.popleft() for _ in range(min(7, len(queue))))
    return {"selection": {"algorithm": "sha256(question_id + dataset_sha256), stratified 3-per-type + 7-abstention", "dataset_sha256": dataset_sha256, "strata": {**{key: min(3, len(values)) for key, values in ordered.items()}, "abstention": min(7, len(abstention))}}, "question_ids": selected}


def _canonical_source(source: Mapping[str, object]) -> dict[str, object]:
    value = dict(source)
    if value != CANONICAL_LME_S_SOURCE:
        raise ValueError("source identity does not match frozen LongMemEval-S source")
    return value


def select_lme_s_25(
    rows: Iterable[Mapping[str, object]], *, source: Mapping[str, object]
) -> dict[str, object]:
    """Select the frozen canonical LongMemEval-S comparative cohort.

    This profile is intentionally stricter than :func:`select_question_ids`:
    it accepts only the public frozen source identity and rejects incomplete
    source populations before it can emit a misleading 25-case artifact.
    """

    source_identity = _canonical_source(source)
    dataset_sha256 = str(source_identity["sha256"])
    buckets = {question_type: [] for question_type in QUESTION_TYPE_ORDER}
    abstention: list[str] = []
    seen: set[str] = set()
    for row in rows:
        question_id = row.get("question_id")
        question_type = row.get("question_type")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("canonical selection refuses blank question_id")
        if question_id in seen:
            raise ValueError("canonical selection refuses duplicate question_id")
        if not isinstance(question_type, str) or question_type not in buckets:
            raise ValueError("canonical selection refuses unknown or missing question_type")
        seen.add(question_id)
        buckets[question_type].append(question_id)
        if question_id.endswith("_abs"):
            abstention.append(question_id)

    census = {question_type: len(buckets[question_type]) for question_type in QUESTION_TYPE_ORDER}
    if len(seen) != source_identity["row_count"]:
        raise ValueError("canonical selection row count differs from frozen source")
    if census != source_identity["type_census"]:
        raise ValueError("canonical selection type census differs from frozen source")
    if len(abstention) != source_identity["abstention_count"]:
        raise ValueError("canonical selection abstention census differs from frozen source")
    selected: list[str] = []
    for question_type in QUESTION_TYPE_ORDER:
        ordered = _ordered(
            (question_id for question_id in buckets[question_type] if not question_id.endswith("_abs")),
            dataset_sha256,
        )
        if len(ordered) < 3:
            raise ValueError(f"canonical selection has undersized {question_type} non-abstention stratum")
        selected.extend(ordered[:3])
    ordered_abstentions = _ordered(abstention, dataset_sha256)
    if len(ordered_abstentions) < 7:
        raise ValueError("canonical selection has fewer than 7 abstentions")
    selected.extend(ordered_abstentions[:7])
    if len(selected) != 25 or len(selected) != len(set(selected)):
        raise ValueError("canonical selection did not produce 25 unique question IDs")
    return {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "lme-selection.v1",
        "source_identity": source_identity,
        "selection_algorithm_version": LME_S_25_ALGORITHM_VERSION,
        "selection_algorithm": LME_S_25_ALGORITHM,
        "question_type_order": list(QUESTION_TYPE_ORDER),
        "quotas": LME_S_25_QUOTAS,
        "target_question_ids": selected,
    }
