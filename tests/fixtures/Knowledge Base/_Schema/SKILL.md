---
name: knowledge-base
description: Operates on Hugo's personal Obsidian Knowledge Base — raw sources, compiled research notes, insights, failures, patterns, experiments, production-logs, typed entities, and Evidence artifacts. Triggers when the user wants to save, file, log, compile, distill, search, audit, supersede, or preserve anything in their knowledge base, vault, KB, Obsidian, notes, or "my docs," including oblique phrasings ("interesting, save it," "I want to remember this") when context implies a KB operation. Do NOT trigger for operations on parts of the vault outside the Knowledge Base folder — Cognitive Core, Domains, Prompt Bank, Products, and Personal Context are read-only inputs to this skill, never write targets.
metadata:
  version: 0.8.0
---

# Knowledge Base

Hugo's compiled, structured layer of the Obsidian vault. Everything in `Knowledge Base/` is either a raw source (immutable), compiled material under explicit governance, or a preserved factual artifact (Evidence). The rest of the vault (`Cognitive Core/`, `Domains/`, `Prompt Bank/`, `Products/<X>/Strategy.md` etc., `Personal Context/`) is hand-authored and is **read-only input** to this skill.

The Knowledge Base does not replace those curated trees. It is a parallel substrate for compounding LLM-assisted research, insights, failure modes, experiments, productions, entity knowledge, and architectural documentation — kept structurally separate so its epistemic status is always clear.

## Core principle

**Sources are immutable. Compiled material is governed. Evidence is preserved. Curated thinking is sacred.**

- `Sources/` — raw inputs. Append-only. Never edited after capture.
- `Notes/`, `Entities/` — compiled, structured, supersedable. Always carry frontmatter, sources, and links.
- `Evidence/` — raw legal/factual artifacts (binaries, documents, screenshots). Append-only. No analysis at this layer.
- The rest of the vault — Claude reads, Claude does not write.

## Vault layout

```
<vault>/Knowledge Base/
├── index.md                      Top-level catalog; updated on every write
├── log.md                        Append-only activity log; most recent first
├── _Schema/
│   ├── SKILL.md                  This file (canonical)
│   └── references/
│       ├── page-types.md         Page-type taxonomy
│       ├── frontmatter.md        Frontmatter spec for each page type
│       ├── write-scope.md        What's writeable vs. read-only
│       ├── supersession.md       Supersession protocol
│       └── operations.md         Detailed per-operation specs
├── Sources/
│   ├── Articles/                 Captured web/PDF content
│   ├── Sessions/                 Conversation transcripts OR Claude-written session captures
│   └── Books/                    Book notes/excerpts
├── Notes/
│   ├── Research/{Substrate, Q, Endstate, Sift, Together Unprocessed,
│   │            Health, Finance, Creative, Science, Travel,
│   │            Book Club}/      Project- or domain-scoped research (incl. hubs + snapshots)
│   ├── Insights/                 Distilled cross-cutting lessons
│   ├── Failures/                 Documented failure modes
│   ├── Patterns/                 Reusable patterns
│   ├── Experiments/{Food, ...}/  Primary experiments — protocol/data/results
│   └── Productions/{Reels, ...}/ Creative artifacts + production knowledge
├── Entities/
│   ├── People/
│   ├── Concepts/
│   ├── Libraries/
│   └── Decisions/
└── Evidence/
    └── <scope>/                  Per-incident binary/document/factual preservation
                                  (e.g., Evidence/Yolo/, Evidence/Mother Cancer/)
```

`<vault>` resolves to the Obsidian vault root. Path differs by machine. From WSL (where Claude Code runs), use these forms:
- Desktop: `/path/to/your/vault` (Windows `<your-vault>`)
- Laptop: `/path/to/your/vault` (Windows `<your-vault>`)

Both paths are enrolled in `Q_MNT_ALLOWLIST` in `~/.claude/hooks/user-bash-guard.sh` (yadm-tracked). If the relevant path doesn't resolve on the current machine, you're on the other one — try the alternate. Verify allowed filesystem paths before writing.

## Operations

Eight operations, dispatched by intent. The user does not name them — the skill matches their phrasing to one of these. When intent is ambiguous, ask before acting.

