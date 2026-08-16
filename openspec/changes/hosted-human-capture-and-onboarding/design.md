# Design

## Context

Reproduced locally on 2026-08-16 against fresh vaults. Scaffolded and un-scaffolded
vaults behave identically, so vault init is not involved — an earlier hypothesis that it
was is recorded here as rejected so it is not re-investigated.

The engine is sound. `capture_source` already accepts ordinary prose repeatedly and the
result is retrievable:

```
OK 'Dentist Thursday' -> Knowledge Base/Sources/Other/2026-08-16-dentist-thursday.md
OK 'Kim train'        -> Knowledge Base/Sources/Other/2026-08-16-kim-train.md
find 'dentist'        -> the captured source
```

So no new lane is needed. The work is making refusals legible, and specifying the lane
so the UI has something to be correct against.

## Goals / Non-Goals

**Goals**
- A refused write names its cause and carries the evaluator's remediation.
- The capture lane is specified as the supported path for unstructured human capture.

**Non-Goals**
- Relaxing the semantic contract on `remember`. It is doing its job.
- Moving contract enforcement from write-time to promotion-time. That remains an open
  question worth revisiting, but it is a larger change and this one does not need it.
- Any UI change; that is the companion `substrate` change.

## Decisions

### Preserve the structured error rather than re-deriving the code downstream

`commands.py` raises `ValueError(f"{e.code}: {e.reason}")` at 26 sites. The comment at
one of them explains why: *"FastMCP serializes raised exceptions; we want a structured
shape."* The intent was right and the execution loses the structure it was trying to
create — the code ends up inside a string, and `relation_review._translate` inspects
`.code`, finds `None`, and falls through to `SEMANTIC_CREATION_FAILED`.

**Decision:** raise an exception type that is still a `ValueError` (so every existing
`except ValueError` keeps working, and the serialized message is unchanged) but that
carries `.code`, `.reason` and `.remediation` as attributes.

*Alternative rejected:* parse the code back out of the message string in `_translate`.
It would work and it is smaller, but it re-derives structure that was thrown away one
frame earlier, and the next raise site that formats its message differently breaks it
silently.

*Alternative rejected:* change all 26 sites to raise a non-`ValueError`. Larger blast
radius across callers and tests for no additional benefit.

### `remediation: null` must mean "none was produced"

Today it means "we lost it". Once the code survives, remediation should survive with it;
where the evaluator genuinely produced none, the response should say so explicitly
rather than leaving a caller unable to distinguish the two.

### The capture lane is specified, not built

`capture_source` exists and works. The spec records it as the supported path for
unstructured human capture so the UI change is measured against a requirement rather
than an implementation detail that could drift.

## Risks

- **26 raise sites.** A single mechanical transform keyed on the exact
  `raise ValueError(f"{e.code}: {e.reason}")` shape, with the count asserted, rather
  than 26 hand edits — the same discipline used for the nine admission predicates in
  substrate, where a missed site would have failed only at a later stage.
- **Message stability.** Some tests likely assert on the flattened message text. Keeping
  the new type a `ValueError` with an identical `str()` keeps them passing; any that
  fail are asserting on the bug and should be updated deliberately, not silenced.
