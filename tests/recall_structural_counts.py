"""Count real request work without interpreting SQL or changing admission."""

from __future__ import annotations

import inspect
import os
import sqlite3
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from test_recall_integration import _PageReadSentinel

from exomem import call_spans, find_candidates, lexstore
from exomem.find_types import FindTimings


class RequestCounts:
    """Exclusive VM steps by span, with connection and full-proof attribution."""

    def __init__(self, root: Path, monkeypatch) -> None:
        self.active = False
        self.owner = threading.get_ident()
        self.stage = ContextVar("structural_stage", default="unattributed")
        self.scope = ContextVar("structural_scope", default="request")
        lexical_path = os.path.realpath(lexstore.get_store(root).path)
        self.selection = ContextVar("structural_selection", default=0)
        self.next_selection = 0
        self.steps = Counter()
        self.connections = Counter()
        self.proofs = Counter()
        self.candidates = defaultdict(set)
        self.sources = {}
        self.pages = _PageReadSentinel(root)
        self.pages.install(monkeypatch)
        connect = sqlite3.connect

        def measured_connect(database, *args, **kwargs):
            # Preserve the caller's factory and options; install before setup executes SQL.
            connection = connect(database, *args, **kwargs)
            location = os.fsdecode(database)
            if kwargs.get("uri") and location.startswith("file:"):
                location = url2pathname(urlsplit(location).path)
            resolved = os.path.realpath(location)
            store = "lexical" if resolved == lexical_path else Path(resolved).name
            if self.measuring():
                self.connections[self.key(store)] += 1

            def progress():
                if self.measuring():
                    self.steps[(*self.key(store), self.stage.get())] += 1
                return 0

            connection.set_progress_handler(progress, 1)
            return connection

        monkeypatch.setattr(sqlite3, "connect", measured_connect)
        span = FindTimings.span

        @contextmanager
        def measured_span(timings, name, *args, **kwargs):
            with span(timings, name, *args, **kwargs):
                with self.at(timings.current_stage()):
                    yield

        monkeypatch.setattr(FindTimings, "span", measured_span)
        phase = call_spans.span

        @contextmanager
        def measured_phase(name, *args, **kwargs):
            with self.at(f"{self.stage.get()}/{name}"):
                if name == "lexical.proof" and self.measuring():
                    self.proofs[self.key("lexical")] += 1
                with phase(name, *args, **kwargs) as value:
                    yield value

        monkeypatch.setattr(call_spans, "span", measured_phase)
        selection_scope = lexstore.catalog_selection_scope

        @contextmanager
        def measured_selection(*args, **kwargs):
            if self.measuring():
                self.next_selection += 1
                selection = self.next_selection
            else:
                selection = 0
            token = self.selection.set(selection)
            try:
                with selection_scope(*args, **kwargs) as value:
                    yield value
            finally:
                self.selection.reset(token)

        monkeypatch.setattr(lexstore, "catalog_selection_scope", measured_selection)
        self.wrap(monkeypatch, lexstore.LexicalStore, "_capture_read_view", scope="kb+vault")
        self.wrap(monkeypatch, lexstore.LexicalStore, "_serve_ready_catalog")
        # These are maintained query boundaries, not a vocabulary of query meanings.
        for name in ("_bm25_query", "_substring_query", "_keyword_groups_query"):
            self.wrap(monkeypatch, lexstore.LexicalStore, name, query=True, candidates=True)
        self.wrap(monkeypatch, lexstore.LexicalStore, "_emitted_parent_hints_query", query=True)
        self.wrap(monkeypatch, lexstore, "_eligibility_metadata_query", query=True)
        collect = find_candidates.collect_candidates

        def measured_candidates(*args, **kwargs):
            result = collect(*args, **kwargs)
            if self.measuring():
                self.sources.update(result.frame_sources)
                self.sources.update({path: source for path, (source, _ts)
                                     in result.frame_attribution.items()})
                self.candidates["fused"].update(path for path, _score in result.fused)
                for lane in ("bm25_ranking", "keyword_ranking", "graph_ranking", "temporal_ranking"):
                    self.candidates[lane].update(getattr(result, lane))
            return result

        monkeypatch.setattr(find_candidates, "collect_candidates", measured_candidates)

    def measuring(self):
        return self.active and threading.get_ident() == self.owner

    def key(self, store):
        return self.selection.get(), store, self.scope.get()

    @contextmanager
    def at(self, name):
        token = self.stage.set(name)
        try:
            yield
        finally:
            self.stage.reset(token)

    def wrap(self, monkeypatch, target, name, *, scope=None, query=False, candidates=False):
        original = getattr(target, name)
        signature = inspect.signature(original)

        @wraps(original)
        def measured(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            scope_token = self.scope.set(arguments.get("scope", scope or self.scope.get()))
            try:
                with self.at(f"{self.stage.get()}/{name}" if query else self.stage.get()):
                    result = original(*args, **kwargs)
                if candidates and self.measuring():
                    if name == "_keyword_groups_query":
                        paths = [row.path for row in result]
                        for row in result:
                            self.sources[row.path] = row.source_path
                            if row.frame_path:
                                self.sources[row.source_path] = row.frame_path
                    else:
                        paths = result.keys() if isinstance(result, dict) else (
                            row[0] if isinstance(row, tuple) else row for row in result
                        )
                    self.candidates[name].update(paths)
                return result
            finally:
                self.scope.reset(scope_token)

        monkeypatch.setattr(target, name, measured)

    @contextmanager
    def request(self):
        self.steps.clear()
        self.connections.clear()
        self.proofs.clear()
        self.candidates.clear()
        self.sources.clear()
        self.pages.reset()
        self.next_selection = 0
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def snapshot(self):
        return {
            "steps": dict(self.steps), "connections": dict(self.connections),
            "proofs": dict(self.proofs),
            "candidates": {name: frozenset(paths) for name, paths in self.candidates.items()},
            "page_reads": self.pages.count,
            "read_paths": [path for path, _caller in self.pages.read],
            "sources": dict(self.sources),
        }
