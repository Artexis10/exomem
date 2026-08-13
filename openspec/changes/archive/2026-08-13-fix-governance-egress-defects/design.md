## Context

The governance kernel, release gate, disclosure receipts, governance tools and cross-domain
bridges shipped across waves 0–5 and merged to `main`. The release plane is well built: the
three-state policy contract (`empty` / `blocked` / compiled), the decision memo keyed on page
identity, fail-closed membership resolution, and the count-oracle handling in the walk filter
are all correct and deliberately reasoned.

The five defects here are in the seams nobody probed: the fallback path taken only while a
policy mutation is pending, the conflict-copy guard written against one sync tool, the error
path around the terminal filter, one missing field in the provenance strip set, and one
operational directory that was never added to the corpus exclusions. Four of the five are
one-line or near-one-line changes. Their combined effect is that the two strongest claims
the governance work makes — "a governed vault never fails open" and "a withheld item is
indistinguishable from a missing one" — are both currently false under conditions that occur
in normal operation.

Two are reachable on the maintainer's own vault as it stands: it is Syncthing-replicated with
conflict copies already present at the knowledge-base root, and its governance tree exists
while yielding no compiled policy.

## Goals / Non-Goals

**Goals**

- A governed vault fails closed, not open, whenever the current policy state cannot be trusted
   — including while a policy mutation is pending, in every process regardless of uptime.
- A synchronisation conflict copy can never act as, resurrect, or weaken policy, and can never
  extend the receipt chain.
- No payload leaving the shared dispatcher names a withheld item, whether the command returned
  or raised.
- Reverse citation provenance is treated exactly like forward citation provenance.
- Operational run state is not knowledge and is not recalled or dumped into released text.

**Non-Goals**

- No change to the policy grammar, the disclosure ladder, receipt schemas, the command
  surface, or any request/response shape.
- No change to the empty-policy fast path for a vault with no governance tree, including its
  measured latency budget.
- Not addressed here: non-markdown items being selectable only by path/ref selectors, and the
  absence of a default-deny primitive. Both are real and both are scoped to the changes that
  follow this one.

## Decisions

### Gate the last-good cache on "produced governance", not on "did not fail"

The current guard admits any load that is not blocked and carries no error findings. The empty
open singleton satisfies both. The fix is to require the compile to have produced governance —
neither the empty sentinel nor the blocked sentinel — before it is retained as last-good.

This is preferred over changing the guarded fallback to inspect what it found, because the
invariant belongs at the point of retention: the cache is defined as "the last policy worth
falling back to", and an open policy is never worth falling back to. Once the cache cannot
hold an open policy, the existing fallback code is already correct — a vault with no governed
compile falls to the blocked floor by the existing `last_good is None` branch.

The behaviour change to state plainly: a vault whose governance tree exists but currently
compiles to nothing will fail closed during a pending mutation where it previously served
open. A vault with no governance tree never reaches this path and is untouched.

### Make the conflict marker a set, applied at every governance walk

The marker becomes a collection covering both the parenthesised Obsidian form and the
hyphenated sync-conflict form, matched case-insensitively as today. It is applied at policy
document discovery, the full governance file walk, and the receipt tree.

The receipt tree matters and is easy to miss: operational state is pruned from the governance
walk, so a conflict copy inside the receipt directory is invisible to the conflict refusal
while still being able to fork an append-only hash chain. The chain integrity check must
refuse rather than extend.

Every other walker in the codebase already filters the hyphenated form. This makes the one
place where getting it wrong is a disclosure bug consistent with the rest.

### Filter error payloads at the dispatcher, and fix the raise site too

Two changes, both needed. The dispatcher gains an error path that runs the payload through the
same terminal filtering as a result, so a future raise site cannot open a fresh bypass. The
identity-collision raise site independently stops embedding the colliding paths, because an
error whose *code* is the signal does not need them and because the dispatcher fix should not
be the only thing standing between a path list and a caller.

Defence in depth is deliberate here: the structural fix is what makes the guarantee hold
going forward, and the raise-site fix is what makes it hold for the case that is known to be
reachable.

### Cut the per-path dump at the source rather than scanning every body

The adoption run manifest embeds a machine-readable dump of every path and hash in the run.
The alternative fix — scanning page bodies as free text in the artifact-reference gate — was
rejected: the gate builds a full recursive vault index per call with no memoisation, so
scanning every body would put an O(vault) walk on every governed call and would be caught by
the latency gate. It is also the wrong shape: released text should not contain the dump in
the first place.

So the manifest carries counts and the run reference, and `_Adoption/` joins the excluded
directory set alongside the governance tree. Per-item detail stays in the run object, read
through the command that owns the run, where a disclosure decision applies. The run object
itself is untouched.

The same reasoning settles the stateless `save-manifest` report, which carried the identical
dump from a sibling function: its human-readable sections already say everything the page is
for, and the full report remains available live from `adopt(mode="scan-only")` where a
disclosure decision applies. Two functions rendering released pages, one rule.

### Exclude operational state from BOTH walkers, not just the corpus one

There are two exclusion sets and they had silently diverged. `find_corpus.EXCLUDED_DIR_NAMES`
governs `scope="kb"`/`"kb-only"`; `scope="vault"` reaches `vault.walk_vault_md` through
`bm25`, which filters `vault.VAULT_SCAN_SKIP_DIRS` — a set naming neither `_Adoption` nor
`_Governance`. `scope` is a caller-controlled parameter and the tool docs actively suggest
widening to `vault` when KB recall is sparse, so the corpus exclusion was bypassable by
asking for it.

Adding the names to only one set would have been the shape of the original defect. Both go
in both. Excluding `_Governance` here is deliberately in scope even though the defect was
reported against run state: leaving policy state reachable by the walker we are already
fixing, having just seen why the sets must agree, would be knowingly shipping the same hole.

The regression asserts absence at every scope rather than parity between the two trees —
parity is satisfied by two trees leaking equally, which is exactly the state we found.

### Prove the cross-process claim with a real second process

The last-good defect is process-state dependent, so a same-process test can assert the
invariant but cannot demonstrate the bug it fixes. The regression must exercise a warmed
cache and a cold cache and assert both reach the same decision, using the existing crash-seam
and cache-manipulation patterns rather than a new harness.

## Risks / Trade-offs

- **A governed-but-empty vault now fails closed during mutations.** This is the intended
  correction, and it is the difference between the two sentinels the kernel already documents
  as never-to-be-conflated. The blast radius is one narrow window; the alternative is keeping
  a fail-open path. Called out explicitly in the proposal's impact section.
- **Excluding `_Adoption/` from the corpus removes run manifests from recall.** If any existing
  test asserts a manifest is findable, that assertion encodes the leak and must be updated
  rather than the fix narrowed. The implementer reports it rather than working around it.
- **The receipt-tree conflict check sits on the append path.** It must not add measurable cost
  to the receipt latency budget; the check is a filename test on files already being
  enumerated.
- **Error filtering adds work to the raise path.** Only for governed vaults with a resolved
  principal; the ungoverned fast path must remain free, and the overhead gate covers it.

## Migration Plan

None. No stored format, schema version, sidecar, or policy document changes. Existing vaults
need no action. The manifest body shape changes for manifests written after the change;
manifests already on disk are unaffected and are simply no longer indexed.

## Open Questions

None blocking. Two items are deliberately deferred to later changes and named in Non-Goals:
non-markdown scope selection, and a default-deny policy primitive.
