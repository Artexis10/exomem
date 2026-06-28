# Design — Order the contradiction review queue

## Context

`audit._check_corpus_contradictions` already sweeps every active read-write
compiled conclusion against the embedding sidecar and reports deduped, unordered
file pairs whose max chunk-cosine lands in `[floor, dup_threshold)`. It is
**measurement-only** (cosine can't separate "X works" from "X doesn't"; the
reader reconciles). The defect is purely *presentation*: ~877 flat pairs sorted
by cosine, dominated by same-family `Notes/Research/<X>/*-architecture`
adjacency. We reorder + cap the surfaced list; we change nothing about what is
measured.

## Goals / non-goals

- **Goal:** surface the most-worth-reviewing pairs first; demote same-family
  architecture-cluster noise; cap the default set with an explicit omitted count.
- **Goal:** reuse the `stale_review` ACT-R machinery rather than reinvent a
  dormancy signal; no new sidecar.
- **Non-goal:** any mutation, auto-supersede, or change to `find` ranking. This
  is the same pure-substrate measurement, only ordered.
- **Non-goal:** changing the eligible set, the band edges, or the dedup.

## Why dormancy belongs in the priority

A close pair where both notes are fresh and frequently surfaced is probably a
known, deliberate restatement — low review value. A close pair where one note is
**dormant** (old, never re-surfaced in `find`, never cited) is the high-value
case: "is this still true, or did I forget I already concluded the opposite?"
So the most-forgotten endpoint of a pair *raises* its priority. Dormancy is
exactly what `stale_review` already computes via ACT-R base-level activation, so
we reuse it — no second model, no new telemetry.

## The formula (exact)

For each surfaced in-band pair `(a, b)` with max chunk-cosine `cos ∈ [floor, ceiling)`:

1. **Per-note ACT-R activation** (reusing `_stale_access_events(today)`,
   `_stale_activation_params()`, and `_activation()`):

   ```
   B(note) = ln( Σⱼ wⱼ · Δtⱼ^(−d) )
   ```

   over the note's weighted access events — find-surfacings (`w_surfaced`),
   get-reads (`w_read`), citations (`w_cited`) — with decay `d` (default 0.5).
   Higher `B` = more recently/often accessed = **less** dormant.

2. **Per-note dormancy**, squashing activation into `[0, 1]`:

   ```
   dormancy(note) = 1.0                     if B is None
                  = 1 / (1 + e^B)           otherwise
   ```

   `B` is `None` when the note has no access events (never accessed) **or** the
   access signal is gated/unavailable (`KB_MCP_DISABLE_RELEVANCE_CHECK`, or no
   log) — in both cases the note is treated as maximally dormant (`1.0`), never a
   fabricated "active". A highly-active note (large `B`) → `~0`; a barely-active
   note (very negative `B`) → `~1`.

3. **Pair dormancy** = the most-forgotten endpoint:

   ```
   pair_dormancy = max( dormancy(a), dormancy(b) )
   ```

4. **Review priority**:

   ```
   priority = cos + W_DORMANCY · pair_dormancy
   ```

   `cos` is the proximity strength (closer → higher). `W_DORMANCY` defaults to
   `0.5` (env `KB_MCP_CONTRADICTION_W_DORMANCY`). Because `cos` occupies a narrow
   band (~`[0.5, 0.93)`) while `pair_dormancy ∈ [0, 1]`, a dormant pair earns up
   to `+0.5`, enough to let a forgotten close pair outrank a fresher equally-close
   pair, while cosine still anchors the base ordering.

## Sort + demotion

Sort key (ascending) per surfaced pair:

```
( same_family, -priority, a, b )
```

- `same_family` is `False`/`True`, so all cross-family pairs sort *before* every
  same-family pair, regardless of priority — the architecture-cluster noise sinks
  to the bottom of whatever survives the cap.
- within a family-bucket, `-priority` orders most-actionable first; `(a, b)`
  breaks ties deterministically.

`same_family` is `True` iff both notes share the same `Notes/Research/<X>/`
subfolder. `_contradiction_family(rel_path)` returns `"Notes/Research/<X>"` for
a path under that tree (after stripping the `Knowledge Base/` prefix) and `None`
otherwise; the pair is same-family when both families are non-`None` and equal.
The flag is recorded on `meta.same_family` so a client can style/skip it.

## Cap + explicit count

`KB_MCP_CONTRADICTION_TOP_N` (default 40) caps the surfaced pairs. After sorting:

- `top_n > 0` and `len(ordered) > top_n` → surface `ordered[:top_n]` and append
  ONE summary finding (no `paths`): `meta.truncated = omitted`,
  detail = `"<omitted> more lower-priority/same-family contradiction pair(s) not
  shown (showing top <shown> of <total>; raise KB_MCP_CONTRADICTION_TOP_N or set
  it to 0 to see all)."`. **No silent truncation.**
- `top_n <= 0` → uncapped; surface all, no summary finding.

The summary finding carries no `paths`, so pair-oriented consumers (and the
existing tests' `_pairs()` helper) ignore it.

## Decisions

- **Reuse, don't reinvent.** Dormancy is the existing `stale_review` ACT-R calc;
  the only new code is the squash, the pair-combine, the family check, and two
  env readers. Same decay/weight knobs (`KB_MCP_STALE_*`).
- **`max` over the pair, not `min` or mean.** The review trigger is "*one* of
  these is forgotten" — the most-dormant endpoint should pull the pair up.
- **Demote, don't drop.** Same-family pairs are still real measurements; they're
  sorted last and (when the cap bites) summarized, never silently removed. With
  the cap disabled they all still appear.
- **Cosine stays in `meta`.** The existing `meta["cosine"]` key is preserved so
  prior consumers/tests are unaffected; `priority`, `dormancy`, and `same_family`
  are added alongside it.
- **Gated/torch-less safety.** When `KB_MCP_DISABLE_EMBEDDINGS` is set the whole
  category still short-circuits to `[]` before any of this runs. When the access
  signal is gated, dormancy degrades to `1.0` for all notes, so ordering falls
  back to pure cosine (with same-family still demoted) — deterministic, no crash.

## Risks

- The `+0.5` dormancy weight could over-promote a barely-close dormant pair over
  a very-close fresh pair. Mitigated: `W_DORMANCY` is env-tunable, and cosine
  still dominates within ~0.5 of band width; the default is a starting point to
  tune against the live vault.
- Family detection is path-shaped (`Notes/Research/<X>/`); other architecture
  clusters outside that tree won't be demoted. Acceptable — that subtree is the
  documented noise source; widening it later is a follow-up, not a correctness
  gate.
