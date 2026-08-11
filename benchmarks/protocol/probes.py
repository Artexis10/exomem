"""Known-answer probe definitions; providers execute these elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Literal


@dataclass(frozen=True)
class ProbeSpec:
    kind: Literal["lexical-rare-token", "semantic-zero-overlap", "update-current-state"]
    prompt: str
    allowed_outcomes: tuple[str, ...]
    fact: str = ""
    query: str = ""
    old_marker: str = ""
    current_marker: str = ""


def known_answer_probe_specs() -> tuple[ProbeSpec, ...]:
    outcomes = ("pass", "fail", "inconclusive-by-design")
    return (
        ProbeSpec("lexical-rare-token", "Retrieve the unique rare token exactly.", outcomes, fact="The rare orchard marker identifies the record.", query="Retrieve the rare marker."),
        ProbeSpec("semantic-zero-overlap", "Where was azure atlas relocated?", outcomes, fact="The cobaltic cartographer moved the porcelain codex to the littoral archive.", query="Where was azure atlas relocated?"),
        ProbeSpec("update-current-state", "Return only the current revision after an update.", outcomes, fact="revision-old-opaque revision-current-opaque", query="Which revision is currently authoritative?", old_marker="revision-old-opaque", current_marker="revision-current-opaque"),
    )


def classify_update_outcome(
    hits: Iterable[object], *, old_marker: str = "stale", current_marker: str = "current",
) -> Literal["superseded", "both_returned", "stale_only", "unresolvable"]:
    labels: set[str] = set()
    for hit in hits:
        if isinstance(hit, Mapping):
            label = hit.get("record_id") or hit.get("state") or hit.get("revision") or hit.get("kind") or ""
        else:
            label = str(hit)
        folded = str(label)
        if folded == current_marker:
            labels.add("current")
        if folded == old_marker:
            labels.add("stale")
    if labels == {"current"}:
        return "superseded"
    if labels == {"current", "stale"}:
        return "both_returned"
    if labels == {"stale"}:
        return "stale_only"
    return "unresolvable"


def diagnostic_probe_events():
    """Diagnostic writes are harness-authored user messages, never dataset roles."""
    from .models import DatasetIdentity, EventProvenance, ProtocolEvent

    identity = DatasetIdentity(id="diagnostic", variant="fixture", source="local", revision="1", sha256="0" * 64, case_count=0)
    return tuple(
        ProtocolEvent(
            dataset=identity, case_id=f"__probe__-{spec.kind}", session_ordinal=1, sequence=0,
            role="user", turn_ordinal=1, content=spec.fact, content_sha256=__import__("hashlib").sha256(spec.fact.encode()).hexdigest(),
            original_timestamp="2026-01-01T00:00:00Z", timestamp_semantics="ingestion_order_only", ingestion_ordinal=0,
            provenance=EventProvenance(dataset_row_index=0, upstream_session_id_sha256="0" * 64, converter="harness-probe", converter_version="1"),
        ) for spec in known_answer_probe_specs()
    )