| Op | Intent | Writes to |
|---|---|---|
| **add** | Capture raw input as immutable source | `Sources/<type>/` |
| **note** | Compile a structured note from raw input or thinking | `Notes/<type>/` |
| **link** | Create or update an entity, wire backlinks | `Entities/<type>/` |
| **preserve** | Capture a binary / factual artifact for an incident scope | `Evidence/<scope>/` |
| **schema** | Update the skill's own scaffolding (`_Schema/`, structural conventions, log discipline) | `_Schema/` (canonical; harness sees changes via symlink — see rule 8) |
| **find** | Type-aware search across the KB (read-only) | — |
| **audit** | Lint pass: orphans, broken links, supersession integrity | proposals only |
| **replace** | Supersession: mark old, write new with header pointer | both old + new |

For the full per-operation spec — inputs, validation, write rules, edge cases — see `references/operations.md`.

### Phrasing → operation mapping (heuristic, not exhaustive)

- "save this," "log this," "capture this," "add to my KB" → **add**
- "compile this into a note," "make a note on this," "write this up," "distill this" → **note** (typically preceded by an implicit **add**)
- "log this experiment," "I'm running a 30-day X protocol" → **note** with type=experiment
- "log this reel batch," "add this episode," "record this PDF launch" → **note** with type=production-log
- "this is connected to [[X]]," "link this to Q strategy," "create an entity for X" → **link**
- "preserve this letter," "file this in evidence," "save this for the record" → **preserve**
- "update the skill," "bump the schema," "the KB structure needs to change" → **schema**
- "what do I have on X," "find my notes on Y," "have I covered Z" → **find**
- "audit the KB," "lint the vault," "check for orphans," "clean up stale notes" → **audit**
- "this replaces the old strategy," "supersede the old note on X" → **replace**

When the user says something oblique like "interesting, save it," default to **add** + ask whether to compile a note.

## Activity log

`log.md` at the vault root is the append-only chronological record of every confirmed write. **Most recent first.**

Format per entry:

```
## [YYYY-MM-DD] <op> | <title>

<one-paragraph description summarising what was written and why>
```

Parseable: `grep '^## \[' log.md | head -20` lists the most recent entries.

Distinction from `index.md`:

- **`log.md`** is the *activity feed* — chronological, durable, content-focused. The authoritative record.
- **`index.md` § Recent activity** is a *cap-50 view* derived from log.md — most-recent first, terse one-line summaries, for quick navigation. When log.md grows beyond cap, older entries fall off the index but remain in log.md.

Both update on every confirmed write.

**Trim discipline.** When a write causes one or more entries to fall off the index's cap-50 window, the triggering log entry must explicitly note the trim — e.g. *"(bottom entry X drops off at cap-50; trimmed N this write)"* — so a future reader scanning log.md sees the displacement, not just the new write. The trim is a side effect that deserves a paper trail. (History was: cap-20 prior to v0.8.0; raised to cap-50 because write velocity was aging entries off in ~2 days.)

## Descriptive vs analytical coverage

The KB serves two complementary purposes:

- **Descriptive coverage** — *describe what is.* Architecture hubs (`Notes/Research/<project>/<subsystem>-architecture`), point-in-time snapshots (`<thing>-catalog-snapshot`), concept entities (`Entities/Concepts/<term>`). These let a future planner walk into a system cold and orient quickly.
- **Analytical coverage** — *extract reusable lessons.* Patterns (`Notes/Patterns/`), insights (`Notes/Insights/`), failure modes (`Notes/Failures/`), decisions (`Entities/Decisions/`). These compound across projects.

Both are first-class. When orienting a new project area, descriptive hubs typically come first; patterns and insights extract from the descriptive substrate as second-order knowledge.

Descriptive hubs naturally drift — the system evolves; the hub becomes stale. Acceptable for snapshots (refresh when the question warrants it); for architecture hubs, refresh on major capability ships.

## Write discipline

These rules are non-negotiable.

1. **Read-only paths.** Never write to anything outside `Knowledge Base/`. Specifically: `Cognitive Core/`, `Systems Thinking/Domains/`, `Systems Thinking/AI Collaboration/Prompt Bank/Primitives/`, `Products/<X>/Strategy.md`, `Products/<X>/Vision and Economics.md`, `Products/<X>/Roadmap.md`, `Personal Context (Evolving)/` are inputs only. Compiled notes may **link to** them (`[[Domain - AI Systems & Architecture]]`) but never modify them.

   **Exception via rule 8 symlink:** because `~/.claude/skills/knowledge-base/` is a symlink to the KB canonical `_Schema/`, writes through that path resolve into the vault — no real exception needed. No other writes outside the vault are permitted.

