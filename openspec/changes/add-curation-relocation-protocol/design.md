## Context

The prepared-relocation protocol in this change is blocked on a capability the
held filesystem does not have. Every guarantee below rests on a durable
monotonic parent-namespace generation token for both the retained source and
destination parents: restart-comparable, non-repeating within its filesystem
epoch, and changing for every namespace-entry mutation including a rename
followed by its inverse. Without that token, a stable file identity plus a
post-crash placement hash cannot tell a completed rename from a crash followed
by an external inverse rename, and `test_curation_execution.py::test_matching_
relocation_hashes_cannot_authorize_placement_recovery` pins exactly that
indistinguishability. The ordered candidate/authorization records, the
both-parent flush discipline, and exact-target adoption are all consumers of the
token; none of them can be built first and retrofitted with it, because each one
decides what to do by comparing generations.

## Decision

Sequence the ABI work before the protocol work. Add and prove the generation-token
capability probe in the held filesystem, with its own tests for the reset,
unavailable, non-monotonic, cross-epoch, and incomparable cases, and only then
implement candidate publication, authorization, rename, and recovery on top of
it. Until that lands, `add-governed-curation-lane`'s v1 fail-closed requirement
is the whole of the shipped behavior, and this change stays unstarted rather
than half-built: a partially implemented relocation protocol is strictly worse
than a refusal, because it can publish a transition record it cannot later
adjudicate.

## The deferred prepared-relocation protocol

Moved verbatim from `add-governed-curation-lane`'s design when the relocation
protocol was carved out of that change. It describes the target behavior, not
shipped behavior: v1 refuses every relocation step fail-closed, and nothing
below exists in code today.

Filesystem placement leaves (`move`, `delete`, and `recover`) cannot atomically
commit a namespace rename and a separate witness file on the supported POSIX
and Windows filesystems. They use an ordered durable prepared-relocation
protocol instead of claiming impossible cross-file atomicity. V1 placement
requires the source and destination parents to exist already; it never creates
knowledge or trash destination directories as an unrecorded precursor effect.
After canonical leaf validation and exact destination allocation, but before any
transition artifact or rename, the leaf completes the held-parent capability,
identity, generation, and durability preflight described below. It then publishes
canonical create-only `prepared.json` as an inert candidate, flushes its file and
the transition directory and every newly created governed-run ancestor entry,
and only then publishes create-only `authorized.json`, flushes that file, and
flushes its containing transition directory. The authorization binds the
immutable plan and approval digests to the complete prepared-file digest. Only
a fully verified authorization permits the rename; a candidate alone has no
effect authority.

The prepared candidate binds the run, plan, approval, ordinal, step and operation
identity; source and destination; exact source SHA-256, size and stable held-
filesystem identity; both parent identities and filesystem epoch; destination
absence; lifecycle and graph transition identities where present; the complete
deterministic auxiliary-write manifest; parent compensation identity; a logical
witness-time basis; and the complete deterministic final-witness bytes and
closed-terminal projection, including their digests. The logical time basis
records preparation/authorization lineage and does not claim to be the
unknowable physical instant of a rename recovered after a crash.

The identities avoid a digest cycle. `preparation_id` hashes a domain separator
and the immutable candidate core. `authorization_id` hashes its own domain,
approval digest, operation id, and `preparation_id`. The final witness binds
those two ids, the logical time basis, and closed result digest. `prepared.json`
then contains the core, ids, exact final-witness bytes, and witness SHA-256;
`authorized.json` contains the approval digest, approved plan membership,
operation and preparation ids, SHA-256 of the complete `prepared.json` bytes,
final-witness SHA-256, and `authorization_id`. Both files are re-read and
verified before rename.

Before candidate publication, both existing parents are opened through held no-
follow operations; a missing parent refuses without creating it. The held
filesystem capability probe must capture durable, monotonic
namespace-generation tokens for both source and destination parents (one token
when they are the same parent), bind the parent identities and filesystem epoch,
certify that the token is non-repeating within that epoch and changes for every
namespace-entry mutation including rename followed by inverse rename, and
durably flush those parents. Unrelated entry changes may conservatively block.
A volume without comparable restart-durable tokens and retained-parent flush
capability refuses placement before candidate publication. The rename
revalidates the held source identity, destination absence, authorization, and
parent generations, then performs one no-replace executor rename attempt. It flushes
the held source parent and then the held destination parent, or the shared parent
once, with a recoverable cut before and between those flushes. Exact-prior
recovery may reissue the rename only after unchanged lineage proves no durable
namespace transition. The invariant is at most one durable or adopted placement
effect, not at most one rename syscall across power loss. A post-rename
flush failure never triggers an inverse rename; it leaves the authorized
transition unwitnessed for exact recovery. Exact-target recovery repeats both
idempotent parent flushes before any auxiliary suffix. The final witness
reproduces the exact bytes sealed by `prepared.json`, and its file plus containing
evidence directory are durable before any terminal receipt is created.

The preparation and authorization are immutable recovery evidence and are never
deleted. Before ordinary stale-binding blockers run, recovery verifies the
approved plan, authorization, candidate, capability and live generations, then
classifies exact placement:

