# Frontmatter Spec

Every file in `Knowledge Base/` (except `index.md` files; sources also carry
frontmatter) carries YAML frontmatter at the top. The frontmatter is the metadata
layer; without it, audit cannot do its job (and optional Obsidian tooling — Dataview, base
files — can't scope). Frontmatter is required regardless of viewer; Dataview/bases are a nicety on top.

## Common fields

These appear on every page type:

| Field | Required | Type | Notes |
|---|---|---|---|
| `exomem_id` | new pages | UUID | immutable identity assigned by Exomem; legacy pages gain one only through explicit ID backfill |
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
| `project` | yes | a slug-shaped project key registered in `_Schema/project-keys.yaml`; unknown keys **auto-register on first use** (typo-guarded). An open set, not a closed enum — e.g. `personal`, `project-alpha`, `project-beta`, `work` |
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
| `domain` | yes | `workflow`, `research`, `ops`, etc. — matches the subfolder under `Notes/Experiments/` |
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
| `medium` | yes | `posts`, `articles`, `pdfs`, `episodes`, etc. — matches the subfolder under `Notes/Productions/` |
| `status` | yes | one of: `planned`, `recorded`, `edited`, `published`, `reflected`, `dropped`, `archived`. Different from other page types — production-logs have lifecycle states. |
| `recorded` | optional | ISO date primary capture happened |
| `published` | optional | ISO date or `null` while still pre-publish |
| `projects` | optional | list of project keys (typically one) |
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

For experiments specifically: `active` covers both planning and running; once
concluded but still relevant, leave `active`; archive only when the experiment is
no longer being referenced.

For production-logs specifically: status reflects production lifecycle (`planned`
→ `recorded` → `edited` → `published` → `reflected`), plus exit states
(`dropped`, `archived`).

A page never carries `status: deleted`. Deletion happens by archive, not by
removal.

## Explicit non-fields

The following fields are deliberately **not** in the spec:

- `confidence` — numeric scores misrepresent the underlying signal. Trust comes
  from sources and link counts, both visible in frontmatter and via backlinks.
- `decay_at` / `expires_at` — knowledge does not expire on a schedule.
  Supersession or archival is explicit.
- `auto_*` anything — no field reflects an automated background process.
  Operations on the KB are explicit.

## Wikilink format in frontmatter

YAML strings, double-quoted, using **full vault-rooted paths** without the `.md`
extension. For KB material this is `Knowledge Base/<rest>`:

```yaml
sources:
  - "[[Knowledge Base/Sources/Articles/2026-05-09-retrieval-patterns]]"
  - "[[Knowledge Base/Sources/Sessions/2026-05-04-architecture-debate]]"
```

This is plain-markdown wikilink syntax; it is also Obsidian-compatible and survives optional Dataview queries. The exomem
writer normalizes any input form (bare names, KB-relative, with `.md`, with
`[[ ]]` wrappers, with `|alias`, with `#anchor`) to this canonical form on every
write. You can paste in any shape; the file on disk lands canonical.

## Tags

- Lowercase
- Dash-separated multi-word: `agentic-rag`, not `agentic_rag` or `AgenticRAG`
- No `#` prefix in frontmatter
- Avoid generic catch-alls like `important` or `todo` — they don't help retrieval

## Example: full frontmatter for a research note

```yaml
---
exomem_id: 123e4567-e89b-42d3-a456-426614174000
type: research-note
project: project-alpha
status: active
created: 2026-05-09
updated: 2026-05-12
sources:
  - "[[Knowledge Base/Sources/Articles/2026-05-09-retrieval-patterns]]"
  - "[[Knowledge Base/Sources/Sessions/2026-05-09-architecture-debate]]"
supersedes: "[[Knowledge Base/Notes/Research/Project Alpha/old-rag-stub]]"
tags: [retrieval, agentic-rag, knowledge-graph, governance]
---
```

## Example: full frontmatter for an experiment

```yaml
---
exomem_id: 123e4567-e89b-42d3-a456-426614174001
type: experiment
domain: workflow
status: active
created: 2026-05-01
updated: 2026-05-09
started: 2026-05-09
duration: "30 days"
n: 1
hypothesis: "Batching code review into one daily slot cuts context-switching"
sources:
  - "[[Knowledge Base/Sources/Books/2026-04-deep-work]]"
tags: [workflow, batching, focus]
---
```

## Example: full frontmatter for a production-log

```yaml
---
type: production-log
medium: posts
status: recorded
created: 2026-05-09
updated: 2026-05-09
recorded: 2026-05-09
published: null
projects: [project-alpha]
host: the host
editor: a teammate
sources:
  - "[[Knowledge Base/Sources/Sessions/2026-05-09-launch-planning]]"
related:
  - "[[Knowledge Base/Notes/Research/Project Alpha/launch-messaging]]"
  - "[[Knowledge Base/Notes/Patterns/short-form-post-template]]"
tags: [posts, launch, batch-01]
---
```
