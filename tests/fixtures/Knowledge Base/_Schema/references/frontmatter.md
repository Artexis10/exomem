# Frontmatter Spec

Every file in `Knowledge Base/` (except `index.md` files and Sources/raws — though sources also carry frontmatter) carries YAML frontmatter at the top. The frontmatter is the metadata layer; without it, audit cannot do its job and Obsidian queries (Dataview, base files) cannot scope.

## Common fields

These appear on every page type:

| Field | Required | Type | Notes |
|---|---|---|---|
| `type` | yes | enum | `source`, `research-note`, `insight`, `failure`, `pattern`, `experiment`, `production-log`, `entity` |
| `status` | yes | enum | `draft`, `active`, `superseded`, `archived` (production-logs use a richer status set — see below) |
| `created` | yes | ISO date | `YYYY-MM-DD`, set on creation, never edited |
| `updated` | yes | ISO date | `YYYY-MM-DD`, refreshed on every edit |
| `tags` | yes | list | freeform, lowercase, dash-separated |

## Per-type fields

### source

| Field | Required | Notes |
|---|---|---|
| `source_type` | yes | `article`, `session`, `book`, `paper`, `video`, `other` |
| `captured` | yes | ISO date — same as `created` for sources |
| `url` | conditional | required for articles, videos, papers |
| `author` | optional | |
| `ingested_into` | yes | list of wikilinks to compiled notes that cite this; starts as `[]` |

### research-note

| Field | Required | Notes |
|---|---|---|
| `project` | yes | one of: `substrate`, `q`, `endstate`, `sift`, `tu`, `book-club`, `health`, `finance`, `creative`, `science`, `travel`, `personal` |
| `tenant` | optional | for Q tenants only — current values: `ask-dr-chaffee`, `tu`. See SKILL.md § Q tenants for the dual role of `tu`. |
| `sources` | yes | list of wikilinks to `Sources/` files this note draws from |
| `supersedes` | optional | wikilink to the page this one replaces |
| `superseded_by` | optional | wikilink to the page that replaced this one (set when status flips to `superseded`) |

### insight, failure, pattern

| Field | Required | Notes |
|---|---|---|
| `sources` | yes | list of wikilinks |
| `projects` | optional | list of project keys this applies to |
| `supersedes`, `superseded_by` | optional | as above |
| `severity` | failure-only, optional | qualitative: `minor`, `moderate`, `serious`, `critical` |
| `pattern_type` | pattern-only, optional | `architectural`, `workflow`, `prompting`, `governance`, `pedagogical`, etc. |

### experiment

| Field | Required | Notes |
|---|---|---|
| `domain` | yes | `food`, `health`, `workflow`, etc. — matches the subfolder under `Notes/Experiments/` |
| `started` | yes | ISO date the experiment actually began (may differ from `created` if planning preceded execution) |
| `duration` | yes | freeform string: `"30 days"`, `"2 weeks"`, `"ongoing"` |
| `concluded` | optional | ISO date the experiment ended; absent while ongoing |
| `n` | optional | sample size — default 1 if absent |
| `hypothesis` | optional | one-line hypothesis (also restated in body); useful for find/audit |
| `sources` | optional | wikilinks to any source material that informed the protocol |
| `supersedes`, `superseded_by` | optional | as above |

### production-log

| Field | Required | Notes |
|---|---|---|
| `medium` | yes | `reels`, `episodes`, `pdfs`, `posts`, etc. — matches the subfolder under `Notes/Productions/` |
| `status` | yes | one of: `planned`, `recorded`, `edited`, `published`, `reflected`, `dropped`, `archived`. Different from other page types — production-logs have lifecycle states. |
| `recorded` | optional | ISO date primary capture happened |
| `published` | optional | ISO date or `null` while still pre-publish |
| `projects` | optional | list of project keys (typically one — e.g., `[tu]`, `[substrate]`) |
| `host` | optional | who's on camera / lead author |
| `editor` | optional | who's editing / producing |
| `sources` | optional | wikilinks to source material that informed the production |
| `related` | optional | wikilinks to research-notes, patterns, failures, entities the production draws on or applies |
| `supersedes`, `superseded_by` | optional | as above |

### entity

| Field | Required | Notes |
|---|---|---|
| `entity_type` | yes | `person`, `concept`, `library`, `decision` |
| Other | per type | see `page-types.md` for entity-type-specific fields |

