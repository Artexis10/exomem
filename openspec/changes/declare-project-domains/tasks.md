## 1. Registry and governed write

- [ ] 1.1 Read `domain_origin` and `domain_rejected` in `project_keys.py`. A declaration without an origin reads as `reviewed`.
- [ ] 1.2 Add `schema_memory` `inspect`, `propose` and `save` for project-key domains with `expected_hash` and `why`. A save may register new domain keys through the domains registry writer.
- [ ] 1.3 Make a rename add a domains-registry alias, and make a reject remove the declaration and record the rejection.

## 2. Vocabulary family

- [ ] 2.1 Register the `project-domain/1` family under the agent-led vocabulary workflow, with its evidence, validator, decisions and authority mapping.
- [ ] 2.2 Produce a consideration for each project key that has member pages and no domain, and for each declaration that does not resolve. Bound and fingerprint the evidence.

## 3. Onboarding and skill

- [ ] 3.1 Add the domain review step to `adopt_vault` and the first-session bootstrap.
- [ ] 3.2 Teach the workflow in the generic skill scaffold, and keep the scaffold generic.

## 4. Proof and rollout

- [ ] 4.1 Add the fixture end-to-end check: undeclared keys, then inferred declarations, then topics on the map and domain maps returning members.
- [ ] 4.2 Run the real-model check on dogfood vaults, including the hostile cases.
- [ ] 4.3 After release, run the workflow on the personal vault through the governed route, coordinated with its configuration-hold owner, and report the declared domains to the owner for review.
