# Design: a sensed epistemic model

## Context

The dreamer (`src/exomem/dreamer.py`) is a default-off background thread in the managed local service. It reads derived state and pages and writes only its disposable sidecar (`dreamer.sqlite`). Its families are structural: they count independent origins and read graph shape, with no word list and no model. Its module docstring and `tests/test_dreamer_no_side_effects.py` pin seven nevers, one of which is "never loads or runs a model".

One model is admitted today: the frozen stance verifier (`src/exomem/claims.py:1380-1398`, mDeBERTa-v3-base-xnli-multilingual, revision `b5113eb3…`, weights `b1dbf445…`, label map `v2`, fixture set `stance-v2-multilingual`). It runs inside the audit request (`src/exomem/audit.py:6218-6268`) over the surfaced contradiction pairs. It attaches a label and records nothing durable.

This change turns models into recorded instruments and keeps every modelling decision deterministic.

## Rulings (owner, 2026-09-28, final)

The dreamer is Exomem's deterministic epistemic modelling engine: how notes connect, refine, supersede, contradict, drift and converge. It surfaces what it finds to the primary agent. It is never a generative second brain. No model writes to the vault, rewrites it, summarises into it or decides. In the owner's words: "we are supposed to be the king of epistemics in this space."

LLMs are welcome as instruments. The matrix's deterministic measurements keep the name "sensors"; model components are "instruments".

- **R1 — Generative instruments are admitted.** An instrument uses a versioned template, with vault text only in delimited data slots. Its output is logits or probabilities over a closed label set plus abstain, never sampled text. This amends `openspec/specs/frozen-verifiers/spec.md:29`.
- **R2 — Readings may originate sensed families.** They never suppress, reorder or alter structural families. They never enter canon except through a write the agent authors (the existing relation writer, citing the reading id). This replaces close-memory-loop `design.md:212` condition 4 and its scenario "Verifier labels do not reach upkeep" (`specs/adaptive-memory-maintenance/spec.md:250-253`).
- **R3 — The ledger is durable.** It is an append-only, per-vault readings ledger in the state root. It is not in the vault and not in the disposable `dreamer.sqlite`, and it is included in backup and export.
- **R4 — Cloud runs the dreamer.** The dreamer runs in cells, reversing close-memory-loop `design.md:208`, and sensing goes to an in-cluster shared plane. No third-party API sees vault text by default. `docs/hosted-inference-boundary.md` keeps its thresholds, rewritten as acceptance measures rather than permission gates. In the owner's words: "i dont see why we need to compromise". This slice specifies the Cloud plane and does not build it.
- **R5 — Local GPU waits.** The LLM instrument senses on CPU at idle until `detect-co-tenant-gpu-pressure` ships. That change is not reordered.
- **R6 — Authored pairs stay unlabelled.** "Asserted pairs carry no model label" (`openspec/specs/contradiction-queue/spec.md:343`) holds for v1.
- **R7 — Local models are preferred.** Local models are the default. An API instrument is an allowed opt-in placement per vault. Its identity is provider + model id + version or snapshot date, and it is flagged `unpinned_weights`; a vendor retirement triggers the normal instrument-migration re-sense. Personal vaults opt in by the owner. Cloud tenants opt in explicitly per tenant, and are off by default. The same closed-label contract applies. In the owner's words: "i prefer using local models instead of api but whatever works. api can be very cheap … saves on resources".

### How the rulings land in existing artifacts

- R1, R2 and R7 amend the canonical `frozen-verifiers` requirements (delta in this change). The "pure substrate" paragraph of `openspec/config.yaml` is updated to match, so later proposals are not generated against the superseded wording.
- R2 and R4 amend `close-memory-loop`, which is still an active change. Its `adaptive-memory-maintenance` requirement is ADDED there and has no canonical copy to modify, so the amendment is made in place:
  - `design.md:208` keeps its structural description and points here for the Cloud reversal.
  - `design.md:212` drops condition 4.
  - The scenario "Verifier labels do not reach upkeep" becomes "Readings reach upkeep only as sensed families".
  - Task 8.4 closes as "admitted, re-laned to add-sensed-epistemic-model".
- R3's export half needs a registered external-state descriptor. The hosted export includes only external state whose logical path the registry classifies as portable-derived, and restore relocates it through the offline state migrator. Adding a descriptor changes the declared descriptor set, and `service_manager.migration_required` then routes the next upgrade through an offline migration instead of the standby cutover. No cell writes readings until the Cloud plane exists, so the descriptor ships with the Cloud-plane slice (task 9.3). Personal backup coverage is a documented state-root path (`docs/ARCHITECTURE.md`) from slice 1.

