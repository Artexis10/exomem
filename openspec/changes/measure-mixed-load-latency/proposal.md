## Why

The proportional-write benchmark verified useful closure but did not establish
graph catch-up time or read/write tails during real media work. The user also
requires current Exomem to match or improve on current Basic Memory for accepted
writes, reads, graph behavior, resilience, performance, and consistency. Establish
that comparison with explicit outcome checks before choosing runtime changes.

## What Changes

- Add an opt-in, isolated public-MCP diagnostic comparing the same foreground
  edit/read/search loop with and without a declared burst of pinned PDF/OCR jobs.
- Retain individual client monotonic timings, correctness, observed media overlap,
  startup proof, graph catch-up evidence, and exact runtime/fixture provenance.
- Pin both repositories' current remote default revisions and build their locked
  dependencies. Keep prior released-wheel measurements historical.
- Compare the same public Markdown edits, exact reads, keyword visibility,
  relation replacement, concurrent accepted writes, and restart durability.
- Observe accepted Markdown file contents separately after the process exits
  and after restart; retain database-to-file lag without delaying the crash.
- Track repeated observations and failures; compare measured source behavior.
  Basic Memory's byte/count batch helper currently has no production callers.
- Keep production behavior unchanged. The diagnostic uses existing deterministic
  PDF/OCR transducers, with embeddings/CLIP disabled; missing extraction dependencies
  produce blocked evidence, never a synthetic completion or a timing pass.

## Capabilities

### New Capabilities

### Modified Capabilities

- `durable-closure-performance`: mixed-load timing and explicit graph catch-up proof.

## Impact

Benchmark scripts, focused harness tests, an opt-in CI workflow, and tracked benchmark reports.
No live service deployment or runtime scheduling change in this measurement
change. Proven product gaps become explicit OpenSpec repair work before parity
can be claimed; the benchmark is not itself completion of that broader objective.
