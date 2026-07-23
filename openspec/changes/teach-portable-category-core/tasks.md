## 1. Red Contract Tests

- [x] 1.1 Add failing registry tests for the sixteen core categories, exact built-in alias table, open unknown labels, and non-fatal legacy collision migration.
- [x] 1.2 Add failing authoring/bootstrap tests for the role-or-domain model, compact and rich examples, and bounded core projection.
- [x] 1.3 Add failing write-feedback and inference tests for the exact bounded feedback schema and complete five-page reviewed candidates.
- [x] 1.4 Add failing parity and leak tests covering public docs, generated tool descriptions, the generic scaffold, and workflow skills.

## 2. Core Vocabulary And Governance

- [x] 2.1 Implement immutable built-in core category definitions and aliases beside core kinds.
- [x] 2.2 Give core keys/aliases deterministic precedence while preserving legacy colliding extensions with non-fatal warnings.
- [x] 2.3 Preserve vault-only registry serialization and expose deterministic core metadata through registry read surfaces.

## 3. Teaching And Feedback

- [x] 3.1 Version the canonical semantic authoring contract with the role-first selection heuristic and paired compact/rich examples.
- [x] 3.2 Project the contract through bootstrap and generated MCP, REST/OpenAPI, and CLI descriptions/results.
- [x] 3.3 Add bounded advisory category-resolution feedback to shared semantic write leaves without changing default write acceptance.
- [x] 3.4 Emit deterministic reviewed registration candidates for recurring unregistered categories without automatic saves or inferred aliases.

## 4. Public Scaffold And Verification

- [x] 4.1 Update the hand-authored generic scaffold, workflow skills, public semantic-language reference, and checked generated projections.
- [x] 4.2 Regenerate only intentional bounded schema-description fixtures and document why the full vocabulary stays out of generated schemas.
- [x] 4.3 Run focused registry compatibility, authoring, bootstrap, write, surface-parity, indexed retrieval, and no-private-leak tests.
- [ ] 4.4 Run Ruff and the full lean test suite, then record verification evidence.

## Verification evidence (2026-07-23)

- Focused retrieval, teaching, surface-parity, registry, bootstrap, write-feedback, workflow-skill, and no-private-leak suite: 259 passed.
- Ruff: all changed/new Python files passed the repository lint selection.
- Canonical semantic authoring contract: `exomem.semantic-authoring:v3` / `sha256:2a754bb2da87cf062876878bfb908a9c8a2bd6ded218443890aa89977057d8d6c`.
- Full Linux lean suite remains delegated to the required GitHub Python 3.11/3.13 matrix because the managed Windows sandbox denies WSL, named pipes, and POSIX-only collection paths.
