## Context

`activate_context` (canonical specs `context-activation`, `context-roles`) resolves a raw
turn to durable anchors and fills a working-memory packet. The role vocabulary is already
a reviewed registry with a vault override. Four other inputs are constants in three
modules: a second cue table, the folders that make a `resource` anchor, the Records field
names that state a condition, and an English stopword list. Two more constants duplicate
layout the product already defines elsewhere.

The constitution applies unchanged: the server measures and the brain reasons, no
server-side reasoning model, nothing the server infers becomes policy. Conventions are
policy, so the owner authors them and the server only reads and validates.

## Goals / Non-Goals

**Goals**

- Every activation input that encodes how one vault is shaped or phrased is data the
  owner can change, with shipped defaults that behave as today.
- One cue vocabulary.
- An agent can make the change on the owner's behalf with the tools it already has, and
  can see what is in effect and what was rejected.
- The compiler follows the product's layout authority instead of copying it.

**Non-Goals**

- New anchor kinds. The role registry keys on the six kinds; opening that set is its own
  change.
- The server proposing conventions from usage. That needs the decision ledger, which
  `add-consolidation-dreamer` owns. This change makes the target of such a proposal
  exist.
- Relocating `Entities/`, `Sources/` or `Evidence/`. Ten modules share those names.

## Decisions

### 1. Role cues are the only cue vocabulary

`CUE_PATTERNS` and `_CUE_CATEGORIES` are deleted. For a turn, the categories that may
earn `category_match` are the union of `categories` over roles whose cue matched and
whose `cue_evidence` is true.

`cue_evidence` is a new boolean role field, default `false`. The shipped registry sets it
`true` on the eight roles the deleted table covered (`preferences`, `constraints`,
`current_state`, `recent_change`, `active_plans`, `methods`, `precedents`,
`open_questions`) and leaves it `false` on `identity`, `resources`, `people`, `location`,
`baseline` and `evidence`, whose cues (`who`, `with`, `where`, `their`) match most turns
and would make a qualifier meaningless. An override may set it on any role, including the
owner's own.

`category_match` stays a qualifier: it never establishes contact, so a broad cue can
promote `partial` to `resolved` only for an anchor the turn already reached.

*Alternative considered:* a `cues` section in the new registry. Rejected: it would keep
two vocabularies and two places to add a German planning verb.

### 2. A sixth registry, same contract as the fifth

`activation-conventions.yaml` loads exactly as `context-roles.yaml` does: shipped text
from the scaffold, optional override under `<Knowledge Base>/_Schema/`, memoised by
content digest, broken override falls back to shipped with findings rather than raising.
Falling back is right here: a typo in an owner's file is the expected failure, and
refusing to activate would cost the owner every packet to protect nothing.

Shipped file (values equal to today's constants):

```yaml
schema_version: 1
anchors:
  resource: {folders: [Products, Systems], tags: [], types: []}
  hub:      {folders: [], tags: [hub], types: []}
state:
  state_fields: [state, status, condition, location, value, balance, remaining]
  date_fields:  [observed_on, occurred_on, as_of, date, updated]
stopwords: [a, an, and, ...]
```

Override grammar:

```yaml
schema_version: 1
anchors:
  resource:
    add_folders: [Equipment, Vehicles/Fleet]
    drop_folders: [Systems]
    add_types: [asset]
state:
  prefer_state_fields: [stock, level]   # tried before the shipped names
  drop_state_fields: [value]
stopwords:
  add: [der, die, das]
  drop: [left]
```

Unlike roles, entries may be dropped. The role registry forbids removal because a packet
that silently lost `constraints` reads as "there are none". Dropping a folder from anchor
membership has no such reading: those pages stop being anchors because the owner said so,
and they remain reachable through recall.

### 3. Membership rules are narrow and cannot claim raw material

A folder rule is a knowledge-base-relative path prefix of at most three segments,
compared per segment under the resolver's normalisation. A rule is rejected with a
finding when it is absolute, contains `..`, starts a segment with `.` or `_`, names the
entity folder, or falls inside an append-only tree as `vault.in_append_only_tree` defines
it. The last rule keeps the existing protection: a captured article that carries
`tags: [hub]` is evidence about the world, not a subject of the owner's turns.

A page matching rules for two kinds takes the earlier kind in `ANCHOR_KINDS` order, as
today. `entity` membership stays with the entity registry.

### 4. Layout comes from the product

`_SKIP_DIR_NAMES` and `_RAW_MATERIAL_FOLDERS` are deleted. The index walk skips what
`vault.VAULT_SCAN_SKIP_DIRS` and `find_corpus.EXCLUDED_DIR_NAMES` skip, and excludes what
`in_append_only_tree` matches. If the union skips a directory today's private list does
not, the implementer reports the difference before changing behaviour.

### 5. Identity and caching

The conventions digest joins the roles hash in the index identity, the packet cache key
and `generation`. An edit therefore rebuilds the disposable index on next use and cannot
serve a packet built under the old rules. The continuity token from
`activate-context-on-host-turns` validates against index identity, so it inherits this
without a new field.

### 6. Bounds

Substring patterns only, no regular expressions: owner-authored regex is a
denial-of-service surface and makes resolution time depend on input. Caps: 32 folders,
32 tags and 32 types per kind; 24 state fields; 12 date fields; 2,000 stopwords; 64
characters per entry. What the caps prevent: unbounded per-page rule evaluation during a
cold build that must fit a request budget. What they cost when they bind: the entries
past the cap are ignored. Who pays: the owner, and they are told, in
`generation.conventions_findings`.

### 7. How an agent configures it

No new tool. The file is a vault page under `_Schema/`; the agent writes it with the
governed write path used for the other registries, calls `activate_context`, and reads
`generation.conventions_source`, `conventions_hash` and `conventions_findings`. The
scaffold skill gains a short reference section: when a packet abstains on a turn that
plainly names something the owner keeps, check whether the vault's folders, state fields
or language are covered, propose the override to the owner, write it on approval.

## Risks / Trade-offs

- **Cue drift on shipped defaults.** The role cues are a superset of the deleted table
  and three category sets are wider. Mitigation: the deterministic activation audit runs
  before and after on the seeded corpus; false activation on the negative twins and
  anchor precision must stay inside the pre-registered bounds. If they do not, narrow the
  shipped `cue_evidence` set rather than reinstating a table.
- **An owner drops every rule.** The catalogue shrinks to entities, plans, collections
  and project keys. That is a legitimate vault, not an error; no finding.
- **Two registries to learn.** Accepted: roles say which questions a packet answers,
  conventions say how this vault spells things. Merging them would put layout in a file
  whose contract forbids removal.

## Migration

None for owners. Sidecars rebuild once. Shipped defaults reproduce today's membership,
state fields and stopwords exactly; a test pins that equivalence against the deleted
constants' values.

## Open Questions

None blocking. Whether the server should surface "turns that abstained while naming a
folder no rule covers" as a review-queue suggestion is deferred to the dreamer change.
