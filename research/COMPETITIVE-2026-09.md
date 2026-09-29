# Competitive analysis: Exomem vs Supermemory, Mem0, Zep/Graphiti, Letta, Basic Memory

Date: 2026-09-29. Scope: research only, no code changes.

## Read this first: evidence quality

This session's egress proxy blocked supermemory.ai, docs.supermemory.ai, docs.mem0.ai, mem0.ai, help.getzep.com, getzep.com, letta.com, docs.letta.com, basicmemory.com and docs.basicmemory.com. What was actually readable:

- GitHub READMEs/changelogs for Supermemory, Mem0, Graphiti (+ its MCP README), Letta, Basic Memory (accessed 2026-09-29).
- Web-search snippets of vendor pages. These are **secondary** and marked "(snippet)" below. **All pricing is snippet-derived; verify on the live pages before quoting.**
- Exomem's own `docs/capabilities.md`, `README.md`, `docs/product-gap-matrix.md`, `openspec/specs/` (read in this repo).

Anything I could not confirm is `unverified`. Vendor benchmark numbers are vendor-claimed and not independently checked. Exomem's own benchmark story has a caveat too: its graph-value comparison against Basic Memory was **withdrawn 2026-08-09** (`docs/product-gap-matrix.md`), so no cross-product win claim from that is citable.

Primary URLs (all accessed 2026-09-29): https://github.com/supermemoryai/supermemory · https://github.com/mem0ai/mem0 · https://github.com/getzep/graphiti (+ `/blob/main/mcp_server/README.md`) · https://github.com/letta-ai/letta · https://github.com/basicmachines-co/basic-memory (+ `CHANGELOG.md`, PR #1618, `integrations/pi/README.md`, `integrations/tau/README.md`, issue #1487) · https://pypi.org/project/basic-memory/ · https://pi.dev/ · snippets from supermemory.ai/pricing, supermemory.ai/docs/concepts/graph-memory, supermemory.ai/changelog/2026-06-24-chrome-extension-imports-memory-profiles, docs.mem0.ai/platform/mem0-mcp, mem0.ai/pricing, getzep.com/pricing, blog.getzep.com (scaling and SOTA posts), docs.letta.com (context-hierarchy, plans).

## 0. Basic Memory v0.24.0: Pi and Tau

