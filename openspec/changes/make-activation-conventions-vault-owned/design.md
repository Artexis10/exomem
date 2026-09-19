## Context

`activate_context` (canonical specs `context-activation`, `context-roles`) resolves a raw
turn to durable anchors and fills a working-memory packet. The role vocabulary is already
a reviewed registry with a vault override. Other inputs are constants in three modules: a
second cue table, the folders that make a `resource` anchor, the folders the index walk
skips, the Records field names that state a condition, an English stopword list, the
rare-term threshold, and an English map from section headings to categories.

The constitution applies unchanged: the server measures and the agent reasons, no
server-side reasoning model, nothing the server infers becomes policy. Conventions are
policy, so the owner authors them and the server only reads, validates and, on the
owner's agent's explicit request, stores them.

An independent critique of the first draft (2026-09-19) found four blocking flaws. This
design answers them; each decision says which.

## Goals / Non-Goals

**Goals**

- Every activation input that encodes how one vault is shaped or phrased is data the
  owner can change, with shipped defaults that resolve exactly as today.
- One file per concern, one place to add a cue.
- An agent can make the change through MCP on every tier, including hosted, and can read
  back what is in effect and what was rejected.
- No override can weaken the evidence rules `make-anchor-resolution-sound` established.

**Non-Goals**

- The tokeniser. Words in every script are recognised by a separate fix on
  `make-anchor-resolution-sound` (tasks 4a); this change depends on it, because a
  stopword or cue in a language whose words never become terms configures nothing.
- Segmenting scripts written without word separators.
- New anchor kinds. The role registry keys on the six kinds.
- The server proposing conventions from usage; that needs the decision ledger owned by
  `add-consolidation-dreamer`.
- Deriving non-distinctive terms from the vault's own term counts. Measured on a
  4,400-page vault, the terms carried by most anchors are template headings
  (`connections`, `summary`, `why`), which no stopword list names. That is a resolution
  change with its own audit and follows this one, using the `resolution` section this
  change creates.
- Relocating `Entities/`, `Sources/` or `Evidence/`.

## Decisions

### 1. Role cues are the cue vocabulary; evidence is explicit and bounded

`CUE_PATTERNS` and `_CUE_CATEGORIES` are deleted. A role gains two optional fields:

```yaml
evidence_cues: [i'm planning, next step]   # default: none
evidence_categories: [action, decision]    # default: none
```

For a turn, the categories that may earn `category_match` are the union of
`evidence_categories` over roles with an **evidence cue** in the turn. An evidence cue is
a cue in the role's effective `evidence_cues` that passes three bounds: it is at least three characters long, it tokenises to at
least one term, and its terms occur in the turn as whole terms in order (the cue is
tokenised with the resolver's own tokeniser and sought as a contiguous run in the turn's
terms). A cue that fails a bound is never evidence and is reported as a finding. Role
*selection* keeps today's substring matching over every cue; only evidence is stricter.

The shipped registry sets `evidence_cues` and `evidence_categories` on the eight roles
the deleted table covered, with the deleted table's patterns and category sets (adding
`what about` to `open_questions`, which lacked it). The other shipped role cues
(`budget`, `left`, `lately`, `next`, `we already` and the rest) select roles as today and
are not evidence. Roles whose cues match most turns (`identity`, `resources`, `people`,
`location`, `baseline`, `evidence`) ship with neither field.

One shipped difference is intended and pinned: the deleted table's `?` pattern cannot be
an evidence cue (one character, no terms), so a turn whose only question signal is a
question mark no longer makes `question` and `problem` eligible. It still selects
`open_questions`. The direction is the safe one: fewer qualifiers, never more.

