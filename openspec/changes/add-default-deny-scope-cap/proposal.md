## Why

The policy grammar has no way to say "nobody but me, unless I name them". Two facts
combine into a hole that no amount of careful authoring closes:

1. **No matching rule means full release.** `_decide_at` computes
   `standing_min = min(ceilings, default=DISCLOSURE_MAX)`, so a scope with no rule for a
   given audience resolves to L6. Confidentiality is opt-in per (scope, audience) pair.
2. **Audiences are matched by exact id.** `normalize_audience` derives an id from the
   issuer and subject, and for non-OAuth MCP clients the subject derives from the bearer
   credential itself. So **rotating a credential mints an audience id that no rule names**,
   and the vault falls open to it.

The consequence is that "this assistant may never receive my private material" is not
currently expressible. The true statement is "…so long as its audience id matches an
authored rule", which is a precondition the owner cannot enforce and will not remember.
Every scope added later inherits the same default, so the hole widens as the vault grows.

This change makes the statement hold against an audience no document names — including one
minted by a credential rotation. It does not make it hold against an audience holding a
grant on an overlapping scope; see the scope note below.

This blocks the governed-consolidation work: the whole point of consolidating two Exomems
into one is that a deterministic release plane replaces physical isolation. Trading a
boundary with no attack surface for one that a credential rotation reopens is not a trade
worth making, and no orchestration above the kernel can fix a default in the lattice.

## What Changes

- A scope MAY declare that an audience no rule names receives nothing. For such a scope,
  the standing default for a non-owner audience becomes the most restrictive level instead
  of the most permissive one.
- The owner is never subject to it. A scope the owner cannot read is a scope that has
  removed the owner's own access to their own vault, which is not what this expresses.
- Existing composition is unchanged: an explicit rule still sets the ceiling for the
  audience it names, a grant still only raises, an org cap still only lowers, and a
  declared purpose still only narrows. The declaration changes exactly one thing — the
  default that applies when nothing matched.
- **Scope of the guarantee.** The declaration closes the RULE lane: a rule naming an
  audience for one scope no longer suppresses a different scope's default. It does NOT
  close the GRANT lane. A grant matches on scope intersection and raises the whole item,
  so a grant naming an audience for a sibling scope still lifts an item that is also in a
  declared scope naming nobody. That is a pre-existing property of the lattice — an
  authored `ceiling: 0` rule behaves identically, verified by an equivalence test — and
  scoping a grant's raise to the scopes it names is a separate change with its own
  compatibility surface. Until then, "an audience no document names receives nothing"
  holds; "an audience named anywhere receives nothing outside what it was named for"
  does not.
- The policy compiler validates and reports the declaration so `explain`/`simulate` can
  show that an item is withheld by default rather than by an authored rule.
- Ungoverned vaults are untouched: no governance tree still means the empty fast path.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `governance-kernel`: a scope may invert the standing default for unnamed audiences, and
  the evaluator resolves that default before grants, org caps and purpose conditions apply.

## Impact

- Affected code: `governance/policy.py` (scope field, compile, validation),
  `governance/decisions.py` (the standing default), `governance/inspection.py` (explain),
  and focused governance tests.
- APIs: no request or response shape changes. Policy documents gain one optional scope
  field; documents that omit it behave exactly as today.
- Dependencies and runtime: no new dependency, model, background task, or sidecar
  migration. The empty-policy fast path and its latency budget are unchanged, and the
  declaration costs one lookup per already-resolved scope on the decision path.
