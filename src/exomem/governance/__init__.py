"""Governance kernel facade — inspection-only, zero enforcement this change.

Nothing in `find`/`get`/`overview`/`graph`/`read_media`/query paths consults
this package yet (see the `add-governance-kernel` proposal). The names below
ARE the kernel's internal read leaves — the surface a later change
(`explain`/`simulate`) will call. `KERNEL_LEAVES` exists purely so that "no
user-facing tool ships this change" is a fact a test can pin, not to register
a command-surface entry: nothing here is added to `commands.COMMANDS`.

Every read leaf that consults a compiled `Policy` must check `.empty` (no
governance configured -> open) THEN `.blocked` (a refused cold-start compile
with nothing good to fall back on -> fail-closed floor) before touching
`scopes`/`rules`/`grants` — see `decide_paths` for the reference
implementation of that contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .. import find_corpus
from .decisions import Decision, decide
from .membership import evaluate as evaluate_membership
from .policy import (
    DISCLOSURE_MAX,
    DISCLOSURE_MIN,
    EMPTY_POLICY,
    GOVERNANCE_VERSION,
    Policy,
    Rule,
    Scope,
    StandingGrant,
    governance_root,
    load,
)

__all__ = [
    "Decision",
    "DISCLOSURE_MAX",
    "DISCLOSURE_MIN",
    "EMPTY_POLICY",
    "GOVERNANCE_VERSION",
    "KERNEL_LEAVES",
    "Policy",
    "Rule",
    "Scope",
    "StandingGrant",
    "decide",
    "decide_paths",
    "evaluate_membership",
    "governance_root",
    "load",
]


def decide_paths(
    vault_root: Path,
    paths: Iterable[str],
    *,
    audience: str,
    purpose: str | None = None,
) -> dict[str, Decision]:
    """Batch read leaf: resolve membership and decide the ceiling for each path.

    Contract (checked in this order, mirroring `policy.Policy.empty`/`.blocked`):

    - `policy.empty` (no `_Governance/` configured) -> the open fast path:
      every path maps to `DISCLOSURE_MAX`, no page is parsed, no sidecar is
      opened.
    - `policy.blocked` (a cold-start compile refusal with no prior good
      policy to fall back on) -> the fail-closed floor: every path maps to
      `DISCLOSURE_MIN`, no page is parsed, no sidecar is opened. This must
      NEVER fall through to the open path — a refused compile is not the
      same as no governance at all.
    - Otherwise, membership + the pure evaluator decide each path normally.

    Like every governance call in this change, the sidecar only exists
    behind an explicit `compile.write_snapshot` — never called from here.
    """
    vault_root = Path(vault_root)
    policy = load(vault_root)
    paths = list(paths)
    if policy.empty:
        return {
            rel_path: Decision(level=DISCLOSURE_MAX, scope_ids=(), rule_ids=())
            for rel_path in paths
        }
    if policy.blocked:
        return {
            rel_path: Decision(level=DISCLOSURE_MIN, scope_ids=(), rule_ids=())
            for rel_path in paths
        }
    out: dict[str, Decision] = {}
    for rel_path in paths:
        full_path = vault_root / rel_path
        try:
            mtime = full_path.stat().st_mtime
        except OSError:
            continue
        page = find_corpus.parse_page(full_path, mtime, vault_root)
        if page is None:
            continue
        scope_ids = evaluate_membership(page, policy)
        out[rel_path] = decide(scope_ids, audience=audience, purpose=purpose, policy=policy)
    return out


KERNEL_LEAVES: dict[str, Callable[..., Any]] = {
    "governance.load": load,
    "governance.evaluate_membership": evaluate_membership,
    "governance.decide": decide,
    "governance.decide_paths": decide_paths,
}
