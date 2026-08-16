# Hosted human capture and first-class onboarding

## Why

The first person to walk Exomem Hosted as a new user could not save anything, and the
first screen they saw was the product's weakest surface.

Typing "Hello there" into the hosted capture box returns
`400 {"code":"SEMANTIC_CREATION_FAILED","message":"hosted command failed","remediation":null}`.
Reproduced locally against fresh vaults, the real behaviour is:

| write | content | result |
| --- | --- | --- |
| 1st | with `## Observations` | OK — an empty corpus auto-bootstraps |
| 2nd | with `## Observations` | `RELATION_DISPOSITION_MISSING` |
| 3rd | plain text, no units | `RELATION_DISPOSITION_MISSING` |

**The second memory a hosted user ever saves fails, regardless of what they type.**

The cause is not a defect in the semantic contract. Exomem already has two lanes, and
they are the right two:

- `capture_source` — raw material, no contract. Verified: three ordinary sentences in a
  row all succeed, and `ask_memory` retrieves them.
- `remember` — a distilled, governed, citable conclusion. Contract applies, correctly.
  A conclusion that other conclusions cite, supersede and contradict must be
  well-formed or the epistemic layer is mush.

The hosted web UI calls `remember` for what is plainly capture. A person typing
"dentist on Thursday" is not authoring a governed conclusion. The contract then did
exactly what it is for, to someone it was never aimed at.

Two further problems compound it:

1. **The diagnosis is destroyed in transit.** `commands.py` flattens structured
   contract errors into `ValueError(f"{e.code}: {e.reason}")` at 26 sites, so `.code` is
   lost and `relation_review._translate` falls through to a generic
   `SEMANTIC_CREATION_FAILED` with `remediation: null` — discarding remediation text
   that exists inside the exception. This cost two wrong diagnoses during the
   investigation before the raw response body was read.
2. **The first run shows a notes app.** Accepting an invitation lands on `/exomem/home`:
   a capture box and a search box. Stripped of the assistant that invites comparison
   against general note apps, a comparison Exomem does not win, while the
   differentiator — memory resident inside Claude and ChatGPT — is invisible.

## What Changes

- Hosted human capture routes to the capture lane instead of the governed-conclusion
  lane, so ordinary typed text saves and stays findable.
- Structured contract errors keep their code and remediation all the way to the caller.
- The authenticated first run leads with connecting an assistant; web capture and
  search remain, demoted.

## Scope: this repository only

The web UI calls `/api/exomem/commands/remember`, so *which* command it calls is a
`substrate` change, not an `exomem` one. This change covers the engine side and the
contract that makes the UI fix correct:

- error fidelity, so a refusal names its cause and carries its remediation
- the capture lane as the specified, supported path for unstructured human capture

The companion `substrate` change — routing the capture box to `capture_source` and
making the authenticated first run connect-first — is tracked separately in that repo
and depends on this one only for the error contract.

## Impact

- **Affected specs:** `command-surface`
- **Affected code:** `src/exomem/commands.py` (26 sites flattening structured contract
  errors into `ValueError`), `src/exomem/relation_review.py` (`_translate` fallback)
- **Not changed:** the semantic contract itself. Strictness on governed conclusions is
  correct and stays exactly as it is. This change stops applying it to material that
  never claimed to be a conclusion, and makes every refusal legible.
