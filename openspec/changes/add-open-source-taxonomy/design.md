## Context

Five facts from the current source shape this design.

First, the closed vocabulary is not a Python enum and never reaches the wire as one. `schema.load_source_schema` scrapes `` `token` `` matches out of one markdown table row in the vault's `_Schema/references/frontmatter.md`, and the published tool schema exposes the parameter as a bare string whose only statement of the permitted set is the sentence in its description. The closedness therefore lives in exactly two places: prose an agent reads, and `schema.validate_source`, which refuses anything outside the scraped tuple. Opening the vocabulary is a validation and contract change, not a schema-shape rewrite.

Second, the scraper's token pattern matches lowercase letters only. A hyphenated kind cannot be expressed in the file that defines the vocabulary, so the current mechanism structurally cannot hold a multi-word label. This is why the vocabulary source of truth has to move rather than be widened in place.

Third, the routing table is a single unguarded dictionary lookup in the capture path, and its folder-description companion has already been duplicated and drifted into the index writer, which carries an extra key the capture path does not. There is one routing decision and two half-synchronised label tables.

Fourth, the pattern this change needs already exists twice. `project_keys` implements an open slug vocabulary with auto-registration on first write, a derived display label, a Levenshtein typo guard that names its near-miss, registry updates folded into the caller's atomic write batch, and a live registration hint read at tool-registration time so the published schema advertises the current set as explicitly non-exhaustive. `semantic_language_registry` implements the richer definition shape — aliases, status, replacement, findings, core-versus-user split — and its category resolver already returns "unregistered but canonicalized" for an unknown value, which is precisely the open posture required here. Its sibling kind resolver returns nothing for an unregistered value, so both the open and the closed posture already coexist in one module and choosing the open one is a precedent rather than an invention.

Fifth, retrieval already covers most of this. Arbitrary frontmatter is filterable through an RFC-6901 pointer field, and the page view already unions singular `project` and plural `projects` frontmatter into one query field — so writing project keys onto a source makes the existing project shortcut work with no retrieval code at all. Separately, index-pushdown planning already reports every page-level expression as unsupported, meaning all page predicates are evaluated in memory over the parsed page. Promoting two more page fields to first-class therefore costs nothing at the storage layer and requires no reindex.

## Goals / Non-Goals

**Goals:**

- Make source kind and subject domain independently open, so a meaningful label the product has never seen works with no release and no migration.
- Keep the semantic metadata authoritative and reduce the filesystem to a deterministic, comprehensible projection of it.
- Preserve every existing client and every already-captured path byte-for-byte.
- Keep capture cheap and unconditional: classification improves organisation, it never becomes a precondition for preserving material.
- Make fallback use visible and measurable rather than silent.
- Reuse the two registry patterns already in the codebase instead of adding a third.

**Non-Goals:**

- Reclassifying, relocating, or rewriting references for any already-captured source.
- Enforcing agreement between an existing source's location and its metadata.
- A universal ontology, or an attempt to enumerate every meaningful kind or domain.
- Server-side inference of classification by any model.
- Persisting dismissal, acceptance, or cooldown state for the new advisory.
- A new tool, service, database, index, or background worker.

## Decisions

### Open the existing kind field rather than adding a second one

`source_type` stays the single durable kind axis and its vocabulary opens. `source_kind` becomes an accepted parameter name for the same axis, and when both are supplied with different values the capture is refused rather than silently preferring one.

The alternative — introducing `source_kind` as a genuinely new frontmatter field with `source_type` retained as a compat shadow — was rejected. It would leave two fields asserting the same thing, force every reader to handle both indefinitely, and give already-captured sources only the legacy one. The field name was never the defect; its vocabulary was closed. Renaming is not required to open it, and a parallel field would create exactly the drift this change exists to remove.

Because an explicit conflict must be detectable, both parameters default to absent rather than the old literal default. The published default for the existing parameter therefore moves from the fallback string to absent. Behaviour is unchanged — absent still resolves to the fallback — and the schema delta is deliberate.

### Move the machine truth into a vault-owned registry, and let the reference doc be documentation

A new `_Schema/source-taxonomy.yaml` joins the four registry files already in that directory. It carries both axes, each entry defining a canonical key, a display label, a path segment, an optional description, aliases, status, replacement, and — for kinds — whether the kind requires a URL.

