"""A caller's admission predicate, offered to the catalogue for publication.

`lexstore.LexicalStore` keeps a caller's per-page verdicts across requests, so a
request decides only the pages that changed since the last one. It never decides
a verdict itself: the predicate that owns the release or content-floor rule
supplies it. This module holds only the shape both sides share, and the scope
in which a publication names the pages it is about to decide.
"""

from __future__ import annotations

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


class PendingDecisions:
    """The pages a catalogue publication is about to decide, one after another.

    A page that a collection store owns is decided on the store thread, which
    serves one request at a time. Naming every such page up front lets the first
    of them take a single round trip for all, instead of one queue entry per
    page. The answers are the request's own: the scope that holds this object
    ends with the publication, and each answer is taken once.
    """

    __slots__ = ("answers", "call", "paths")

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths: dict[str, None] = dict.fromkeys(paths)
        #: What the batched read was asked as; a later call batches only when it asks the same.
        self.call: tuple | None = None
        self.answers: dict[str, tuple[str, object]] = {}


_PENDING: ContextVar[PendingDecisions | None] = ContextVar("published_admission_pending", default=None)


@contextmanager
def deciding(paths: Iterable[str]) -> Iterator[PendingDecisions]:
    """Name the pages a publication will decide, for as long as it decides them."""
    pending = PendingDecisions(paths)
    token = _PENDING.set(pending)
    try:
        yield pending
    finally:
        _PENDING.reset(token)


def pending_decisions() -> PendingDecisions | None:
    """The pages the running publication named, or None outside one."""
    return _PENDING.get()
