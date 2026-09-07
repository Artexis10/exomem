"""Private context seam for committing one curation witness with a leaf batch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class PendingWitness:
    vault_root: Path
    path: Path
    content: str
    finalize: Callable[[list[Any], str], str | None] | None = None
    ready: bool = False
    consumed: bool = False


_ACTIVE: ContextVar[PendingWitness | None] = ContextVar(
    "exomem_curation_atomic_witness", default=None
)


@contextmanager
def atomic_witness(
    vault_root: Path,
    path: Path,
    content: str,
    *,
    finalize: Callable[[list[Any], str], str | None] | None = None,
) -> Iterator[PendingWitness]:
    state = PendingWitness(
        vault_root=Path(vault_root).resolve(),
        path=Path(path),
        content=content,
        finalize=finalize,
    )
    token: Token[PendingWitness | None] = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)


def pending_for(vault_root: Path | None) -> PendingWitness | None:
    state = _ACTIVE.get()
    if state is None or state.consumed or vault_root is None:
        return None
    try:
        if Path(vault_root).resolve() != state.vault_root:
            return None
    except OSError:
        return None
    return state


def augment_batch(
    writes: list[Any],
    *,
    vault_root: Path | None,
    planned_write: Callable[..., Any],
) -> tuple[list[Any], PendingWitness | None]:
    state = pending_for(vault_root)
    if state is None:
        return writes, None
    if any(Path(write.path) == state.path for write in writes):
        raise RuntimeError("curation witness target collides with a leaf write")
    content = state.finalize(writes, state.content) if state.finalize is not None else state.content
    if content is None and not state.ready:
        return writes, None
    content = state.content if content is None else content
    state.content = content
    return [*writes, planned_write(path=state.path, content=content, create_only=True)], state


def render_with_after(content: str, after: list[dict[str, Any]]) -> str:
    value = json.loads(content)
    value["after"] = after
    basis = {key: value[key] for key in value if key not in {"result_digest", "committed_at"}}
    value["result_digest"] = hashlib.sha256(
        json.dumps(
            basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def mark_consumed(state: PendingWitness | None) -> None:
    if state is not None:
        state.consumed = True
