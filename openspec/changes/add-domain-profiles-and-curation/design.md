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

The token gains `profile_generations`, a map from domain key to generation, capped at the sixteen most recently served.

- `decode_continuity` validates it and never raises.
- An unknown field is ignored, so the token version stays the same.
- The token's 8192-character cap is respected. Sixteen entries cost about 1,400 characters, and `method_versions` remains the only part that shrinks.
- The hook keeps held profiles through the token, as it does for skills, and needs no state file.

### D6. Selection

Selection runs inside the activation pass, after the leads and task candidates exist. It reads only data that the pass already holds or can bound to the profile pages: a vault holds at most a few dozen profile pages.

Candidate arms:

- (a) the domains of the turn's leads and slice rows;
- (b) profile pages that appear among the task candidates;
- (c) retrieval restricted to profile pages, using the existing lexical and semantic evidence.

Round R1 compares the arms on a realistic fixture against keys written from the turn text alone. It ships the simplest arm unless a richer arm's interval excludes zero.

Selection adds work to every maps response, so delivery includes a before-and-after measurement on the recall latency instrument.

R1 result. The round ran on a fixture of about 3,300 real notes and six profile pages. A separate lane wrote 48 turns from the turn text alone and split them into dev and test halves of 24. The selection rule was calibrated on dev and fixed for test.

| Arm | Test mean F1 |
|---|---|
| (a) domains of leads and slice rows | 0.407 |
| (b) profile pages among task candidates | 0.367 |
| (c) retrieval restricted to profile pages | 0.671 |

The paired difference c − b was +0.304 (95% CI +0.096 to +0.508), and c − a was +0.264 (95% CI +0.067 to +0.456). Arm (c) ships.

The shipped rule:

1. Score each profile page against the turn. The semantic score is the best chunk similarity to a turn vector that the request already encoded. The lexical score is the bm25 score of the turn's content words.
2. Select a page when its semantic score reaches `profile_semantic_floor`, or its lexical score reaches `profile_lexical_floor`.
3. Keep at most `profile_domains_max` selected domains, ranked by summed score. Then add their ancestors, nearest first.

The floors and the cap are vault-owned activation conventions, with defaults 0.50, 8.5 and 2. The selector reuses the turn slice's vector, or the task-context dense lane's vector when the slice has none. It adds one lexical query and one vector search, both restricted to profile pages, and it encodes nothing. When no turn vector exists, selection is lexical only, and the block's `selection` field says so.

A scale-free lexical rule, where any content-word match selects a page, was rejected. On the R1 test half, 5 of 7 general and unprofiled turns matched a word on some profile page.

### D7. Curation as an upkeep family

The new family is `upkeep_profile_curation`, with kind `profile.curation`. The names `upkeep_profile` and `profile.summary` already mean "a linked page has no summary".

- **Template.** It follows the subject-family template of the episode fold: one proposal per profile page, counting structural evidence newer than the page's own update.
- **Evidence.** The family reads two kinds:
  - live units whose categories belong to the registry roles `preferences` and `constraints`, resolved through `context_roles`. They come from pages of the profile's domain and its descendants, and from pages the profile links to. On a real vault copy, almost no note carried its domain in `topics`, so domain membership alone found nothing. The link a profile already makes to its detail note is the reliable scope. A global profile reads only its links;
  - profile lines whose currency state shows supersession, contradiction, expiry or a passed `check_by`.
- **Dismissal.** The signal digest over the sorted evidence references is the review-state fingerprint. A dismissal therefore holds until the evidence changes.
- **Surfacing.** Items reach the agent through `upkeep.for_packet`, within its existing caps. Their route is the governed `edit_note` preview.

Confirmation stays conversational, as the stance design chose. The agent shows the exact lines, and the owner confirms them or has explicitly asked for them.

A profile write needs a reviewed preview. A governed write that creates or changes a profile page commits only with the review hash from a preview of the same resulting content.

- **What it prevents.** A one-call profile rewrite that the owner never saw, made on an agent's own initiative or under injected content. A profile is served on every turn of its domain, so one silent wrong line costs more than a wrong line in an ordinary note.
- **What it costs when it fires wrongly.** One preview call, paid by the agent. The owner pays nothing.
- **Why automation cannot replace the owner.** Only the owner knows whether a line is true of them. The hash proves only that the agent previewed the exact lines before writing them; the owner's confirmation stays in the conversation.
- **What stays free.** Editing the profile file directly needs no preview. A profile is a note.

