"""A caller's admission predicate, offered to the catalogue for publication.

`lexstore.LexicalStore` keeps a caller's per-page verdicts across requests, so a
request decides only the pages that changed since the last one. It never decides
a verdict itself: the predicate that owns the release or content-floor rule
supplies it. This module holds only the shape both sides share.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable


class Admission:
    """What a predicate tells the catalogue so its verdicts can be reused.

    `identity()` closes over every input of a verdict except the page itself:
    policy, grants, audience, purpose, connector boundary and tombstones. It
    returns None when the predicate cannot name those inputs (a live
    authorization session, a bound collection writer, a blocked policy); the
    catalogue then publishes nothing and the request decides every page itself.

    `decide(path)` returns `(allowed, page_local)`. `page_local` is True only when
    the verdict is a function of the identity and the page's own bytes, so the
    catalogue's `(content_hash, mtime_ns)` for the page proves it still holds.
    A verdict that reads another page or live state is returned, never kept.
    """

    __slots__ = ("decide", "identity")

    def __init__(
        self,
        identity: Callable[[], Hashable | None],
        decide: Callable[[str], tuple[bool, bool]],
    ) -> None:
        self.identity = identity
        self.decide = decide


class AdmittedPredicate:
    """A `path -> bool` predicate that also offers its `Admission`."""

    __slots__ = ("admission",)

    def __init__(self, admission: Admission) -> None:
        self.admission = admission

    def __call__(self, path: str) -> bool:
        return self.admission.decide(path)[0]


def admission_of(predicate: object) -> Admission | None:
    """The `Admission` a predicate offers, or None."""
    offered = getattr(predicate, "admission", None)
    return offered if isinstance(offered, Admission) else None
