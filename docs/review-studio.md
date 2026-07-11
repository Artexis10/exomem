# Epistemic Review Studio

The Review Studio is Exomem's browser control plane for the daily epistemic
loop: inspect the ranked Inbox, opt into corpus activation, understand why a
signal surfaced, and make an explicit governed decision. It is packaged inside
the Python distribution and served by the existing Exomem process. There is no
CDN, Node runtime, frontend daemon, or separate database.

## Quickstart

Start the HTTP service as usual, then print the Studio URL:

```sh
exomem --transport http
exomem studio
```

The default is `http://127.0.0.1:8765/studio/`. `exomem studio --open` opts into
opening the system browser; the plain command only prints the URL and is safe on
headless hosts. `EXOMEM_BASE_URL` or `--url https://kb.example.test` selects a
remote origin.

The shell is public but inert: it contains no vault titles, paths, counts, note
text, graph data, or review state. Vault data crosses the boundary only through
same-origin `/api/*` commands. Set `EXOMEM_REST_API_KEY`, enter that personal key
in the connection screen, and the browser retains it in memory or
`sessionStorage` for the current tab session. It is never placed in a URL,
rendered HTML, persistent cookie, or `localStorage`. On a Cloudflare Access
deployment, use the Access option; the edge assertion is checked by the existing
REST authorization path.

## What the worklists mean

Inbox is the default daily view. It displays `review_memory(mode="attention")`
in exact server order, with recorded categories, reasons, state, summary counts,
and every reported cap. The browser filters the returned view but does not
recalculate rank, severity, confidence, or epistemic meaning.

Activation is separate and opt-in. It displays
`review_memory(mode="activation")`, including denominator-backed structural
coverage. It does not mix activation debt into the Inbox. Activation depends on
the existing-corpus activation scanner; if that command is unavailable, the
Inbox remains usable.

Relations is the batched acceptance queue. It displays
`review_memory(mode="relation-queue")` — deterministic relation candidates
grouped by page, already filtered against authored edges, placeholder targets,
and unexpired dismissals — with the same capped-surfacing honesty as the other
worklists. Its identities are namespaced (`exomem://review/relation/<id>`) so
triaging a relation candidate never resolves an Inbox or Activation item.
Per-candidate Accept requires an audit reason and the reviewed fingerprint;
Dismiss and Snooze record fingerprint-bound decisions that expire when the
underlying signal materially changes.

Selecting an item calls `review_item_context` with its stable
`exomem://review/<id>` reference and current fingerprint. The bounded response
contains the target body, exact reasons, related summaries, provenance/evidence,
graph neighborhood, audit history, and pointer-ordered supersession evolution.
Empty, unavailable, and truncated sections stay visibly distinct. The Studio
does not fetch unrestricted related bodies to fill gaps.

## Writes are confirmation flows

Dismiss, snooze, and reopen use `triage_memory`. Before writing, the Studio
rechecks the selected fingerprint; a changed signal refreshes the worklist and
performs no action.

Conclusion-changing work has a proposal step and a separate confirmation:

- Relation suggestions come from read-only `connect_memory`; single-candidate
  acceptance is an audited `edit_memory` call, and queue acceptance is the
  governed `connect_memory(operation="accept-relation")`, which revalidates the
  candidate's fingerprint, the target page hash, and live eligibility before
  authoring exactly one canonical `## Relations` bullet.
- Source compilation starts with read-only `compile_source`; the editable draft
  becomes knowledge only through confirmed `remember`.
- Supersession previews the exact target, successor draft, reason, and
  consequence before confirmed `replace_memory` preserves the old version.

Validation failures leave the dialog and draft intact. Proposal generation and
cancellation never mutate the vault. Existing server validation, expected-hash
guards, write logs, access policy, and audit behavior remain authoritative.

## Recorded evolution, not generated narrative

The Evolution panel displays stored supersession pointers in order, with
recorded dates, structural claims, transition reasons, paths, and canonical
references. A single-version conclusion produces an honest empty state. The
client does not invent an explanation, confidence score, authority score, or
causal link from edit dates or semantic similarity.

## Local and remote deployment

Local use needs only the existing HTTP service. Remote personal use should put
the same service behind the documented Cloudflare Tunnel and Access boundary;
see [remote-quickstart.md](remote-quickstart.md) and
[deployment.md](deployment.md). Keep `/studio/` and `/api/*` on the same origin.
The CSP rejects external scripts, styles, frames, and cross-origin API calls.

Missing Studio assets return a bounded 503 diagnostic only on Studio routes.
MCP, REST, CLI, favicon, retrieval, and service startup remain independent.

## Deliberate non-goals

This is not a generic notes editor or Basic Memory clone. The first release has
no rich Markdown editor, file manager, canvas, cloud sync, multi-user workspace,
CRDT collaboration, billing, public sharing, background agent, or automatic
cleanup. Its job is narrower and more valuable: make Exomem's governed review,
provenance, activation, explicit decisions, and recorded belief evolution
legible as one end-to-end product loop.
