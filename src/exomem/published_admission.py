"""A caller's admission predicate, offered to the catalogue for publication.

`lexstore.LexicalStore` keeps a caller's per-page verdicts across requests, so a
request decides only the pages that changed since the last one. It never decides
a verdict itself: the predicate that owns the release or content-floor rule
supplies it. This module holds only the shape both sides share, and the scope
in which a command names the pages it is about to decide.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class Admission:
    """What a predicate tells the catalogue so its verdicts can be reused.

    `identity()` closes over every input of a verdict except the page itself:
    policy, grants, audience, purpose, connector boundary and tombstones. It
    returns None when the predicate cannot name those inputs (a live
    authorization session, a bound collection writer, a blocked policy); the
    catalogue then publishes nothing and the request decides every page itself.

    `decide(path, publish)` returns `(allowed, page_local)`. `page_local` is True
    only when `publish` is set and the verdict is a function of the identity and
    the page's own bytes, so the catalogue's `(content_hash, mtime_ns)` for the
    page proves it still holds. A verdict that reads another page, collection
    state or live state is returned, never kept. A caller that only needs the
    verdict leaves `publish` unset and pays nothing for the proof.
    """

    __slots__ = ("decide", "identity")

    def __init__(
        self,
        identity: Callable[[], Hashable | None],
        decide: Callable[[str, bool], tuple[bool, bool]],
    ) -> None:
        self.identity = identity
        self.decide = decide


class AdmittedPredicate:
    """A `path -> bool` predicate that also offers its `Admission`."""

    __slots__ = ("admission",)

    def __init__(self, admission: Admission) -> None:
        self.admission = admission

    def __call__(self, path: str) -> bool:
        return self.admission.decide(path, False)[0]


def admission_of(predicate: object) -> Admission | None:
    """The `Admission` a predicate offers, or None."""
    offered = getattr(predicate, "admission", None)
    return offered if isinstance(offered, Admission) else None


class RequestDecisions:
    """What one command asks the collection store about its pages, and what it was told.

    A page that a collection store owns is decided on the store thread, which serves
    one request at a time, so each request queues behind the store's other work. The
    command names the pages it will decide (`deciding`); the first decision of a
    given shape then decides every named page of that shape in one store request.
    A shape is one decision function asked with one set of arguments: the answers
    are found by equality on the whole shape, so one caller's answer, or one purpose's,
    never serves another's. A refused store request is held against its store for the
    rest of the command, so the pages it would have decided are withheld together
    rather than asked for one by one.

    Answers are returned, never kept: this object ends with the command that opened it.
    """

    __slots__ = ("_known", "_lock", "paths", "refusals", "shapes")

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self.paths: dict[str, None] = dict.fromkeys(paths)
        #: (shape, answers by path): a shape is the decision function and everything its answer reads.
        self.shapes: list[tuple[object, dict[str, object]]] = []
        #: The refusal of each store request that failed whole, by the store it was made to.
        self.refusals: dict[object, BaseException] = {}
        self._known: dict[object, object] = {}

    def announce(self, paths: Iterable[str]) -> None:
        with self._lock:
            self.paths.update(dict.fromkeys(paths))

    def pending(self) -> list[str]:
        """The pages named so far, in the order they were named."""
        with self._lock:
            return list(self.paths)

    def answers(self, shape: object) -> dict[str, object]:
        """The answers recorded for `shape`: the record of an equal shape, or a new empty one."""
        with self._lock:
            for known, answers in self.shapes:
                if known == shape:
                    return answers
            answers = {}
            self.shapes.append((shape, answers))
            return answers

    def once(self, key: object, make: Callable[[], object]) -> object:
        """What `make` returns the first time `key` is asked in this command; a `make` that raises is not remembered."""
        with self._lock:
            if key in self._known:
                return self._known[key]
        value = make()
        with self._lock:
            return self._known.setdefault(key, value)

    def refused(self) -> bool:
        """Whether a store request of this command was refused whole."""
        return bool(self.refusals)


_REQUEST: ContextVar[RequestDecisions | None] = ContextVar("published_admission_request", default=None)


def announce(paths: Iterable[str]) -> None:
    """Name pages the running command will decide. Outside a command nothing hears it, and each page asks alone."""
    if (current := _REQUEST.get()) is not None:
        current.announce(paths)


@contextmanager
def deciding(paths: Iterable[str] = (), *, own: bool = False) -> Iterator[RequestDecisions]:
    """Name the pages a block will decide, for as long as it decides them.

    The command's dispatcher opens the scope, and the block adds its pages to that scope.
    Outside a dispatcher the block opens a scope of its own. `own` opens a scope that shares no
    answer and no refusal with the enclosing one, for a pass that must decide live.
    """
    current = _REQUEST.get()
    if current is not None and not own:
        current.announce(paths)
        yield current
        return
    scope = RequestDecisions(paths)
    token = _REQUEST.set(scope)
    try:
        yield scope
    finally:
        _REQUEST.reset(token)


def request_decisions() -> RequestDecisions | None:
    """The running command's decisions, or None outside one."""
    return _REQUEST.get()
