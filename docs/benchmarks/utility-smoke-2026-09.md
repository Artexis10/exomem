# Utility action smoke — 13 September 2026

The first live utility smoke cost **$0.01278401** and completed six episodes / eighteen fresh sessions in **342.97 seconds**. It verified real actor execution, isolated sessions, released protocol identity, state-based grading and exact usage accounting. It also exposed ambiguous fixture wording. The agent performed no memory capture, maintenance or retrieval, so this run does **not** establish Exomem benefit, neutrality, harm, retrieval latency or coding/conversational utility.

## Recorded results and their limits

| Scenario | Control | Exomem enabled | Interpretation |
| --- | --- | --- | --- |
| Helpful history | Pass | Pass | Current facts were obtainable through one shared inspection call. |
| Self-contained | Fail | Fail | Both copied explanatory prose into a field graded by exact identifier; instrument wording defect. |
| Stale distractor | Pass | Pass | Both inspected current state; neither retrieved stale memory. |

These are the immutable v1 grader outputs, not accepted product utility estimates. No wrong-project writes occurred. All six arms and all eighteen sessions were retained, with no unknown or held charges, fallback, retries or budget failures. In the self-contained pair, conditional harm is undefined because its recorded control-success denominator is zero.

Only one product call occurred: `bootstrap`, taking approximately 0.093 seconds. The three memory cells contained scaffold files only, unchanged throughout all sessions. Write tools were available through `discover_tools`; the actor did not activate them. Both arms retained access to the same ordinary workspace-note tools. Missed capture and zero retrieval remain observable outcomes; filtering such runs would conceal product losses.

## Cost and time

| Metric, summed over each arm's nine sessions | Control | Exomem enabled |
| --- | ---: | ---: |
| Provider input tokens, including cache reads | 22,930 | 194,473 |
| Cache-read tokens | 16,064 | 178,048 |
| Uncached input tokens | 6,866 | 16,425 |
| Output tokens, including reasoning | 3,806 | 3,128 |
| Cost, USD | 0.00341482 | 0.00936919 |
| Model calls | 30 | 20 |
| Model time, seconds | 165.79 | 135.61 |
| Within-phase elapsed time, seconds | 166.42 | 136.66 |

Enabling the installed memory instructions/tools used 8.48 times the provider input tokens and cost 2.74 times as much in this sample, with an absolute difference of **$0.00595437**. It used fewer model calls and less model time. The 39.89 seconds of outside-phase setup/teardown/artifact overhead is recorded at run level, so these figures do not establish an end-to-end per-arm latency difference. Total tool execution across both arms was 0.094 seconds; there was no retrieval call to benchmark.

Provider charges reconcile exactly: 23,291 uncached input tokens at $0.15/M, 194,112 cached input tokens at $0.03/M and 6,934 output tokens at $0.50/M. The 2,823 reasoning tokens are included in output, not added again. Local peak/cumulative context estimates are conservative envelope measurements and must not be mixed with provider billing counts.

The retained run occupies about 3.14 GB, principally copied model caches for the three isolated cells. That is benchmark artifact overhead, not evidence of memory-query latency. Future artifact/cache reuse should preserve identity and isolation without multiplying identical model files per episode.

## Fixture repair and version boundary

The self-contained prompt said `Constraint: foundry clearance must be satisfied first.` Both actors submitted that sentence fragment; the oracle required `foundry clearance`. The tool schema did not distinguish the exact identifier from explanatory prose. Helpful-history and stale-distractor action prompts omitted the prose, leading those actors to copy the exact identifier from inspection.

Generator v2 separates the identifier (`Constraint: foundry clearance.`) from the explanation and documents `apply_config.constraint` as an exact canonical identifier. The grader remains strict. A model-free reference actor now copies the narrative fields verbatim into the real action tool, with a regression covering four seeds; all four failed before the repair. No v1 artifact is rewritten, normalized or silently rescored. No paid v2 run is claimed here.

## Reproduction and next use

The original run used product revision `3677c2e7cd6bf863f0a0149b21bc339adb35ad50`, generator `utility_action_episode.v1`, seed 11, GLM-5.3-Flash's dated wire model `z-ai/glm-5.3-flash-20260826`, Z.AI's `z-ai/fp8` endpoint and high effort. The $2 cap reserved at most $1.1808 for all three pairs. The original and independent reader each verified the digests/protocol and reproduced the stored action grades.

[The machine-readable diagnostic summary](utility-smoke-2026-09/summary.json) contains exact usage, recorded scores, identities and SHA-256 digests of the original manifest, report and artifact index. The full operator-held run is named `utility-live-seed-11`; it must be read from a clean checkout of the recorded revision with `membench utility read`. See [the benchmark instructions](../../benchmarks/README.md) for the command and runtime requirements. The summary is a derived diagnostic record, not a replacement for the full digest-validated run.

The next paid probe should use the repaired version and retain zero memory use as a diagnostic. Broader usefulness requires scenarios with a real cost of rediscovery, held-out seeds and separate context-policy controls. Keep access to task information fair in both arms; do not force memory use or remove failed captures to manufacture a positive result. The reference/candidate regression loop and real coding/conversation canaries remain follow-up work described in OpenSpec.