2. **Sources and Evidence are append-only.** Once a file lands in `Sources/` or `Evidence/`, never edit it. Corrections happen by adding a new source and superseding the old via a compiled note.

3. **Propose before writing compiled material.** For `note`, `link`, `replace`, and `schema` operations, show the user the proposed page content (or diff) and wait for confirmation before writing. The exception is `add` (raw capture), `preserve` (raw evidence), and `find`/`audit` (read-only).

    **Batch waiver:** the user may approve a *scope* of multiple files upfront ("draft all Tier 1," "write all four hubs + concepts") rather than each file individually. In that case, write the batch, then summarise paths + count. The waiver is **per-batch** — a new batch of work needs a new scope-approval, not a standing waiver.

    **Standing waiver:** phrasing like "just write it" or recorded preferences in agent memory.

4. **Frontmatter is mandatory.** Every file written under `Knowledge Base/` must carry frontmatter conforming to `references/frontmatter.md`. Exceptions (index files): `index.md`, `log.md`, and sub-folder `index.md` files. `Sources/` and `Evidence/` raws carry frontmatter unless the artifact is a non-markdown binary (PDF, image, docx) — then the frontmatter lives in a sidecar `.md` if one is needed.

5. **No `confidence` floats.** Trust is conveyed through citations and link counts, not numbers. The frontmatter spec deliberately omits a confidence field.

6. **Supersession over deletion.** When information is replaced, mark the old page `superseded`, link to the new one, and never delete. See `references/supersession.md`.

7. **Always update `index.md` and `log.md`.** Every write that creates or moves a page updates:
    - **Top-level `index.md`** — counts + Recent activity (cap-50).
    - **`log.md`** — append the entry per the Activity log section.
    - **Relevant sub-folder `index.md`** — see sub-folder index conventions below.
    - **`ingested_into:` on source files** — when a `Sources/` file is compiled into a note or entity, append the new artifact's wikilink to its `ingested_into:` frontmatter.

### Sub-folder index conventions

Sub-folder indexes are not universal — they exist when categorization is itself load-bearing.

- **`Notes/Patterns/index.md`** — categorized by sub-type (Architectural / Governance / Workflow / UI / Relational / Pedagogical). Categorization is the index's value-add; flat would underserve.
- **`Notes/Insights/`** — no sub-index. Flat folder; parent `Notes/index.md` links to it directly.
- **`Notes/Failures/`** — no sub-index. Same shape as Insights.
- **`Notes/Research/<scope>/`** — sub-index only when the scope folder warrants categorization (a hub research-note often plays that role, e.g., `tu-operational-system` orients TU's research cluster; Endstate's folder is flat with no sub-index needed yet). Add when warranted; don't pre-create empty.
- **`Notes/Experiments/<domain>/index.md`** — optional; useful when multiple experiments share a domain.
- **`Notes/Productions/<medium>/index.md`** — optional; useful when productions accumulate.
- **`Entities/Concepts/index.md`** — categorized by domain (Metabolism, Thyroid, TU Brand, Governance/failure modes, Infrastructure, Endstate domain vocabulary, etc.). Categorization is load-bearing.
- **`Entities/Decisions/index.md`** — single chronological list with a one-paragraph summary per decision.
- **`Entities/People/index.md`**, **`Entities/Libraries/index.md`** — categorize when the list is long enough to benefit.