The rule is the same for shipped and owner cues, so the role model needs no record of
where a cue came from. An override extends `evidence_cues` and `evidence_categories` by
union, as it already extends `categories`, on shipped roles and its own (both keys join
the loader's `_OVERRIDE_FIELDS`, which today would report them as unknown). An owner who
wants a cue to select a role and to count as evidence writes it in both lists; an
`evidence_cues` entry that is not among the role's cues also selects the role, so one
line in `evidence_cues` is enough. This is how an owner promotes a shipped selection cue
such as `budget`: naming it again under `add_cues` would be a silent no-op, because the
loader skips a cue the role already has. An entry is
valid when the semantic-language registry's `resolve_category` returns a status other
than `unregistered`, and its resolved key is what is stored; anything else is dropped
with a finding. At most 8 per role.

Why explicit cues and categories rather than reusing a role's own: those lists exist
for lane selection and are wider. Reusing the categories made `assumption`, `config` and
`risk` newly eligible, and three turns that correctly abstain today resolved on anchors
they were not about; reusing the cues made five more turns earn a qualifier they do not
earn today. Why the bounds: an owner's one-letter cue, or a cue made only of punctuation
(which tokenises to nothing and would match at every position), would make the qualifier
fire on every turn. What the bounds cost: an owner cannot make a very short or
punctuation-only cue count as evidence, and is told so in `roles_findings`. The cue still
selects the role.

`category_match` stays a qualifier: it never establishes contact. Because it can promote
an anchor from `partial` to `resolved`, the shipped behaviour is pinned by three tests
over the audit corpus and an adversarial turn set: no category becomes eligible that the
deleted table did not make eligible; no anchor becomes `resolved` on a turn for which
the deleted table made no category eligible; and a named-difference fixture asserts
exactly the `?` loss and nothing else.

Eligible categories are computed in the runtime from the effective registry and passed
into `candidates_for`. `analyze_turn` stays free of registries and vault I/O, and
`TurnAnalysis` no longer carries cues.

*Alternatives considered:* a boolean `cue_evidence` reusing `categories` (the first
draft), and explicit categories with every role cue as a trigger (the second). Both
rejected on measurement.

### 2. A conventions registry with the roles registry's load contract, plus a size cap

`activation-conventions.yaml` loads as `context-roles.yaml` does: shipped text from the
scaffold, optional override under `<Knowledge Base>/_Schema/`, memoised by content
digest, a broken override falls back to shipped with findings rather than raising.
Falling back is right here: a typo in an owner's file is the expected failure, and
refusing to activate would cost the owner every packet to protect nothing.

Both loaders read at most 256 KiB and refuse a larger file before parsing, with a
finding. What it prevents: an alias-expansion document expanding in memory before any
per-section cap applies, which matters on a hosted tier where the file is
tenant-authored. What it costs: nothing a real registry approaches.

Shipped file (values equal to today's constants):

```yaml
schema_version: 1
anchors:
  resource: {folders: [Products, Systems], tags: [], types: []}
  hub:      {folders: [], tags: [hub], types: []}
  skip_folders: [_trash, _attachments, _Staging, Templates]
state:
  state_fields: [state, status, condition, location, value, balance, remaining]
  date_fields:  [observed_on, occurred_on, as_of, date, updated]
stopwords: [a, an, and, ...]
resolution:
  rare_term_max_anchors: 3
```

Override grammar:

```yaml
schema_version: 1
anchors:
  resource:
    add_folders: [Equipment, Vehicles/Fleet]
    drop_folders: [Systems]
    add_types: [asset]
  add_skip_folders: [Vorlagen]
state:
  prefer_state_fields: [stock, level]   # tried before the shipped names
  drop_state_fields: [value]
stopwords:
  add: [der, die, das]
resolution:
  rare_term_max_anchors: 2
```

Anchor folders, tags, types and state fields may be dropped: those pages stop being
anchors, or that field stops being read, because the owner said so, and the pages remain
reachable through recall. **Stopwords and skip folders are add-only.** The stopword list
is the only guard behind two shipped scenarios ("a function word is nobody's name", "a
turn made only of structural words reaches nothing"): removing a word can admit it as an
alias, and an alias resolves an anchor alone. What add-only costs: an owner whose domain
uses a listed word as a content word cannot un-list it, and names the thing through an
alias of two or more words instead.

### 2a. Where a threshold lives

One rule, applied to the three that exist:

| A threshold that… | lives in | today's member |
|---|---|---|
| shapes what the index holds | the conventions registry, covered by the sidecar digest | `rare_term_max_anchors` |
| only reorders or bands ranking | the operator's `RankingConfig` | `working_set_vector_strong` |
| is part of the soundness argument | code | the two-shared-terms minimum, the evidence rules |

`working_set_lexical_min_terms` is tunable in `RankingConfig` today and belongs to the
third row. It stays readable there for compatibility, and the resolver enforces a floor
of 2 whatever the file says.

`rare_term_max_anchors` is an integer from 1 to 3; a value outside that range is
ignored with a finding and the shipped value, 3, applies. An owner may tighten rarity
and may not loosen it. What the ceiling prevents: on a small vault a loose threshold
calls a word that names most of the catalogue rare, which defeats the argument the
threshold encodes. What it costs: the owner of a very large vault cannot make
single-word contact easier, and is told. A bound relative to vault size was considered
and rejected: it makes the effective value move as the vault grows, without any file
edit, which the sidecar digest in decision 5 could not see.

The effective stopword list and threshold are read once per build and passed to both of
their users (turn matching and `rare_term` in the resolver, derived short-name admission
in the index).

### 3. Membership rules are narrow, and cannot claim raw material or structured trees

A folder rule is a knowledge-base-relative path prefix of at most three segments,
compared per segment under the resolver's normalisation (case-insensitive). The shipped
`Products` and `Systems` rules are therefore matched case-insensitively too, which is
the one intended difference from today's exact comparison; a test pins it.

A rule is rejected with a finding when it is absolute, contains `..`, starts a segment
with `.` or `_`, begins with the knowledge-base folder's own name (it would match
nothing), names the entity folder, names a tree that holds a Planning or Records
collection, or falls inside an append-only tree as `vault.in_append_only_tree` defines
it. Pages admitted by the Planning and Records manifest passes are never evaluated
against membership rules, so one page is never two anchors.

A page matching rules for two kinds takes the earlier kind in `ANCHOR_KINDS` order, as
today. `entity` membership stays with the entity registry.

No cap limits how many pages one rule admits. A rule naming a large notes folder makes
the catalogue a second copy of the corpus, which is slow and useless but not unsound
(more anchors only tightens `rare_term`). The latency gate in the tasks is the control,
and the Risks section names it.

### 3a. Page categories follow the semantic-language registry

`_CATEGORY_BY_LABEL` maps English section headings to categories. The product already
owns that vocabulary, vault-overridably, in the semantic-language registry
(`category_aliases`). `_categories` consults that registry first and
the built-in map second, so a vault that heads its sections in another language earns
categories once its owner adds the aliases, in the registry they already use for
semantic units. The accessor is the registry's `resolve_category`, which covers the core
categories and a vault's `category_aliases` (the registry's `heading_aliases` belong to
unit kinds and are not used here). Its result is taken only when its status is not
`unregistered`; otherwise the built-in map decides, so a heading such as "Next Steps"
keeps mapping to `action`. No category a page earns today may be lost on shipped
defaults; a test compares the two over the scaffold vault and the audit corpus.

### 4. Layout: one product authority adopted, one list kept

Anchor candidacy and vault scanning are different questions, and the first draft was
wrong to call the index's lists private copies. Decided:

- The index excludes what `vault.in_append_only_tree` matches (replacing
  `_RAW_MATERIAL_FOLDERS`) and the governance trees `_Schema`, `_Governance` and
  `_Adoption`.
- The index keeps its own skip list, now the `anchors.skip_folders` convention.
  `_Staging` holds unvetted uploads and `Templates` holds example frontmatter; both stay
  skipped.
- `_archive` stays walked. Archived anchors remain resolvable and are ranked down by
  lifecycle, which is what the canonical "Superseded knowledge is marked" scenario needs.

### 5. A conventions edit wipes the sidecar

Derived aliases and term counts are stored in the sidecar, and the incremental update
notices only file changes, so a rule change alone recomputes nothing. The conventions
digest is therefore stored in the sidecar's `meta` table and a mismatch is handled
exactly like a `SCHEMA_VERSION` mismatch: the sidecar is wiped and rebuilt. The digest
is taken over the effective conventions, the values in force after bounds and findings
are applied, never over the file's bytes. It also joins the packet cache key, the `generation` block, and the continuity token's
payload beside `roles_hash`, so a token minted under other conventions reports `stale`.
`SCHEMA_VERSION` is bumped once for the new `meta` row.

### 6. Bounds, each with its reason

Literal matching only, no regular expressions: owner-authored regex makes resolution
time depend on input. Caps, per file:

- conventions: 32 folders, 32 tags and 32 types per kind; 32 skip folders; 24 state
  fields; 12 date fields; 2,000 stopwords; 64 characters per entry;
- roles (new, because decision 1 makes that registry evidence-bearing): 32 roles per
  override, 48 cues per role, 64 characters per cue, 8 evidence categories per role.

What they prevent: unbounded per-page and per-turn evaluation inside a request budget.
What they cost when they bind: entries past a cap are ignored. Who pays: the owner, who
is told in `conventions_findings` or `roles_findings`.

### 7. The agent's write path is `schema_memory`

`_Schema/` is refused to `manage_memory_file` and `edit_memory` on hosted, by design: a
prompt-injected capture must not rewrite the doctrine that governs later writes. The
roles registry has no write path at all. So the first draft's "no new tool, the agent
writes the file" was false on every tier.

`schema_memory` gains two subjects, `context-roles` and `activation-conventions`, with
this operation grammar:

- `validate`: a proposed override in, its findings and the current content hash out;
  nothing written.
- `diff`: the proposal against the effective registry.
- `save-roles` and `save-conventions`: dedicated operations on the `save-relations`
  pattern, requiring `proposal`, `why` and `expected_hash`, and refusing the generic
  `save` flag.
- `infer` is refused for both subjects, naming the reason: the server does not propose
  conventions.

Saving is stricter than loading. A load falls back on a broken override so activation
keeps working; a save refuses a proposal that has any finding, so nothing an agent
writes through the tool is silently dropped later. The save writes only the override
file and is allowed through the hosted gateway as a `schema_memory` call while
`_Schema/` stays refused to the generic file tools. No tool is added; one tool's subject
set grows by two.

The agent's loop is: read `generation` on a packet (source, hash, findings), propose an
override to the owner, `validate`, save on approval, activate again and confirm the new
hash. The scaffold skill gains a short reference section for it.

## Risks / Trade-offs

- **An owner widens the triggers.** Shipped evidence never widens (decision 1's three
  tests hold it), but every cue an override puts in `evidence_cues` is a new trigger for
  that role's evidence categories. The three bounds and the category validation limit
  what such a cue can do, `roles_findings` reports what was refused, and the qualifier
  still cannot establish contact. The deterministic audit gates recall as well as
  precision on the shipped registry, before and after.
- **An owner admits everything or drops everything.** Both are legitimate vaults. The
  first is slow; the latency gate reports it.
- **Two registries to learn.** Roles say which questions a packet answers, conventions
  say how this vault spells things.
- **Template headings.** The shipped stopwords name no template heading. In a heavily
  templated vault a heading word carried by most anchors ("summary", "connections") can
  supply the second shared term beside one real name word until the owner adds it to
  `stopwords`. The shipped file's commented example shows that remedy, and deriving such
  words from the vault's own counts is the follow-up named in Non-Goals.
- **A registry save is a policy write.** It is hash-guarded, explained by `why`,
  validated before it lands, and reported on every later packet.

## Migration

None for owners. Sidecars rebuild once. Shipped defaults reproduce today's state fields,
stopwords, threshold, skip list, evidence cues and evidence categories; tests pin each
against the deleted constants' values. Three differences are intended and pinned by
tests: `Products` and `Systems` match case-insensitively; a page inside `_Schema`,
`_Governance` or `_Adoption` is never an anchor; a question mark alone no longer makes
`question` and `problem` eligible.

## Open Questions

None blocking.
