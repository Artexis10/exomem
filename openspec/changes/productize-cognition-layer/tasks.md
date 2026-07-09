# productize-cognition-layer — tasks

## 1. Spec / Product Contract

- [x] 1.1 Add the `cognition-layer` spec and validate with OpenSpec.
- [x] 1.2 Decide whether this change supersedes
      `document-existing-vault-onboarding`; archive or fold that docs-only
      change after this plan is accepted.
- [x] 1.3 Define the canonical user-facing tagline and mental model in README:
      "durable memory with sources, proof, history, and review" or equivalent.

## 2. Existing Vault Adoption

- [x] 2.1 Design `adopt` output shape: scan summary, untouched-files contract,
      candidate packs, suggested compile/copy actions, and truncation markers.
- [x] 2.2 Implement scan-only adoption mode using existing `overview` and
      vault/link/frontmatter scanners; no writes by default.
- [x] 2.3 Add optional manifest save mode, with explicit destination and
      no writes outside `Knowledge Base/`.
- [x] 2.4 Add tests proving scan-only adoption creates/modifies/deletes no vault
      files.
- [x] 2.5 Add a worked existing-vault onboarding path to README/QUICKSTART.

## 3. Knowledge Packs

- [x] 3.1 Define declarative pack schema and built-in pack directory.
- [x] 3.2 Ship initial packs: legal/warranty, creative, technical,
      health/athletic, business, personal records.
- [x] 3.3 Add pack validation tests, including unknown fields and scaffold leak
      checks.
- [x] 3.4 Make adoption report suggest likely packs from deterministic signals
      such as folder names, file names, frontmatter keys, and media types.

## 4. Simple Agent Front Door

- [x] 4.1 Add registry metadata that marks tools as `primary` versus
      `advanced`, with readable intent descriptions.
- [x] 4.2 Decide first-tranche implementation for simple verbs:
      aliases/descriptions only versus thin leaves for `save`, `adopt`,
      `prove`, `review`, `update`, and possibly `ask`.
- [x] 4.3 Update MCP schema snapshots after any public tool/description change.
- [x] 4.4 Add tests that bootstrap and tool schemas expose the simple front door
      while advanced tools remain available.

## 5. Evidence / Proof Workflow

- [x] 5.1 Update docs and skill guidance: Source = raw input; Evidence = source
      or artifact used as proof for a claim/case/context.
- [x] 5.2 Add a case/proof example to the sample vault.
- [x] 5.3 Add or refine `prove` workflow tests: retrieve existing evidence,
      link evidence to a case/claim, and avoid treating all sources as evidence.

## 6. Documentation / Onboarding

- [x] 6.1 Human docs: explain Exomem vs built-in AI memory and Exomem vs note
      graph tools.
- [x] 6.2 Agent docs/bootstrap: teach search-first, save durable conclusions,
      prove with evidence, update via supersession, and hide ontology from the
      user.
- [x] 6.3 Developer/admin docs: document pack schema, adoption modes, and how
      advanced typed tools map under simple verbs.

## 7. Verification

- [x] 7.1 Run OpenSpec validation.
- [x] 7.2 Run `uv run pytest -q` or the targeted test subset if the full suite
      is too slow for the tranche.
- [x] 7.3 Run scaffold leak tests after docs/skill changes.
