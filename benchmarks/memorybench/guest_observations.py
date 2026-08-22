"""Project the Exomem guest's own call log into export observations.

The guest already records a request/response pair for every call it makes, so
nothing here instruments anything new. It reads what was written and publishes
only what those entries prove: a half-recorded call, a response that breaks the
guest's own limit contract, or an empty directory all yield absence, never a
value. Absence keeps its `missing_fields` label; that coupling is enforced by
`MemoryBenchExportCase`.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEARCH_PATH = "/api/ask_memory"
INGEST_PATH = "/api/capture_source"
READ_PATH = "/api/read_memory"
DOCTOR_RESPONSE = "doctor-response"
SEMANTIC_DOCTOR_CHECKS = frozenset({
    "embeddings.enabled",
    "dep.sentence-transformers",
    "dep.torch",
    "dep.pillow",
    "models.cache",
    "embeddings.sidecar",
})

SEARCH_LABELS = frozenset({
    "search.transmitted_query",
    "search.options.limit",
    "search.normalized_hit_ids",
})
INGEST_LABELS = frozenset({"ingest.transmitted_payloads"})

#: The renderer identity is a property of the pinned harness, not of a run.
SESSION_NORMALIZATION = "memorybench.longmemeval_to_corpus/v1"

_ENTRY_NAME = re.compile(r"^operation-(\d{6})-[0-9a-f]{12}\.json$")
_MAX_ENTRY_BYTES = 8 * 1024 * 1024
_MAX_ENTRIES = 100_000

_EVIDENCE_INVALID = "guest_evidence_invalid"
_EVIDENCE_INCOMPLETE = "guest_evidence_incomplete"


@dataclass(frozen=True)
class GuestObservations:
    """What the guest's log proves, plus any reason it proved less than expected."""

    search: dict[str, Any] | None = None
    ingest: dict[str, Any] | None = None
    readiness: list[dict[str, Any]] | None = None
    session_normalization: str | None = None
    problems: frozenset[str] = frozenset()

    def resolved_labels(self) -> frozenset[str]:
        """Missing-field labels this projection is entitled to clear."""

        labels: set[str] = set()
        if self.search is not None:
            labels |= SEARCH_LABELS
        if self.ingest is not None:
            labels |= INGEST_LABELS
        return frozenset(labels)


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def read_guest_evidence(evidence_dir: Path) -> tuple[list[dict[str, Any]], set[str]]:
    """Read one guest evidence directory in transmission order.

    Ordering comes from the sequence field in the filename, never from
    directory order: sequence 10 must not sort before sequence 2.
    """

    problems: set[str] = set()
    try:
        metadata = evidence_dir.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return [], problems
    except OSError:
        return [], {_EVIDENCE_INVALID}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return [], {_EVIDENCE_INVALID}

    numbered: list[tuple[int, Path]] = []
    for child in evidence_dir.iterdir():
        match = _ENTRY_NAME.match(child.name)
        if match is None:
            # The secure descriptor is a sibling input validated independently
            # by the cleanup/export coordinator, never operation evidence.
            if child.name == "service.json":
                continue
            # Lock reservations and unrelated names are not evidence.
            if not child.name.startswith("."):
                problems.add(_EVIDENCE_INVALID)
            continue
        numbered.append((int(match.group(1)), child))
    if len(numbered) > _MAX_ENTRIES:
        return [], {_EVIDENCE_INVALID}

    entries: list[dict[str, Any]] = []
    for _sequence, path in sorted(numbered, key=lambda item: item[0]):
        try:
            if path.lstat().st_size > _MAX_ENTRY_BYTES:
                problems.add(_EVIDENCE_INVALID)
                continue
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            problems.add(_EVIDENCE_INVALID)
            continue
        if not isinstance(entry, dict) or entry.get("protocol_version") != 1:
            problems.add(_EVIDENCE_INVALID)
            continue
        if not isinstance(entry.get("event"), str) or not isinstance(entry.get("data"), dict):
            problems.add(_EVIDENCE_INVALID)
            continue
        entries.append(entry)
    return entries, problems


def _call_path(entry: dict[str, Any]) -> str | None:
    value = entry["data"].get("path")
    return value if isinstance(value, str) else None