The proof shows only that the agent previewed the lines, so a preview of a profile change also says when its proof may be used. Beside its `transition_token`, an in-place preview returns a `next_action`: show the owner the resulting lines, and commit only after the owner confirms them or when the owner's latest message asked for that change. A preference the owner only states is used in the answer and offered as a line, not committed.

R4 result. Twelve cases, written by an independent lane, ran twice each on a GPT model against the built candidate, on a copy of a real vault. A script scored the keys it can check, and a blind judge from another model family scored the rest.

| Case kind | Result |
|---|---|
| A domain question | The games profile shaped the answer in 4 of 4 runs; judged 1.0 |
| An unrelated domain | No profile line was served or used in 4 of 4 runs |
| A provider outage | No stored preference was claimed in 4 of 4 runs |
| A curation proposal, asked for | The agent showed exact lines and waited; it committed with the preview's proof after confirmation in 4 of 4 runs |
| A stated contradiction, or a preference stated before an explicit request | The agent committed an unconfirmed profile line in 7 of 8 runs |

The `next_action` above was the repair. On the four cases that exposed the fault, it cut unconfirmed commits to 1 of 4 runs. The remaining run treated "for future reference" as a request to save. The harness cannot show an upkeep item reaching the agent, because delivery happens once, at a session's first activation, in a process that runs the upkeep worker. Installed acceptance checks that path on the live service.

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

R3 result. The round ran on the R1 fixture, with the vault's real skills added so the routing skill competed with them. An agent on a GPT model answered 11 cases twice in each arm. A blind judge on another model family scored whether each reply pointed to the key row's sources, scoring correct 1, partial 0.5 and wrong 0.

| Arm | Table reached the agent (row cases) | Row score (row cases) | Forced a row (other cases) |
|---|---|---|---|
| Base: ordinary note | 2 of 12 | 0.08 | 0 of 10 |
| Skill | 10 of 12 | 0.50 | 0 of 10 |
| Skill whose body tells the agent to name the row | 11 of 12 | 0.96 | 0 of 4 |

- The skill carrier reached the agent far more often than the note. Per case, skill − base was +0.667 (95% CI +0.333 to +1.000).
- With the table loaded, the plain skill's agent often gave sound advice and cited no source. One line in the skill body fixed that. Per case, the instructed skill scored +0.458 over the plain skill (95% CI +0.125 to +0.792) and +0.875 over the note (95% CI +0.625 to +1.000).
- No arm forced a row onto a near-miss or unrelated turn.

The carrier is therefore a convention, with no product code and no `route` items. A routing table is an ordinary vault skill:

- Its description names the problems the table covers.
- Its body holds the pick-by-problem table.
- Its body tells the agent to name the matched row and point to that row's sources first, and to answer without the table when no row fits.

The generic scaffold teaches the convention. The catalogue and the skill method items carry it as they carry any skill.

## Risks

- **Selection misses a domain.** The agent then answers without the profile, as it does today. R1 measures the miss rate, and real-model cases check use.
- **Profiles drift from their long notes.** Curation raises new preference units and contradicted lines, and dismissals hold only until the evidence changes.
- **A profile line names a withheld page.** The same unit egress applies as for stance. A test covers a release-gated vault.
- **Latency.** Selection reads bounded data. A before-and-after on the recall latency instrument is required before release.
- **An agent saves a stated preference to a profile unasked.** The preview's `next_action` tells it to wait, but the call stays a model's judgement, and a borderline phrasing such as "for future reference" can still commit. In the runs that did, the reply said the line was saved, so the owner can see it and edit the page directly. Curation flags a line that later notes contradict.
- **Lexical floor portability.** The lexical score uses corpus statistics. On a vault with no resident encoder, or for a caller restricted to part of the vault, scores sit lower, and selection serves fewer profiles. A miss leaves the agent where it is today. The vault owns the value.

## Migration

- **No stored page moves automatically.** A vault adopts profiles by creating profile pages, or by retyping its global stance page through the relocation route.
- **Seeding is curation.** The agent drafts the lines from the vault's existing preference notes, and the owner confirms them.
- **Rollback.** Install the previous release. Profile pages stay valid compiled pages, and `stance: true` keeps working.
