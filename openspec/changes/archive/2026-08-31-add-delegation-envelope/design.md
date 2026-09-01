# Design — the delegation envelope

## D1. Ceilings are product law; the envelope moves only below them

The v1 table, from the architecture report §8, adjusted where the shipped
product is already more specific:

| Action class (v1 id) | Ceiling | Disposition | v1 default by prominence (off/light/balanced/maximal) |
|---|---|---|---|
| `hygiene_writes` (index/log/backrefs riding a governed write) | silent | fixed: silent | silent at every level |
| `proactive_capture` (agent-initiated capture/record/plan writes) | silent-capable | ranged: off → advisory → silent | off / off / silent / silent |
| `link_acceptance` (relation-queue acceptance) | confirm | ranged: off → advisory → confirm-shortcut | off / advisory / advisory / advisory |
| `structural_suggestions` (structural advice on every channel: the review/attention plane's structural categories and the write-response structure advisory) | advisory (surface only) | ranged: off → advisory | off / advisory / advisory / advisory |
| `restructure_execution` (restructure apply, supersession commit, entity creation, deletion) | **confirm-required, always in v1** | fixed: confirm — any change request hits the founder-gate refusal | confirm at every level |
| `disclosure` (cross-boundary) | governance plane owns it | none — served marked governance-owned | refused with a named error |

Six classes; three carry ranges. `confirm-shortcut` is an inline single-action
confirmation rendered with the surfaced item — the confirmation step itself is
never skipped, which is why it sits below the `confirm` ceiling.

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
  class disposition composes with it: class `off` stops agent-initiated
  structural advice wholesale; family `quiet` mutes one family. One mechanism
  per scope, no duplicate state. Class `off` never blocks an explicitly
  requested review — dispositions govern agent-initiated engagement only,
  mirroring S6's "quiet is silent, not clean".

**Confirm-required enforcement is tiered, stated plainly.** The served
envelope marks the class; the agent contract requires in-conversation
confirmation; and the server-side gates that exist today stay mandatory —
deletion's explicit `confirm` parameter and the adoption apply surface's
preview-first default. Supersession and entity creation have no server-side
confirm parameter today; v1 does not add one (that is a tool-schema change
behind the documented rollout, named as future work), and the served contract
says so instead of implying a gate that does not exist.

## D2. Configuration model

The envelope lives in the same shared config file as `mode` and `prominence`
(`mode.config_path()`), for the same reason: the MCP server and the CLI are
often different OS users, and bootstrap serves the active envelope from the
server. Shape: an `envelope` object mapping class id → disposition. An absent
key means "derived from prominence"; a present key is an explicit override that
persists across prominence changes and restarts until reset.

**Per-machine by design.** Prominence itself is machine posture in this same
file; the envelope derives from it and inherits that scope deliberately. What
travels with the vault is the family-disposition store (S6, portable review
state). The spec states the split so nobody discovers it as a surprise:
machine posture in the config file, vault decisions in review state.

**Write-time strictness, read-time tolerance.** Unknown class ids and
out-of-range dispositions are refused at write with a named error. An unknown
id or value found in the *stored* file (e.g. written by a newer runtime) is
reported and ignored at read — reading the envelope never breaks bootstrap.

**Rollback is the absent key.** Deleting the `envelope` object restores pure
derivation, which is today's shipped behaviour; no migration is needed in
either direction.

The supported control route is the existing triage surface:
`exomem://envelope/<action-class>` with an allowed disposition as `action`, or
`reset`. It dispatches before family and item references, accepts neither
`until` nor `expected_fingerprint`, and returns the served class, ceiling,
disposition, provenance, and stable ref. No tool input schema moves.

Derivation is a pure function `derive_envelope(prominence_level) -> dict` in
`prominence.py`'s import-cheap tier: no I/O, pinned by test against the table
above, so the served envelope is always attributable to (level, overrides).

## D3. The closed class set

Six ids, a closed enum. New action classes arrive only through a spec change —
an unclassified future behaviour has no envelope cell and therefore no
authority (fail-closed, matching the matrix's posture). `restructure_execution`
covers the adoption/curation apply surfaces, supersession commits, entity
creation and deletion because they share one property: hard-to-reverse
mutation of canon.

## D4. Adaptation: deterministic, consent-shaped, once

Only explicit triage history moves anything, and only by offering:

- A plain-language request ("stop suggesting projects") is mapped by the agent
  to a registered family and lands through the existing S6 surface:
  `triage_memory(ref="exomem://review/family/<family>", action="quiet")`. The
  envelope adds no second quieting mechanism and no server-side language
  resolver — the mapping is the decider's judgment, which is where the
  constitution puts it.
- Three **manual-origin dismissal events** in one family arm the next surfacing
  of that family with exactly one quiet-offer annotation. Events are counted
  from the durable review-state records themselves — not from the live surface
  index — with an `updated_at` strictly after that family's durable normal-reset
  epoch. Pre-reset and automatic-origin decisions never count. The offer is recorded durably
  against the family (`quiet_offered_at`), written through the review-state
  store's existing concurrent-write discipline. It is cleared only by an
  explicit reset of the family to `normal` (which clears the family's slate);
  a decline without a reset never re-offers. A write advisory records its kind
  in the exact first-surfaced ledger row; on the first eligible warning that
  same warning carries the compact quiet offer before its unchanged terminal
  review ref and fingerprint. Ledger or offer failure stays fail-open and does
  not spend the offer marker.
- No behavioural inference from reads, queries or engagement; the spec bans the
  input, not just the current implementation.

## D5. The agent contract and its budget

The decider protocol the bootstrap teaches, per action the agent is about to
take: name the class → ceiling check (above-ceiling intent becomes a proposal,
never an act) → disposition check (off: don't initiate; advisory: surface in
domain language; silent: act, narrate per prominence; confirm /
confirm-shortcut: get the confirmation first) → record the outcome through
triage. Budget: **at most fifty lines per carrier** (compact bootstrap,
scaffold SKILL.md, plugin SKILL.md, hookless custom-instructions block), with
compact bootstrap's share additionally byte-measured against
`COMPACT_BYTE_CEILING` and before/after sizes recorded — the S1/S6 pattern.
Fifty total across four carriers was arithmetic that could not hold six
classes, four dispositions and a protocol; per-carrier is the honest budget.

## D6. Explicitly out of v1

Standing delegation of restructure execution ("do this kind of thing from now
on") is a founder decision (report §16.2). The spec pins the refusal so the
capability cannot drift in as a convenience — and makes that refusal the sole
specified error for the class, so it cannot be shadowed by a generic
range-refusal message. Also out: new server-side confirmation parameters (see
D1), hosted-side enforcement changes, and any new tool schema.