## Roles

| Role | What it is | Writes |
|---|---|---|
| Instrument | A pinned model answering one closed question | readings, appended to the ledger |
| Sensor worker | A disposable child process that runs instruments under budgets | the ledger (append only) |
| Dreamer | A deterministic modeller over readings, graph and pages | its disposable sidecar |
| Carrier + S6 | Pull-first delivery: status lines, and the existing upkeep block for tensions | nothing canonical |
| Agent | The sole decider | canon, through existing governed writers |

## Decisions

### D1. Closed questions, versioned templates and label maps

A question-registry entry fixes:

- the question type;
- the template version, which is how the inputs reach the model;
- the label map version, which maps probability vectors to the closed label set plus `abstain`;
- the unit scope, which says which units are eligible;
- the fixture set that admits it.

Every field is a repository artifact. No environment value, runtime configuration or vault content may add, select or alter one.

Registry, in build order:

| Question | Labels | Instrument | Slice |
|---|---|---|---|
| `pair.relation` | contradicts, refines, restates, neutral, abstain (both directions) | mDeBERTa NLI pin (already admitted) | 1 |
| `mention.same_referent` | same, different, abstain | open-weight 3–8B instruct model chosen by a fixture spike | 2 |
| `recap.covered_by` | entailed, partial, absent | NLI | 4 |
| supersession direction, relation typing, `term.same_meaning` | closed per question | chosen per question | 4 |

**`pair.relation` (slice 1).**

- Template `nli-pair-v1`: the two texts enter as a classification pair in each order, `(a, b)` and `(b, a)`. There is no prompt, and no text in instruction position.
- Label map `relation-v1` reads the head's declared `(entailment, neutral, contradiction)` columns per direction. `c` and `e` are the contradiction and entailment probabilities over the two directions. The rules are checked in this order:
  1. `contradicts` when `min(c) ≥ 0.93`.
  2. `restates` when `min(e) ≥ 0.95`.
  3. `abstain` (`directional_asymmetry`) when `max(c) ≥ 0.93`: a one-way contradiction is its own state and never collapses into another label.
  4. `refines` when `max(e) ≥ 0.95`. The direction whose premise entails the hypothesis names the refining side.
  5. `neutral` otherwise.
- The instrument abstains with `input_too_long` rather than truncating when the pair exceeds the model's sequence budget. That reading carries no vector.
- `p` is the probability that met the deciding threshold, and for `neutral` the mean neutral probability.
- The thresholds equal `v2`'s, so the stance fixture evidence carries over. Fixture set `relation-v1-multilingual` extends it with a negative twin per label (D10).
- Unit scope `units-v1`:
  - compact observations whose category key is one of `claim, decision, fact, finding, insight, constraint, assumption, risk, preference`;
  - rich blocks whose core kind is one of `claim, finding, evidence, decision, assumption, inference, constraint, hypothesis, prediction, result, pattern`.

  These are registry keys, never words matched in text. The text fed in (extractor `unit-text-v1`) is the unit's graph text, NFC-normalised, whitespace-collapsed and stripped. An empty text, or one longer than 2,000 characters, is out of scope.

**Generative instruments (R1, slice 2 onward).** A template is a versioned string with named, delimited data slots. Vault text may appear only inside a slot, escaped so it cannot close its delimiter. The instrument reads the logits of the label tokens at one fixed position and normalises them over the closed set. It never samples, and never returns or stores generated text. A generative instrument's identity also names its template and label-token map.

### D2. The reading record and the ledger

The ledger is `<vault state dir>/sensing/readings.sqlite` (WAL). It is resolved through the single state-root seam, and it is created only when sensing is on. Its tables:

- `instruments(instrument_id PRIMARY KEY, identity_json)`;
- `readings(reading_id PRIMARY KEY, seq UNIQUE, question_type, instrument_id, input_key, inputs_json, output_json, verdict, label_map_version, fixture_set, sensed_at, placement)`;
- `meta(schema_version)`.

`BEFORE UPDATE` and `BEFORE DELETE` triggers abort every update and delete, so the ledger is append-only by construction. Re-sensing the same inputs with the same instrument has the same `reading_id` and is ignored.

