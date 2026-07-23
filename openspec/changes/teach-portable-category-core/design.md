## Context

Semantic-unit categories are already open labels with deterministic normalization, optional aliases, deprecation, replacement, project/page-type scope, exact structured retrieval, and corpus inference. Governed rich-block kinds, tags, and typed relations are separate dimensions. The missing layer is a portable vocabulary and one teaching projection: the built-in registry has core kinds but no core categories, the shipped extension registry is empty, and bootstrap/tool/scaffold examples do not give agents enough repeated guidance to converge.

Basic Memory demonstrates why point-of-use examples matter, but it stores category spellings verbatim and has no executable taxonomy or alias governance. Exomem should copy the low-friction teaching loop while retaining normalization, reviewed evolution, structured retrieval, and rich typed relations. The scaffold is public OSS source and must never contain or be generated from private-vault identifiers.

This change depends on `restore-indexed-category-recall`. Teaching agents to use categories more often is activated only after exact category lookup is candidate-bounded, non-poisoning after transient locks, and correct without FTS5.

## Goals / Non-Goals

**Goals:**

- Give every client a small, versioned category vocabulary that works before a vault extension registry exists.
- Support a mixed model with a deterministic selection heuristic: category is one primary lens and may describe role or domain.
- Teach category, governed kind, tags, and relations without bloating every generated schema.
- Preserve open authoring and add bounded, non-blocking resolution feedback.
- Make recurring corpus vocabulary reviewable as complete deterministic proposals.
- Upgrade existing registries without invalidating all category resolution.

**Non-Goals:**

- A closed ontology, mandatory note migration, rejection of unknown categories, or category ranking boosts.
- Automatic semantic equivalence or fuzzy correction between differently authored labels.
- Confidence/authority scores, server-side reasoning, or private-vault-derived public defaults.

## Decisions

### 1. Built-in categories are code-owned; vault categories remain extensions

Add an immutable `core_categories` ring beside `core_kinds`. It contains exactly:

`decision`, `fact`, `finding`, `insight`, `constraint`, `requirement`, `assumption`, `risk`, `problem`, `question`, `action`, `technique`, `preference`, `code`, `design`, and `config`.

The exact built-in alias table is:

- `decisions` → `decision`; `facts` → `fact`; `findings` → `finding`; `insights` → `insight`
- `constraints` → `constraint`; `requirements` → `requirement`; `assumptions` → `assumption`; `risks` → `risk`
- `problems` → `problem`; `questions`, `open_question`, `open_questions` → `question`
- `actions` → `action`; `techniques` → `technique`; `preferences` → `preference`
- `designs` → `design`; `configs`, `configuration`, `configurations` → `config`

Keys and aliases use the existing NFKC, case-fold, whitespace/hyphen/underscore normalization. No fuzzy aliases are inferred.

Core precedence is deterministic and backward compatible. If an existing extension registry contains a now-reserved core key or alias, the entry remains byte-preserved in extension serialization, core resolution wins, and the registry emits a non-fatal `core_category_shadowed` warning. Warnings do not put the whole registry into `registry_invalid`; only error-severity findings do. New saves may preserve or remove the shadowed entry but cannot change core resolution. This avoids bricking a vault on upgrade.

Alternative considered: seed only the scaffold YAML. Rejected because existing vaults and clients without a registry would still resolve the vocabulary as unregistered.

### 2. Prefer a meaningful role; use domain when the role would be generic

The selection rule is:

1. If an epistemic or operational role is meaningful, use it as category and put the domain in tags: `[constraint] ... #code`.
2. If the role would only be generic (`fact`, `finding`, or `observation`) and the durable retrieval lens is the domain, use a domain category: `[design] ... #api`.
3. Use exactly one primary category; secondary dimensions belong in tags.

Kind remains the governed form and typed relations remain edges. For a rich unit, omitting category continues to default it to the governed kind. If that kind is also a core role category, inference counts it as that role; authors are told not to add redundant `- category: decision`. An explicit domain category is used only when it intentionally differs from the rich kind.

### 3. One versioned contract, bounded per-surface projections

The semantic authoring contract owns the definitions, alias table, selection rule, examples, and open escape hatch. Full bootstrap, the public semantic-language reference, the generic scaffold, and authoring workflow skills project the complete contract. Generated MCP/REST/OpenAPI/CLI write surfaces project only the contract identity, the short selection rule, one compact example, and a pointer to bootstrap/full guidance. Parity tests compare the identity and bounded projection rather than duplicating all sixteen keys in every schema.

### 4. Feedback is exact, bounded, and advisory

The shared semantic write leaf may return `category_feedback` with at most eight entries and `category_feedback_omitted`. Each entry is one of:

`{unit_ref, authored, normalized, canonical, status, replacement}`

where `status` is exactly `alias`, `deprecated`, `scope_violation`, or `open`; `replacement` is nullable. Unknown well-formed categories use `open`, remain successful, and are never rewritten. No near-match or fuzzy suggestion is part of this change. Existing saved strict contracts retain their authority; the starter vocabulary never enables strictness.

### 5. Evolution emits a complete saveable proposal

Inference emits one deterministic `register_category` candidate when an unregistered normalized category appears on at least five distinct selected pages. The candidate includes exact counts, at most five bounded examples, and a complete extension-registry proposal with the fixed generic description `User-defined semantic category observed across multiple pages.` It never invents aliases, scope, or semantic meaning. Core categories—including rich units whose omitted category defaults to a core kind—never become registration candidates. Saving still requires the existing explicit reviewed compare-and-save operation.

## Risks / Trade-offs

- [Sixteen labels become too prominent] → Keep the escape hatch explicit and apply no ranking boost or rejection.
- [Role/domain inconsistency persists] → Use the role-first heuristic and paired examples; inspect actual use through inference.
- [Legacy custom key conflicts with core] → Preserve bytes, give core deterministic precedence, and emit a non-fatal migration warning.
- [Generated schemas become noisy] → Project the contract identity and short rule only; keep the full table in bootstrap/reference/skill.
- [Generic inferred descriptions are intentionally weak] → They make proposals saveable without pretending the server inferred semantics; review can refine them.

## Migration Plan

1. Land and verify `restore-indexed-category-recall` first.
2. Introduce core categories with legacy collision warnings that do not invalidate registry resolution.
3. Version the authoring contract and regenerate bounded projections.
4. Add exact advisory feedback and complete inference candidates.
5. Run registry compatibility, leak, projection, write, bootstrap, retrieval, and full lean tests before activating the new guidance.

Rollback removes the built-in projection and guidance. No note or extension-registry rewrite needs reversal.

## Open Questions

None. New core labels or aliases require a later reviewed contract version.