The frontmatter reference keeps its `source_type` row, still marked required so the required-field parser continues to find it, but the row now documents an open vocabulary and points at the registry. The enum scrape is removed. This is the load-bearing move: a markdown table parsed by regex is the root cause, not an incidental implementation detail.

`requires_url` in the registry replaces the hard-coded three-element tuple in the schema parser, so a user-defined kind can declare its own URL requirement instead of that property being reserved to three built-ins.

### Accept an unregistered key immediately and register it in the same atomic write

An unregistered but valid key is used at once, and its registry entry is folded into the capture's existing atomic write batch — the same mechanism project keys already use. There is no administrative ceremony, and no second call.

Persisting the entry rather than merely canonicalizing in memory earns three things: the typo guard has a set of known keys to measure against, so vocabulary drift is actually preventable; the display label and path segment become editable afterwards; and the difference between a registered and a merely-used label becomes observable. The cost is one additional small file in a batch that already writes several, which is the cost model project keys already accepted.

### Derive the path from the validated key, never from the input

The projection is the canonical kind's path segment, then the canonical domain's path segment when a domain is present, then the existing date-and-slug filename. A path segment comes from the registry, or is derived by title-casing the canonical key when the registry declares none.

This is what makes the safety argument structural rather than a filter list. Every hostile form — traversal, absolute and drive-qualified paths, network shares, embedded separators in either direction, bare and repeated dots, trailing dots and spaces, control characters, pathological Unicode — is eliminated at normalization, because normalization runs the value through the existing slugifier and then requires the result to match the canonical-key pattern. Nothing that fails that gate can reach a path segment, so the projection never treats user vocabulary as path input.

Two guards remain genuinely new, because a valid slug can still be a bad directory name. A canonical key naming a filesystem-reserved device would title-case into an unopenable folder, and the repository has cross-OS intent but no such guard anywhere today. And two distinct keys whose path segments differ only by letter case would collide on a case-insensitive filesystem, so registration refuses that collision and registry validation reports it.

### Omit the domain level rather than substituting a placeholder

A capture with no domain projects to the kind level alone. This is what makes the change invisible to existing clients: a bare legacy kind lands exactly where it landed before, byte-for-byte, with no placeholder directory and no migration.

It also makes the fallback case legacy-compatible by construction. The fallback kind with a domain projects beneath the fallback's own segment, which is precisely where real vaults already grew their subject folders. The change does not have to reconcile with that existing shape; it produces it.

Depth is capped at two levels beneath the source root, so the projection cannot deepen without a contract change.

### Deliberately do not enforce a path-to-metadata invariant on sources

Compiled notes carry a bidirectional correspondence between their canonical destination and their declared type, and a mismatch is a blocking finding. Sources must not acquire that.

Applying it here would instantly mark every source already sitting under the fallback's subject folders as violating, and every such finding would carry an implicit instruction to move an append-only, provenance-bearing file. The invariant would manufacture exactly the migration pressure this change refuses. The projection therefore applies at capture time only, and an already-captured source is valid wherever it is.

### Reuse the advisory-suggestion channel, distinguished by its own kind

The advisory is emitted through the same `structure_suggestion` field the structural-promotion detector already uses, carrying a new kind value, and it reuses that detector's established conventions: bounded payload, deterministically ordered reason codes, `strength` of exactly `strong` or `moderate`, no numeric confidence, and a bare exception guard after the write completes so a detector fault cannot convert a committed capture into a failure.

A separate `taxonomy_suggestion` field was rejected. There is one advisory channel and one presentation contract in the agent guidance; a second field would double both for no gain, and the requirement is explicitly to avoid a second structural-suggestion subsystem.

Detection uses two cheap deterministic sensors: a fallback capture that nonetheless carries a domain, which is moderate on its own, and several fallback captures sharing one domain, which is strong. Recurrence is established by a single bounded directory listing that stops counting at the threshold, so there is no corpus scan, no model call, and no new persistent state.

The exclusion that matters most is that the fallback kind is also used internally as a marker — evidence sidecars, media sidecars, and imported adoption material all carry it on pages that are not user classifications at all. Those are excluded, mirroring the compiled-types-only gate the structural detector already applies.

Durable dismissal and cooldown are deliberately absent. The write-warning suppression change owns that mechanism; this change emits an advisory with a fingerprint-able identity so the two compose rather than competing.

### Promote two filter fields because it is free, and keep tags out of it