- **Instrument identity.** The identity record holds model, revision, weights sha256 (or `unpinned_weights: true`), runtime and runtime version, template version, label-map version, fixture-set version and placement.
  - `instrument_id = sha256(model, revision, weights|"unpinned", runtime, runtime_version, template_version)`. These fields determine the stored vectors.
  - The label-map and fixture-set versions are recorded with each reading but are deliberately outside `instrument_id`. Neither changes a vector, so a label-map change re-derives verdicts (D3) instead of forking every reading id.
- **Inputs.** For each input unit:
  - the unit ref and page path, as context of the first sensing;
  - `text_sha256`, the sha256 of the exact text fed in (precedent: `claims._checksum`, `claims.py:271-279`);
  - the extractor version.

  Inputs are ordered by `text_sha256`, which makes a pair unordered. `input_key = sha256(question_type, ordered text hashes)` indexes lookup across instruments.
- **Output.** `columns` and one probability vector per direction (`ab`, `ba`, in input order), rounded to six decimals. The verdict under the reading's label map, with `direction` for `refines` and `abstain_reason` for `abstain`. `sensed_at` (UTC). `placement`: `local-cpu`, `cloud-plane` or `api`.
- **Identity.** `reading_id = sha256(question_type, instrument_id, ordered text hashes)`.
- **What the ledger never holds.** It never stores vault text. Hashes and refs identify the inputs, and the texts stay in the vault.
- **Durability.** The ledger is durable user state (R3). Wiping the dreamer sidecar never touches it. Losing it costs re-sensing, never correctness. It is named in the state-root backup guidance (`docs/ARCHITECTURE.md`). It becomes a registered portable-derived export family with the Cloud plane (task 9.3; the reason is under Rulings).

### D3. Invalidation and migration

Consumption is by content, never by reading age. For a proposed pair, the dreamer computes the current text hash of each unit and looks up readings by `input_key`:

| Situation | State | Consumed | Action |
|---|---|---|---|
| The active instrument has a reading for the current hashes | `current` | yes | none |
| A unit's text changed since its last reading | `stale` (old reading kept) | no | queue a re-sense |
| No reading yet | `pending` | no | queue |
| Only an older instrument has read the current hashes | `migrating` | no | queue, ahead of new pairs |

- **Unit-level hashing.** An edit to another unit, or to page prose outside every unit, changes no unit hash, so nothing is re-sensed.
- **Label-map change.** Verdicts are re-derived from the stored vectors under the active label map; nothing is sensed. The stored verdict remains the historical verdict under its own map.
- **New pin.**
  - Old readings are kept, and only the active instrument's readings are consumed.
  - The page's `evidence_complete` is false until its pairs drain.
  - The re-sense is lazy and ordered: pairs that already carried a consumed edge (open work) come first, then pairs on recently changed pages, then the rest.
  - A vendor retiring an API model (R7) is the same migration.

### D4. The sensor worker

Inference runs only in a supervised, disposable child process: `python -m exomem.sensor_worker_child`, on the `media_worker` pattern (`src/exomem/media_worker.py:1-12`). The dreamer thread and request threads never import a model runtime, so the dreamer's "never loads or runs a model" stays literally true, and its spy test keeps passing.

- **Supervision.** The dreamer loop drives the supervisor once per poll. While a child is alive, the loop re-evaluates at least every 2 seconds.
- **Launch.** The supervisor launches a child only when all of these hold:
  - the sensing setting is `on`;
  - the dreamer's gate would run a tick (`dreamer_policy.decide`: setting, standby, quiet mode, pressure, foreground idle, freshness, settle, graph debt, index backlog, the dreamer's own budget and backoff);
  - the sense queue is not empty;
  - the worker's own hourly budgets have room.
- **Termination.** The supervisor terminates the child when any gate closes, with a 5-second grace before a kill. Quiet mode, auto-quiet pressure and standby kill it, so its RAM returns to the host.
- **Budgets.**
  - 300 CPU-seconds per rolling hour (about 8% of one core), counting model load.
  - 600 judgements per rolling hour.
  - The child runs one inference thread on CPU (R5), at the lowest OS priority (`runtime_resources.lower_background_priority`).
  - It exits after 60 seconds without work.
  - It reports its spend after every judgement to a small spend file beside the ledger, and the supervisor charges that spend to its rolling window.
  - A child that dies without reporting is charged its whole allotment.
