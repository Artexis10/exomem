## 1. Contract and red-first behavioural acceptance

- [x] 1.1 Add the `command-surface` delta covering open kind and domain vocabularies, separate multi-valued project association, the deterministic projection, path safety, the fallback contract, the advisory suggestion, legacy compatibility, index rendering, and independent retrieval filtering.
- [x] 1.2 Add the `agent-bootstrap-contract` delta covering open-vocabulary guidance, fallback-versus-missing-vocabulary handling, suggestion presentation, and advisory-only pack integration.
- [x] 1.3 Build the positive acceptance fixtures with synthetic material only: a research report about travel belonging to a user project, an academic paper about health, an invoice about equipment, and official guidance about travel. Assert none resolve to the fallback location.
- [x] 1.4 Add the previously-unseen-kind and previously-unseen-domain acceptance cases, proving each succeeds with no code change and lands a registry entry.
- [x] 1.5 Add the near-miss refusal case proving the refusal names the resembled key and the escape hatch.
- [x] 1.6 Add legacy-compatibility regressions: every previously accepted kind routes to its exact prior location, and a pre-existing fallback-located source stays valid, indexed, and retrievable with no migration.
- [x] 1.7 Add the fallback contract cases: an unclassified capture still succeeds and resolves to the fallback, and a confidently supplied unknown kind is never demoted to it.
- [x] 1.8 Add the parameter-alias cases: either name accepted, a conflicting pair refused, neither supplied resolves to the fallback.
- [x] 1.9 Add the path-safety suite over both axes: traversal, absolute, drive-qualified, network-share, embedded separators both directions, bare and repeated dots, trailing dots and spaces, control characters, pathological Unicode, empty, normalize-to-nothing, over-length, reserved device names, and case-insensitive path-segment collision. Assert each is refused or normalized and never reaches a path segment.
- [x] 1.10 Add the projection cases: canonical key differing from its path segment, omitted domain omitting a level, determinism for identical metadata, and the two-level depth bound.
- [x] 1.11 Add the no-retroactive-invariant regression: an existing source whose location does not match today's projection raises no finding and is not moved.
- [x] 1.12 Add the retrieval cases: kind, domain, and project each filtering alone, all combinations, and classification filterable with no tags present.
- [x] 1.13 Add the provenance regression: capture through compilation still maintains the source reference list, the back-reference, stable identity, and relations, and the existing session-based note-type heuristic still fires.
- [x] 1.14 Add the advisory cases: recurring fallback captures in one domain produce a strong suggestion, a single unusual fallback capture stays quiet, a coherently classified capture stays quiet, internal fallback markers are never analysed, and an injected detection failure still commits the capture.
- [x] 1.15 Add the index-rendering cases: an unshipped kind appears with a registered or generic description, and counts include sources nested beneath a domain.

## 2. Taxonomy module and registry

- [x] 2.1 Add the taxonomy module with the two-axis definition shape: canonical key, display label, path segment, description, aliases, status, replacement, built-in marker, and the kind-only URL requirement.
- [x] 2.2 Implement normalization: Unicode normalization, the existing slugifier, and canonical-key pattern validation, refusing empty, degenerate, and over-length values.
- [x] 2.3 Implement kind and domain resolution returning canonical key, labels, and status, accepting an unregistered valid key by canonicalizing it.
- [x] 2.4 Ship generic, public-safe built-in kinds and domains, preserving every legacy kind's existing path segment and adding plural aliases.
- [x] 2.5 Implement the projection with registry-declared or derived path segments, the reserved-device-name guard, the case-insensitive collision guard, and the depth bound.
- [x] 2.6 Implement registry load, validation findings, and a plan-then-write registration folded into a caller's atomic batch, reusing the near-miss guard.
- [x] 2.7 Add the vault-owned registry starter file to the scaffold schema directory beside the existing registries.

## 3. Capture path and schema

- [x] 3.1 Remove the enum scrape from the source-schema loader while keeping required-field and location/naming parsing intact, so startup still validates the reference doc without owning the vocabulary.
- [x] 3.2 Delegate kind acceptance to the taxonomy module in source validation, and move URL conditionality onto the registry's per-kind requirement.
- [x] 3.3 Replace the hard-coded folder lookup in the capture path with the projection.
- [x] 3.4 Thread domain and project keys into source frontmatter, registering project keys through the existing project-key planner.
- [x] 3.5 Fold the taxonomy and project-key registry writes into the capture's single atomic write batch.
- [x] 3.6 Derive source-category descriptions from the registry with a generic fallback, and collapse the drifted duplicate in the index writer against it.
- [x] 3.7a Remove the fallback default from the CLI capture alias and give it the kind, domain, and project arguments, so no capture surface can express only the fallback.
- [x] 3.7 Add the kind, domain, and project parameters to the capture command and its underlying operation, refusing an explicit kind-name conflict, and rewrite the parameter descriptions to state the vocabulary is open. The published schema carries the contract only; the live set is published through bootstrap so no vault's own vocabulary is serialized to a model provider or committed to this repository.