- a candidate without authorization is pre-effect only while exact source,
  absent destination, and every live guard still match; resume may recreate only
  its deterministic authorization, while any target placement blocks;
- with authorization, exact source plus absent destination permits the same
  operation to retry only when both parent namespace-generation tokens remain
  exactly at their prepared values and every guard matches; an advanced,
  unavailable, reset, or incomparable token is
  `CURATION_RENAME_HISTORY_UNPROVABLE`, never evidence that the rename did not
  happen;
- absent source plus exact destination with the prepared stable identity means
  the authorized desired placement currently exists. Recovery adopts that exact
  placement without claiming whether the executor or an external actor produced
  it, issues no executor rename, repeats both parent flushes, and only then rolls
  forward the bound missing auxiliary writes, graph/lifecycle suffix, exact
  prepared witness, and terminal receipt. Parent generations must be comparable
  in the same filesystem epoch and show a namespace transition; an exact target
  with unchanged generations is invalid;
- a final witness without a terminal receipt is verified and receipted without
  invoking the leaf; and
- missing, reset, incomparable, or non-monotonic parent generation history,
  including unchanged generations at exact target, is
  `CURATION_RENAME_HISTORY_UNPROVABLE` and blocks before placement attribution;
- both paths, neither path, a wrong hash or stable identity, a competing or
  substituted transition record, a missing authorization after target placement,
  a third-state auxiliary write, or any authorization/preparation/witness/live
  disagreement is `CURATION_OUTCOME_UNCERTAIN` and blocks.

The recovery cut this protocol adds to the executor's enumerated cuts, also
verbatim from the v1 design's five-cut list, where it was cut 3:

3. a placement transition with no final witness: a lone prepared candidate is
   inert; an authorized exact-prior placement retries only with unchanged parent
   generations; an authorized exact-target placement is adopted and rolls
   forward with no recovery-issued rename; and any other state blocks as
   uncertain;

## Residual v1 design text moved here

Removed from `add-governed-curation-lane`'s design when the relocation protocol
was carved out of that change, so that design agrees with its own spec and
tasks. Every block below is verbatim, labelled by the v1 design section it came
from. It describes target behavior for this change, not shipped behavior.

### From decision 2, canonical run layout

Two entries in the run-layout tree:

```
  transitions/<operation_id>/prepared.json
  transitions/<operation_id>/authorized.json
```

and the state-reconstruction sentences:

can be rebuilt from immutable plans, approvals, relocation candidates,
relocation authorizations, witnesses, and receipts, so a stale state projection
is repairable rather than authoritative. Each operation has exactly one
canonical `prepared.json` candidate and one canonical `authorized.json`; digest-
named preparation aliases and competing candidates are refused.

### From decision 7, partial failure

with the same operation id after guards are rechecked. A placement failure after
relocation preparation is classified from its verified candidate, authorization,
namespace lineage, and exact placement before retry, roll-forward, or refusal.

### From decision 8, compensation

It is immutable, has its own fingerprint and approval rationale, uses the same
one-step content-witness or prepared-relocation evidence protocol and terminal
receipts, and links every result to the forward plan.

### From decision 9, hosted and standalone

same retained filesystem epoch reconstructs from those vault artifacts. If a
placement transition's filesystem epoch or parent tokens become incomparable,
the same portable evidence still powers status but recovery blocks with
`CURATION_RENAME_HISTORY_UNPROVABLE`; portability does not manufacture rename
history on a different filesystem.

### Relocation fault-injection barriers, from decision 10

The curation executor exposes test-only barriers after prepared-state commit;
for placement leaves, after each retained-parent preflight flush, candidate-file
flush, each newly created governed-run ancestor-entry flush, candidate containing-
parent flush, authorization-file flush, authorization-parent flush, rename before
any parent flush, each distinct parent flush, each bound auxiliary-write prefix,
graph/lifecycle finalisation, final-witness file flush, and witness-parent flush;
for content leaves,
after leaf+witness commit; after terminal-receipt commit; and after each
equivalent compensation cut. Tests terminate/recreate the executor with an
abrupt `BaseException` at every barrier, then call read-only `status` and exact
replay. Acceptance requires zero or one durable/adopted semantic effect, at most
one durable/adopted placement and no recovery-issued rename from an exact target,
one terminal receipt, correct next-step selection, and byte-
identical plan identity at every cut. A changed
plan id/fingerprint, altered target, substituted stable identity, advanced or
unavailable prior-state generation, unchanged target-state generation,
cross-epoch lineage, or ambiguous placement must refuse, not recover
optimistically.

### From Risks / Trade-offs

- **[Risk] Commit evidence touches several mature writers.** → Keep the content
  witness seam private and additive; use ordered durable operation-bound
  candidate and authorization records for rename leaves; test every allowed
  adapter against its ordinary leaf; and reject a step kind whose effect is
  neither same-batch witnessed nor exactly reconstructible at every transition
  cut.

### From the Migration Plan

3. Add the private content-leaf commit-witness seam and the placement-leaf
   prepared-relocation seam, one adapter at a time with crash tests before
   enabling its step kind.
