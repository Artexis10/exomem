# document-existing-vault-onboarding — design

## Context

Docs-only companion to `add-vault-overview` and `add-setup-wizard`. The
mechanics exist; the narrative for "I already have a vault" doesn't.

## Goals / Non-Goals

**Goals:** one canonical section a nervous first-timer can read in a minute and
come away knowing their files are safe, where the KB goes, and what to ask
Claude first.

**Non-Goals:** no migration tooling, no vault-restructuring advice beyond "you
don't have to," no changes to the scaffold beyond consistency checking.

## Decisions

1. **The section lives in SETUP-LOCAL.md** (the doc the wizard points at), right
   after "One command" — it's an onboarding concern, not a README concern; the
   README carries one paragraph + link.
2. **Daily-notes vaults get their own bullet.** The observed failure case; the
   message is "leave it as it is — the KB is a compiled layer beside your log."
3. **Same-vault is the stated default**, separate vault positioned as the
   hard-isolation exception, matching `init`'s refuse-if-exists safety.

## Risks / Trade-offs

- [Wording drift vs SKILL.md guidance] → cross-check task; both say
  "overview first, never bulk-read."

## Open Questions

(none)
