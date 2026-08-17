# Hosted human capture and first-class onboarding

## Why

The first person to walk Exomem Hosted as a new user could not save anything, and the
first screen they saw was the product's weakest surface.

Typing "Hello there" into the hosted capture box returns
`400 {"code":"SEMANTIC_CREATION_FAILED","message":"hosted command failed","remediation":null}`.

Two separate defects produce that one response.

### 1. The hosted UI calls the governed lane for what is plainly capture

Exomem already has two lanes, and they are the right two:

- `capture_source` — raw material, no contract. Verified locally: three ordinary
  sentences in a row all succeed, and `ask_memory` retrieves them.
- `remember` — a distilled, governed, citable conclusion. Contract applies, correctly.
  A conclusion that other conclusions cite, supersede and contradict must be
  well-formed or the epistemic layer is mush.

The hosted web UI calls `remember`. A person typing "dentist on Thursday" is not
authoring a governed conclusion. The contract then did exactly what it is for, to
someone it was never aimed at. **That routing bug is the reason a user cannot save**,
and the fix lives in `substrate`, not here.

### 2. Every hosted refusal is unactionable

`server_hosted._error_response` builds its error block as:

```python
error = {"code": code, "message": _message_for(code), "remediation": None}
```

It keeps the code and **hardcodes `remediation: None`**, substituting a generic message
from a code lookup. `cli_ops.error_dict` — one frame earlier — has already computed the
specific message and the evaluator's full remediation text, and both are discarded.
That is precisely the observed `"message": "hosted command failed", "remediation": null`.

The redaction is deliberate: the handler comment reads *"private boundary redacts
exception text"*, and exception text can carry vault paths and internal detail. The
boundary is right; the consequence is that a legitimate, expected contract refusal
reaches a hosted user with nothing they can act on.

## Corrections to the first draft of this change

This change was first written against a causal chain that reproduction falsified. The
wrong version is recorded here so it is not re-derived:

- **Claimed:** `commands.py` flattens structured errors, so `.code` is lost and
  `relation_review._translate` falls through to `SEMANTIC_CREATION_FAILED`.
- **Actual:** `_translate` runs *deeper* in the stack than `commands.py`, so the
  flattening cannot starve it. Instrumented reproduction shows `_translate` never fires
  on this path at all. The refusal arrives as `SEMANTIC_CONTRACT_BLOCKED`.
- **Actual:** the flattening to a bare `ValueError` *is* real (17 plain sites, 1 with a
  `(missing:)` suffix, 8 further `e.code` formatting sites — 26 in total), but it is
  survivable. `cli_ops.error_dict` walks the `__cause__`/`__context__` chain for an
  `as_semantic_validation_error` projector and falls back to parsing `CODE: reason` out
  of the message string, so the code is recovered either way. It is latent fragility,
  not the active cause, and rewriting 26 sites is therefore **out of scope** here.

Verified at the boundary on current `main`, calling the exact function the hosted
handler calls:

```
op_remember(plain text)  -> error_dict -> code='missing_semantic_unit'  remediation=<full text>
op_remember(2nd governed)-> error_dict -> code='SEMANTIC_CONTRACT_BLOCKED' remediation=<full text>
```

So the engine and `cli_ops` already do the right thing. Only the hosted boundary throws
the answer away.

## What Changes

- Hosted refusals carry an actionable message and a remediation string, drawn from a
  static code-keyed table in the hosted module. No text derived from an exception
  crosses the boundary, so the redaction guarantee is preserved exactly as it is.
- The capture lane is specified as the supported path for unstructured human capture,
  so the companion UI change is measured against a requirement rather than an
  implementation detail.

## Scope: this repository only

The web UI calls `/api/exomem/commands/remember`, so *which* command it calls is a
`substrate` change. This change covers the engine side and the contract that makes the
UI fix correct.

The companion `substrate` change — routing the capture box to `capture_source` and
making the authenticated first run connect-first — is tracked separately in that repo.

## Impact

- **Affected specs:** `command-surface`
- **Affected code:** `src/exomem/server_hosted.py` (`_error_response`, `_message_for`)
- **Not changed:** the semantic contract itself. Strictness on governed conclusions is
  correct and stays exactly as it is.
- **Explicitly not changed:** the 26 error-flattening sites in `src/exomem/commands.py`,
  and `relation_review._translate`. Reproduction showed neither is on the failing path.
