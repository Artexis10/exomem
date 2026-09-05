#!/usr/bin/env python3
"""Trustworthy relation-review scale and concurrency evidence.

The executable fixture writes only a disposable synthetic vault.  Private
paths, hashes, identities, and monotonic timestamps remain in typed internal
records; the CLI emits a closed aggregate schema.
"""

from __future__ import annotations

import argparse
import builtins
import json
import logging
import math
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from exomem import (
    corpus_aware,
    embeddings,
    epistemic_graph,
    find,
    freshness,
    graph_sync,
    relation_queue,
    semantic_contract,
    vault,
    writer_lease,
)

_KB = "Knowledge Base/Notes/Insights"
_FIXED_INDEXED_QUERIES = 8
_POST_RECOVERY_READS = 1
_QUEUE_STATUSES = frozenset({"available", "warming", "pending", "unavailable"})
_SUBSTITUTE_KINDS = (
    "validation_only",
    "no_op",
    "graph_excluded",
    "ineligible",
    "wrong_path",
    "wrong_hash",
    "unchanged_generation",
)


class ScaleGateError(AssertionError):
    """A run did not supply the evidence required by the gate."""


@dataclass(frozen=True, slots=True)
class ScaleConfig:
    pages: int = 3_600
    streams: int = 20
    groups: int = 20

    def validate(self, *, minimum_pages: int = 3_600) -> None:
        if type(self.pages) is not int or self.pages < minimum_pages:
            raise ValueError(f"pages must be at least {minimum_pages}")
        if type(self.streams) is not int or self.streams < 20:
            raise ValueError("streams must be at least 20")
        if type(self.groups) is not int or not 1 <= self.groups <= 20:
            raise ValueError("groups must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class RunPolicy:
    minimum_pages: int
    enforce_absolute_timing: bool
    coordination_timeout_s: float = 30.0


_CALIBRATED_POLICY = RunPolicy(3_600, True)


def semantic_test_policy(*, minimum_pages: int) -> RunPolicy:
    """Bounded real-orchestration policy without absolute latency assertions."""
    return RunPolicy(minimum_pages, False, 15.0)


class FakeClock:
    """Tiny deterministic clock used by the commit-seam regression."""

    def __init__(self, value_ns: int) -> None:
        self._value_ns = value_ns

    def now_ns(self) -> int:
        return self._value_ns

    def advance_ms(self, milliseconds: float) -> None:
        self._value_ns += round(milliseconds * 1_000_000)


@dataclass(slots=True)
class QueueCounters:
    snapshots: int = 0
    queries: int = 0
    markdown_reads: int = 0
    markdown_parses: int = 0
    markdown_walks: int = 0
    embedding_calls: int = 0
    snapshot_generation: int = 0
    snapshot_current: bool = False


@dataclass(frozen=True, slots=True)
class SourceObservation:
    relative_path: str
    source_hash: str
    review_id: str
    ref: str
    fingerprint: str
    evidence_nonempty: bool

    @property
    def identity_bound(self) -> bool:
        return all((self.review_id, self.ref, self.fingerprint)) and self.evidence_nonempty


@dataclass(frozen=True, slots=True)
class QueueSample:
    stream_id: int
    started_ns: int
    completed_ns: int
    status: str
    snapshot_generation: int
    snapshot_current: bool
    snapshots: int
    queries: int
    markdown_reads: int
    markdown_parses: int
    markdown_walks: int
    embedding_calls: int
    eligible_pages: int
    candidate_numerator: int
    candidate_denominator: int
    observations: tuple[SourceObservation, ...] = ()

    @property
    def duration_ms(self) -> float:
        return round((self.completed_ns - self.started_ns) / 1_000_000, 3)

    def report_value(self, *, final_commit_ns: int | None) -> dict[str, Any]:
        started_after = final_commit_ns is not None and self.started_ns > final_commit_ns
        recovery_ms = (
            round((self.completed_ns - final_commit_ns) / 1_000_000, 3)
            if final_commit_ns is not None and self.completed_ns >= final_commit_ns
            else None
        )
        return {
            "stream_id": self.stream_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "snapshot_generation": self.snapshot_generation,
            "snapshot_current": self.snapshot_current,
            "snapshots": self.snapshots,
            "queries": self.queries,
            "markdown_reads": self.markdown_reads,
            "markdown_parses": self.markdown_parses,
            "markdown_walks": self.markdown_walks,
            "embedding_calls": self.embedding_calls,
            "started_after_final_commit": started_after,
            "recovery_ms": recovery_ms,
        }


def reference_queue_sample(
    *,
    stream_id: int,
    started_ns: int,
    completed_ns: int,
    snapshot_generation: int,
) -> QueueSample:
    return QueueSample(
        stream_id=stream_id,
        started_ns=started_ns,
        completed_ns=completed_ns,
        status="available",
        snapshot_generation=snapshot_generation,
        snapshot_current=True,
        snapshots=1,
        queries=_FIXED_INDEXED_QUERIES,
        markdown_reads=0,
        markdown_parses=0,
        markdown_walks=0,
        embedding_calls=0,
        eligible_pages=3_600,
        candidate_numerator=20,
        candidate_denominator=3_600,
    )


class CountingCursor:
    def __init__(self, cursor: Any, counters: QueueCounters) -> None:
        self._cursor = cursor
        self._counters = counters

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._counters.queries += 1
        return self._cursor.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._counters.queries += 1
        return self._cursor.executemany(*args, **kwargs)

    def __enter__(self) -> CountingCursor:
        self._cursor.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._cursor.__exit__(*args)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class CountingConnection:
    def __init__(self, connection: Any, counters: QueueCounters) -> None:
        self._connection = connection
        self._counters = counters

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._counters.queries += 1
        return self._connection.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._counters.queries += 1
        return self._connection.executemany(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> CountingCursor:
        return CountingCursor(self._connection.cursor(*args, **kwargs), self._counters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _inside_markdown_root(path: Any, root: Path) -> bool:
    candidate = Path(path)
    if candidate.suffix.lower() != ".md":
        return False
    try:
        candidate.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return True


def monitor_queue_cost(measurement: Any, root: Path) -> ExitStack:
    """Count every queue-reachable graph query and hidden corpus-work seam."""
    stack = ExitStack()
    original_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot
    original_walk = find._walk_md
    original_parse = find._parse_page
    original_cosine = corpus_aware._best_cosine_per_file
    original_embed = embeddings.embed_texts
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_path_open = Path.open
    original_builtin_open = builtins.open
    original_unpinned = vault.read_bytes_without_pinning

    def active() -> QueueCounters | None:
        value = getattr(measurement, "sample", None)
        return value if isinstance(value, QueueCounters) else None

    def open_counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        connection = original_open(self, *args, **kwargs)
        counters = active()
        if connection is None or counters is None:
            return connection
        counters.snapshots += 1
        row = connection.execute(
            "SELECT value FROM graph_meta WHERE key = 'graph_sync_generation'"
        ).fetchone()
        value = row[0] if row is not None else None
        counters.snapshot_generation = int(value) if str(value or "").isdigit() else 0
        counters.snapshot_current = True
        return CountingConnection(connection, counters)

    def walked(*args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None:
            counters.markdown_walks += 1
            raise ScaleGateError("structural queue violation: Markdown walk")
        return original_walk(*args, **kwargs)

    def parsed(*args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None:
            counters.markdown_parses += 1
            raise ScaleGateError("structural queue violation: Markdown parse")
        return original_parse(*args, **kwargs)

    def cosine(*args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None:
            counters.embedding_calls += 1
            raise ScaleGateError("structural queue violation: embedding call")
        return original_cosine(*args, **kwargs)

    def embed(*args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None:
            counters.embedding_calls += 1
            raise ScaleGateError("structural queue violation: embedding call")
        return original_embed(*args, **kwargs)

    def read_text(path: Path, *args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None and _inside_markdown_root(path, root):
            counters.markdown_reads += 1
            raise ScaleGateError("structural queue violation: Markdown read")
        return original_read_text(path, *args, **kwargs)

    def read_bytes(path: Path, *args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None and _inside_markdown_root(path, root):
            counters.markdown_reads += 1
            raise ScaleGateError("structural queue violation: Markdown read")
        return original_read_bytes(path, *args, **kwargs)

    def read_unpinned(path: Path, *args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None and _inside_markdown_root(path, root):
            counters.markdown_reads += 1
            raise ScaleGateError("structural queue violation: Markdown read")
        return original_unpinned(path, *args, **kwargs)

    def path_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None and _inside_markdown_root(path, root):
            counters.markdown_reads += 1
            raise ScaleGateError("structural queue violation: Markdown open")
        return original_path_open(path, *args, **kwargs)

    def builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        counters = active()
        if counters is not None:
            try:
                is_markdown = _inside_markdown_root(file, root)
            except TypeError:
                is_markdown = False
            if is_markdown:
                counters.markdown_reads += 1
                raise ScaleGateError("structural queue violation: Markdown open")
        return original_builtin_open(file, *args, **kwargs)

    stack.enter_context(
        patch.object(epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", open_counted)
    )
    stack.enter_context(patch.object(find, "_walk_md", walked))
    stack.enter_context(patch.object(find, "_parse_page", parsed))
    stack.enter_context(patch.object(corpus_aware, "_best_cosine_per_file", cosine))
    stack.enter_context(patch.object(embeddings, "embed_texts", embed))
    stack.enter_context(patch.object(Path, "read_text", read_text))
    stack.enter_context(patch.object(Path, "read_bytes", read_bytes))
    stack.enter_context(patch.object(Path, "open", path_open))
    stack.enter_context(patch.object(builtins, "open", builtin_open))
    stack.enter_context(patch.object(vault, "read_bytes_without_pinning", read_unpinned))
    return stack


@dataclass(frozen=True, slots=True)
class OverlapEvidence:
    commit_boundary_entered: bool
    held_reader_stream_ids: tuple[int, ...]
    release_after_reader_completions: int
    boundary_held_after_release: bool
    held_mutation_committed: bool


class OverlapCoordinator:
    """Deterministic held-token protocol shared by real and ordinary runs."""

    def __init__(self, *, required_readers: int) -> None:
        self.required_readers = required_readers
        self._lock = threading.Lock()
        self._entered = False
        self._held = False
        self._readers: set[int] = set()
        self._release_after = 0
        self._mutation_committed = False
        self.entered_event = threading.Event()
        self.release_event = threading.Event()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            if self._held:
                raise ScaleGateError("overlap boundary re-entered")
            self._entered = True
            self._held = True
            self.entered_event.set()
        try:
            yield
        finally:
            with self._lock:
                self._held = False

    def reader_completed(self, stream_id: int) -> bool:
        with self._lock:
            if not self._entered or not self._held or stream_id in self._readers:
                return False
            self._readers.add(stream_id)
            if len(self._readers) == self.required_readers:
                self._release_after = len(self._readers)
                self.release_event.set()
                return True
            return False

    @property
    def release_ready(self) -> bool:
        return self.release_event.is_set()

    def mutation_finished(self, *, committed: bool) -> None:
        with self._lock:
            self._mutation_committed = committed

    def evidence(self) -> OverlapEvidence:
        with self._lock:
            return OverlapEvidence(
                self._entered,
                tuple(sorted(self._readers)),
                self._release_after,
                self._held,
                self._mutation_committed,
            )


def validate_overlap(evidence: OverlapEvidence) -> None:
    if not evidence.commit_boundary_entered:
        raise ScaleGateError("overlap real boundary entry missing")
    readers = evidence.held_reader_stream_ids
    if len(readers) != 2 or len(set(readers)) != 2:
        raise ScaleGateError("overlap requires two distinct held readers")
    if evidence.release_after_reader_completions != 2:
        raise ScaleGateError("overlap boundary released prematurely")
    if evidence.boundary_held_after_release:
        raise ScaleGateError("overlap held token survived release")
    if not evidence.held_mutation_committed:
        raise ScaleGateError("overlap held mutation did not commit")


@dataclass(frozen=True, slots=True)
class CommitSeamRecord:
    commit_seam_ns: int
    expected_path: str
    fanout_paths: tuple[str, ...]
    checkpoint: graph_sync.GraphSyncCheckpoint | None


class CommitSeamCapture:
    """Capture the real leaf seam after checkpoint publication, before fanout."""

    def __init__(self, now_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self._now_ns = now_ns
        self._local = threading.local()
        self._lock = threading.Lock()
        self._records: dict[str, CommitSeamRecord] = {}

    def activate(self, *, expected_path: str) -> None:
        self._local.expected_path = expected_path

    def deactivate(self) -> None:
        if hasattr(self._local, "expected_path"):
            del self._local.expected_path

    @property
    def commit_seam_ns(self) -> int | None:
        with self._lock:
            if not self._records:
                return None
            return max(record.commit_seam_ns for record in self._records.values())

    def record_for(self, expected_path: str) -> CommitSeamRecord | None:
        with self._lock:
            return self._records.get(expected_path)

    def wrap_post_commit_fanout(
        self,
        original: Callable[..., Any],
        *,
        checkpoint_reader: Callable[[Path], graph_sync.GraphSyncCheckpoint | None] = (
            graph_sync.read_checkpoint
        ),
    ) -> Callable[..., Any]:
        def wrapped(
            vault_root: Path | None,
            replaced_paths: list[Path],
            index_reports: list[Any] | None,
            semantic_states: Mapping[str, Any] | None,
            **kwargs: Any,
        ) -> Any:
            expected = getattr(self._local, "expected_path", None)
            if expected is not None and vault_root is not None:
                root = Path(vault_root)
                relative = tuple(
                    Path(path).absolute().relative_to(root.absolute()).as_posix()
                    for path in replaced_paths
                )
                record = CommitSeamRecord(
                    self._now_ns(),
                    expected,
                    relative,
                    checkpoint_reader(root),
                )
                with self._lock:
                    self._records[expected] = record
            return original(
                vault_root,
                replaced_paths,
                index_reports,
                semantic_states,
                **kwargs,
            )

        return wrapped


@dataclass(frozen=True, slots=True)
class MutationAttempt:
    stream_id: int
    outcome: str
    substitute: str
    expected_path: str
    pre_hash: str
    post_hash: str
    before_generation: int
    checkpoint_generation: int
    checkpoint_paths: tuple[tuple[str, str | None], ...]
    checkpoint_sha256: str
    fanout_paths: tuple[str, ...]
    commit_seam_ns: int | None
    index_requested_paths: tuple[str, ...]
    index_eligible_paths: tuple[str, ...]
    graph_component_outcome: str
    graph_terminal: str
    graph_provisioned: bool
    post_eligible: bool = False
    published_source_hash: str = ""
    identity_bound: bool = False


def reference_checkpoint(*, generation: int) -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id="1" * 24,
        paths=((f"{_KB}/source-0000.md", "a" * 64),),
        created_paths=(),
    )


def reference_mutation_attempt() -> MutationAttempt:
    path = f"{_KB}/source-0000.md"
    return MutationAttempt(
        stream_id=0,
        outcome="committed",
        substitute="none",
        expected_path=path,
        pre_hash="0" * 64,
        post_hash="a" * 64,
        before_generation=40,
        checkpoint_generation=41,
        checkpoint_paths=((path, "a" * 64),),
        checkpoint_sha256="b" * 64,
        fanout_paths=(path,),
        commit_seam_ns=1,
        index_requested_paths=(path,),
        index_eligible_paths=(path,),
        graph_component_outcome="completed",
        graph_terminal="completed",
        graph_provisioned=False,
        post_eligible=True,
        published_source_hash="a" * 64,
        identity_bound=True,
    )


def validate_mutation_attempt(attempt: MutationAttempt) -> None:
    if attempt.substitute != "none":
        raise ScaleGateError(f"invalid mutation substitute: {attempt.substitute}")
    if attempt.outcome != "committed":
        raise ScaleGateError("mutation did not reach committed outcome")
    if attempt.pre_hash == attempt.post_hash:
        raise ScaleGateError("mutation did not change distinct canonical bytes")
    if attempt.commit_seam_ns is None:
        raise ScaleGateError("canonical pre-fanout commit seam missing")
    if attempt.fanout_paths != (attempt.expected_path,):
        raise ScaleGateError("mutation fanout used the wrong path")
    if attempt.checkpoint_generation <= attempt.before_generation:
        raise ScaleGateError("unchanged checkpoint generation")
    if len(attempt.checkpoint_paths) != 1 or attempt.checkpoint_paths[0][0] != attempt.expected_path:
        raise ScaleGateError("checkpoint path does not bind the exact mutation")
    if attempt.checkpoint_paths[0][1] != attempt.post_hash:
        raise ScaleGateError("checkpoint hash does not bind canonical bytes")
    if attempt.index_requested_paths != (attempt.expected_path,):
        raise ScaleGateError("index report requested the wrong path")
    if attempt.index_eligible_paths != (attempt.expected_path,):
        raise ScaleGateError("mutation was graph-excluded or ineligible")
    if not (
        attempt.graph_component_outcome == "completed"
        or attempt.graph_component_outcome in {"registered", "deferred"}
        and attempt.graph_provisioned
    ):
        raise ScaleGateError("mutation graph work was not completed or durably provisioned")
    if attempt.graph_terminal not in {"completed", "pending", "queued"}:
        raise ScaleGateError("mutation terminal lacks a governed graph outcome")
    if not attempt.post_eligible:
        raise ScaleGateError("post-mutation eligibility was not published")
    if attempt.published_source_hash != attempt.post_hash:
        raise ScaleGateError("published source hash does not bind canonical bytes")
    if not attempt.identity_bound:
        raise ScaleGateError("published queue identity or evidence is missing")


def mutation_content(original: str, sequence: int) -> str:
    replacement = f"\nSynthetic canonical mutation {sequence}.\n"
    return original if replacement in original else original + replacement


def _component(report: Any, name: str) -> Any:
    return next(
        (
            item
            for item in getattr(report, "components", ())
            if getattr(item, "component", None) == name
        ),
        None,
    )


def _terminal_graph(terminal: Any) -> str:
    if isinstance(terminal, Mapping):
        value = terminal.get("graph_sync")
        if value is None and isinstance(terminal.get("leaf_result"), Mapping):
            value = terminal["leaf_result"].get("graph_sync")
        return str(value or "absent")
    return "absent"


def _busy_error(error: BaseException) -> bool:
    value = f"{type(error).__name__} {error}".upper()
    return any(token in value for token in ("BUSY", "CONTENTION", "LEASE_HELD"))


def mutate_eligible_page(
    root: Path,
    page: Path,
    sequence: int,
    *,
    stream_id: int,
    pre_eligible: bool,
    capture: CommitSeamCapture,
) -> MutationAttempt:
    """Run one exact-path governed mutation and retain its observed proof."""
    relative = page.absolute().relative_to(root.absolute()).as_posix()
    before = graph_sync.read_checkpoint(root)
    before_generation = before.generation if before is not None else 0
    original = page.read_text(encoding="utf-8")
    replacement = mutation_content(original, sequence)
    pre_hash = vault.content_hash(original)
    reports: list[Any] = []
    terminal: Any = None
    error: BaseException | None = None

    def leaf(vault_root: Path, **_kwargs: Any) -> dict[str, Any]:
        vault.batch_atomic_write(
            [vault.PlannedWrite(page, replacement)],
            vault_root=vault_root,
            index_reports=reports,
            post_commit_fanout=True,
        )
        writer_lease.mark_active_mutation_committed()
        return {"status": "committed", "mutated": True}

    capture.activate(expected_path=relative)
    try:
        command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
        terminal = writer_lease.get_manager().invoke(command, (root,), {})
    except BaseException as caught:  # noqa: BLE001 - classified below
        error = caught
    finally:
        capture.deactivate()

    post_text = page.read_text(encoding="utf-8")
    post_hash = vault.content_hash(post_text)
    seam = capture.record_for(relative)
    checkpoint = seam.checkpoint if seam is not None else graph_sync.read_checkpoint(root)
    index_report = reports[-1] if reports else None
    graph_component = _component(index_report, "epistemic_graph")
    component_outcome = str(getattr(graph_component, "outcome", "absent"))
    provisioned = False
    if checkpoint is not None and component_outcome in {"registered", "deferred"}:
        provisioned = graph_sync.repair_is_provisioned(
            root, checkpoint, outcome=component_outcome
        )

    if error is not None:
        outcome = "busy" if _busy_error(error) else "failed"
    elif isinstance(terminal, Mapping) and terminal.get("status") == "committed":
        outcome = "committed"
    else:
        outcome = "failed"

    checkpoint_paths = checkpoint.paths if checkpoint is not None else ()
    checkpoint_generation = checkpoint.generation if checkpoint is not None else before_generation
    if not pre_eligible:
        substitute = "ineligible"
    elif replacement == original:
        substitute = "no_op"
    elif checkpoint_generation <= before_generation:
        substitute = "validation_only" if post_hash == pre_hash else "unchanged_generation"
    elif seam is None or seam.fanout_paths != (relative,):
        substitute = "wrong_path"
    elif len(checkpoint_paths) != 1 or checkpoint_paths[0][0] != relative:
        substitute = "wrong_path"
    elif checkpoint_paths[0][1] != post_hash:
        substitute = "wrong_hash"
    elif index_report is None or relative not in getattr(index_report, "eligible_paths", ()):
        substitute = "graph_excluded"
    else:
        substitute = "none"

    return MutationAttempt(
        stream_id=stream_id,
        outcome=outcome,
        substitute=substitute,
        expected_path=relative,
        pre_hash=pre_hash,
        post_hash=post_hash,
        before_generation=before_generation,
        checkpoint_generation=checkpoint_generation,
        checkpoint_paths=checkpoint_paths,
        checkpoint_sha256=checkpoint.checkpoint_sha256 if checkpoint is not None else "",
        fanout_paths=seam.fanout_paths if seam is not None else (),
        commit_seam_ns=seam.commit_seam_ns if seam is not None else None,
        index_requested_paths=tuple(getattr(index_report, "requested_paths", ())),
        index_eligible_paths=tuple(getattr(index_report, "eligible_paths", ())),
        graph_component_outcome=component_outcome,
        graph_terminal=_terminal_graph(terminal),
        graph_provisioned=provisioned,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((len(ordered) * percentile) / 100) - 1)
    return round(ordered[index], 3)


def _distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


def _write_corpus(root: Path, pages: int) -> tuple[Path, tuple[Path, Path]]:
    target = root / _KB / "target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\nstatus: active\n---\n# Target\n\nSynthetic target.\n",
        encoding="utf-8",
    )
    sources: list[Path] = []
    target_link = f"{_KB}/target"
    for number in range(pages):
        page = root / _KB / f"source-{number:04d}.md"
        page.write_text(
            "---\ntype: insight\nstatus: active\n---\n"
            f"# Source {number:04d}\n\nSee [[{target_link}]].\n",
            encoding="utf-8",
        )
        if len(sources) < 2:
            sources.append(page)
    return target, (sources[0], sources[1])


def _prebuild_current_graph(root: Path) -> None:
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()


def _observations(result: Mapping[str, Any], wanted: frozenset[str]) -> tuple[SourceObservation, ...]:
    observed: list[SourceObservation] = []
    for group in result.get("groups") or []:
        path = str(group.get("source_path") or group.get("path") or "")
        if path not in wanted:
            continue
        items = list(group.get("items") or [])
        if not items:
            continue
        item = items[0]
        evidence = item.get("evidence")
        observed.append(
            SourceObservation(
                path,
                str(group.get("source_content_hash") or group.get("content_hash") or ""),
                str(item.get("review_id") or ""),
                str(item.get("ref") or ""),
                str(item.get("fingerprint") or ""),
                isinstance(evidence, Mapping) and bool(evidence),
            )
        )
    return tuple(observed)


def _timed_queue(
    root: Path,
    *,
    stream_id: int,
    groups: int,
    measurement: threading.local,
    wanted_paths: frozenset[str],
) -> QueueSample:
    counters = QueueCounters()
    measurement.sample = counters
    started = time.perf_counter_ns()
    try:
        result = relation_queue.build_queue(root, limit_pages=groups, limit_per_page=1)
        completed = time.perf_counter_ns()
    finally:
        del measurement.sample
    status = str(result.get("status") or "unavailable")
    coverage = dict(result.get("coverage") or {})
    eligible = int(coverage.get("eligible_pages", 0))
    numerator = int(coverage.get("relation_candidates_found", result.get("shown", 0)) or 0)
    return QueueSample(
        stream_id,
        started,
        completed,
        status,
        counters.snapshot_generation,
        counters.snapshot_current,
        counters.snapshots,
        counters.queries,
        counters.markdown_reads,
        counters.markdown_parses,
        counters.markdown_walks,
        counters.embedding_calls,
        eligible,
        numerator,
        eligible,
        _observations(result, wanted_paths),
    )


def qualifying_recovery_ms(
    samples: list[QueueSample] | tuple[QueueSample, ...],
    *,
    final_commit_ns: int,
    final_generation: int,
) -> float | None:
    qualifying = [
        (sample.completed_ns - final_commit_ns) / 1_000_000
        for sample in samples
        if sample.started_ns > final_commit_ns
        and sample.status == "available"
        and sample.snapshot_current
        and sample.snapshot_generation >= final_generation
    ]
    return round(min(qualifying), 3) if qualifying else None


def _bind_publication(
    attempts: list[MutationAttempt], sample: QueueSample
) -> list[MutationAttempt]:
    observed = {item.relative_path: item for item in sample.observations}
    return [
        replace(
            attempt,
            post_eligible=attempt.expected_path in observed,
            published_source_hash=(
                observed[attempt.expected_path].source_hash
                if attempt.expected_path in observed
                else ""
            ),
            identity_bound=(
                observed[attempt.expected_path].identity_bound
                if attempt.expected_path in observed
                else False
            ),
        )
        for attempt in attempts
    ]


def _aggregate_report(
    *,
    config: ScaleConfig,
    samples: list[QueueSample],
    attempts: list[MutationAttempt],
    participants: set[int],
    overlap: OverlapEvidence,
    rebuilds: int,
    policy: RunPolicy,
) -> dict[str, Any]:
    if len(attempts) != 2:
        raise ScaleGateError("mutation attempt accounting is incomplete")
    final_attempt = max(attempts, key=lambda item: item.checkpoint_generation)
    if final_attempt.commit_seam_ns is None:
        raise ScaleGateError("missing final graph-relevant commit seam")
    recovery_ms = qualifying_recovery_ms(
        samples,
        final_commit_ns=final_attempt.commit_seam_ns,
        final_generation=final_attempt.checkpoint_generation,
    )
    publication_samples = [
        sample
        for sample in samples
        if sample.started_ns > final_attempt.commit_seam_ns
        and sample.status == "available"
        and sample.snapshot_current
        and sample.snapshot_generation >= final_attempt.checkpoint_generation
    ]
    if not publication_samples:
        raise ScaleGateError("missing recovery after final graph-relevant commit")
    bound_attempts = _bind_publication(attempts, publication_samples[0])
    for attempt in bound_attempts:
        validate_mutation_attempt(attempt)
    validate_overlap(overlap)

    available_samples = [sample for sample in samples if sample.status == "available"]
    unavailable_samples = [sample for sample in samples if sample.status != "available"]
    available = [sample.duration_ms for sample in available_samples]
    unavailable = [sample.duration_ms for sample in unavailable_samples]
    final_sample = publication_samples[0]
    generations = sorted(item.checkpoint_generation for item in bound_attempts)
    graph_completed = sum(
        item.graph_component_outcome == "completed" for item in bound_attempts
    )
    graph_provisioned = sum(item.graph_provisioned for item in bound_attempts)
    substitute_counts = {
        kind: sum(item.substitute == kind for item in bound_attempts)
        for kind in _SUBSTITUTE_KINDS
    }
    reports = [
        sample.report_value(final_commit_ns=final_attempt.commit_seam_ns)
        for sample in samples
    ]
    denominator = final_sample.candidate_denominator
    normalized = (
        round(final_sample.candidate_numerator * 1_000 / denominator, 6)
        if denominator
        else 0.0
    )
    report: dict[str, Any] = {
        "schema": "relation-review-scale/v1",
        "corpus": {
            "eligible_pages": final_sample.eligible_pages,
            "candidate_numerator": final_sample.candidate_numerator,
            "candidate_denominator": denominator,
            "candidates_per_1000_eligible": normalized,
        },
        "workload": {
            "streams": config.streams,
            "requested_groups": config.groups,
            "participating_stream_ids": sorted(participants),
        },
        "overlap": {
            "commit_boundary_entered": overlap.commit_boundary_entered,
            "queue_reads_completed_while_commit_boundary_held": len(
                overlap.held_reader_stream_ids
            ),
            "queue_read_during_graph_mutation": len(overlap.held_reader_stream_ids) == 2,
            "held_reader_stream_ids": list(overlap.held_reader_stream_ids),
            "release_after_reader_completions": overlap.release_after_reader_completions,
            "boundary_held_after_release": overlap.boundary_held_after_release,
            "held_mutation_committed": overlap.held_mutation_committed,
        },
        "substitutes": substitute_counts,
        "mutations": {
            "attempted": len(bound_attempts),
            "committed": sum(item.outcome == "committed" for item in bound_attempts),
            "busy": sum(item.outcome == "busy" for item in bound_attempts),
            "failed": sum(item.outcome == "failed" for item in bound_attempts),
            "graph_completed": graph_completed,
            "graph_provisioned": graph_provisioned,
            "distinct_bytes": sum(item.pre_hash != item.post_hash for item in bound_attempts),
            "checkpoint_exact": sum(
                item.checkpoint_paths == ((item.expected_path, item.post_hash),)
                for item in bound_attempts
            ),
            "published_eligible": sum(item.post_eligible for item in bound_attempts),
            "published_source_hash_bound": sum(
                item.published_source_hash == item.post_hash for item in bound_attempts
            ),
            "published_identity_bound": sum(item.identity_bound for item in bound_attempts),
        },
        "checkpoints": {"committed_generations": generations},
        "queue": {
            "available": _distribution(available),
            "unavailable": _distribution(unavailable),
            "availability_ratio": round(
                len(available_samples) / len(samples) if samples else 0.0, 6
            ),
            "typed_statuses": sorted(
                {sample.status for sample in unavailable_samples}
            ),
            "samples": reports,
        },
        "recovery": {"current_available_after_final_commit_ms": recovery_ms},
        "structural": {
            "requests_measured": len(samples),
            "snapshot_count": sum(sample.snapshots for sample in samples),
            "indexed_query_count": sum(sample.queries for sample in samples),
            "max_snapshots_per_request": max(
                (sample.snapshots for sample in samples), default=0
            ),
            "min_snapshots_per_available_request": min(
                (sample.snapshots for sample in available_samples), default=0
            ),
            "max_indexed_queries_per_request": max(
                (sample.queries for sample in samples), default=0
            ),
            "min_indexed_queries_per_available_request": min(
                (sample.queries for sample in available_samples), default=0
            ),
            "markdown_reads": sum(sample.markdown_reads for sample in samples),
            "markdown_parses": sum(sample.markdown_parses for sample in samples),
            "markdown_walks": sum(sample.markdown_walks for sample in samples),
            "embedding_calls": sum(sample.embedding_calls for sample in samples),
            "mutation_full_rebuilds": rebuilds,
        },
    }
    validate_report(
        report,
        minimum_pages=policy.minimum_pages,
        enforce_absolute_timing=policy.enforce_absolute_timing,
    )
    return report


def run_calibrated(
    *,
    root: Path,
    config: ScaleConfig | None = None,
    _policy: RunPolicy = _CALIBRATED_POLICY,
) -> dict[str, Any]:
    """Run the real governed workload and return its closed aggregate report."""
    config = config or ScaleConfig()
    config.validate(minimum_pages=_policy.minimum_pages)
    root = Path(root)
    if root.exists():
        raise ValueError("synthetic root must not already exist")
    root.mkdir(parents=True)
    _, mutation_pages = _write_corpus(root, config.pages)
    _prebuild_current_graph(root)
    setup_queue = relation_queue.build_queue(root, limit_pages=config.groups, limit_per_page=1)
    setup_coverage = dict(setup_queue.get("coverage") or {})
    if (
        setup_queue.get("status") != "available"
        or int(setup_coverage.get("eligible_pages", 0)) < config.pages
    ):
        raise ScaleGateError("synthetic corpus did not publish eligible graph pages")
    wanted_paths = frozenset(
        page.absolute().relative_to(root.absolute()).as_posix() for page in mutation_pages
    )
    initial = {item.relative_path: item for item in _observations(setup_queue, wanted_paths)}
    if set(initial) != set(wanted_paths):
        raise ScaleGateError("canonical mutation input was not review-eligible")
    for page in mutation_pages:
        relative = page.absolute().relative_to(root.absolute()).as_posix()
        current_hash = vault.content_hash(page.read_text(encoding="utf-8"))
        if initial[relative].source_hash != current_hash or not initial[relative].identity_bound:
            raise ScaleGateError("canonical eligible input identity/hash was not published")

    start = threading.Barrier(config.streams)
    first_done = threading.Event()
    final_ready = threading.Event()
    errors: list[BaseException] = []
    attempts: list[MutationAttempt] = []
    samples: list[QueueSample] = []
    participants: set[int] = set()
    state_lock = threading.Lock()
    measurement = threading.local()
    overlap = OverlapCoordinator(required_readers=2)
    capture = CommitSeamCapture()
    rebuild_count = 0
    rebuild_lock = threading.Lock()
    original_locked_write = vault._batch_atomic_write_locked
    original_fanout = vault.post_commit_batch_fanout
    original_rebuild = epistemic_graph.EpistemicGraphIndex.rebuild_all

    def record_error(error: BaseException) -> None:
        with state_lock:
            errors.append(error)

    def append_sample(stream_id: int) -> QueueSample:
        sample = _timed_queue(
            root,
            stream_id=stream_id,
            groups=config.groups,
            measurement=measurement,
            wanted_paths=wanted_paths,
        )
        with state_lock:
            samples.append(sample)
        return sample

    def held_first_write(*args: Any, **kwargs: Any) -> Any:
        writes = list(args[0]) if args else list(kwargs.get("writes", ()))
        if any(Path(write.path) == mutation_pages[0] for write in writes):
            with overlap.hold():
                if not overlap.release_event.wait(_policy.coordination_timeout_s):
                    raise ScaleGateError("non-overlapping mixed phase")
        return original_locked_write(*args, **kwargs)

    def counted_rebuild(index: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal rebuild_count
        if Path(index.vault_root).absolute() == root.absolute():
            with rebuild_lock:
                rebuild_count += 1
            raise ScaleGateError("mutation-triggered whole-graph rebuild")
        return original_rebuild(index, *args, **kwargs)

    def reader(stream_id: int) -> None:
        try:
            start.wait()
            if stream_id in {2, 3}:
                if not overlap.entered_event.wait(_policy.coordination_timeout_s):
                    raise ScaleGateError("missing graph-relevant commit boundary")
                append_sample(stream_id)
                overlap.reader_completed(stream_id)
            if not final_ready.wait(_policy.coordination_timeout_s):
                raise ScaleGateError("missing recovery after final graph-relevant commit")
            append_sample(stream_id)
            with state_lock:
                participants.add(stream_id)
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)

    def first_mutator() -> None:
        try:
            start.wait()
            attempt = mutate_eligible_page(
                root,
                mutation_pages[0],
                1,
                stream_id=0,
                pre_eligible=True,
                capture=capture,
            )
            with state_lock:
                attempts.append(attempt)
                participants.add(0)
            overlap.mutation_finished(
                committed=attempt.outcome == "committed" and attempt.substitute == "none"
            )
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)
            overlap.mutation_finished(committed=False)
        finally:
            first_done.set()

    def second_mutator() -> None:
        try:
            start.wait()
            if not first_done.wait(_policy.coordination_timeout_s):
                raise ScaleGateError("first mutation did not finish")
            attempt = mutate_eligible_page(
                root,
                mutation_pages[1],
                2,
                stream_id=1,
                pre_eligible=True,
                capture=capture,
            )
            with state_lock:
                attempts.append(attempt)
                participants.add(1)
            final_ready.set()
            append_sample(1)
        except BaseException as error:  # noqa: BLE001 - transport thread errors
            record_error(error)
            final_ready.set()

    threads = [
        threading.Thread(target=first_mutator, name="relation-scale-mutation-0"),
        threading.Thread(target=second_mutator, name="relation-scale-mutation-1"),
        *(
            threading.Thread(
                target=reader,
                args=(stream_id,),
                name=f"relation-scale-reader-{stream_id}",
            )
            for stream_id in range(2, config.streams)
        ),
    ]
    wrapped_fanout = capture.wrap_post_commit_fanout(original_fanout)
    with (
        monitor_queue_cost(measurement, root),
        patch.object(vault, "_batch_atomic_write_locked", held_first_write),
        patch.object(vault, "post_commit_batch_fanout", wrapped_fanout),
        patch.object(epistemic_graph.EpistemicGraphIndex, "rebuild_all", counted_rebuild),
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(_policy.coordination_timeout_s * 2)

    if any(thread.is_alive() for thread in threads):
        raise ScaleGateError("controlled request stream did not finish")
    if errors:
        raise errors[0]
    return _aggregate_report(
        config=config,
        samples=samples,
        attempts=attempts,
        participants=participants,
        overlap=overlap.evidence(),
        rebuilds=rebuild_count,
        policy=_policy,
    )


def _reference_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(20):
        samples.append(
            {
                "stream_id": (index + 1) % 20,
                "status": "available",
                "duration_ms": 10.0,
                "snapshot_generation": 42,
                "snapshot_current": True,
                "snapshots": 1,
                "queries": 8,
                "markdown_reads": 0,
                "markdown_parses": 0,
                "markdown_walks": 0,
                "embedding_calls": 0,
                "started_after_final_commit": index == 0,
                "recovery_ms": 100.0 if index == 0 else None,
            }
        )
    samples.append(
        {
            "stream_id": 3,
            "status": "warming",
            "duration_ms": 2.0,
            "snapshot_generation": 0,
            "snapshot_current": False,
            "snapshots": 0,
            "queries": 0,
            "markdown_reads": 0,
            "markdown_parses": 0,
            "markdown_walks": 0,
            "embedding_calls": 0,
            "started_after_final_commit": False,
            "recovery_ms": None,
        }
    )
    return samples


def reference_report() -> dict[str, Any]:
    """Deterministic closed aggregate used for fail-closed mutation tests."""
    samples = _reference_samples()
    return {
        "schema": "relation-review-scale/v1",
        "corpus": {
            "eligible_pages": 3_600,
            "candidate_numerator": 20,
            "candidate_denominator": 3_600,
            "candidates_per_1000_eligible": 5.555556,
        },
        "workload": {
            "streams": 20,
            "requested_groups": 20,
            "participating_stream_ids": list(range(20)),
        },
        "overlap": {
            "commit_boundary_entered": True,
            "queue_reads_completed_while_commit_boundary_held": 2,
            "queue_read_during_graph_mutation": True,
            "held_reader_stream_ids": [2, 3],
            "release_after_reader_completions": 2,
            "boundary_held_after_release": False,
            "held_mutation_committed": True,
        },
        "substitutes": dict.fromkeys(_SUBSTITUTE_KINDS, 0),
        "mutations": {
            "attempted": 2,
            "committed": 2,
            "busy": 0,
            "failed": 0,
            "graph_completed": 2,
            "graph_provisioned": 0,
            "distinct_bytes": 2,
            "checkpoint_exact": 2,
            "published_eligible": 2,
            "published_source_hash_bound": 2,
            "published_identity_bound": 2,
        },
        "checkpoints": {"committed_generations": [41, 42]},
        "queue": {
            "available": {"count": 20, "p50_ms": 10.0, "p95_ms": 10.0, "max_ms": 10.0},
            "unavailable": {"count": 1, "p50_ms": 2.0, "p95_ms": 2.0, "max_ms": 2.0},
            "availability_ratio": 0.952381,
            "typed_statuses": ["warming"],
            "samples": samples,
        },
        "recovery": {"current_available_after_final_commit_ms": 100.0},
        "structural": {
            "requests_measured": 21,
            "snapshot_count": 20,
            "indexed_query_count": 160,
            "max_snapshots_per_request": 1,
            "min_snapshots_per_available_request": 1,
            "max_indexed_queries_per_request": 8,
            "min_indexed_queries_per_available_request": 8,
            "markdown_reads": 0,
            "markdown_parses": 0,
            "markdown_walks": 0,
            "embedding_calls": 0,
            "mutation_full_rebuilds": 0,
        },
    }


def _schema_error(message: str) -> ScaleGateError:
    return ScaleGateError(f"report schema violation: {message}")


def _keys(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise _schema_error(f"{location} keys")
    return value


def _integer(value: Any, location: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise _schema_error(location)
    return value


def _number(value: Any, location: str, *, minimum: float = 0.0) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < minimum:
        raise _schema_error(location)
    return float(value)


def _boolean(value: Any, location: str) -> bool:
    if type(value) is not bool:
        raise _schema_error(location)
    return value


def _distribution_schema(value: Any, location: str) -> None:
    item = _keys(value, {"count", "p50_ms", "p95_ms", "max_ms"}, location)
    _integer(item["count"], f"{location}.count")
    for key in ("p50_ms", "p95_ms", "max_ms"):
        _number(item[key], f"{location}.{key}")


def _validate_schema(report: Any) -> None:
    top = _keys(
        report,
        {
            "schema", "corpus", "workload", "overlap", "substitutes", "mutations",
            "checkpoints", "queue", "recovery", "structural",
        },
        "root",
    )
    if type(top["schema"]) is not str or top["schema"] != "relation-review-scale/v1":
        raise _schema_error("schema enum")
    corpus = _keys(
        top["corpus"],
        {"eligible_pages", "candidate_numerator", "candidate_denominator", "candidates_per_1000_eligible"},
        "corpus",
    )
    for key in ("eligible_pages", "candidate_numerator", "candidate_denominator"):
        _integer(corpus[key], f"corpus.{key}")
    _number(corpus["candidates_per_1000_eligible"], "corpus.normalized")
    workload = _keys(
        top["workload"], {"streams", "requested_groups", "participating_stream_ids"}, "workload"
    )
    _integer(workload["streams"], "workload.streams", minimum=1, maximum=256)
    _integer(workload["requested_groups"], "workload.groups", minimum=1, maximum=20)
    if type(workload["participating_stream_ids"]) is not list:
        raise _schema_error("workload.participating_stream_ids")
    for value in workload["participating_stream_ids"]:
        _integer(value, "workload.stream_id", maximum=255)
    overlap = _keys(
        top["overlap"],
        {
            "commit_boundary_entered", "queue_reads_completed_while_commit_boundary_held",
            "queue_read_during_graph_mutation", "held_reader_stream_ids",
            "release_after_reader_completions", "boundary_held_after_release",
            "held_mutation_committed",
        },
        "overlap",
    )
    for key in (
        "commit_boundary_entered", "queue_read_during_graph_mutation",
        "boundary_held_after_release", "held_mutation_committed",
    ):
        _boolean(overlap[key], f"overlap.{key}")
    for key in ("queue_reads_completed_while_commit_boundary_held", "release_after_reader_completions"):
        _integer(overlap[key], f"overlap.{key}", maximum=256)
    if type(overlap["held_reader_stream_ids"]) is not list:
        raise _schema_error("overlap.held_reader_stream_ids")
    for value in overlap["held_reader_stream_ids"]:
        _integer(value, "overlap.reader_stream_id", maximum=255)
    substitutes = _keys(top["substitutes"], set(_SUBSTITUTE_KINDS), "substitutes")
    for key in _SUBSTITUTE_KINDS:
        _integer(substitutes[key], f"substitutes.{key}")
    mutation_keys = {
        "attempted", "committed", "busy", "failed", "graph_completed", "graph_provisioned",
        "distinct_bytes", "checkpoint_exact", "published_eligible",
        "published_source_hash_bound", "published_identity_bound",
    }
    mutations = _keys(top["mutations"], mutation_keys, "mutations")
    for key in mutation_keys:
        _integer(mutations[key], f"mutations.{key}")
    checkpoints = _keys(top["checkpoints"], {"committed_generations"}, "checkpoints")
    generations = checkpoints["committed_generations"]
    if type(generations) is not list:
        raise _schema_error("checkpoints.generations")
    for generation in generations:
        _integer(generation, "checkpoints.generation", minimum=1)
    queue = _keys(
        top["queue"],
        {"available", "unavailable", "availability_ratio", "typed_statuses", "samples"},
        "queue",
    )
    _distribution_schema(queue["available"], "queue.available")
    _distribution_schema(queue["unavailable"], "queue.unavailable")
    ratio = _number(queue["availability_ratio"], "queue.availability_ratio")
    if ratio > 1.0:
        raise _schema_error("queue.availability_ratio")
    statuses = queue["typed_statuses"]
    if type(statuses) is not list or any(
        type(status) is not str or status not in _QUEUE_STATUSES - {"available"}
        for status in statuses
    ):
        raise _schema_error("queue.typed_statuses enum")
    samples = queue["samples"]
    if type(samples) is not list or len(samples) > 512:
        raise _schema_error("queue.samples")
    sample_keys = {
        "stream_id", "status", "duration_ms", "snapshot_generation", "snapshot_current",
        "snapshots", "queries", "markdown_reads", "markdown_parses", "markdown_walks",
        "embedding_calls", "started_after_final_commit", "recovery_ms",
    }
    for index, sample_value in enumerate(samples):
        sample = _keys(sample_value, sample_keys, f"queue.samples[{index}]")
        _integer(sample["stream_id"], "queue.sample.stream_id", maximum=255)
        if type(sample["status"]) is not str or sample["status"] not in _QUEUE_STATUSES:
            raise _schema_error("queue.sample.status enum")
        _number(sample["duration_ms"], "queue.sample.duration")
        for key in (
            "snapshot_generation", "snapshots", "queries", "markdown_reads", "markdown_parses",
            "markdown_walks", "embedding_calls",
        ):
            _integer(sample[key], f"queue.sample.{key}")
        _boolean(sample["snapshot_current"], "queue.sample.snapshot_current")
        _boolean(sample["started_after_final_commit"], "queue.sample.started_after")
        if sample["recovery_ms"] is not None:
            _number(sample["recovery_ms"], "queue.sample.recovery")
    recovery = _keys(top["recovery"], {"current_available_after_final_commit_ms"}, "recovery")
    if recovery["current_available_after_final_commit_ms"] is not None:
        _number(recovery["current_available_after_final_commit_ms"], "recovery.current")
    structural_keys = {
        "requests_measured", "snapshot_count", "indexed_query_count",
        "max_snapshots_per_request", "min_snapshots_per_available_request",
        "max_indexed_queries_per_request", "min_indexed_queries_per_available_request",
        "markdown_reads", "markdown_parses", "markdown_walks", "embedding_calls",
        "mutation_full_rebuilds",
    }
    structural = _keys(top["structural"], structural_keys, "structural")
    for key in structural_keys:
        _integer(structural[key], f"structural.{key}")


def _reported_distribution(samples: list[dict[str, Any]]) -> dict[str, float | int]:
    return _distribution([float(sample["duration_ms"]) for sample in samples])


def validate_report(
    report: dict[str, Any],
    *,
    minimum_pages: int = 3_600,
    enforce_absolute_timing: bool = True,
) -> None:
    """Recompute every aggregate and fail closed on semantic inconsistency."""
    _validate_schema(report)
    corpus = report["corpus"]
    workload = report["workload"]
    mutations = report["mutations"]
    substitutes = report["substitutes"]
    checkpoints = report["checkpoints"]
    queue = report["queue"]
    structural = report["structural"]
    if corpus["eligible_pages"] < minimum_pages:
        raise ScaleGateError("structural corpus threshold failed")
    if corpus["candidate_denominator"] != corpus["eligible_pages"]:
        raise ScaleGateError("candidate denominator is not the resulting eligible corpus")
    normalized = round(
        corpus["candidate_numerator"] * 1_000 / corpus["candidate_denominator"], 6
    )
    if corpus["candidates_per_1000_eligible"] != normalized:
        raise ScaleGateError("candidate normalization does not reconcile")
    streams = workload["streams"]
    participants = workload["participating_stream_ids"]
    if streams < 20 or participants != list(range(streams)):
        raise ScaleGateError("controlled stream participation does not reconcile")
    if any(substitutes.values()):
        raise ScaleGateError("invalid substitute detected")
    attempted = mutations["attempted"]
    if attempted != mutations["committed"] + mutations["busy"] + mutations["failed"]:
        raise ScaleGateError("mutation outcomes do not reconcile")
    if mutations["committed"] == 0 and mutations["busy"]:
        raise ScaleGateError("all-busy mutation run")
    if mutations["committed"] < 2:
        raise ScaleGateError("fewer than two committed graph-relevant mutations")
    generations = checkpoints["committed_generations"]
    if generations != sorted(set(generations)) or len(generations) != mutations["committed"]:
        raise ScaleGateError("committed mutation generation evidence does not reconcile")
    for key in (
        "distinct_bytes", "checkpoint_exact", "published_eligible",
        "published_source_hash_bound", "published_identity_bound",
    ):
        if mutations[key] != mutations["committed"]:
            raise ScaleGateError(f"committed mutation proof does not reconcile: {key}")
    if mutations["graph_completed"] + mutations["graph_provisioned"] != mutations["committed"]:
        raise ScaleGateError("governed graph outcomes do not reconcile")
    overlap = report["overlap"]
    evidence = OverlapEvidence(
        overlap["commit_boundary_entered"],
        tuple(overlap["held_reader_stream_ids"]),
        overlap["release_after_reader_completions"],
        overlap["boundary_held_after_release"],
        overlap["held_mutation_committed"],
    )
    validate_overlap(evidence)
    if (
        overlap["queue_reads_completed_while_commit_boundary_held"] != 2
        or not overlap["queue_read_during_graph_mutation"]
    ):
        raise ScaleGateError("non-overlapping mixed phase")
    samples = queue["samples"]
    available = [sample for sample in samples if sample["status"] == "available"]
    unavailable = [sample for sample in samples if sample["status"] != "available"]
    if structural["requests_measured"] != len(samples):
        raise ScaleGateError("request accounting does not reconcile")
    if queue["available"] != _reported_distribution(available) or queue[
        "unavailable"
    ] != _reported_distribution(unavailable):
        raise ScaleGateError("queue distributions do not reconcile")
    ratio = round(len(available) / len(samples) if samples else 0.0, 6)
    if queue["availability_ratio"] != ratio:
        raise ScaleGateError("availability ratio does not reconcile")
    if queue["typed_statuses"] != sorted({sample["status"] for sample in unavailable}):
        raise ScaleGateError("typed unavailable statuses do not reconcile")
    reconciled = {
        "snapshot_count": sum(sample["snapshots"] for sample in samples),
        "indexed_query_count": sum(sample["queries"] for sample in samples),
        "max_snapshots_per_request": max((sample["snapshots"] for sample in samples), default=0),
        "min_snapshots_per_available_request": min(
            (sample["snapshots"] for sample in available), default=0
        ),
        "max_indexed_queries_per_request": max((sample["queries"] for sample in samples), default=0),
        "min_indexed_queries_per_available_request": min(
            (sample["queries"] for sample in available), default=0
        ),
        "markdown_reads": sum(sample["markdown_reads"] for sample in samples),
        "markdown_parses": sum(sample["markdown_parses"] for sample in samples),
        "markdown_walks": sum(sample["markdown_walks"] for sample in samples),
        "embedding_calls": sum(sample["embedding_calls"] for sample in samples),
    }
    if any(structural[key] != value for key, value in reconciled.items()):
        raise ScaleGateError("per-request structural accounting does not reconcile")
    if not available:
        raise ScaleGateError("all-warming reader run")
    if any(
        sample["snapshots"] != 1 or sample["queries"] != _FIXED_INDEXED_QUERIES
        for sample in available
    ):
        raise ScaleGateError("available request violated the fixed query plan")
    if any(
        sample[key]
        for sample in samples
        for key in ("markdown_reads", "markdown_parses", "markdown_walks", "embedding_calls")
    ):
        raise ScaleGateError("hidden corpus work was observed in a queue request")
    if structural["mutation_full_rebuilds"]:
        raise ScaleGateError("mutation-triggered whole-graph rebuild observed")
    final_generation = generations[-1]
    recovery_values = [
        sample["recovery_ms"]
        for sample in samples
        if sample["status"] == "available"
        and sample["started_after_final_commit"]
        and sample["snapshot_current"]
        and sample["snapshot_generation"] >= final_generation
        and sample["recovery_ms"] is not None
    ]
    recovery = report["recovery"]["current_available_after_final_commit_ms"]
    if not recovery_values or recovery is None:
        raise ScaleGateError("missing recovery")
    if recovery != round(min(recovery_values), 3):
        raise ScaleGateError("recovery sample does not reconcile")
    if enforce_absolute_timing:
        if ratio < 0.90:
            raise ScaleGateError("timing availability ratio threshold failed")
        if recovery > 5_000:
            raise ScaleGateError("timing recovery threshold failed")
        if queue["available"]["p95_ms"] >= 1_000 or queue["available"]["max_ms"] >= 2_000:
            raise ScaleGateError("timing available latency threshold failed")
        if unavailable and queue["unavailable"]["p95_ms"] >= 250:
            raise ScaleGateError("timing unavailable latency threshold failed")


def assert_privacy_safe(report: dict[str, Any]) -> None:
    """The exact recursive schema is the privacy boundary."""
    _validate_schema(report)


def render_report(report: dict[str, Any]) -> str:
    assert_privacy_safe(report)
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the synthetic relation-review scale gate")
    parser.add_argument("--pages", type=int, default=3_600)
    parser.add_argument("--streams", type=int, default=20)
    parser.add_argument("--groups", type=int, default=20)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    config = ScaleConfig(pages=args.pages, streams=args.streams, groups=args.groups)
    logging.disable(logging.CRITICAL)
    try:
        if args.root is None:
            with tempfile.TemporaryDirectory(prefix="exomem-relation-scale-") as temporary:
                report = run_calibrated(root=Path(temporary) / "synthetic", config=config)
        else:
            report = run_calibrated(root=args.root, config=config)
    finally:
        logging.disable(logging.NOTSET)
    print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
