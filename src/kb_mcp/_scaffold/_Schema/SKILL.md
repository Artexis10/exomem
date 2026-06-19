---
name: knowledge-base
description: Operates on Hugo's personal Obsidian Knowledge Base — raw sources, compiled research notes, insights, failures, patterns, experiments, production-logs, typed entities, and Evidence artifacts. Triggers when the user wants to save, file, log, compile, distill, search, audit, supersede, or preserve anything in their KB, vault, Obsidian, or notes — including oblique phrasings ("interesting, save it," "I want to remember this"). Also engages proactively, without being told — it consults the KB for prior conclusions when a turn touches a project, domain, decision, or topic it likely covers, and captures durable conclusions when the conversation reaches a stepping-stone (agreement, decision, solved problem, diagnosed failure, or recognized pattern). Do NOT trigger for writes outside the Knowledge Base folder — Cognitive Core, Domains, Prompt Bank, Products, and Personal Context are read-only inputs.
version: 0.15.0
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

## Proactive engagement

This skill is **context-aware, not just request-driven.** It engages on its own in two situations and stays quiet otherwise. (There are still no hooks/schedules/background triggers — "proactive" means Claude's own judgment mid-conversation, not automation.)

**Proactive retrieval (read) — quiet, surface only hits.** When a turn references something the KB plausibly holds — a project key (`q`, `endstate`, `substrate`, …), a domain (health, finance, …), a named entity/person/decision, or phrasings like "what did I conclude about X," "have I looked at Y," "where did we land on Z" — run a quiet `find` **first** and fold what you find into the answer. Don't narrate the search; mention the KB only when it returned something relevant, and cite the page(s) you used. A miss is "not found in what I searched," never "it doesn't exist" (see Search → never report a miss as absence) — and an empty find means *no coverage yet*, a reason to consider capturing (new ground), not a signal to disengage. Skip the find only on pure chit-chat the KB plainly has no bearing on.

**Stepping-stone capture (write) — autonomous, then report.** When the conversation reaches a **stepping-stone** — you and Hugo agree on something, a decision is made, a problem is solved, a failure is diagnosed, a pattern is recognized — capture it without being asked:

- **Coverage-agnostic — capture whether or not the KB already holds the topic.** A durable conclusion on brand-new ground is first-class: it becomes the first page on that topic, which is exactly how the corpus grows. Never gate capture on prior coverage.
- Raw material → **add** (already no-confirmation).
- A durable conclusion → draft the compiled **note**/**link**, run **suggest_links** + the near-duplicate check first, write it under the **standing waiver** (don't ask per-note), then report one line: `Saved → <path>`.
- The guardrails that remain are the ones that matter: dedupe (prefer **edit**/**replace** over a parallel page; surface a near-duplicate `warning` when it fires — that's a hit worth surfacing) and clean links. The per-write approval is waived, not the integrity checks.
- Still pause and ask **only** when type/scope is genuinely ambiguous (research vs insight vs experiment; which `Notes/Research/<scope>`; Q-vs-tenant) — the one-line questions from "When to ask vs. when to proceed" still apply.
- **If the write fails** (connector down, 401/502, service issue): on a *proactive* capture, fail soft — one line ("couldn't save: <reason>") and keep going; the content's still in the thread to retry, and a side-channel write must never block the substantive answer. On an *explicit* "save this," don't just move on — diagnose (401 = connection-side, 502 = service down) and retry or fall back to the desktop filesystem write, or say plainly it didn't land. Proactive writes degrade quietly; requested writes are never silently dropped.

Not a stepping-stone: mid-thought exploration, brainstorm tangents, unresolved questions, things Hugo is still weighing. Capture at the landing, not during the flight.

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
│   ├── Books/                    Book notes/excerpts
│   ├── Papers/                   Academic papers
│   ├── Videos/                   Video transcripts/notes (e.g. a pasted YouTube transcript)
│   └── Other/                    Miscellaneous captures
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

## Loading the tools

The KB tools may be **deferred** — the client lists them by name and you load a
tool's schema before you can call it. Two habits keep that from costing extra
round-trips:

1. **Load the core set up front, in one shot.** You'll almost always need
   `find` (search), `get` (read a page), and one or more of `note`, `add`,
   `link`, `suggest_links`, `edit`, `audit`. In Claude Code, load them by exact
   name in a single call — this skips relevance ranking entirely:

   `ToolSearch("select:find,get,note,add,link,suggest_links,edit,audit")`

   On clients without a `select:` syntax (e.g. claude.ai), search by capability
   — "search the knowledge base", "read a KB page", "compile a note" — and each
   resolves to the right tool. Don't keep re-searching for `find`; it's the
   read-only hybrid (semantic + keyword) search and your default entry point.

2. **Pick one server, not both.** This KB is exposed by two identical
   connectors — **Knowledge Base** (desktop) and **Knowledge Base (Laptop)**.
   They front the same vault per machine. Use the one whose vault path resolves
   on the current machine (see the machine note above); don't fan the same call
   out to both.

Reach for the Tier 2 filesystem ops only when no Tier 1 op fits — and note they
may be turned off on lean deployments (`KB_MCP_DISABLE_TIER2`), in which case
only the Tier 1 ops below are registered.

## Operations

Operations are split into two tiers. **Tier 1 is primary** — every typed-note workflow goes through it because the type-routing IS the discipline. **Tier 2 is the escape hatch** for cases that don't fit a Tier 1 shape. If a write fits Tier 1, use it.

### Tier 1 — type-routed (primary)

These encode the KB's discipline: filenames, folders, frontmatter, supersession, and index updates are determined by the operation, not the caller. Dispatched by intent — the user phrases the request; the skill matches one of these.

| Op | Intent | Writes to |
|---|---|---|
| **add** | Capture raw input as immutable source | `Sources/<type>/` |
| **note** | Compile a structured note from raw input or thinking | `Notes/<type>/` |
| **link** | Create or update an entity, wire backlinks | `Entities/<type>/` |
| **preserve** | Capture a binary / factual artifact for an incident scope | `Evidence/<scope>/` |
| **edit** | In-place edit of a compiled page. One mode per call: whole `body` / `tags` / surgical `old_string`→`new_string`; **`edits=[…]`** several surgical pairs in one atomic commit (sequential; any failing pair aborts the batch — folds in former `multi_edit`); **`row_key`+`take`** fill a `[take: ]` opinion row by its leading text, server locates the row, no body re-send (former `set_take`); **`field`+`value`** patch ONE frontmatter field, requires `why:` (former `set_frontmatter_field`). Bumps `updated:`. Optional `expected_hash` (drift guard) + `validate_only` | the page |
| **find** | Type-aware search across the KB (read-only) | — |
| **suggest_links** | Surface existing pages a draft or page should link to, hub-aware (read-only) | — |
| **get** | Read a full file by path (any tree under vault root); **`frontmatter_only=true`** returns just the frontmatter, no body (folds in former `get_frontmatter`). Returns `content_hash` + `mtime` for the two-writer drift guard (echo `content_hash` to `edit` via `expected_hash`). Read-only | — |
| **audit** | Lint pass: orphans, broken links, supersession integrity, aged unprocessed sources | proposals only |
| **propose_compilation** | Draft a note scaffold from unprocessed source(s) — the backlog-drain companion to audit (read-only) | proposals only |
| **replace** | Supersession: mark old, write new with header pointer | both old + new |
| **reconcile** | Heal drift from out-of-band edits (Obsidian/mobile/manual): recompute index counts + incrementally re-embed stale files + report remaining drift. Narrower than `audit_fix` — no wikilink/frontmatter rewrites. Idempotent; `dry_run` reports only | drifted indexes + embedding sidecar |
| **provenance_report** | Scan note bodies for `<!-- key:value -->` provenance tags (filter by key/value/path) — e.g. "all conv-derived takes," "outstanding add-to-imdb flags." Read-only | — |

For the full per-operation spec — inputs, validation, write rules, edge cases — see `references/operations.md`.

### Tier 2 — filesystem-parity (escape hatches)

These exist for things Tier 1 can't express. Use them when:

1. **Building new folder structures.** New top-level KB folders like `Identity/`, `Templates/` — no Tier 1 op routes there.
2. **Files that aren't typed notes.** Skill files, config files, scratch — they don't fit the page-type taxonomy.
3. **Edits the Tier 1 set can't express.** Simple appends, file renames. (Single frontmatter-field changes are now Tier 1 — `edit` with `field`/`value`.)

Do NOT use Tier 2 when Tier 1 fits. If it's a research-note → `note`. If it's an entity → `link`. If it's a source → `add`. If it's evidence → `preserve`. If it's a body/tags/frontmatter-field edit on a compiled page → `edit`. Tier 2 is the fallback, not the default.

| Op | Intent | Writes to |
|---|---|---|
| **create_file** | Write a file at any vault path (optional frontmatter dict). **`kind="dir"`** instead makes a folder (mkdir -p; folds in former `create_directory`) | arbitrary path |
| **list_directory** | List files+subfolders at a path (recursive optional). Read-only | — |
| **move_file** | Rename/relocate a file; rewrites inbound wikilinks by default. Intra-tree moves within `Sources/`/`Evidence/` allowed (themed sub-folders); boundary-crossing still refused | both old + new |
| **delete** | Trash a file OR folder — auto-detected (moves to `_trash/`, recoverable). Requires `confirm=true`; folders need `recursive=true` if non-empty; refuses on inbound links unless `force_orphan`. Folds in former `delete_file`/`delete_directory` | path → `_trash/` |
| **list_trash** | Enumerate recoverable trash entries (with metadata + drift detection). Read-only | — |
| **recover_from_trash** | Undo a delete: move from `_trash/` back to original (or custom) location, clean sidecar | `_trash/` → restored path |
| **append_to_file** | Append text to an existing file | the file |
| **list_inbound_links** | Find all files whose wikilinks resolve to a target. Read-only | — |

### Discipline preserved across BOTH tiers

These constraints apply equally to Tier 1 and Tier 2 ops — no escape hatch around them:

- **Sources/ and Evidence/ are append-only.** `create_file`, `delete`, `append_to_file` (for Sources), and `edit`'s frontmatter-patch mode all refuse on these trees. Use `add` and `preserve` (the only content writers). **Exception — `move_file` relocation:** a move that stays *within* the same append-only tree (themed sub-foldering) is allowed, since it changes location not content; boundary-crossing moves (out of, or into from elsewhere) are still refused.
- **Curated trees are write-protected.** `Cognitive Core/`, `Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`, `Systems Thinking/` refuse Tier 2 writes by default. Pass `allow_curated=true` only when genuinely building infrastructure inside one — it's not a "I just want to write here" override; it's a deliberate per-call acknowledgement. Reads are unrestricted.
- **Every write logs to `Knowledge Base/log.md`** with the operation, path, and a one-line rationale. Where appropriate ops require a `why:` (e.g. `edit`'s frontmatter-patch mode). Additionally, every MCP call (reads and writes) is recorded in the service log at `logs/kb-mcp.log` with the tool name, duration, and outcome — for operational/debug visibility without polluting `log.md`'s content-history role.
- **Deletes are never permanent at the MCP layer.** `delete` moves targets to `Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>` with a `.meta.json` sidecar capturing original path, timestamp, inbound link count, and which force-flags fired. Recovery is `recover_from_trash` (or `move_file` from the trash path back). Permanent removal happens desk-side via `rm Knowledge Base/_trash/...`. The guards (`confirm=true`, `force_orphan`, `force_superseded`, `allow_curated`) still mark the action as deliberate even when it's reversible. The `_trash/` subtree is excluded from `find` and `audit`.
- **Supersession over deletion** still applies. `delete` refuses on pages with `superseded_by:` set unless `force_superseded=true`. For compiled material, prefer `replace`. For multi-file supersession-chain cleanup (e.g. trashing v1 *and* v2), `delete` accepts `expected_dead_inbound: list[str]` — name the files whose links should be ignored because they're being trashed in the same workflow.
- **Wikilink integrity.** `move_file` defaults to updating inbound links. `delete` refuses on files (or trees) with inbound links unless `force_orphan=true`. The KB is a graph; ops that fragment it are explicit.

### Phrasing → operation mapping (heuristic, not exhaustive)

- "save this," "log this," "capture this," "add to my KB" → **add**
- "compile this into a note," "make a note on this," "write this up," "distill this" → **note** (typically preceded by an implicit **add**)
- "log this experiment," "I'm running a 30-day X protocol" → **note** with type=experiment
- "log this reel batch," "add this episode," "record this PDF launch" → **note** with type=production-log
- "this is connected to [[X]]," "link this to Q strategy," "create an entity for X" → **link**
- "preserve this letter," "file this in evidence," "save this for the record" → **preserve**
- "update the skill," "bump the schema," "the KB structure needs to change" → no MCP tool — hand-edit `_Schema/` files through the rule-8 symlink (or directly in Claude Code via the Edit tool); the harness sees changes immediately because it's the same file.
- "fill in the take for X," "write my take on X," "set the take on that row" → **edit** (`row_key`+`take`)
- "make these few edits to the page," "fix these N lines in one go" (same page) → **edit** (`edits=[…]`)
- "show all conv-derived takes," "what's flagged add-to-imdb," "where did this opinion come from" → **provenance_report**
- "what do I have on X," "find my notes on Y," "have I covered Z" → **find**
- "what should this link to," "what existing notes relate to this draft," "densify this page's links" → **suggest_links**
- "what should I compile next," "drain the source backlog," "draft a note from these sources" → **propose_compilation**
- "audit the KB," "lint the vault," "check for orphans," "clean up stale notes" → **audit**
- "I edited the vault directly / in Obsidian / on my phone — sync it up," "heal the drift," "counts/embeddings are stale after out-of-band edits" → **reconcile**
- "this replaces the old strategy," "supersede the old note on X" → **replace**
- "make a new folder for X," "scaffold a Templates/ directory" → **create_file** (`kind="dir"`, Tier 2)
- "create a file at X with this content," "write an Identity/ page" (path doesn't fit a typed-note route) → **create_file** (Tier 2)
- "rename this page to X," "move this note to Patterns/" → **move_file** (Tier 2; defaults to updating inbound wikilinks)
- "what's in folder X," "list the files under Y" → **list_directory** (Tier 2)
- "what links to X" → **list_inbound_links** (Tier 2)
- "flip the status to archived," "set tenant: tu on this page" (single-field tweak) → **edit** (`field`+`value`)
- "tack this onto the end of X" → **append_to_file** (Tier 2)
- "delete this file" → **delete** (Tier 2; trash semantics — recoverable. Supersession still preferred for compiled material — rule 6)
- "delete this folder," "drop the whole subtree" → **delete** (Tier 2; auto-detects folder; needs `recursive=true` if non-empty)
- "what's in the trash," "show me recoverable deletes" → **list_trash** (Tier 2)
- "undelete," "recover this," "put it back where it was" → **recover_from_trash** (Tier 2; the ergonomic undo for `delete`)

**Implicit (no explicit ask) — proactive engagement:**
- topic maps to a project/domain/entity, or "what did I conclude / where did we land on X" → proactive **find** first, fold the hits into the answer (quiet; surface only on a hit)
- "ok let's go with that," "that settles it," "makes sense, let's do X," or a problem just got solved → stepping-stone: capture via **add**/**note** under the standing waiver, then report the path

When the user says something oblique like "interesting, save it," default to **add** + ask whether to compile a note.

## Search

`find` runs in **hybrid mode** by default: BM25 + local vector embeddings (BAAI/bge-base-en-v1.5, 768-dim, runs on the local GPU when available) fused via reciprocal rank fusion. Natural-language queries reach pages that don't contain the literal terms — "glucose regulation and brain function" surfaces inflammation/blood-sugar notes; "distributed system failures" surfaces architecture patterns without matching the words.

Modes:

- `mode="hybrid"` (default) — BM25 + vector + graph + keyword fused via RRF. As of v0.10.3 this is a **strict superset of keyword**: every page keyword would surface lands in the hybrid candidate pool, so hybrid never returns fewer results than keyword for the same query. Best recall on natural-language queries. Falls back to BM25-only if the embedding sidecar is missing.
- `mode="keyword"` — strict case-insensitive substring matching: every whitespace-separated token must appear as a substring in title or body, sorted by `updated:`. Use when you want **precision-only** behaviour — exact-phrase / entity-name / code-identifier lookups where you'd rather get zero results than fuzzy ones.
- `mode="vector"` — vector-only. Diagnostic / testing aid.

**When to use which:**
- Default to **hybrid**. Natural-language queries ("thoughts on X", "what did I conclude about Y", "find my note on Z"), topic browsing, anything where you'd accept semantically-adjacent matches.
- Reach for **keyword** when (a) you know an exact phrase the page uses, (b) you're verifying whether a specific string appears anywhere in the KB, (c) hybrid surfaced a thematically-noisy top-N and you want strict literal matching as a precision tool. The trade-off: keyword returns zero results for natural-language queries whose target page uses synonyms instead of literal tokens (e.g. "London accommodation" misses a page that says "Airbnb in Stamford Street").
- **vector** is for diagnostics — comparing what semantic recall alone surfaces vs what BM25 + keyword catch.

Empty queries always degrade to filtered-most-recent regardless of mode — there's nothing to embed or substring-match.

**Scope — the vault is bigger than the KB:**
- `scope="kb"` (default) searches `Knowledge Base/` first and **auto-widens to the whole vault** when the KB doesn't fill `limit`. So content in sibling folders (`Tracking/`, `Reference/`, `Finance/`, … and the curated trees `Cognitive Core/`, `Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`) is reachable, not silently invisible. Widened hits carry `outside_kb: true` and their `path` shows the sibling folder. Outside-KB recall is BM25/keyword with a relaxed gate (the vector sidecar is KB-only), so even a terse, numbers-heavy file (a workout/finance tracker) surfaces on a partial token match.
- `scope="vault"` always walks the whole vault. `scope="kb-only"` is the strict opt-out (KB only, never widens) — use it when you deliberately want curated KB material and nothing else.
- **Never report a search-miss as absence.** An empty result means *"not found in what I searched,"* not *"it doesn't exist."* If the user is sure something exists and `find` returns nothing, the data may be tabular/terse or live outside the KB — try `scope="vault"`, vary the query terms, or `get` a path you suspect. Say "I didn't find it" — distinguish that from "it isn't there."

Additional knobs on `find`:

- **`graph=true`** (default for hybrid/vector) — outbound wikilinks of top BM25/vector hits contribute a third RRF ranking, surfacing 1-hop neighbours of strong matches even when they share no query tokens. Graph seeds are gated to "strong" matches only (vector hits or BM25 hits passing the stem-aware all-tokens check), so noisy queries don't flood results with neighbours of weak matches.
- **`rerank=true`** (off by default, opt-in due to model load) — runs the top fused candidates through `BAAI/bge-reranker-base` (a CrossEncoder) and re-sorts by reranker score. ~50ms/candidate on Blackwell. Useful when ambiguous queries float topically-off vector matches; for everyday hybrid queries the default fusion already handles this.
- **`prefer_compiled=true`** (default) — applies a small post-fusion multiplier (×1.15) to compiled types (`insight`, `pattern`, `failure`, `research-note`, `entity`) and a small penalty (×0.85) to raw `source`. Reflects the KB's epistemic hierarchy: compiled distillations are the intentional output, sources are inputs. Also re-applied to `rerank_score` so the preference survives reranking. Set false to retrieve raw source discussion verbatim ("what did I capture from Dr. X").

**Stemming**: BM25's corpus and the BM25-only stem-aware gate both use Snowball English stems — `regulation` reaches a page that uses `regulator`, `compounding` reaches one that uses `compound`. Keyword mode stays strict-substring (the precision is the feature there).

**Hit signals**: hybrid/vector results carry `signals: {bm25_rank?, vector_rank?, vector_score?, graph_hop?, graph_in_degree?, keyword_rank?, rerank_score?}` — handy for debugging which ranker surfaced a given hit. `graph_in_degree` is the number of top-N seeds whose body wikilinks to this hit; surfaces hub pages that are both independently scoring AND highly-linked-from strong matches (independent of `graph_hop`, which only fires for graph-only results). `keyword_rank` is the path's position in the keyword scan — a `b1 v1 k1` row means BM25, vector, AND keyword all ranked it #1 (the strongest possible consensus pick). Keyword mode omits the field for backward compat.

Vector embeddings live in a per-machine sidecar at `<vault>/Knowledge Base/.embeddings.sqlite` (dotfile — Obsidian Sync ignores it). Writers refresh the sidecar incrementally after every atomic batch. To bootstrap (first run, after a machine swap, or if the sidecar drifts), call `audit_fix(rebuild_embeddings=true)` — wipes and rebuilds from the markdown source of truth.

### Corpus-aware suggestions

The retrieval stack also runs at *authoring* time so each new entry strengthens the graph instead of just adding to the pile:

- **`suggest_links`** — given a draft (`draft_title` + `draft_body`) or an existing `path`, returns ranked existing pages to link `{path, title, type, why, excerpt}`, hub-preferring, excluding the page itself and anything already linked. Run it before drafting a note's Connections, or on any under-linked existing page to densify the graph retroactively.
- **`note` returns a `suggestions` block** (related pages you didn't cite) plus a near-duplicate `warning` when a draft closely matches an existing page. Both are non-binding signals — surfaced, never auto-applied. Review them and wire in the relevant links via **edit**.

### Measured retrieval (desk-side)

Ranking is evidence-tuned: `scripts/eval_retrieval.py` scores `find()` (NDCG/MRR/recall) against `tests/golden/queries.yaml`; `logs/queries.jsonl` × `logs/writes.jsonl` join into weak `(query → cited_path)` labels that grow the golden set from real usage (`scripts/derive_relevance_pairs.py`). Desk-side dev tooling — not invoked during normal KB ops.

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

**Trim discipline.** When a write pushes entries off the index's cap-50 window, the triggering log entry must note the trim — e.g. *"(bottom entry X drops off at cap-50; trimmed N this write)"* — so a reader scanning log.md sees the displacement, not just the new write.

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

2. **Sources and Evidence are append-only.** Once a file lands in `Sources/` or `Evidence/`, never edit its *content*. Corrections happen by adding a new source and superseding the old via a compiled note. Rule 2 protects content immutability, **not file location**: relocating a file *within* the same append-only tree (e.g. `Sources/Other/x.md` → `Sources/Other/Health/x.md`, into a themed sub-folder) is allowed via `move_file` — the bytes are carried verbatim and inbound wikilinks are rewritten. Crossing the boundary is still forbidden: moving *out* of `Sources/`/`Evidence/`, or *into* one from elsewhere (use `add`/`preserve` for that).

3. **Propose before writing compiled material.** For `note`, `link`, and `replace` operations (and any hand-edit of `_Schema/` files), show the user the proposed page content (or diff) and wait for confirmation before writing. The exception is `add` (raw capture), `preserve` (raw evidence), and `find`/`audit` (read-only).

    **Batch waiver:** the user may approve a *scope* of multiple files upfront ("draft all Tier 1," "write all four hubs + concepts") rather than each file individually. In that case, write the batch, then summarise paths + count. The waiver is **per-batch** — a new batch of work needs a new scope-approval, not a standing waiver.

    **Standing waiver:** phrasing like "just write it," recorded preferences in agent memory, or a stepping-stone reached in an autonomous working session (Hugo's default mode) — draft, write, and report rather than pre-approve (see Proactive engagement).

4. **Frontmatter is mandatory.** Every file written under `Knowledge Base/` must carry frontmatter conforming to `references/frontmatter.md`. Exceptions (index files): `index.md`, `log.md`, and sub-folder `index.md` files. `Sources/` and `Evidence/` raws carry frontmatter unless the artifact is a non-markdown binary (PDF, image, docx) — then the frontmatter lives in a sidecar `.md` if one is needed.

5. **No `confidence` floats.** Trust is conveyed through citations and link counts, not numbers. The frontmatter spec deliberately omits a confidence field.

6. **Supersession over deletion.** When information is replaced, mark the old page `superseded`, link to the new one, and never delete. See `references/supersession.md`.

7. **Always update `index.md` and `log.md`.** Every write that creates or moves a page updates:
    - **Top-level `index.md`** — counts (Sources, Notes, Entities) + Recent activity (cap-50). All count rows are auto-refreshed by the writer; drift used to be a manual reconciliation step and is now closed at the source.
    - **`log.md`** — append the entry per the Activity log section.
    - **`Notes/index.md` and `Entities/index.md`** — count numbers are auto-refreshed on every write (kb-mcp's `indexes.compute_subindex_writes`). Hand-curated descriptions on each bullet are preserved — only the `(N)` count tokens after `[[link]]` and after `### Type — desc (N)` headers get rewritten.
    - **`ingested_into:` on source files** — when a `Sources/` file is compiled into a note or entity, append the new artifact's wikilink to its `ingested_into:` frontmatter.

### Sub-folder index conventions

Two flavors of sub-folder index:

**Auto-maintained (writer keeps current):**
- **`Notes/index.md`** — count numbers in `### Type — desc (N)` headers and `[[link|Subfolder]] (N) — desc` bullets are auto-refreshed on every write. Descriptions, section ordering, and the "Type distinctions reminder" stay hand-curated.
- **`Entities/index.md`** — count numbers in `[[link|Type]] (N)` bullets are auto-refreshed. Descriptions stay hand-curated.
- **Top-level `index.md` Counts section** — Sources, Notes, Entities rows are all auto-refreshed.

**Hand-curated (writer leaves alone):**
- **`Notes/Patterns/index.md`** — categorized by sub-type (Architectural / Governance / Workflow / UI / Relational / Pedagogical). Categorization is the index's value-add; flat would underserve. Writer doesn't touch — Hugo maintains.
- **`Notes/Insights/`** — no sub-index. Flat folder; parent `Notes/index.md` links to it directly.
- **`Notes/Failures/`** — no sub-index. Same shape as Insights.
- **`Notes/Research/<scope>/`** — sub-index only when the scope folder warrants categorization (a hub research-note often plays that role, e.g., `tu-operational-system` orients TU's research cluster; Endstate's folder is flat with no sub-index needed yet). Add when warranted; don't pre-create empty.
- **`Notes/Experiments/<domain>/index.md`** — optional; useful when multiple experiments share a domain.
- **`Notes/Productions/<medium>/index.md`** — optional; useful when productions accumulate.
- **`Entities/Concepts/index.md`** — categorized by domain (Metabolism, Thyroid, TU Brand, Governance/failure modes, Infrastructure, Endstate domain vocabulary, etc.). Categorization is load-bearing.
- **`Entities/Decisions/index.md`** — single chronological list with a one-paragraph summary per decision.
- **`Entities/People/index.md`**, **`Entities/Libraries/index.md`** — categorize when the list is long enough to benefit.

If you add a new subfolder under `Notes/Research/<scope>/`, `Entities/*`, etc. that doesn't already have a bullet in the relevant index, the writer leaves the index untouched and `audit` surfaces the gap via `index_drift` — add a bullet with a description manually so the auto-refresh has a count token to update.

8. **Deploy via symlink.** The harness loader at `~/.claude/skills/knowledge-base/` is a directory symlink to the KB canonical `_Schema/` folder on each machine — canonical and deployed are literally the same files, so a schema edit takes effect immediately and the drift class is gone. Per-machine targets (e.g. `C:\Users\<you>\.claude\skills\knowledge-base\` → `<vault>\Knowledge Base\_Schema\`); the links are per-machine, excluded from yadm tracking, with canonical content synced across machines via Obsidian Sync. First-time setup is a one-time symlink creation per machine — `New-Item -ItemType SymbolicLink` (needs Windows Developer Mode for non-admin), or `MSYS_NO_PATHCONV=1 cmd /c mklink /D "<link>" "<target>"` from Bash if PowerShell is blocked. `audit`'s symlink-integrity check flags a broken link.

For the full read-only/write-target map see `references/write-scope.md`.

## Page types

Eight page types under `Knowledge Base/`, each with a required frontmatter shape, naming rule, and location. **Full per-type spec + content shapes: `references/page-types.md`; frontmatter: `references/frontmatter.md`.** The behaviorally-load-bearing distinctions:

- **source** — raw input, `Sources/<type>/`. Two flavors (same frontmatter): *transcript* (content as-is) and *origination record* (Claude-written session-reasoning capture, `ingested_into:` listing what it produced).
- **research-note** — `Notes/Research/<scope>/`. Informal subtypes: *standard*; *hub* (orients a subsystem, links out; refresh on major ships); *snapshot* (point-in-time, drift OK, say "snapshot" in body).
- **insight** — cross-cutting lesson, `Notes/Insights/`.
- **failure** — failure mode, `Notes/Failures/`.
- **pattern** — reusable pattern, `Notes/Patterns/`. Use `projects:` (plural) when it spans products.
- **experiment** — hypothesis + protocol + primary data, `Notes/Experiments/<domain>/`.
- **production-log** — creative artifact + production knowledge, `Notes/Productions/<medium>/`.
- **entity** — typed node, `Entities/<entity-type>/` (People / Concepts / Libraries / Decisions).

### Research scope keys

The `project` field on a research note is one of:

- Umbrella / company: `substrate` (the company that owns Q, Endstate, Sift, and future products; also the landing page repo)
- Products: `q`, `endstate` (covers both `endstate` engine and `endstate-gui`), `sift`
- Activities: `tu` (Together Unprocessed podcast), `book-club`
- Domains: `health`, `finance`, `creative`, `science`, `travel`
- Cross-cutting / personal: **`personal`** — load-bearing in practice; covers anything not tied to a specific product, activity, or domain (vehicle profiles, household infrastructure, personal admin). Not a fallback for "I'm not sure"; pick the most-specific scope first.

Use `substrate` for company-level material — landing page, brand, positioning, infrastructure shared across products, business strategy spanning products. Use product-specific keys (`q`, `endstate`, `sift`) for product-specific work. If a thought turns out to belong at a different level, change the `project` field and move the file.

For **patterns** that apply across multiple products, use `projects:` (plural list) instead of `project:` (singular). The plural form is correct when the pattern's claim is genuinely cross-project (e.g., `projects: [endstate, q, substrate]`).

If you find yourself wanting a scope that isn't on this list, the writer auto-registers it (see below) — but pause to consider whether the new key is genuinely new or whether an existing key already fits. Avoid project-key sprawl: the typo guard catches single-/double-edit-distance mistakes, but doesn't catch *semantic* duplication of existing concepts under a new slug.

**Auto-registration of new project keys.** The `note`, `replace`, `edit` (frontmatter-patch mode), and `link` (for decision entities) writers auto-append unknown slug-shaped project keys to `_Schema/project-keys.yaml` and create the matching `Notes/Research/<Folder>/` directory on first use — no manual YAML edit needed. The registration surfaces as a warning in the write response (`"Auto-registered project key 'X' (folder: 'Y')"`). Pass `project_category` on the call to land the new key under the right bucket (umbrella / product / activity / domain / situation / cross-cutting); when omitted, the key lands as `uncategorized` and Hugo can hand-edit later. A **typo guard** rejects new keys within Levenshtein distance ≤2 of any existing registered key — `helath` raises `PROJECT_KEY_TYPO` with `"Did you mean 'health'?"` so the agent can self-correct instead of polluting the registry with typos.

### Q tenants

Q is a multi-tenant platform. When research is about a specific Q tenant, set `project: q` and add `tenant: <key>`. Current tenants:

- `example-tenant` — an example client's knowledge platform
- `tu` — Together, Unprocessed (the TU podcast also runs on Q)

**Disambiguating `tu`:** the same key `tu` is used both as a top-level project (when research is about the podcast as content/activity — episodes, guests, audience, narrative) and as a Q tenant key (when research is about TU's deployment on the Q platform — infrastructure, configuration, integrations). Disambiguate by the `project` field:
- `project: tu` → about the podcast as activity
- `project: q, tenant: tu` → about TU as a Q tenant deployment

If a tenant isn't on this list, surface it before assuming.

### Experiment vs production-log

Easy to confuse (both time-bounded, date-prefixed, with outcomes). **Experiment** = a hypothesis tested under a protocol with primary data (`Notes/Experiments/`); ends in confirm/refute/qualify. **Production-log** = a creative artifact + its production knowledge (`Notes/Productions/`); ends in engagement metrics + reflection, and the value is the thing made. Quick test: set out to *learn whether X is true* (experiment) or to *make a thing the world sees* (production)? Full treatment + the production-log-vs-research-note case in `references/page-types.md`.

## Workflow: typical add-then-compile session

1. **User pastes raw material or asks to log something.**
2. **Skill creates a `source` file.** Picks the subfolder from the input shape — `Sources/Articles/` (web/PDF), `Sources/Sessions/` (a pasted conversation), `Sources/Books/`, `Sources/Papers/`, `Sources/Videos/`, or `Sources/Other/`. Filename: ISO-date + slug. Frontmatter per `references/frontmatter.md`. Updates `Sources/index.md`.
   - **Videos are capturable** — file the *transcript* under `Sources/Videos/` (`source_type: video`, `url` required). YouTube usually exposes a transcript/captions panel, so it's a paste, no ASR needed. kb-mcp does not download or transcribe media itself; only a video with no available transcript would need an external ASR step first.
3. **Skill asks: "Compile a note from this? If yes, what type — research, insight, failure, pattern, experiment, production-log? And what scope (for research) / domain (for experiment) / medium (for production)?"** Skip if the user already specified.
4. **Skill drafts the compiled page** with frontmatter, sources block (linking back to the source file), wikilinks to existing entities/concepts where they obviously match, and a "Connections" section listing the wikilinks. **Run `suggest_links` on the draft (title + body) first** — it surfaces related existing pages (hub-preferring) you'd otherwise miss; fold the relevant ones into Connections.
5. **Skill shows the draft, waits for confirmation.** User can revise inline.
6. **On confirm: writes the page**, updates the relevant `index.md`, appends to `log.md`, and reports paths. The `note` result carries a `suggestions` block (related pages not yet cited) and any near-duplicate `warning` — review them and wire in the relevant links via **edit** (or, if it's a genuine duplicate, prefer `replace`/`append` over a parallel page).

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

**Canonical wikilink form: full vault-rooted.** Every wikilink resolves cleanly under the vault root with no prefix guessing.

- KB-internal targets: `[[Knowledge Base/Entities/Concepts/Profile]]`, `[[Knowledge Base/Notes/Patterns/specs-as-canonical-behaviour-for-ai-assisted-development]]`.
- Curated-tree targets (vault-relative, no `Knowledge Base/` prefix because they don't live there): `[[Cognitive Core/Strategy]]`, `[[Domains/Domain - AI Systems & Architecture]]`, `[[Products/Q/Strategy]]`.
- Link back to the originating `Sources/` file via the `sources:` frontmatter list (mirrors the source's `ingested_into:` list).

**The writer normalizes on your behalf.** kb-mcp's writers (`note`, `link`, `edit`, `replace`, `create_file`) run every wikilink through `vault.normalize_wikilink()` before writing — bare names, KB-relative paths, `.md` suffixes, and stale paths get rewritten to canonical full form. Bare names also resolve against frontmatter `title:` so `[[North-Led Content Manual]]` finds the date-prefixed source whose title matches. You can write in any form; the on-disk file lands canonical.

If a wikilink target doesn't exist yet, prefer creating the entity stub via the **link** operation rather than leaving a dangling link. Dangling links accumulate and surface in **audit** as `broken_wikilink`.

### Pointer entities vs mirror entities

When creating an `Entities/Libraries/` or `Entities/Concepts/` page that references a **currently-evolving external artifact** (operational skill, code library, live service config, live spec in another doc system), use **pointer-style** — summary + canonical-source pointer + connective tissue — not **mirror-style** (versions, file inventories, command lines, subtype tables, workflow steps copied verbatim). Mirroring guarantees drift. See [[Knowledge Base/Notes/Patterns/pointer-entities-for-live-artifacts]] for the worked discipline.

Frozen things (Sources captures, decisions about past events) and KB-native content (insights, patterns, failures, research-notes) are explicitly out of scope — the KB *is* the source of truth for those.

## Audit (lint) checks

The **audit** operation runs read-only checks and proposes fixes (never auto-fixes); the report is reviewed before anything is written. It covers: orphans, broken wikilinks, supersession integrity, stale frontmatter, `index.md`/`log.md` drift, aged unprocessed sources (oldest-first — pair with `propose_compilation`), status/location mismatch, unfinished experiments, stalled production lifecycles, stale hubs/snapshots, harness symlink integrity, unregistered project keys, embedding drift, and pending relevance pairs.

Per-check detail — exactly what each flags, its severity, and the proposed fix — is in **`references/audit-checks.md`**. Read it when running or acting on an audit.

## What this skill does NOT do

- Touch anything outside `Knowledge Base/` (the dual-write exception for `~/.claude/skills/knowledge-base/` under hand-edits of `_Schema/` is the only carve-out — see rule 8).
- Auto-compile *blindly* after every capture. Compilation is a deliberate step taken at a stepping-stone (see Proactive engagement) — under the standing waiver it needs no per-note approval, but it stays a judgment call and is always reported, never a silent dump of every passing remark.
- Assign numeric confidence scores. Use citation count and recency as the trust signal.
- Apply retention decay or "forgetting curves." Old material stays. If superseded, mark it; if irrelevant, archive into a `_archive/` subfolder of its current location.
- Run on hooks, schedules, or background triggers (claude.ai can't, and we don't want automated writes). Operations happen because the user asked, or because the conversation reached a point where consulting or capturing is clearly warranted — see Proactive engagement.
- Modify `Sources/` or `Evidence/` files after creation. Mistakes get superseded, not edited.

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
- Proactive `find` for context (read-only) — see Proactive engagement.
- Capturing a clear stepping-stone conclusion whose type and scope are unambiguous — write under the standing waiver and report the path.
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
- `references/audit-checks.md` — per-check detail for the audit operation

Read each on first use. The SKILL.md you're reading now is the contract; the references are the manual.