8. **Deploy via symlink.** The harness loader at `~/.claude/skills/knowledge-base/` is a directory symlink to the KB canonical `_Schema/` folder on each machine — making the canonical and deployed copy literally the same files. Schema ops write to the canonical path; the harness sees the change immediately because it's the same file.

    Per-machine symlink targets:
    - Desktop: `C:\Users\<you>\.claude\skills\knowledge-base\` → `<your-vault>\Knowledge Base\_Schema\`
    - Laptop: `C:\Users\<you>\.claude\skills\knowledge-base\` → `<your-vault>\Knowledge Base\_Schema\`

    The symlinks are per-machine (different targets) and **must be excluded from yadm tracking** so each machine maintains its own local link. The canonical content is sync'd across machines via Obsidian Sync; each machine's symlink resolves to its local vault path.

    Setup (one-time per machine; requires Windows Developer Mode for non-admin symlink creation):

    ```powershell
    # 1. Stop yadm tracking the skill folder (run from any directory)
    yadm rm --cached -r ~/.claude/skills/knowledge-base
    # Add ".claude/skills/knowledge-base/" to yadm's gitignore equivalent
    yadm commit -m "Exclude knowledge-base skill folder; per-machine symlink"

    # 2. Backup-then-replace the existing folder
    Move-Item "$env:USERPROFILE\.claude\skills\knowledge-base" `
              "$env:USERPROFILE\.claude\skills\knowledge-base.pre-symlink-backup"

    # 3. Create the symlink (target per machine — adjust path)
    New-Item -ItemType SymbolicLink `
             -Path "$env:USERPROFILE\.claude\skills\knowledge-base" `
             -Target "<vault>\Knowledge Base\_Schema"

    # 4. Verify
    Get-Item "$env:USERPROFILE\.claude\skills\knowledge-base" | Select-Object Target
    ```

    *Why this works:* Claude Code's skill loader reads files at the symlink path; the OS transparently dereferences to the canonical. Editing the canonical = editing the deploy. The structural duplication is gone, and so is the drift class.

For the full read-only/write-target map see `references/write-scope.md`.

## Page types

Eight page types live under `Knowledge Base/`. Each has a required frontmatter shape, writing conventions, and naming rules.

- **source** — raw input under `Sources/<type>/`. **Two flavors:**
    - *Transcript* — content the user provided (pasted conversation, captured article, book excerpt). The source captures content as-is.
    - *Origination record* — Claude-written session capture documenting the reasoning behind what was compiled in a session, with `ingested_into:` listing every downstream artifact the session produced. Used when a single conversation produces multiple compiled artifacts and the session itself is the reasoning trail.
    
    Both flavors share the same frontmatter shape; the body content differs.
- **research-note** — compiled, project-or-domain-scoped research under `Notes/Research/<scope>/`. **Informal subtypes (not separate page types):**
    - *Standard* — synthesised research on a topic.
    - *Hub* — orients around a major subsystem or workstream, linking out extensively. Refresh on major capability ships. E.g., `tu-operational-system`, `engine-architecture`, `hosted-backup-architecture`.
    - *Snapshot* — explicitly point-in-time (e.g., `openspec-capability-catalog-snapshot`); drift is acceptable; refresh when the question being asked warrants it. Note "snapshot" in the body.
- **insight** — distilled cross-cutting lesson under `Notes/Insights/`
- **failure** — documented failure mode under `Notes/Failures/`
- **pattern** — reusable cross-cutting pattern under `Notes/Patterns/`. Uses `projects:` (plural list) in frontmatter when the pattern applies across multiple products, e.g., `projects: [endstate, q, substrate]`.
- **experiment** — primary experiment (hypothesis + protocol + data) under `Notes/Experiments/<domain>/`
- **production-log** — creative artifact + production knowledge under `Notes/Productions/<medium>/`
- **entity** — typed node under `Entities/<entity-type>/`

Detailed spec for each: `references/page-types.md`. Frontmatter for each: `references/frontmatter.md`.

### Research scope keys

The `project` field on a research note is one of:

- Umbrella / company: `substrate` (the company that owns Q, Endstate, Sift, and future products; also the landing page repo)
- Products: `q`, `endstate` (covers both `endstate` engine and `endstate-gui`), `sift`
- Activities: `tu` (Together Unprocessed podcast), `book-club`
- Domains: `health`, `finance`, `creative`, `science`, `travel`
- Cross-cutting / personal: **`personal`** — load-bearing in practice; covers anything not tied to a specific product, activity, or domain (vehicle profiles, household infrastructure, personal admin). Not a fallback for "I'm not sure"; pick the most-specific scope first.

Use `substrate` for company-level material — landing page, brand, positioning, infrastructure shared across products, business strategy spanning products. Use product-specific keys (`q`, `endstate`, `sift`) for product-specific work. If a thought turns out to belong at a different level, change the `project` field and move the file.

For **patterns** that apply across multiple products, use `projects:` (plural list) instead of `project:` (singular). The plural form is correct when the pattern's claim is genuinely cross-project (e.g., `projects: [endstate, q, substrate]`).

If you find yourself wanting a scope that isn't on this list, surface it and ask before adding. Avoid project-key sprawl.

### Q tenants

Q is a multi-tenant platform. When research is about a specific Q tenant, set `project: q` and add `tenant: <key>`. Current tenants:

