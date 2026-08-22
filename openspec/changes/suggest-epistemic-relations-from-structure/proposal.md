# Suggest Epistemic Relations From Authored Structure

## Why

The four shipped relation-suggestion generators emit only `links_to`,
`derived_from` and `relates_to`, so an epistemic-family suggestion is
structurally impossible. The measured consequence in a real vault: 58% of
registered relation uses are `relates_to`, 188 relation-debt findings, and 35%
typed coverage.

The gap is not a modelling problem — it is a visibility problem. A typed unit
relation authored as `- relations: answers: [[Q]]` produces a **block-level**
edge and no page-level edge at all, while a plain `- supports [[X]]` bullet
inside the same block produces exactly the opposite. The scaffold documents the
metadata form as *the* way to write typed unit relations, so every one of them
is an author-written directional epistemic claim that the page-level graph,
relation-filtered recall, and contract inference cannot see. Nothing proposes
promoting it.

Two further structural facts are already materialized in the graph sidecar and
unused by any suggester: two pages can carry the same open question, and two
pages can each answer or resolve the same target. Both are real shared
observations that mirror the existing `shared_sources` method.

## What Changes

- Add three deterministic relation-suggestion generators to `suggest_relations`,
  reading the typed graph sidecar through **one** shared validated read
  snapshot and soft-failing to no candidates when the sidecar is unavailable:
  - `unit_relation_lift` — promote a kind the author already typed on one of
    this page's own semantic units to a page-level proposal, when no page-level
    edge of that kind to that target exists. Gated to an allowlist of relation
    **families** resolved through the registry at call time, and to registry
    statuses `core`, `alias` and `extension`.
  - `shared_open_question` — two pages carrying the same normalized open
    question, proposed as `relates_to`.
  - `shared_resolution_target` — two pages whose semantic units each `answers`
    or `resolves` the same target, proposed as `relates_to`.
- Register the three after the existing deterministic generators and before the
  optional embedding lane, because `suggest_relations` truncates at `limit` and
  the wikilink generator is unbounded.
- Carry the driving page's unit identity (`unit_ref`, anchor, relation kinds
  used) in each candidate's evidence, so a dismissed candidate resurfaces when
  the page that produced it changes.
- Aggregate to one candidate per `(target, relation type)` with all matches
  folded into evidence, because candidate deduplication keys on
  `(from, to, relation_type, method)` and excludes evidence.

## Non-Goals

- No fragment (unit-level) relation targets. Targets stay page-level, which is
  why the two co-participation generators propose `relates_to` and nothing
  narrower — with a page-level target, `- duplicates [[B]]` would assert that
  the *pages* duplicate, which is false when only their question units do.
- No sidecar schema change, no rebuild, no write-path work, and no change to
  the acceptance queue, the command registry, or the pinned tool surface.
- No model-backed suggestion path. `model_suggestions_available` stays `false`.
