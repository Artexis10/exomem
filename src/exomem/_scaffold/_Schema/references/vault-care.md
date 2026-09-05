# Vault structure, adoption, and review

## Vault layout

```
<vault>/Knowledge Base/
├── index.md                      Top-level catalog; updated on every write
├── log.md                        Append-only activity log; most recent first
├── _access.yaml                  (optional) per-subtree readonly/excluded — see references/write-scope.md
├── _Schema/
│   ├── SKILL.md                  This file (canonical)
│   ├── project-keys.yaml         Registered research scope keys
│   ├── workflow-skills/          Named agent workflows built on the core contract
│   └── references/
│       ├── page-types.md         Page-type taxonomy
│       ├── frontmatter.md        Frontmatter spec for each page type
│       ├── write-scope.md        What's writeable vs. read-only
│       ├── supersession.md       Supersession protocol
│       ├── operations.md         Detailed per-operation specs
│       └── audit-checks.md       Per-check detail for the audit operation
├── Sources/
│   ├── Articles/                 Captured web/PDF content
│   ├── Sessions/                 Conversation transcripts OR session captures
│   ├── Books/                    Book notes/excerpts
│   ├── Papers/                   Academic papers
│   ├── Videos/                   Video transcripts/notes
│   └── Other/                    Miscellaneous captures
├── Notes/
│   ├── Research/<scope>/         Project- or domain-scoped research (incl. hubs + snapshots)
│   ├── Insights/                 Distilled cross-cutting lessons
│   ├── Failures/                 Documented failure modes
│   ├── Patterns/                 Reusable patterns
│   ├── Experiments/<domain>/     Primary experiments — protocol/data/results
│   └── Productions/<medium>/     Creative artifacts + production knowledge
├── Entities/
│   ├── People/
│   ├── Concepts/
│   ├── Libraries/
│   └── Decisions/
└── Evidence/
    └── <scope>/                  Per-incident binary/document/factual preservation
```

