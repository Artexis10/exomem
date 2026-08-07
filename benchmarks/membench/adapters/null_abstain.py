"""Null-abstain adapter: the floor, and a validity check on every dimension.

A contender that retrieves nothing, ever, so the shared answerer abstains on
every query. It is the cheapest possible system that is still honest, and its
score is the number every real result must be read against.

Why a floor is not busywork
===========================

**Abstention is the dimension most at risk of measuring nothing.** On the first
like-for-like head-to-head exomem scored 180/56 against basic-memory's 156/80,
and that gap was reported as real signal. But a gate that rewards declining to
answer is trivially gamed by declining to answer, and nothing in the suite
established what pure abstention earns. If this adapter scores near 180 the
dimension is measuring willingness to say nothing rather than knowing when to,
and the headline needs withdrawing. If it scores far below, the gap is real and
now has a floor under it. Either way the question stops being open.

The same logic generalises: any dimension where the floor is close to a
contender's score is a dimension that contender is not actually being tested
on. Published figures carry floor and ceiling for this reason — a number
between two unknown bounds is not a measurement.

Not the same as a broken run
============================

Returning zero hits is exactly what a silently-failing harness looks like, and
this suite has published 236 plausible zeros before (task 4b.24). The
difference is *declared intent*: this adapter says up front that it retrieves
nothing, so the retrieval floor guard must not read it as an environment fault.
That is why it is a registered contender with a name rather than a flag on
another adapter — a zero from `null-abstain` is a measurement, a zero from
`exomem-local` is an incident, and the two must never be confusable.
"""

from __future__ import annotations

import time
from pathlib import Path

from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    register_adapter,
)
from membench.schema import SourceRecord, load_jsonl

PROFILE_NOTE = (
    "null floor: ingests the corpus and retrieves nothing by design, so the "
    "shared answerer abstains on every query; zero hits here is a declared "
    "measurement, never an environment fault"
)


class NullAbstainAdapter:
    name = "null-abstain"
    supports_group_reuse = False
    #: Bulk load, nothing compiled. Declared rather than defaulted so an
    #: adapter author has to look at it; see INGESTION_ALTITUDES.
    ingestion_altitude = "raw_source"
    #: Read by the retrieval floor guard. Zero hits from this adapter is a
    #: declared measurement; zero hits from anything else is an incident.
    retrieves_nothing_by_design = True

    def __init__(self, *, mode: str = "leaf", search_style: str = "neutral") -> None:
        self._mode = mode
        self._search_style = search_style
        self._workdir: Path | None = None
        self._profile: Profile | None = None
        self._ingested = 0

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INGEST_API, Capability.SEARCH})

    def setup(self, workdir: Path, profile: Profile) -> None:
        self._workdir = Path(workdir)
        self._profile = profile
        self._workdir.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        self._ingested = 0

    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        """Ingest honestly, then decline to use any of it.

        The documents really are accepted: the ingested-doc-count parity check
        (falsification register item 2) compares contenders on what they were
        given, and a floor that claimed to have received nothing would be
        excluded from that comparison instead of anchoring it. What this
        adapter withholds is retrieval, not ingestion.
        """

        corpus_dir = Path(corpus_dir)
        try:
            sources = load_jsonl(SourceRecord, corpus_dir / "sources.jsonl")
        except FileNotFoundError as exc:
            raise AdapterEnvironmentError(f"corpus incomplete: {exc}") from exc
        results: list[OpResult] = []
        for seq, source in enumerate(sources):
            started = time.perf_counter()
            results.append(
                OpResult(
                    seq=seq,
                    op="ingest",
                    source_id=source.source_id,
                    ok=True,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
            )
        self._ingested = len(results)
        return results

    def search(self, query: str, limit: int) -> list[Hit]:
        if self._workdir is None:
            raise AdapterEnvironmentError("adapter not set up")
        return []

    def export_state(self) -> StateExport:
        raise AdapterUnsupported("the null floor declares no STATE_EXPORT")

    def version_info(self) -> dict[str, str]:
        info = {
            "provider": self.name,
            "profile_note": PROFILE_NOTE,
            "ingested_sources": str(self._ingested),
            "retrieves": "nothing (by design)",
            "requested_mode": self._mode,
            "requested_search_style": self._search_style,
        }
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("null-abstain", lambda **kw: NullAbstainAdapter(**kw))
