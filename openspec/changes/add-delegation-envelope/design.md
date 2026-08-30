# Design — the delegation envelope

## D1. Ceilings are product law; the envelope moves only below them

The v1 table, from the architecture report §8, adjusted where the shipped
product is already more specific:

| Action class (v1 id) | Ceiling | Envelope range below the ceiling | v1 default by prominence |
|---|---|---|---|
| `hygiene_writes` (index/log/backrefs riding a governed write) | silent | silent only | silent at every level |
| `proactive_capture` (agent-initiated capture/record/plan writes) | silent-capable | off → advisory → silent | off / off / silent / silent |
| `link_acceptance` (relation-queue acceptance) | confirm | off → advisory → confirm-shortcut | off / advisory / advisory / advisory |
| `structural_suggestions` (review-plane surfacing) | advisory (surface only) | off → advisory | off / advisory / advisory / advisory |
| `restructure_execution` (also supersession commit, entity creation, deletion) | **confirm-required, always in v1** | confirmation UX only | confirm at every level |
| `disclosure` (cross-boundary) | governed by the governance plane | not envelope-configurable | refused with a named error |

Two deliberate deltas from the report's draft table, both because shipped
behaviour is the authority the spec must not silently regress:

- **`proactive_capture` at `balanced` stays autonomous.** The report reserved
  "silent" for maximal; but `balanced` has captured stepping-stones on its own,
  with a one-line report, since the prominence contract shipped. The envelope's
  `silent` disposition means "acts without asking"; *narration* stays the
  prominence axis it already is. Tightening `balanced` to advisory would turn
  every routine capture into a question — a nag increase inside the no-nudge
  programme.
- **`structural_suggestions` has no separate `family-quiet` disposition.** The
  S6 family-disposition store already IS that state, per family. The envelope
  class disposition composes with it: class `off` empties the plane wholesale;
  family `quiet` mutes one family. One mechanism per scope, no duplicate state.

## D2. Configuration model

The envelope lives in the same shared config file as `mode` and `prominence`
(`mode.config_path()`), for the same reason: the MCP server and the CLI are
often different OS users, and bootstrap serves the active envelope from the
server. Shape: an `envelope` object mapping class id → disposition. An absent
key means "derived from prominence"; a present key is an explicit override that
persists across prominence changes and restarts until reset. Unknown class ids
and dispositions outside the class's range are refused with a named error —
the same refusal discipline the family-disposition store uses.

Derivation is a pure function `derive_envelope(prominence_level) -> dict` in
`prominence.py`'s import-cheap tier: no I/O, pinned by test against the table
above, so the served envelope is always attributable to (level, overrides).

## D3. The closed class set

Six ids, a closed enum. New action classes arrive only through a spec change —
an unclassified future behaviour has no envelope cell and therefore no
authority (fail-closed, matching the matrix's posture). `restructure_execution`
covers the adoption/curation apply surfaces, supersession commits, entity
creation and deletion because they share one property: hard-to-reverse
mutation of canon; v1 keeps them behind explicit confirmation regardless of
any other setting.

## D4. Adaptation: deterministic, consent-shaped, once

Only explicit triage history moves anything, and only by offering:

- A plain-language request ("stop suggesting projects") maps to the existing
  S6 surface: `triage_memory(ref="exomem://review/family/<family>",
  action="quiet")`. The envelope adds no second quieting mechanism.
- Three dismissals of items in one family — counted from the review-state
  records that already exist, not from any usage signal — make the *next*
  surfacing of that family carry exactly one quiet-offer annotation. The offer
  is recorded in the family's review-state entry (`quiet_offered_at`), so it is
  made once per family, survives restarts, and is never repeated after a
  decline. Nothing is auto-quieted, ever.
- No behavioural inference from reads, queries or engagement; the spec bans the
  input, not just the current implementation.

## D5. The agent contract and its budget

The decider protocol the bootstrap teaches, per action the agent is about to
take: name the class → ceiling check (above-ceiling intent becomes a proposal,
never an act) → disposition check (off: don't; advisory: surface in domain
language; silent: act, narrate per prominence; confirm: ask first) → record the
outcome through triage. The teaching must fit the compact envelope: the target
is ≤ ~50 lines across all carriers, measured in tasks against
`COMPACT_BYTE_CEILING` with before/after bytes recorded — the S1/S6 pattern.

## D6. Explicitly out of v1

Standing delegation of restructure execution ("do this kind of thing from now
on") is a founder decision (report §16.2). The spec pins the refusal so the
capability cannot drift in as a convenience. Also out: any hosted-side
enforcement changes (the envelope is served and taught; hosted profiles read
the same config), and any new tool surface (inspection rides
`review_memory(mode="dispositions")` and `bootstrap`).
