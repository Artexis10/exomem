## Context

`find()` ranks pages with several lanes: BM25, vector, CLIP, graph and the
conjunctive keyword lane. A candidate produced only by BM25 is a false
-positive risk — BM25's word-level tokenizer surfaces pages that share a
single token with the query — so `_supported_stems` vetoes it unless it
passes a lexical corroboration gate.

The gate has two forms today:

- **Semantic lanes live** (any vector or CLIP candidate exists):
  every query stem must appear in the page's stem set.
- **Semantic lanes absent** (no vector or CLIP candidate at all): a
  strict majority of the query's words must be present — whole-word
  presence, where a word's every BM25 subtoken stem must occur — and at
  least one present word must be a content word.

The lanes-absent form was designed for the degraded profile: with no
semantic evidence anywhere, retention falls back to lexical corroboration,
and an all-stems veto would return empty results for answerable questions.
The same question phrasing that triggered the degraded fix also vetoes
candidates when the lanes are live but simply did not rank this page — the
vector lane's absence of a hit is not evidence against the BM25 candidate.

## Decision

Make the gate per-candidate: **any candidate that only the BM25 lane
produced must pass the strict word-majority plus content-word gate**,
regardless of whether the semantic lanes are live.

Alternatives considered:

- **Keep all-stems when lanes are live (status quo).** Rejected: the veto
  conditions on an aggregate property of the candidate bundle, not on
  evidence about this candidate. A page the vector lane missed for ranking
  reasons (phrasing mismatch, chunk boundary, score below cutoff) is
  indistinguishable from a page the lane correctly rejected.
- **Require all content-word stems instead of majority.** Rejected: the
  measured defect is exactly this shape — ordinary question phrasing
  ("after", "turn") is tokenized as content words and is still absent from
  a correct page. Function-word classification cannot predict which words
  a target lacks.
- **Drop corroboration when a vector lane is live.** Rejected: the
  single-token false-positive risk the veto exists for is real, and lanes
  being live does not reduce it for BM25-only candidates.

## Consequences

- More BM25-only candidates survive the veto: a page holding a strict
  majority of the query's words, including at least one content word, is
  retained even when the vector lane is live and did not rank it. Ranking
  still orders by the same signals; the gate only removes the weakest
  candidates.
- Precision is preserved by the two existing guards: exactly-half is
  still vetoed, and function-word majorities alone retain nothing.
- The degraded profile is unchanged — it already uses this gate.
- The recall canary's natural-question cases can exercise the live-lanes
  path they were written for.