## Status semantics

For most page types:

- **draft** — page is being authored; lint may skip some checks
- **active** — page is live and current
- **superseded** — replaced by a newer page; `superseded_by` must point to it
- **archived** — moved to `<location>/_archive/`; not deleted, just stepped down from active rotation

For experiments specifically: `active` covers both planning and running; once concluded but still relevant, leave `active`; archive only when the experiment is no longer being referenced.

For production-logs specifically: status reflects production lifecycle (`planned` → `recorded` → `edited` → `published` → `reflected`), plus exit states (`dropped`, `archived`). `superseded` is rare for production-logs but possible if an entire production gets re-released or rebuilt.

A page never carries `status: deleted`. Deletion happens by archive, not by removal.

## Explicit non-fields

The following fields are deliberately **not** in the spec:

- `confidence` — numeric scores misrepresent the underlying signal. Trust comes from sources and link counts, both of which are visible in frontmatter and via backlinks.
- `decay_at` / `expires_at` — Knowledge does not expire on a schedule. Supersession or archival is explicit.
- `auto_*` anything — no field reflects an automated background process. Operations on the KB are explicit.

## Wikilink format in frontmatter

YAML strings, double-quoted, using full vault-relative paths without the `.md` extension:

```yaml
sources:
  - "[[Sources/Articles/2026-05-09-llm-wiki-v2-gist]]"
  - "[[Sources/Sessions/2026-05-04-q-strategy-debate]]"
```

This format is Obsidian-compatible and survives Dataview queries.

## Tags

- Lowercase
- Dash-separated multi-word: `agentic-rag`, not `agentic_rag` or `AgenticRAG`
- No `#` prefix in frontmatter
- Project keys (`q`, `endstate`, `tu`, `substrate`) are also fine as tags but are redundant with the `project` / `projects` field
- Avoid generic catch-alls like `important` or `todo` — they don't help retrieval

## Example: full frontmatter for a research note (Q tenant)

```yaml
---
type: research-note
project: q
tenant: ask-dr-chaffee
status: active
created: 2026-05-09
updated: 2026-05-12
sources:
  - "[[Sources/Articles/2026-05-09-llm-wiki-v2-gist]]"
  - "[[Sources/Sessions/2026-05-09-kb-architecture-debate]]"
supersedes: "[[Notes/Research/Q/old-rag-stub]]"
tags: [retrieval, agentic-rag, knowledge-graph, governance]
---
```

## Example: full frontmatter for a Substrate (company-level) research note

```yaml
---
type: research-note
project: substrate
status: active
created: 2026-05-09
updated: 2026-05-09
sources:
  - "[[Sources/Articles/2026-04-landing-page-conversion-patterns]]"
tags: [landing-page, positioning, brand]
---
```

## Example: full frontmatter for a TU podcast research note

```yaml
---
type: research-note
project: tu
status: active
created: 2026-05-09
updated: 2026-05-09
sources:
  - "[[Sources/Sessions/2026-05-08-guest-prep]]"
tags: [guest-research, episode-prep]
---
```

(Compare to a TU-as-Q-tenant note, which would be `project: q, tenant: tu` and live in `Notes/Research/Q/`.)

## Example: full frontmatter for an experiment

```yaml
---
type: experiment
domain: food
status: active
created: 2026-05-01
updated: 2026-05-09
started: 2026-05-09
duration: "30 days"
n: 1
hypothesis: "Eliminating dairy reduces sinus inflammation"
sources:
  - "[[Sources/Books/2026-04-the-elimination-diet]]"
tags: [diet, elimination, sinus]
---
```

## Example: full frontmatter for a production-log

```yaml
---
type: production-log
medium: reels
status: recorded
created: 2026-05-09
updated: 2026-05-09
recorded: 2026-05-09
published: null
projects: [tu]
host: Hugo
editor: Kim
sources:
  - "[[Sources/Sessions/2026-05-09-metabolism-curriculum-design]]"
related:
  - "[[Notes/Research/Together Unprocessed/metabolic-literacy-curriculum]]"
  - "[[Notes/Patterns/conversational-reel-script]]"
  - "[[Notes/Failures/formulaic-rehook-tic]]"
tags: [reels, metabolism, batch-02, tu]
---
```
