# document-existing-vault-onboarding — tasks

## 1. Docs

- [x] 1.1 SETUP-LOCAL.md: add "Already have a vault full of notes?" after the
      "One command" section (write contract, searchability, same-vs-separate
      vault, daily-notes bullet, `overview` worked example).
- [x] 1.2 README quickstart: link the existing-vault paragraph to the new
      SETUP-LOCAL section.
- [x] 1.3 Cross-check scaffold SKILL.md "Assessing a vault you didn't build"
      wording for consistency (no scaffold edit expected).

## 2. Verification

- [x] 2.1 `uv run pytest -q` still green (docs-only); read both sections
      end-to-end for coherence.

## Closure evidence (T10 audit, 2026-10-09)

- 1.1: Commit 2cc6c8076 added "Already have a vault full of notes?" to SETUP-LOCAL.md and shipped in v0.3.0. PR #106 renamed that file to QUICKSTART.md, which still carries the section.
- 1.2: README.md links that section at `QUICKSTART.md#already-have-a-vault-full-of-notes`.
- 1.3: The T10 audit rechecked the scaffold: `references/vault-care.md` treats everything outside the governed folder as read-only input, which agrees with the section. The "Assessing a vault you didn't build" heading that it cites no longer exists; the audit reports that as debt.
- 2.1: The change touches documentation only. The T10 audit read both sections end to end; every later PR runs the full suite over these files.
