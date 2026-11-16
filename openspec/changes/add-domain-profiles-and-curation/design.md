# Design

## Context

Context compiler v2 delivers session context on every turn without a tool call:

- the global profile, in the `stance` block;
- the vault skills catalogue, in the `skills` block;
- the map.

Each block has its own bound outside the turn's `max_chars`. Each is sent once per conversation, with an `unchanged` state carried through the continuity token. This change adds per-domain profiles, keeps them current, and evaluates pick-by-problem routing tables as a method carrier.

Evidence from a real vault, read-only:

- Per-domain preferences already exist as long research notes. One hub note holds a cross-domain through-line, and one note per domain inherits from it.
- These notes run to tens of units each, so sending one whole note without a search would cost too much context.
- The domain registry is flat: about a hundred keys, with overlapping pairs such as a broad audio key and a narrower audio-engineering key.
- No code resolves a turn to a registered domain. On two probe turns, the turn slice was `skipped` or `empty`, and the relevant preference note arrived only as the second task lead, behind an unrelated page.

Design sources:

- Anthropic, "Effective context engineering for AI agents" (2025): send the smallest high-signal context, selected for the turn.
- Anthropic, "Agent Skills": deliver procedural knowledge on demand, not through global instructions.
- Anthropic, "Demystifying evals for AI agents" (2026): settle uncertain choices with small measured rounds.

## Goals and non-goals

Goals:

- An agent advising in a domain receives that domain's preferences, and its ancestors' preferences, as session context.
- Profiles stay short and current: the owner confirms every line, and Exomem finds what needs review.
- A pick-by-problem routing table reaches an agent whose task matches one of its rows.

Non-goals:

- No server-side model, and no automatic profile writes.
- No new folder level per domain. The root-native layout keeps compiled areas flat.
- No change to the global profile's wire key or to its existing contract.

## Decisions

### D1. A `profile` note type in one area

The shipped registry gains `profile`: compiled, folder `Notes/Profiles`, no partition.

- **Selection key.** The page's existing `topics` field identifies its domain. That field already resolves aliases and replaced keys, and it already places the page in its domain map. The `domain` field is not used: it names a source's subject or an experiment's folder.
- **Topic count.** Exactly one resolved topic makes a domain profile. No topics makes a global profile. More than one topic is rejected at write time.
- **Unresolved topics.** Topics that resolve to nothing supply no lines and produce a finding. Without this rule, an unregistered key would silently turn a domain profile into a global profile.
- **Layout.** In the root-native layout of `make-vault-store-canonical`, the area becomes `Profiles/` through the registry folder, with no special case.

Alternatives:

- **Partition by domain.** A domain folder would hold one file each, and the root-native relayout flattens partition folders anyway.
- **Serve the existing long research notes directly.** Their tagged units would fill the budget in page order, and their prose principles would not be served at all.

A profile is a short, curated page that links its long note as detail.

### D2. Domain parents

The domain registry entry gains the generic registry `parent` field. The generic registry entry already supports it; only the domain spec omitted it.

- Validation rejects an unknown parent and a cycle.
- Inheritance serves a touched domain's profile, then its ancestors' profiles, nearest first.

This gives "deeper profiles where adequate" without nested folders. It also lets a vault organise overlapping flat keys.

### D3. The global profile reuses the stance block

A profile page with no topics supplies stance exactly as a `stance: true` page does, under the existing `stance` key. Existing hooks and clients see no wire change. The scaffold teaches the profile type and describes `stance: true` as the older name for the same thing.

### D4. A `domain_profiles` block

The block is named `domain_profiles` so that it cannot be confused with the existing `exomem://profile/topic/<key>` and `exomem://profile/anchor/...` read views. Those views are compiled from the map; they are not the owner's preferences.

The block reuses the stance pattern:

- compilation from admitted catalogue inputs;
- unit admission and terminal egress;
- currency;
- a cache keyed on admitted inputs, the limit and date boundaries;
- a pinned block re-checked by `egress.guard_working_set`, keyed on (generation, state).

It has two budgets of its own, `domain_profile_max_chars` (1200) and `profiles_max_chars` (2400), outside `max_chars`. A block served on many packets must not draw on `max_chars`. When the stance block once did, the task leads on real turns fell from 25 to 16.