- `example-tenant` — an example client's knowledge platform
- `tu` — Together, Unprocessed (the TU podcast also runs on Q)

**Disambiguating `tu`:** the same key `tu` is used both as a top-level project (when research is about the podcast as content/activity — episodes, guests, audience, narrative) and as a Q tenant key (when research is about TU's deployment on the Q platform — infrastructure, configuration, integrations). Disambiguate by the `project` field:
- `project: tu` → about the podcast as activity
- `project: q, tenant: tu` → about TU as a Q tenant deployment

If a tenant isn't on this list, surface it before assuming.

### Experiment vs production-log

These are easy to confuse. Both are time-bounded, both have date-prefixed filenames, both can have outcomes. The difference:

- **Experiment** = a hypothesis tested under a protocol with primary data (`Notes/Experiments/<domain>/`). Ends with a conclusion that confirms, refutes, or qualifies the hypothesis. E.g., "30-day dairy elimination → sinus inflammation."
- **Production-log** = a creative artifact plus the production knowledge around it (`Notes/Productions/<medium>/`). Ends with engagement metrics and reflection — but the artifact's value is the artifact itself, not a finding. E.g., "May 2026 metabolism reels batch."

When in doubt: did Hugo set out to learn whether X is true (experiment) or to make a thing the world will see (production)?

## Workflow: typical add-then-compile session

1. **User pastes raw material or asks to log something.**
2. **Skill creates a `source` file.** Picks `Sources/Articles/`, `Sources/Sessions/`, or `Sources/Books/` based on the input shape. Filename: ISO-date + slug. Frontmatter per `references/frontmatter.md`. Updates `Sources/index.md`.
3. **Skill asks: "Compile a note from this? If yes, what type — research, insight, failure, pattern, experiment, production-log? And what scope (for research) / domain (for experiment) / medium (for production)?"** Skip if the user already specified.
4. **Skill drafts the compiled page** with frontmatter, sources block (linking back to the source file), wikilinks to existing entities/concepts where they obviously match, and a "Connections" section listing the wikilinks.
5. **Skill shows the draft, waits for confirmation.** User can revise inline.
6. **On confirm: writes the page**, updates the relevant `index.md`, appends to `log.md`, and reports paths.

### Batch form

When the user approves a scope of multiple files upfront ("draft all Tier 1," "all four hubs + concepts"), the workflow collapses:

1. Skill drafts all files in the scope.
2. Skill writes the batch (single approval covers all).
3. Skill updates indexes + `log.md` — one entry per file, or one composite entry when the batch is structurally a single unit (e.g., 8 concept entity stubs as "8 Endstate domain-vocabulary entities").
4. Skill appends the new artifacts to the originating source's `ingested_into:` frontmatter.
5. Skill reports paths + count.

The batch waiver is per-batch, not standing.

For other operation flows see `references/operations.md`.

## Linking discipline

Every compiled page should link out. Linking is what turns the KB from a junk drawer into a graph.

- Link to entities (`[[Knowledge Base/Entities/Concepts/Profile]]`, `[[Knowledge Base/Entities/People/Andrej Karpathy]]`).
- Link to upstream curated material when relevant (`[[Domain - AI Systems & Architecture]]`, `[[Products/Q/Strategy]]`).
- Link to other compiled notes when they cover related ground.
- Link back to the originating `Sources/` file via the `sources:` frontmatter list (mirrors the source's `ingested_into:` list).

If a wikilink target doesn't exist yet, prefer creating the entity stub via the **link** operation rather than leaving a dangling link. Dangling links accumulate and surface in **audit**.

### Pointer entities vs mirror entities

When creating an `Entities/Libraries/` or `Entities/Concepts/` page that references a **currently-evolving external artifact** (operational skill, code library, live service config, live spec in another doc system), use **pointer-style** — summary + canonical-source pointer + connective tissue — not **mirror-style** (versions, file inventories, command lines, subtype tables, workflow steps copied verbatim). Mirroring guarantees drift. See [[Knowledge Base/Notes/Patterns/pointer-entities-for-live-artifacts]] for the worked discipline.

Frozen things (Sources captures, decisions about past events) and KB-native content (insights, patterns, failures, research-notes) are explicitly out of scope — the KB *is* the source of truth for those.

## Audit (lint) checks

The **audit** operation runs the following checks and proposes fixes (never auto-fixes):

- **Orphans** — compiled pages with zero inbound links and zero outbound links beyond their `sources` block. Propose: link or archive.
- **Broken wikilinks** — `[[X]]` where `X` does not resolve. Propose: fix path or create stub entity.
- **Supersession integrity** — pages marked `superseded` must have `superseded_by` pointing to a real page; pages marked `active` must not appear as the target of any `superseded_by`.
- **Stale frontmatter** — required fields missing for the page type. Includes: research-notes with `tenant` set but `project` not equal to `q` (the `tenant` field is Q-only); patterns with `project:` (singular) when `projects:` (plural) is the convention for cross-project patterns.
- **`index.md` / `log.md` drift** — files in folders that are not catalogued, catalogue entries pointing to missing files, or `log.md` entries without corresponding artifacts on disk (and vice versa).
- **Unprocessed sources** — files in `Sources/` with no `ingested_into:` field after a configurable threshold (default: 30 days).
- **Status / location mismatch** — pages with `status: archived` not living in an `_archive/` subfolder, and vice versa.
- **Unfinished experiments** — experiments with `status: active` and `started` date older than the experiment's `duration` field. Propose: write up results, mark concluded, or extend.
- **Unfinished production lifecycles** — production-logs with `status: recorded` or earlier whose `published` field has been null for >60 days. Propose: update status, fill outcomes, or move to dropped.
- **Stale hubs / snapshots** — research-notes flagged as hub or snapshot with `updated:` older than threshold (default: 90 days for hubs, 30 days for snapshots). Propose: refresh or mark explicitly as historical.
- **Harness symlink integrity** — `~/.claude/skills/knowledge-base/` is supposed to be a symlink to the local KB's `_Schema/` folder per rule 8. Check: `Get-Item <path> | Select-Object Target` (PowerShell) or `test -L <path> && readlink <path>` (Bash). If the path is a regular folder (not a symlink), or the target doesn't resolve, the symlink is broken — drift is back. Propose: re-run the symlink setup from rule 8. Cheap check; run on every audit.

Audit is read-mostly. The output is a proposal report that the user reviews; nothing is rewritten without explicit confirmation per item or batch.

## What this skill does NOT do

- Touch anything outside `Knowledge Base/` (the dual-write exception for `~/.claude/skills/knowledge-base/` under `schema` ops is the only carve-out — see rule 8).
- Auto-compile sources without confirmation. Sources land; compilation is always a conscious step.
- Assign numeric confidence scores. Use citation count and recency as the trust signal.
- Apply retention decay or "forgetting curves." Old material stays. If superseded, mark it; if irrelevant, archive into a `_archive/` subfolder of its current location.
- Run on hooks, schedules, or background triggers. Operations happen because the user asked.
- Modify `Sources/` or `Evidence/` files after creation. Mistakes get superseded, not edited.
- Operate on mobile claude.ai (no filesystem MCP access on phone). Mobile is capture-only — paste into Obsidian directly, run the skill from desk later.

## When to ask vs. when to proceed

**Ask before:**
- Writing any compiled note, entity, experiment, production-log, supersession, or schema update.
- Choosing a page type when intent is ambiguous (research vs. insight vs. experiment vs. production-log, etc.).
- Choosing a scope under `Notes/Research/` when the user hasn't named one.
- Choosing a domain under `Notes/Experiments/` or medium under `Notes/Productions/` when not stated.
- For Q research: confirming whether the note is about Q itself or about a specific tenant (and which one).
- Choosing whether a research-note is *standard*, *hub*, or *snapshot* — when the framing materially affects scope.
- Marking an existing page `superseded`.

**Proceed without asking:**
- `add` operations — raw capture into `Sources/`.
- `preserve` operations — raw capture into `Evidence/<scope>/`.
- `find` and `audit` — read-only.
- Updating `index.md`, `log.md`, and `ingested_into:` frontmatter after a confirmed write.
- Resolving obvious wikilink targets when the entity exists exactly.
- Continuing a previously-approved batch (scope-level approval covers all files in the batch).

## References (read on demand)

- `references/page-types.md` — full page-type taxonomy with naming conventions
- `references/frontmatter.md` — frontmatter spec per page type
- `references/write-scope.md` — full read-only / writeable path map
- `references/supersession.md` — supersession protocol
- `references/operations.md` — detailed per-operation specs

Read each on first use. The SKILL.md you're reading now is the contract; the references are the manual.
