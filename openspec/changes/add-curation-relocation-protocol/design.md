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
