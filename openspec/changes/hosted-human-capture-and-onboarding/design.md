# Design

## Context

Reproduced locally on 2026-08-16 against fresh vaults, with `relation_review._translate`
instrumented and `cli_ops.error_dict` called directly — the same function
`server_hosted._execute_command` calls on the failure path.

Two hypotheses were tested and rejected before the real one held:

1. **Rejected — vault scaffolding.** Scaffolded and un-scaffolded vaults behave
   identically, so vault init is not involved.
2. **Rejected — `commands.py` flattening starves `_translate`.** `_translate` runs
   deeper in the stack than `commands.py`, so it cannot be downstream of the flattening.
   The instrumented spy never fired on this path. `error_dict` also recovers the code by
   walking `__cause__`/`__context__` and, failing that, by parsing `CODE: reason` from
   the message, so the flattening is survivable.

The engine is sound. `capture_source` already accepts ordinary prose repeatedly and the
result is retrievable:

```
OK 'Dentist Thursday' -> Knowledge Base/Sources/Other/2026-08-16-dentist-thursday.md
OK 'Kim train'        -> Knowledge Base/Sources/Other/2026-08-16-kim-train.md
find 'dentist'        -> the captured source
```

`cli_ops.error_dict` is also sound — it already returns the specific code and the full
remediation. The loss happens in exactly one place, `server_hosted._error_response`.

## Goals / Non-Goals

**Goals**
- A hosted refusal carries a message and remediation the recipient can act on.
- The capture lane is specified as the supported path for unstructured human capture.

**Non-Goals**
- Relaxing the semantic contract on `remember`. It is doing its job.
- Rewriting the 26 flattening sites in `commands.py`. Real fragility, not this bug;
  changing 26 raise sites that many tests assert message text against is a
  disproportionate risk for a latent issue.
- Changing `relation_review._translate`. It is not on this path.
- Moving contract enforcement from write-time to promotion-time. Still an open question,
  and this change does not need it.
- Any UI change; that is the companion `substrate` change.

## Decisions

### Restore remediation with a static table, not by passing exception text through

`_error_response` currently hardcodes `remediation: None` and derives the message from
`_message_for(code)`. The obvious fix — copy `error["message"]` and
`error["remediation"]` from the `error_dict` payload — is **rejected**. That payload is
derived from the exception, and the handler that produced it is explicitly a redaction
boundary (*"private boundary redacts exception text"*); `error_dict`'s own fallback
branch returns `{"code": "OP_ERROR", "message": str(exc)}`, so passing the message
through would send raw exception text to a hosted client for any unclassified failure.

**Decision:** add `_remediation_for(code)`, a pure code → static-text lookup mirroring
the existing `_message_for(code)`, and extend `_message_for` with entries for the
contract-refusal codes. Nothing derived from an exception crosses the boundary; the code
was already crossing it. This follows the module's established pattern — `details` is
likewise filtered through the `_HOSTED_MUTATION_DETAIL_FIELDS` allowlist rather than
copied wholesale.

*Alternative rejected:* allowlist which codes may pass their real message through. It
gives a better message for the codes in the list, but it makes the redaction guarantee
conditional on table maintenance — a new code that forgets the list leaks by default.
A static table fails the other way: a missing entry degrades to today's generic message,
which is the safe direction.

### Source the remediation text from the contract definitions

Where the authoring contract already defines remediation
(`semantic_authoring.AUTHORING_CONTRACT.findings`), derive the table entry from it at
import time rather than hand-copying the string, so the two cannot drift. These
definitions are static module data, not exception-derived, so this respects the boundary.

`RELATION_DISPOSITION_MISSING` and `RELATION_DISPOSITION_STALE` are built in
`semantic_contract._disposition_finding` with per-instance remediation text and are not
in `AUTHORING_CONTRACT.findings`, so their hosted entries are authored here. Their
in-contract wording is tuned to an agent retry loop (`validate_only=true`,
`transition_token=<returned>`); the hosted entry says the equivalent thing to a person.

### The specific code, where we already have it

`missing_semantic_unit` surfaces specifically because `SemanticWriteError.
as_semantic_validation_error` projects it. `RELATION_DISPOSITION_MISSING` does not — it
is outside `AUTHORING_CONTRACT.findings`, so the projector returns `None` and the
envelope code `SEMANTIC_CONTRACT_BLOCKED` surfaces instead.

**Decision:** leave the projector alone and give `SEMANTIC_CONTRACT_BLOCKED` its own
hosted entry. Widening the canonical projection changes non-hosted callers too, and
`test_disposition_remediation_is_route_neutral` guards a response-budget ceiling on that
text. The hosted user gets actionable guidance either way; precision beyond that is not
worth the blast radius here.

### The capture lane is specified, not built

`capture_source` exists and works. The spec records it as the supported path for
unstructured human capture so the UI change is measured against a requirement.

## Risks

- **Under-fixing.** This makes refusals legible; it does not make the hosted capture box
  work. That depends entirely on the companion `substrate` routing change. Neither is
  sufficient alone, and the acceptance test is on the `substrate` side.
- **Table drift.** A contract code added later without a hosted entry degrades to
  today's generic message. That is the safe direction, and a test asserts the entries
  that exist stay wired to their contract-definition source.
