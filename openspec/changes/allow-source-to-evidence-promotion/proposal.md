# Allow promoting a Source into Evidence

## Why

Whether a raw item is a Source or Evidence is a judgement about **purpose**, not
about the material: "a raw item is a Source by default; it becomes Evidence when
preserved for a claim, case, dispute, warranty, record, or other proof-bearing
context." The same PDF belongs in either tree depending on what it is for.

That judgement is made at capture time, when the answer is frequently unknowable.
A receipt is reference material until the appliance fails; correspondence is
background until it becomes a dispute. And today the classification is
**irreversible**: `move_file` refuses every boundary-crossing move, so a Source
that becomes case-relevant cannot be filed into the case it now belongs to.

The only workaround is to re-capture through `preserve_evidence`, which produces
a second copy of a file whose whole point is to be singular, and abandons the
original's identity and provenance.

The refusal is also justified by reasoning that does not survive inspection.
`move_file.py` states: *"Rule 2 protects content immutability, not file location:
a move that stays WITHIN the same append-only tree carries the bytes verbatim and
only relocates them, so it is permitted."* That argument applies identically to a
`Sources/` → `Evidence/` move — the bytes are carried verbatim there too. The
guard conflates *landing content from outside the governed trees* (which must go
through `add`/`preserve`) with *relocating between two append-only trees*.

The emitted error compounds it: `"Moves OUT of Sources/ are forbidden; relocation
WITHIN Sources/ is allowed"` — advice that cannot help a caller whose item needs
to be in a case file.

## What Changes

- Permit exactly one boundary crossing: `Sources/` → `Evidence/`, carrying bytes
  verbatim, with a required promotion reason recorded in the activity log.
- Keep refusing every other crossing, including `Evidence/` → `Sources/`.
- Replace the misleading refusal text for the demotion direction so it names the
  invariant being protected rather than repeating advice that does not apply.

## The asymmetry, and why it is principled

`Evidence/` carries **per-case completeness**: `Evidence/<scope>/` claims to hold
everything preserved for that case, and handing over a case means handing over
that folder. Removing an item alters what the folder claims to contain — bytes
unchanged, integrity broken. That is a real invariant and demotion violates it.

`Sources/` carries no such property. It is an unordered bag of inputs; removing
one subtracts from nothing that any consumer relies on. Promotion therefore adds
to a case without weakening anything.

The blunt rule protects a genuine invariant in one direction and an imaginary one
in the other. This change keeps the first and drops the second.

## Capabilities

### Modified Capabilities

- `command-surface`: `move_file` permits `Sources/` → `Evidence/` with a recorded
  reason, and continues to refuse all other boundary crossings.

## Impact

- `move_file.py` guard and its refusal messages; activity-log entry gains the
  promotion reason.
- No change to append-only content immutability: promotion relocates bytes and
  never rewrites them.
- No schema change and no new tool; the existing move surface gains one
  permitted direction.