## 4. Advisory suggestion

- [x] 4.1 Add the deterministic classification sensors: fallback-with-domain at moderate strength, recurring fallback-in-domain at strong, using one bounded directory listing that stops at the threshold.
- [x] 4.2 Exclude material that carries the fallback kind as an internal marker rather than a user classification.
- [x] 4.3 Emit the suggestion on the capture result through the existing advisory field with its own kind value, inside a failure-isolating guard that runs after the write completes.
- [x] 4.4 Extend the validated advisory bounds for the new kind rather than bypassing them, in both the leaf and the committed-response projection, so the suggestion actually reaches the caller.

## 5. Retrieval

- [x] 5.1 Add source kind and domain as first-class page filter fields and map them in the page view.
- [x] 5.2 Add matching recall shortcuts alongside the existing project and tag shortcuts.
- [x] 5.3 Confirm project filtering works through the pre-existing singular/plural union with no retrieval change, and that the frontmatter-pointer form still works alongside the new fields.

## 6. Agent contract, scaffold, packs, and generated surfaces

- [x] 6.1 Update the scaffold frontmatter reference: keep the required kind row but document an open vocabulary pointing at the registry, and add the domain and project rows.
- [x] 6.2 Update the scaffold page-type reference location, naming, and required-field lines.
- [x] 6.3 Update the scaffold operations reference capture spec and its kind-inference guidance.
- [x] 6.4 Update the scaffold skill's subfolder-selection contract and source-capture routing, leaving the pinned verbatim tool-selection strings untouched.
- [x] 6.5 Update the scaffold source index template and the capture and ingest workflow skills.
- [x] 6.6 Add bootstrap open-vocabulary and fallback guidance, remove the fallback from the capture route arguments, register the new advisory kind in the post-write guidance keys, and bump the contract version with its pinned assertion.
- [x] 6.7 Add suggested kinds and domains to the pack schema and to every built-in pack with generic values only, validated against the taxonomy, surfaced only at profiles that already carry pack detail.
- [x] 6.8 Hand-update the schema-fidelity fixture vault's copies of the frontmatter and page-type references, which have no sync test.
- [x] 6.9 Regenerate the tool-schema fixture and packaged tool-surface contract, the capability document, and the Claude plugin skill copies from their generators.
- [x] 6.10 Record the pending external-connector refresh in the attestation the release gate checks, with the new digest and the awaiting-refresh rollout state.
- [x] 6.11 Regenerate the hosted candidate artifacts, leave the frozen released identity pins untouched, and confirm with the hosted staleness check.
- [x] 6.12 Update the assistant guide, product model, knowledge-pack, workflow-skill, and quickstart docs.
- [x] 6.13 Add the new module and its test to the strict lint and targeted type-check lists.

## 7. Verification

- [x] 7.1 Prove the tests bind the mechanism: disable the projection and the advisory in turn, confirm the positive cases fail while the negative controls still pass, and record the failure output.
- [x] 7.2 Run the focused taxonomy, capture, schema, structured-filter, compile-proposal, and media-sidecar tests.
- [x] 7.3 Run the contract-surface tests: schema fidelity, connector guardrails, plugin sync, hosted rendering, bootstrap, bootstrap compact budget, workflow skills, and scaffold leak.
- [x] 7.4 Run the semantic write-latency gate before and after on the same quiesced machine, and report the delta against the absolute ceilings and the size-scaling bound.
- [x] 7.5 Run the retrieval latency gate and the product end-to-end loop.
- [x] 7.6 Validate the change in strict mode, validate the specs in strict mode, and run the repository lint and the full lean suite with no test selection filter.
- [x] 7.7 Run the public-artifact validation over the repository and over a built distribution.
- [x] 7.8 Drive a real capture through the installed surface with kind, domain, and projects, and confirm the location, frontmatter, index rows, advisory payload, and independent filtering on each axis.
- [x] 7.9 Read the complete diff for private material: real names, client or organisation names, private project or trip identifiers, and financial, health, equipment, or correspondence details. Replace any accidental example with a synthetic one.
