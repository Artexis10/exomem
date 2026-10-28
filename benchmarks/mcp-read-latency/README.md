# MCP read latency gate

The gate compares the MCP read tools of the working tree (head) with the latest release (base).
It times each read through the managed worker's socket, the same boundary a service client uses
(`mcp-worker-socket-v1` in `src/exomem/request_latency_contract.json`).

`scripts/mcp_read_latency_gate.py` is the runner. `profile.json` defines the generated fixture,
and `summary.schema.json` defines every document the runner writes.

## What one run does

Each arm is one release with its own interpreter, vault, state root, lease coordinator and managed
worker (`python -m exomem.service_manager worker`). The arms never share state.

1. Create matching notes for both arms so that their pages are byte-identical.
2. Write the legacy trackers so that both arms hold the same tracker pages.
3. Start each arm so that its managed worker becomes quiet.
4. Load generated collections through each arm's change tools so that both hold the fixture.
5. Wait for each worker to use under 20% of one CPU for 10 seconds; fixture preparation is quiet.
6. Check state and derived readiness so that both arms meet the timing requirements.
7. Pause both workers so that only the arm under measurement runs.
8. Time each case in adjacent pairs so that each sample has matched base and head values.

The first arm alternates. The first repeat of each case is an unmeasured warm-up.

The timer stops when the parsed terminal response arrives (`recall_canary.measure_call`).
Assertions and statistics stay outside it.

The workers stay running between fixture loading and measurement. The report's `ready_s` and
`quiescent_s` describe the initial start. The `fixture-prep` progress line reports loading and the
quiet wait after loading separately.

Progress also marks the readiness checks, each case, measurement completion and cleanup.
Case lines contain only their position and elapsed time. Failed samples report the arm, repeat and outcome,
including warm-ups. These lines keep a timed-out run diagnosable without logging request or response content.

## Modes

| Command | Purpose |
|---|---|
| `plan` | Pin the latest release and list the shards from `profile.json`. |
| `paired --shard N` | Time one shard of the generated fixture on both arms. Write a fragment. |
| `aggregate FRAGMENT...` | Join the fragments of every shard into per-group verdicts. |
| `private` | Time a private prepared vault on both arms. See Private mode. |
| `validate-summary FILE` | Validate a stored plan, fragment or summary against the schema. |

Run it with `uv run --frozen --group recall-benchmark python scripts/mcp_read_latency_gate.py ...`.
The runner needs `gh` and a tag-bearing clone to pin the release.

A reduced run keeps `--max-cases N` cases per group and, with `--notes-pages N`, a corpus of N pages
with embeddings off. A reduced run states `reduced: true` and its verdict is `reduced`, never `passed`.

## Verdicts

The unit of a verdict is a gating group of the contract. For each group the runner takes, per case,
the median over the repeats of the paired difference (head minus base). It then takes the mean of the
case medians and a Student-t interval around it. SciPy supplies the quantile.

The interval is corrected for the whole family with Bonferroni: the confidence per tail is
`1 - (1 - confidence) / (2 k)`, where `k` is the number of gating groups in the contract.

| Interval against the allowance `B` | Verdict |
|---|---|
| Lower bound above `B` | `failed` |
| Upper bound at or below `B` | `demonstrated` |
| Otherwise | `inconclusive`: a visible warning, and the run passes |

The contract keeps `allowance_ms` at null until A/A dispatch runs set it. With `B` null the summary
reports each interval and says `allowance-not-set`. It invents no number.

A broken instrument fails the run: a failed case, a missing pair, a contract digest that differs
between fragments, or a group below the case floor. List groups are reported, not gated.

## Refusals

The runner refuses to time an arm in two cases. Each refusal is a closed code in the summary.

- `arm-state-created-during-run`. Vault state is keyed by the vault path, so an arm whose state
  directory did not exist before its timed start would be timed cold. The runner compares the state
  directories before the start with those after quiet.
- `derived-collection-not-ready`. A derived (bulk-import) collection in `rebuilding` makes the
  store retry every 2 seconds under the writer lease and adds a tail to many calls. The runner reads
  each `collections-derived.sqlite` read-only and refuses when any derived collection is not `ready`.
  The generated fixture has no derived collections.

An arm that never goes quiet is refused as `arm-not-quiescent`. This catches a worker that
re-embeds after startup.

## Private mode

`private --golden DIR --cases FILE --arm-roots BASE,HEAD (--prepare | --reuse)` times a prepared
private vault. The golden directory holds `vault`, `state`, `lease` and `coordinator.sqlite`. The
cases file uses the shape of `recall_canary.load_cases` and gives every case a group.
Nothing private enters the repository, the tests or CI: the operator keeps the golden, the
cases and the arm roots on the host.

- `--prepare` copies the golden into both fixed roots and starts each arm once, so its state exists.
  It refuses a root that already exists.
- `--reuse` times the roots as prepared and never rebuilds them. A copy at a new path starts cold,
  so the roots stay in place between runs.
- `--embeddings` starts the arms with the real encoder settings. Without it embeddings are off.

The summary adds, per store group, the head p95 against `p95_target_ms` from the contract
(`meets target` or `misses target`). It records `/proc/pressure/cpu` avg60 at the start and at the end.
An absolute verdict counts only on a quiet host.

## Fixture

`gen_collection_fixture` in `scripts/synth_vault.py` builds the collections from `profile.json`:
30 Records collections, 24 Planning collections with skewed row counts and 3 legacy trackers.
Registry-constrained values come from the shipped vocabulary packs at run time. Planning items form
the kind hierarchy that the registry's `parents` rules require, sized by `planning.fanout`; the
loader adds parents first and builds each child's `parent` reference from the add response. The cases come from
the same profile, so the generator's input is the source of truth for each expected value.
The `recall` cases send a page title as the whole turn and expect the default map packet.
Each packet must contain a generated collection's path, title and description within the same item, at any rank.
The expected material comes from the fixture's authored manifest. The map does not depend on the turn;
these cases measure the map path, not turn-specific anchor resolution.
Every fragment pins `profile_sha256` and `cases_sha256`; the contract pins the profile as
`fixture_profile_sha256`. A changed profile needs a new digest in the contract.

Case shards divide measurement time across CI jobs. Each shard prepares the complete fixture;
aggregation requires every shard before it computes group verdicts.