- **Pi** is the Pi coding agent, an MIT-licensed terminal agent extended via TypeScript extensions/packages (https://pi.dev/, https://github.com/earendil-works/pi). Basic Memory ships `integrations/pi` (`bm install pi`; tools `bm_recall`, `bm_capture`; auto recall/capture only in trusted workspaces).
- **Tau** is an agent client that loads extensions from `~/.tau/extensions` (`bm install tau --sync`): `bm_<tool>` passthrough, `/bm-checkpoint`, `/bm-orient`, `/bm-remember`, lifecycle hooks (session start, settle, pre-compaction, shutdown), session summaries kept separate from raw transcripts. Exactly which "Tau" project this is: **unverified** (README cites "Stock Tau 0.4.1"; a summarizer guessed huggingface/tau; another candidate is a Pi-based coding agent).
- Pattern: Basic Memory now treats agent lifecycle hooks (checkpoint before compaction, orient at start) as a product surface. Exomem has this for Claude Code and Codex (README), so it is parity, except for Pi/Tau/Hermes/OpenClaw.
- v0.24.0 other items (CHANGELOG/PR #1618): POSIX tools (`cat/grep/ls/find/tail/man`, projects as mount points), deterministic wiki projection (index/log files), PDF ingestion with citations plus Word/PowerPoint/CSV sidecar notes, partitioned vector indexes, compare-and-swap writes, locked notes, `bm prune`, "temporal qualifiers" (semantics unverified). PyPI still showed 0.23.2 at fetch time.

## 1. Feature matrix

`?` = unverified from primary sources reachable today.

| Dimension | Exomem | Supermemory | Mem0 | Zep / Graphiti | Letta | Basic Memory |
|---|---|---|---|---|---|---|
| Ingestion | Markdown in place; PDF, Office, images (OCR), audio/video (ASR, scene frames, diarization) locally; `adopt_vault`, Adoption Studio. **No SaaS connectors** | PDF, images, video, code (README); connectors Drive/Notion/OneDrive/Gmail/S3/web crawler (snippet); Chrome ext imports from ChatGPT/Claude/Gemini/Grok (snippet) | Text/chat `add`; formats/multimodal `?` | Episodes (text/JSON/messages); multimodal `?` | Conversation + agent-written blocks/passages; files `?` | Markdown; PDF (+citations), Word/PPT/CSV sidecars (v0.24); importers |
| Storage model | Plain Markdown vault + SQLite/FTS5 + vector sidecar; user owns files | Hosted memory/graph store; local binary stores in `./.supermemory` | Vector store (e.g. Qdrant) + entity linking | Temporal KG on Neo4j/FalkorDB/Neptune/Kuzu | Core blocks / archival / recall in Letta DB | Plain Markdown + SQLite/Postgres index |
| Retrieval | Hybrid (BM25 + dense), optional rerank, graph enrich, unit-level recall, structured filters | Hybrid RAG + memory (README); rerank `?` | Semantic + BM25 + entity (README); rerank `?` | Semantic + BM25 + graph traversal (README); rerank `?` | Semantic archival search; hybrid `?` | FTS + FastEmbed vector + hybrid; optional cross-encoder rerank |
| Context assembly | `activate_context` (query-less compiled working-memory packet, abstains), context packs, `bootstrap` | Auto user profile (`/v4/profile`, snippet) | none confirmed | "Context block" (from memory, `?`) | Core memory blocks are always in context | `build_context` over `memory://` + wikilinks |
| Temporal / supersession | `replace_memory` atomic supersession, history via `get(include_history)`, review queues; no confirmed as-of query | "Updates/Extends/Derives" relations; new fact supersedes for retrieval, history kept (snippet) | `?` | Validity windows, invalidate-not-delete (README); bi-temporal per paper | none | v0.24 "temporal qualifiers" (`?`) |
| Provenance / evidence | Separate Sources / Evidence (append-only, hashed) / compiled notes; provenance report; unit refs | `?` | `?` | Every entity/edge traces to source episodes (README) | `?` | PDF citations; else `?` |
| Governance / audiences | Opt-in confidential governance policy (`govern_memory`: audiences, ceilings, grants, TTL sessions), review inbox, contradiction queue | Container tags as isolation; scoped keys (snippet) | user/session/agent scopes; RBAC only at Enterprise (snippet) | `group_id` namespaces; RBAC `?` | Shared blocks `?` | Projects; locked notes |
| Typed memory kinds | Governed semantic kinds (decision, etc.), note types, entity types, Records, Planning | Update/extend/derive relations; typed memories `?` | user/session/agent scope; kinds `?` | Preference, Requirement, Procedure, Location, Event, Person, Organization, Document, Topic | core/archival/recall | observations + relations, note types |
| User-defined schemas | `schema_memory` infer/validate/diff, saved contracts, workflow contracts, relation/vocabulary evolution | metadata filters; schemas `?` | custom categories `?` | Pydantic entity/edge types | n/a | `schema_infer/validate/diff` |
| MCP / clients | MCP + REST + CLI parity; Claude Code, Codex (hooks+skills), Cursor, claude.ai/ChatGPT via remote MCP (manual skill upload) | Hosted MCP; Claude Desktop/Code, Cursor, Windsurf, VS Code, OpenCode, OpenClaw, Hermes; Codex/ChatGPT not listed | Hosted MCP: Claude, Claude Code, Codex, Cursor, Windsurf, VS Code, OpenCode (snippet); ChatGPT `?` | MCP server (14 tools): Claude Desktop, Cursor, VS Code; Codex/ChatGPT `?` | 3rd-party MCP only found; native `?` | Claude, Claude Code, Codex, Cursor, VS Code, ChatGPT, Pi, Tau, Hermes, OpenClaw |
| Hosted vs self-host | Self-host first; managed single-vault "cells" designed, gateway lives outside this repo; no public pricing found | Cloud + "Supermemory Local" binary (README) | Cloud, self-host lib/server, OpenMemory | Zep cloud; Graphiti self-host | Letta Cloud; server repo archived, dev moved to `letta-code` | Local free; Cloud sync |
| Latency claims | Hybrid `find` 864 ms @ 50k notes, reference desktop (own benchmark) | 187 ms server / 356 ms e2e search (snippet); ~50 ms profile (README) | ~0.9-1.1 s p50 (README) | P95 ~200 ms context, ~150 ms graph (vendor blog snippet) | `?` | `?` |
| Pricing | Free/OSS; no hosted price found | Free; $19; $100; $399; Enterprise (snippet) | Free; $19; $249; Enterprise (snippet) | Free 10k credits; $125; $375; Enterprise (snippet) | Free; $20 Pro; API plan $20 + usage (snippet) | Local free; Cloud ~$15/mo (PyPI text) |
| License | AGPL-3.0-or-later | MIT (README, LICENSE file not read) | Apache-2.0 | Graphiti: `?` (believed Apache-2.0) | Apache-2.0 | AGPL-3.0-or-later |

Vendor benchmark claims (unverified; methodologies differ): Supermemory LongMemEval "95% R@15"; Mem0 LoCoMo 92.5, LongMemEval 94.4; Zep DMR 94.8%, LongMemEval 71.2%. Exomem has a small self-graded golden set and no LongMemEval/LoCoMo number.

## 2. Exomem: differentiators and gaps (blunt)

**Real differentiators** (each backed by shipped surface in `capabilities.md`/specs)

1. **You own the substrate.** Files stay Markdown/Obsidian; nothing is imported into a proprietary store. Only Basic Memory shares this; nobody else does.
2. **Epistemic governance is the deepest in the set.** Sources vs compiled notes vs append-only hashed Evidence, atomic supersession, semantic units with stable refs, contradiction queue, attention/Epistemic Inbox, Review Studio, provenance report. Others store facts; Exomem stores facts *plus what justified them and what replaced them*.
3. **Confidential governance** (audiences, ceilings, grants, TTL sessions) is opt-in and real. Competitor governance is mostly tenant isolation.
4. **Context compiler.** `activate_context` builds a bounded packet from a raw turn with no query and can abstain. Closest rival is Supermemory's profile; that is a different, generic-summary idea.
5. **Broad local multimodal** (OCR, ASR, diarization, video scene frames) with zero third-party upload.
6. **Client-agnostic with agent-behaviour engineering**: prominence levels, hooks, compaction checkpoints, bootstrap contract, Claude Code plugin.

**Real gaps**

1. **Time-to-value.** Its own gap matrix rates fresh setup "Behind". Supermemory/Mem0 are a key and one API call; Exomem is a vault, a wizard, models.
2. **No connectors or importers.** No Drive/Notion/Gmail sync, no import-from-ChatGPT/Claude. This is Supermemory's headline and Exomem has nothing comparable.
3. **No hosted product you can buy today** (in this repo the gateway/billing live elsewhere; no public pricing found). Cloud-first buyers cannot try it.
4. **No headline benchmark.** LongMemEval/LoCoMo are the table stakes buyers quote; Exomem has a tiny self-graded set, and its one cross-product comparison was withdrawn.
5. **Surface complexity.** 32 commands, dozens of parameters (`ask_memory` has ~35), a heavy contract. Competitors sell two calls: add and search.
6. **Write friction.** Semantic-unit requirements gate every active note. Great for quality, hostile to "just remember this".
7. **Automatic extraction is deliberately absent** ("measures, never judges"): reasoning stays in the client model, so there is no fact-extraction-from-conversation pipeline like Mem0/Supermemory/Zep. Defensible, but it is the reason a naive user sees "it doesn't remember anything on its own".
8. **Ecosystem reach.** No Pi/Tau/Hermes/OpenClaw/Windsurf-class integrations; ChatGPT/claude.ai have no hooks so capture is skill-driven and manual to install.
9. **Temporal queries.** Supersession and history exist, but I found no as-of/valid-time query surface comparable to Graphiti's validity windows. Treat as a gap until shown otherwise.
10. AGPL and a heavy Python/model install may deter commercial embedding (Supermemory MIT, Mem0/Letta Apache).

## 3. What Supermemory does that users love, and does Exomem match it?

Messaging is from the README and search snippets (site itself unreachable); "love" here means what the vendor leads with, not measured sentiment.

| Supermemory pitch | Exomem today |
|---|---|
| "No vector DB config, no embedding pipelines, no chunking" (zero-config API) | **No.** Setup wizard, doctor, model downloads. |
| One memory across every assistant (MCP, plugins, Chrome extension) | **Partly.** Same vault across MCP clients, yes. Consumer capture path (browser extension) no. |
| Import your ChatGPT/Claude/Gemini memory (Chrome ext, snippet) | **No.** Not found in the command registry. |
| Connectors (Drive, Notion, Gmail, S3, crawler) | **No.** |
| Auto user profile, ~50 ms | **Different.** `activate_context` is heavier, and query-less; no cheap always-on profile blob. |
| Speed (sub-400 ms e2e) | **Comparable on a warm local box**, unmeasured hosted. |
| Graph with update/extend/derive relations | **Ahead on rigor** (typed relation registry, review), **behind on automatic extraction.** |
| Benchmark #1 claims | **No number.** |
| Local option | **Match, arguably better** (files, not a data dir). |
| Free tier / clear pricing | **No.** |

Net: Exomem wins on trust, governance and ownership; it loses on first-five-minutes experience, input breadth and marketing proof. Different buyers: Supermemory sells memory-as-an-API; Exomem sells an auditable second brain.

## 4. What to adopt or build (ranked by user value per effort)

1. **Publish a real LongMemEval/LoCoMo run with a reproducible script.** Highest value per effort: harness infrastructure already exists (`scripts/eval_retrieval.py`, benchmark-protocol spec). Report honestly, including where compiled-note discipline hurts. Without a number Exomem is invisible in every comparison thread.
2. **Two-call "quick memory" front door.** A `remember`/`ask` path that accepts plain text with auto-generated semantic unit and defaults, plus a `quickstart` that is one command to a working demo on the user's own notes. Attacks gaps 1, 5, 6 at once. Keep governance as the upgrade path, not the entry fee.
3. **Import from other assistants and note apps.** ChatGPT/Claude export files, Notion export, Obsidian already covered. Local file import is cheap (no OAuth, no hosted service) and answers the Chrome-extension pitch without building an extension. `adopt_vault`/Adoption Studio is the base.
4. **Ship one hosted trial with public pricing.** The cell architecture exists; the missing piece is a URL and a free tier. Even a waitlisted "managed Exomem" removes the "cloud-only buyers can't try it" objection.
5. **A lightweight always-on profile/working-set artifact.** Cheap to derive from existing compiled notes and the activation packet, cached, and injected by hooks. Gives the Supermemory-profile experience without abandoning the governed model. Then extend integrations to Pi/Tau/Hermes/OpenClaw by reusing the shared hook contract (Basic Memory just did this in v0.24).

**Do NOT chase**

1. **A full connector marketplace (Gmail, Drive, Notion sync, S3).** OAuth, rate limits, privacy review, and it collides with "files you own" plus the privacy gate. Import files, don't sync SaaS.
2. **Server-side auto-extraction with a hosted LLM.** It would reverse the "measures, never judges" boundary, make hosted cost/latency unpredictable, and turn Exomem into a Mem0 clone with weaker provenance.
3. **A general agent runtime (Letta-style self-editing memory blocks) or a canvas/editor UI race.** Different product category; Basic Memory's editor/canvas breadth is already conceded in Exomem's own matrix. Studio is enough until usage is measured.

## Caveats

Nothing was checked against live vendor docs or pricing pages (blocked); re-verify before external use. Exomem claims come from its own docs and specs; I did not run its code or benchmarks.