### D5. Continuity

The token gains `profile_generations`, a map from domain key to generation, capped at eight entries.

- `decode_continuity` validates it and never raises.
- An unknown field is ignored, so the token version stays the same.
- The token's 8192-character cap is respected. Eight entries cost under 700 characters, and `method_versions` remains the only part that shrinks.
- The hook keeps held profiles through the token, as it does for skills, and needs no state file.

### D6. Selection

Selection runs inside the activation pass, after the leads and task candidates exist. It reads only data that the pass already holds or can bound to the profile pages: a vault holds at most a few dozen profile pages.

Candidate arms:

- (a) the domains of the turn's leads and slice rows;
- (b) profile pages that appear among the task candidates;
- (c) retrieval restricted to profile pages, using the existing lexical and semantic evidence.

Round R1 compares the arms on a realistic fixture against keys written from the turn text alone. It ships the simplest arm unless a richer arm's interval excludes zero. The result is recorded below when the round completes.

Selection adds work to every maps response, so delivery includes a before-and-after measurement on the recall latency instrument.

R1 result: pending.

### D7. Curation as an upkeep family

The new family is `upkeep_profile_curation`, with kind `profile.curation`. The names `upkeep_profile` and `profile.summary` already mean "a linked page has no summary".

- **Template.** It follows the subject-family template of the episode fold: one proposal per profile page, counting structural evidence newer than the page's own update.
- **Evidence.** The family reads two kinds:
  - live units whose categories belong to the registry roles `preferences` and `constraints`, resolved through `context_roles`, on pages of the profile's domain and its descendants;
  - profile lines whose currency state shows supersession, contradiction, expiry or a passed `check_by`.
- **Dismissal.** The signal digest over the sorted evidence references is the review-state fingerprint. A dismissal therefore holds until the evidence changes.
- **Surfacing.** Items reach the agent through `upkeep.for_packet`, within its existing caps. Their route is the governed `edit_note` preview.

Confirmation stays conversational, as the stance design chose. The agent shows the exact lines, and the owner confirms them or has explicitly asked for them. A server-side review gate would prevent nothing that an owner-driven conversation does not already prevent, and it would add a second approval surface.

A vault may tag preferences with its own categories, such as `interface_preference`. Those count only when the vault's context-role overlay adds them to the `preferences` role. That mapping is vault data; the product holds no category list.

### D8. Routing tables

The hypothesis to test first needs no product code: a routing table is a vault skill.

- The skill is a `SKILL.md` with a `name` and a `description`. Its body holds the pick-by-problem table, and each row points at the sections or pages that the problem needs.
- The existing skills catalogue lists it.
- The existing skill method projection serves it when retrieval over its body matches the turn, so a turn shaped like a row can select it.

Round R3 compares two arms on real models:

- **Base:** the table sits in an ordinary note.
- **Skill:** the table is a vault skill.

The cases are row-shaped turns, near misses and unrelated turns. Keys give the correct row per case.

- If the skill arm loads the table and picks the right row, the carrier is a documented convention plus a `vault-skills` delta.
- If agents load the table but pick wrong rows, a follow-up specifies `kind: "route"` method items from parsed table rows. A markdown parser reads the rows, never a regex.

The evaluation result and the chosen carrier are specified here before any code.

R3 result: pending.

## Risks

- **Selection misses a domain.** The agent then answers without the profile, as it does today. R1 measures the miss rate, and real-model cases check use.
- **Profiles drift from their long notes.** Curation raises new preference units and contradicted lines, and dismissals hold only until the evidence changes.
- **A profile line names a withheld page.** The same unit egress applies as for stance. A test covers a release-gated vault.
- **Latency.** Selection reads bounded data. A before-and-after on the recall latency instrument is required before release.

## Migration

- **No stored page moves automatically.** A vault adopts profiles by creating profile pages, or by retyping its global stance page through the relocation route.
- **Seeding is curation.** The agent drafts the lines from the vault's existing preference notes, and the owner confirms them.
- **Rollback.** Install the previous release. Profile pages stay valid compiled pages, and `stance: true` keeps working.
