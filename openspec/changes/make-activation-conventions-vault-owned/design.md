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

`CUE_PATTERNS` and `_CUE_CATEGORIES` are deleted. A role gains one optional field:

```yaml
evidence_categories: [action, decision]   # default: none
```

For a turn, the categories that may earn `category_match` are the union of
`evidence_categories` over roles with an **evidence cue** in the turn. A role's cue is an
evidence cue only when it is at least three characters long and occurs in the turn on
term boundaries (whole terms, in order), not as a bare substring. Role *selection* keeps
today's substring matching; only evidence is stricter.

The shipped registry sets `evidence_categories` on the eight roles the deleted table
covered, with exactly the deleted table's category sets, and adds the one deleted
pattern the roles lacked (`what about`, to `open_questions`). Roles whose cues match most
turns (`identity`, `resources`, `people`, `location`, `baseline`, `evidence`) ship with
none.

An override may add to a role's `evidence_categories`, on shipped roles and its own.
Every entry must be a category the product's semantic-language registry knows (decision
3a); an unknown one is dropped with a finding. At most 8 per role.

Why explicit categories rather than reusing a role's `categories`: those lists exist for
lane selection and are wider. Reusing them made `assumption`, `config` and `risk` newly
eligible, and three turns that correctly abstain today resolved on anchors they were not
about, on shipped defaults. Why term boundaries and a length floor: `open_questions`
carries the cue `?`, and an owner's one-letter cue would otherwise make the qualifier
fire on every turn. What the bounds cost: an owner cannot make a one- or two-character
cue count as evidence, and is told so in `roles_findings`. The cue still selects the
role.

`category_match` stays a qualifier: it never establishes contact. Because it can promote
an anchor from `partial` to `resolved`, the shipped behaviour is pinned by two tests: an
equivalence test (shipped evidence cues and categories produce the deleted table's
result on every turn of the audit corpus and an adversarial set) and a property test (an
anchor in `partial` contact never becomes `resolved` through a category the deleted
table did not make eligible, unless the owner's override named it).

Eligible categories are computed in the runtime from the effective registry and passed
into `candidates_for`. `analyze_turn` stays free of registries and vault I/O, and
`TurnAnalysis` no longer carries cues.

*Alternative considered:* a boolean `cue_evidence` reusing `categories` (the first
draft). Rejected on the measurement above.

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

`rare_term_max_anchors` is an integer from 1 to 10 and may not exceed the larger of 3
and one hundredth of the anchors the index holds. A value outside either bound is
ignored with a finding and the shipped value applies. The relative bound is the one that
defends the argument: on a vault of a dozen anchors, "names at most ten" is not rare.

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
(`heading_aliases`, `category_aliases`). `_categories` consults that registry first and
the built-in map second, so a vault that heads its sections in another language earns
categories once its owner adds the aliases, in the registry they already use for
semantic units. No category a page earns today may be lost on shipped defaults; a test
compares the two over the scaffold vault and the audit corpus.

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
also joins the packet cache key, the `generation` block, and the continuity token's
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

`schema_memory` gains two subjects, `context-roles` and `activation-conventions`, on the
`traversal-profiles` pattern: `validate` a proposed override and return its findings,
`diff` it against the effective registry, and save a reviewed proposal under an
`expected_hash` guard with a `why`. The save writes only the override file, only after
the proposal loads with no rejected entry the caller has not acknowledged, and is
allowed through the hosted gateway as a `schema_memory` call while `_Schema/` stays
refused to the generic file tools. No tool is added; one tool's subject set grows by
two.

The agent's loop is: read `generation` on a packet (source, hash, findings), propose an
override to the owner, `validate`, save on approval, activate again and confirm the new
hash. The scaffold skill gains a short reference section for it.

## Risks / Trade-offs

- **Shipped evidence drift.** Role cues are a superset of the deleted patterns, so more
  turns can earn `category_match` for the same categories. Mitigation: the equivalence
  and property tests in decision 1; the deterministic audit gates recall as well as
  precision, before and after; if a role drifts, its shipped `evidence_categories`
  shrink. No table is reinstated.
- **An owner admits everything or drops everything.** Both are legitimate vaults. The
  first is slow; the latency gate reports it.
- **Two registries to learn.** Roles say which questions a packet answers, conventions
  say how this vault spells things.
- **A registry save is a policy write.** It is hash-guarded, explained by `why`,
  validated before it lands, and reported on every later packet.

## Migration

None for owners. Sidecars rebuild once. Shipped defaults reproduce today's membership
(case-insensitively), state fields, stopwords, threshold, skip list and evidence
categories; tests pin each against the deleted constants' values.

## Open Questions

None blocking.