Project filtering needs no retrieval work at all: the page view already unions singular and plural project frontmatter into one query field, so writing project keys onto a source activates the existing shortcut.

Kind and domain become first-class page filter fields with matching recall shortcuts. Index-pushdown planning already treats every page-level predicate as unsupported and evaluates it in memory over the parsed page, so this adds no table, no column, and no reindex — the pre-existing frontmatter-pointer form keeps working alongside it as the general escape hatch.

Tags remain optional secondary labels and are not used to carry any of the three axes.

### Publish the live vocabulary through bootstrap, not through the tool schema

The sibling project-key registry formats its current set into a hint read at tool-registration time, so the published schema advertises the live keys. This change deliberately does not copy that. The `capture_source` schema states the *contract* in frozen text — two independent axes, both open, name what you actually mean, the fallback means low confidence — and the current set reaches the agent through bootstrap instead.

Two reasons, one practical and one that matters more. Practically, the tool schema is generated once and verified byte-for-byte against a committed fixture, so a per-vault description would require registering the tool as an explicit exception to that gate. More importantly, a tool description is serialized to whichever model provider is connected, and regenerating the fixture against a real vault would commit that vault's own kinds and domains into this public repository. Bootstrap is already per-vault and already stays on the machine, so the live vocabulary belongs there. A test asserts that vault-added labels never appear in the published schema.

### Packs suggest labels; the registry owns them

Each built-in pack gains suggested kinds and domains. The pack schema has no optional fields and rejects unknown ones by design, so these are added as required fields across every built-in pack with generic values only, and surfaced only at the profiles that already carry pack detail so the compact size budget is unaffected.

They are discovery hints that resolve against the same registry. A pack defines no vocabulary of its own and selecting one creates nothing, preserving the existing rule that pack selection is guidance rather than structure. Classification works fully with no pack selected.

### Migration is a named follow-up, not an omission

No already-captured source is moved, no reference is rewritten, and no path history is introduced. Existing fallback material stays where it is and stays fully readable and retrievable, and adopting this change requires no migration step.

The gap is real and is recorded rather than left implicit: a governed source-reclassification operation would need stable identity preservation, an exact dry-run preview, atomic rewriting of inbound wikilinks and the `sources` and `ingested_into` reference fields, an audit trail, path-history or redirect semantics, and refusal on ambiguous partial migration. That is a separate change with its own risk surface, and folding it in here would let migration scope consume the feature that motivated it. Until it exists, a source captured before this change stays at its original path by design, not by oversight.

## Risks / Trade-offs

- [An open kind vocabulary could accumulate near-duplicate labels] -> Reuse the project-key typo guard so a near-miss is refused and names the key it resembles; persist registrations so the guard has a set to measure against.
- [The typo guard can refuse a legitimately similar new label] -> Accept it, consistently with the existing project-key behaviour: the refusal states the hand-edit path, and the alternative is silent vocabulary drift that nothing can later repair.
- [Removing the enum scrape weakens a startup consistency check] -> The check was over a table that could not express the vocabulary it defined. Registry validation replaces it with findings that can name aliases, status, and collisions, which the scrape never could.
- [Moving the vocabulary source of truth changes four copies of one reference doc] -> Three are the canonical scaffold, its regenerated plugin copy, and the schema-fidelity fixture vault; the fixture copy has no sync test, so forgetting it silently breaks the golden. Name it explicitly as a task rather than trusting the generator.
- [Registering on the capture path adds a write] -> One small file folded into a batch that already writes the source, two indexes, and the log, with no additional lock or fsync round trip beyond that entry; the existing write-latency ceilings remain the gate.
- [A new advisory could become nagging] -> Require recurrence for `strong`, keep the single-capture case at `moderate`, exclude internal fallback markers, and leave durable suppression to the change that owns it.
- [The parameter default moves from a literal to absent] -> Behaviour is preserved because absent resolves to the fallback; the alternative was silently ignoring one of two conflicting explicit arguments, which is worse than a visible schema delta.
- [The published tool-surface digest moves] -> Regenerate the fixture and packaged contract, and record the pending external-connector refresh in the attestation the release gate checks; the tool set is unchanged, so capability and command fingerprints do not move.
- [Legacy fallback material stays under the fallback] -> Intended. It remains readable and retrievable, the new advisory makes the pattern visible, and relocation waits for the governed migration named above rather than being done implicitly.