**This tree is the `Knowledge Base/` layer only — not the shape of your whole vault.**
The vault around it is yours: any top-level folders you keep (`Daily/`, `Projects/`,
`Reference/`, a journal — whatever) sit *beside* `Knowledge Base/` and are **read-only
input** to this skill. Don't infer a fixed vault shape from the tree above. On your
first engagement in a vault, run `browse_memory` once to learn its real top-level layout
(see § Assessing a vault you didn't build), then treat everything outside
`Knowledge Base/` as read-only. Only `Knowledge Base/` is governed and writeable.

`<vault>` resolves to your markdown vault root (Obsidian optional) — the folder that
contains `Knowledge Base/`, set via `EXOMEM_VAULT_PATH`. Verify allowed filesystem
paths before writing.

The research scopes are an open set you grow over time, registered in
`_Schema/project-keys.yaml` (see § Research scope keys). New users start with a
small set (e.g. `personal`, `project-alpha`, `work`) and add their own.

### Assessing or adopting a vault you didn't build

**Make this your first move in any unfamiliar vault.** For import/adoption questions, run **adopt_vault(mode="scan-only")** first: it wraps the bounded scan, states the read-only contract, suggests likely knowledge packs, reports a bounded semantic census, and lists safe next actions. It never rewrites originals in scan-only mode; explicit write modes stay under `Knowledge Base/` and either save the manifest or copy selected legacy text files as Sources with original path/hash provenance. `compile-selected` returns a proposal, not a compiled note: review it, then call `note()` so the normal semantic precommit contract still applies.

For structural questions —
"what does this vault look like," "how is this vault organized," "is there junk in
here" — and simply to learn the layout before you write, run **browse_memory** first: one bounded,
read-only report of folder structure, counts, frontmatter coverage, naming
patterns, and junk candidates (zero-byte files, sync-conflict duplicates). It
works on any folder under the vault root, including trees outside
`Knowledge Base/` (a `Daily/` or `Journal/` folder), and on vaults with no KB at
all. The folders it reports *outside* `Knowledge Base/` are read-only input — link
to them, never write them; only `Knowledge Base/` is governed. Drill down from there: `browse_memory` on folders of interest,
`ask_memory scope="vault"` for content, targeted `read_memory` for individual files. Use adoption output to decide whether to save a manifest, copy selected originals into Sources, or compile selected material into governed notes; do not rewrite the old vault by default. **Never
bulk-read a vault file-by-file to answer a structural question** — the report
answers in one call what would otherwise cost hundreds of reads.

## Activity log

`log.md` at the vault root is the append-only chronological record of every
confirmed write. **Most recent first.** Format per entry:

```
## [YYYY-MM-DD] <op> | <title>

<one-paragraph description summarising what was written and why>
```

Distinction from `index.md`:

- **`log.md`** is the *activity feed* — chronological, durable, content-focused.
- **`index.md` § Recent activity** is a *cap-50 view* derived from log.md — terse
  one-line summaries for quick navigation. When log.md grows beyond cap, older
  entries fall off the index but remain in log.md.

Both update on every confirmed write.

## Audit (lint) checks

The **review_memory(mode="audit")** operation runs read-only checks and proposes fixes (never
auto-fixes); every audit presentation remains read-only. The default
`detail="actionable"` puts current blockers first, then malformed semantic units
and unregistered relation work, then ordinary findings. Grandfathered relation-
disposition debt is grouped into one deterministic `legacy_backlog` with exact
counts and bounded samples instead of flooding the action list. Use
`detail="full"` to enumerate the raw findings; `legacy_sample_limit` is an
integer from 0 through 50 and controls samples in actionable output only. The
checks cover:
orphans, broken wikilinks, supersession integrity, stale frontmatter,
`index.md`/`log.md` drift, aged unprocessed sources (oldest-first — pair with
`propose_compilation`), status/location mismatch, unfinished experiments, stalled
production lifecycles, **stale-review candidates** (active conclusions that are old
AND rarely surfaced in `find` AND low inbound-link degree — surfaced for review
only, never decayed or down-ranked; hubs/snapshots excluded as expected-to-drift),
unregistered project keys, and embedding drift.

Per-check detail — exactly what each flags, its severity, and the proposed fix —
is in **`audit-checks.md`**.

## What this skill does NOT do

- Touch anything outside `Knowledge Base/`.
- Auto-compile *blindly* after every capture. Compilation is a deliberate step
  taken at a stepping-stone; it's always reported, never a silent dump of raw
  transcripts or every passing remark. "No silent dump" targets *noise* —
  transcripts, mid-flight tangents — not *signal*: it never licenses pruning
  context or detail from a note (see *Comprehensive coverage, minimal expression*
  under Proactive engagement).
- Assign numeric confidence scores. Use citation count and recency as the trust
  signal.
- Apply retention decay or "forgetting curves." Old material stays. If superseded,
  mark it; if irrelevant, archive into an `_archive/` subfolder. (`review_memory`'s
  `stale_review` check **surfaces** old/cold/low-inbound conclusions as *review
  candidates* for you to judge — but never auto-decays, down-ranks, hides, or moves
  anything; review surfacing has no effect on `ask_memory`/`find` ordering.
  Surfacing a candidate ≠ a forgetting curve. Authored typed relations DO
  legitimately inform retrieval ranking — that is connectivity you wrote, not
  decay the system invented.)
- Run on a schedule, or read or write your vault in the background. Every operation
  happens inside a turn — because you asked, or because the conversation reached a
  point where consulting or capturing is clearly warranted. (On clients that support
  them, optional hooks may *remind* the assistant to check at the start or end of a
  turn. A reminder is not an operation: it prompts the same in-turn judgment
  described under **Proactive engagement**, and nothing touches the vault until the
  assistant decides to act.)
- Modify `Sources/` or `Evidence/` files after creation. Mistakes get superseded,
  not edited.