def _project_search(entries: Sequence[dict[str, Any]], problems: set[str]) -> dict[str, Any] | None:
    request: dict[str, Any] | None = None
    response: Any = None
    for entry in entries:
        if _call_path(entry) != SEARCH_PATH:
            continue
        if entry["event"] == "request":
            if request is not None:
                # More than one search per case is outside this contract.
                problems.add(_EVIDENCE_INVALID)
                return None
            body = entry["data"].get("body")
            request = body if isinstance(body, dict) else None
            if request is None:
                problems.add(_EVIDENCE_INVALID)
                return None
        elif entry["event"] == "response":
            response = entry["data"].get("response")
    if request is None:
        return None

    query = request.get("query")
    limit = request.get("limit")
    if not isinstance(query, str) or not query:
        return None
    if not isinstance(limit, bool) and isinstance(limit, int) and limit > 0:
        pass
    else:
        return None
    if response is None:
        problems.add(_EVIDENCE_INCOMPLETE)
        return None
    if not isinstance(response, list):
        problems.add(_EVIDENCE_INVALID)
        return None

    hit_ids: list[str] = []
    for selected in response:
        if not isinstance(selected, dict):
            problems.add(_EVIDENCE_INVALID)
            return None
        path = selected.get("path")
        if not isinstance(path, str) or not path:
            problems.add(_EVIDENCE_INVALID)
            return None
        hit_ids.append(path)
    if len(hit_ids) > limit:
        # The guest refuses an over-limit response before returning, so
        # evidence showing one is not a value to publish.
        problems.add(_EVIDENCE_INVALID)
        return None
    return {
        "transmitted_query": query,
        "options": {"limit": limit},
        "normalized_hit_ids": hit_ids,
    }


def _project_ingest(entries: Sequence[dict[str, Any]], problems: set[str]) -> dict[str, Any] | None:
    digests: list[str] = []
    for entry in entries:
        if entry["event"] != "request" or _call_path(entry) != INGEST_PATH:
            continue
        body = entry["data"].get("body")
        if not isinstance(body, dict):
            problems.add(_EVIDENCE_INVALID)
            return None
        digests.append(_canonical_digest(body))
    if not digests:
        return None
    return {"transmitted_payload_sha256": digests}


def _project_readiness(
    entries: Sequence[dict[str, Any]], problems: set[str]
) -> list[dict[str, Any]] | None:
    checks: dict[str, str] | None = None
    for entry in entries:
        if entry["event"] != DOCTOR_RESPONSE:
            continue
        response = entry["data"].get("response")
        if not isinstance(response, dict):
            problems.add(_EVIDENCE_INVALID)
            return None
        candidate = response.get("checks")
        if not isinstance(candidate, list):
            problems.add(_EVIDENCE_INVALID)
            return None
        projected: dict[str, str] = {}
        for check in candidate:
            if not isinstance(check, dict):
                problems.add(_EVIDENCE_INVALID)
                return None
            check_id, status = check.get("id"), check.get("status")
            if (
                not isinstance(check_id, str)
                or not check_id
                or status not in {"pass", "warn", "fail"}
                or check_id in projected
            ):
                problems.add(_EVIDENCE_INVALID)
                return None
            projected[check_id] = status
        checks = projected
    if checks is None:
        return None

    failed = sorted(name for name in SEMANTIC_DOCTOR_CHECKS if checks.get(name) != "pass")
    verified = not failed
    evidence = (
        "hybrid doctor checks pass: " + ", ".join(sorted(SEMANTIC_DOCTOR_CHECKS))
        if verified
        else "hybrid doctor checks failed: " + ", ".join(failed)
    )
    return [{
        "lane": "semantic",
        "requested": True,
        "verified": verified,
        "method": "doctor-check" if verified else "readiness-unverifiable",
        "evidence": evidence,
        # A failed semantic check on this path means the guest served a
        # degraded lane, which is precisely a fallback.
        "fallback_detected": bool(failed),
    }]


def project_guest_evidence(evidence_dir: Path) -> GuestObservations:
    """Read one guest evidence directory and publish only what it proves."""

    entries, problems = read_guest_evidence(Path(evidence_dir))
    search = _project_search(entries, problems)
    ingest = _project_ingest(entries, problems)
    readiness = _project_readiness(entries, problems)
    return GuestObservations(
        search=search,
        ingest=ingest,
        readiness=readiness,
        session_normalization=SESSION_NORMALIZATION if entries else None,
        problems=frozenset(problems),
    )
