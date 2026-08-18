## 1. Contract and behavioural acceptance

- [x] 1.1 Add the `command-surface` delta covering the correction operation, its refusals, the metadata-only guarantee, the projection-derived relocation, reference preservation, the read-only proposal mode, and the never-automatic rule.
- [x] 1.2 Add the positive cases with synthetic material only: a fallback source corrected to a real kind, a domain corrected alone, and a correction whose projection resolves to the source's current location.
- [x] 1.3 Add the refusal cases: neither axis supplied, no reason supplied, a value that cannot normalize into a safe canonical key, and a path that is not a source.
- [x] 1.4 Add the immutability regressions: the body is byte-identical after a correction, and identity, capture timestamp, origin, tags, and ingested-into entries are all unchanged.
- [x] 1.5 Add the reference-preservation cases: a citing note's provenance entry and a linking page's wikilink both follow the source, and no reference to the previous location survives.
- [x] 1.6 Add the atomicity regression: an injected failure part-way leaves the source at its original location with its original classification and no reference rewritten.
- [x] 1.7 Add the previous-path case: the corrected source records where it used to be.
- [x] 1.8 Add the proposal cases: a preview writes nothing, a domain already present in the location is proposed with its supporting observation, and a source whose evidence supports no kind is reported as having no proposal rather than being offered the fallback.
- [x] 1.9 Add the never-automatic regressions: capture, compilation, index update, and maintenance relocate nothing, and a registry path-segment rename migrates nothing.

## 2. Core operation

- [x] 2.1 Add the reclassification module resolving the supplied kind and domain through the existing taxonomy rules and deriving the destination from the existing projection.
- [x] 2.2 Rewrite only the classification fields and the correction-record fields, asserting the body is unchanged rather than assuming it.
- [x] 2.3 Reuse the existing within-tree relocation and inbound-reference rewriting rather than reimplementing either.
- [x] 2.4 Record the previous path and the stated reason on the corrected source.
- [x] 2.5 Fold the relocation, the metadata rewrite, and every reference rewrite into one atomic batch.
- [x] 2.6 Refuse a correction that supplies no axis, no reason, an unsafe value, or a path outside the source tree.
- [x] 2.7 Add the read-only proposal mode reporting destination, affected-reference count, and per-value evidence, declining rather than guessing.

## 3. Surfaces

- [x] 3.1 Expose the operation and its proposal mode on the product command surface with parameter descriptions stating that classification values are open and that the caller decides.
- [x] 3.2 Expose it on the CLI alongside the existing capture and maintenance commands.
- [x] 3.3 Teach the correction path in the agent contract, including that the classification advisory is now actionable and how to act on it.
- [x] 3.4 Update the scaffold reference documenting source mutability so it states that classification is correctable while the body is not.

## 4. Verification

- [x] 4.1 Prove the tests bind the mechanism: disable the metadata rewrite and the reference rewrite in turn, confirm the positive cases fail while the refusal controls still pass, and record the output.
- [x] 4.2 Run the focused reclassification, capture, taxonomy, move, and edit tests.
- [x] 4.3 Run the contract-surface tests: schema fidelity, connector guardrails, plugin sync, hosted rendering, bootstrap, bootstrap compact budget, and scaffold leak.
- [x] 4.4 Validate the change in strict mode, validate the specs in strict mode, and run the repository lint and the full lean suite with no test selection filter against an `origin/main` baseline.
- [x] 4.5 Drive a real correction through the installed surface and confirm the location, the frontmatter, the untouched body, the rewritten references, and the recorded previous path.
- [x] 4.6 Read the complete diff for private material and replace any accidental example with a synthetic one.