- **What the child reads.** Only the dreamer sidecar's sense queue (read-only, non-waiting) and the ledger. It never reads or writes the vault, takes no lease and schedules no index work.
- **Soft failure.** A child that cannot admit its instrument exits with a named refusal, and the supervisor reports it without relaunching until the setting or the pin changes. Refusal causes: gate off, no pin, weights missing, digest mismatch, dependency missing, fixtures failed. Sensing off is byte-identical to a build without sensing.

### D5. Pair proposers

The proposers read stored data only; the dreamer never encodes. Each predicate is a function of the two units and their own pages, so a pair's selection never depends on a third page. That is the property egress needs (D8).

- **Cosine.** The stored unit vectors of the ranked encoder, where the vector's source text hash equals the unit's current text hash. A pair is proposed when its cosine is at least `θ = 0.72`: a fixed per-pair threshold, never top-k and never corpus-relative. θ is bound to the encoder fingerprint, and vectors of another encoder propose nothing.
- **Structural co-occurrence.** Units on two pages joined by a graph edge in either direction.
- **Temporal same-subject.** Units on two pages that both link the same target spelling (the shared fold key over each page's own authored link targets) and carry different knowledge dates.
- **Bounds.** Only pairs across two pages are proposed, and identical texts are never paired. Each page proposes at most 128 pairs, chosen in a fixed order: structural, then temporal, then cosine by descending similarity, then pair key. A page whose candidates exceed the cap is `capped`, and its sensed items are served only to owner-bound principals (D8). The cosine matrix is loaded once per tick, bounded at 16,384 in-scope units. Past that bound the cosine proposer stands down and reports it, and the result is the same for every caller.

### D6. Deterministic projections

Every projection is a pure function of (ledger snapshot, graph, pages):

- **Sensed unit edges.** For each proposed pair: its state, verdict and direction under the active label map, `p`, instrument and reading id.
- **Contradiction components.** Connected sets over `contradicts` edges between active pages. They are computed per request from released edges, and the traversal is bounded at 32 pages.
- **Refinement and supersession chains, in time order.** Built from `refines` edges and authored supersession edges, ordered by knowledge date (`created`, else `updated`) and then by path. They are computed per request and bounded at 8 pages.
- **Convergence** across independent origins (the union-find over declared Sources) and **emerging connections to recent work** (the heat projection's recent pages) are later-slice projections over the same edges (task 7).

**Uncertainty is never collapsed:**

- Directional asymmetry is its own state (`abstain: directional_asymmetry`).
- Abstentions are recorded.
- When two active instruments read the same inputs and disagree, the edge is `instruments_disagree` and carries both verdicts.
- A served item names its verdict, `p`, the instrument (model, revision, placement) and the instrument's fixture precision for that label: correct over total fixture pairs of that label at the admitted pin.

**Reproducibility.**

- Fingerprints bind to `(question_type, ordered text hashes, verdict, direction)` and never to the reading id.
- The projection never reads `seq` or `sensed_at`.
- Replaying the ledger into a fresh sidecar therefore yields byte-identical edges and fingerprints. A test pins that.

### D7. Delivery: pull-first

1. **Point-of-use status line.** When a page is read (`read_memory`) or resolved as an activation anchor, the response carries `epistemic_status` for that page.
   - Contents:
     - a line such as `refined by 2 later notes; 1 open contradiction`;
     - the released items behind each count, with verdict, `p`, instrument, fixture precision and reading id;
     - the page's bounded refinement chain and the size of its contradiction component;
     - `evidence_complete`.
   - The counts are:
     - `refined_by_later`: distinct released pages whose unit refines one of this page's units and whose knowledge date is later;
     - `open_contradictions`: distinct released pages with a current `contradicts` edge where neither page is superseded or archived, and no authored supersession joins them.
   - It never ranks, reorders or filters anything, and it counts released pages only. It is absent when every count is zero, so a page with nothing sensed is byte-identical.
   - On activation it is attached after the packet is built, outside the packet cache, like the upkeep block. Its characters are charged to the packet budget.
2. **Tensions on active or recent work** (slice 3) ride the existing upkeep block: its caps, its delivery ledger and its S6 session-start rule. Sensed families use their own `upkeep_sensed_*` family names, never alter a structural family's rows, order or caps, and route to the existing relation writer. That writer cites the reading id in its `why` evidence. Nothing new pushes.

### D8. Egress

- **Consumability.** A reading is consumable for a caller only when every input unit's page is released to that caller. Items, counts, components and chains are recomputed per request from released edges only.
- **Parity.** A withheld page equals an absent one in the status line, its counts, its chain and its component. This holds because each pair's selection depends only on its own two pages (D5), and a reading is keyed by its inputs alone.
- **Capped pages are owner-only.** A capped page's cap ranks candidates that may include withheld pages. Serving what survived it to a restricted caller would let a withheld page change that caller's view, so its sensed edges (in its own status and in the counts of pages paired with it) are served only to owner-bound principals, using the rule `working_set.band_audience_allowed` applies. This is the accepted residual: for a restricted caller, a capped page's items are absent whatever the withheld pages are.
- **Timing residual.** Budget ordering is the accepted timing residual: when a pair is sensed can depend on the whole queue, withheld pages included. What is served never depends on it, and nothing is served for a pair without a current reading.

### D9. Placements (R4, R7)

- **Local.** The default for personal vaults: the sensor worker on CPU (D4).
- **API (opt-in).** A per-vault setting written by the owner for personal vaults, and a per-tenant grant for Cloud, off by default.
  - Identity: provider, model id, version or snapshot date, and `unpinned_weights: true`.
  - The template, closed label set, abstain and fixture admission are the same. Output is the provider's token log-probabilities over the label set, never text.
  - The request carries only the delimited slot texts. It never carries an instruction built from vault text.
  - A vendor's retirement of the model id is an instrument migration (D3).
- **Cloud plane (slice 5).**
  - Cells run the dreamer.
  - Sensing is served by an in-cluster shared plane: a pool of pinned local instruments behind a request interface scoped to the cell.
  - No third-party API sees vault text unless the tenant has opted in.
  - The plane is measured against the acceptance measures in `docs/hosted-inference-boundary.md`: image size, cold and warm latency, peak RSS, cells per node, idle reclamation, failure isolation.
  - The ledger joins hosted export and restore as a registered portable-derived family.
  - The cells' readings stay per vault.

### D10. Evaluation

- **Fixtures.**
  - Set `relation-v1-multilingual`: English, same-language non-English and mixed-language pairs over every label, each with a negative twin (a minimally different pair that must not get that label).
  - It is checked at the exact pin through the existing `_verify_fixtures` machinery. One miss refuses the whole pair.
  - Twins the pin mislabels are recorded under "Known misses" below, never in the admission gate.
- **Lean suite.** Stub instruments (a deterministic function of the text hashes) under a stub identity cover:
  - the ledger, invalidation, migration and replay;
  - the proposers, projections and status line;
  - the egress twins;
  - the supervisor's gates, budgets and kill.

  The real pin runs only in the dedicated `nli` lane (`EXOMEM_RUN_REAL_NLI=1`), as the stance verifier's gate does.
- **Resource probes.** A supervised child's CPU seconds per judgement, peak RSS and kill-to-exit time are measured and recorded below. Quiet mode must end the child within one supervisor poll.
- LongMemEval is not used.

## Slices

1. Ledger and reading record; invalidation and migration; the sensor worker with gates, budgets and the quiet kill; `pair.relation` on the admitted NLI pin; the proposers; contradiction components and refinement/supersession chains; the status line on read and activation; egress twins, replay reproducibility and fixtures. Close close-memory-loop 8.4.
2. `mention.same_referent` on an instruct model chosen by a fixture spike; the generative template runtime (R1); local GPU placement after co-tenant pressure detection ships (R5).
3. Tensions through the upkeep block as sensed families (R2), with reading-id citation in the relation writer; the audit's NLI enrichment moves onto the ledger.
4. `recap.covered_by`; supersession direction, relation typing, `term.same_meaning`; convergence and emerging-connection projections.
5. The Cloud plane (R4), the API placement (R7) and the ledger's export and restore registration.

## Risks and trade-offs

- **A capped page is owner-only for restricted callers.** That is the price of a bound that ranks candidates across pages (D8). The cap is set well above an ordinary page's pairs.
- **The dreamer sidecar moves to schema 5.** That is a reseed, and it resets the delivery ledger as close-memory-loop already accepts.
- **Model load cost on relaunch.** Terminating the child on every closed gate costs a model load at the next idle window. The CPU budget counts that load, so a chatty day senses less rather than costing more.
- **CPU only.** Sensing a vault's backlog on CPU is slow, and deliberately so: the owner games on this machine (R5). The migration drain states `evidence_complete: false` rather than rushing.

## Measured

Filled from the real-pin probe run in the `nli` lane (task 10.5).

## Known misses

None recorded yet (task 10.4).
