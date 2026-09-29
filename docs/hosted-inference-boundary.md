<!-- authority:non-specification -->

# Hosted inference: candidate jobs and acceptance measures

Status: this page sets acceptance measures. It is not a permission gate.

The owner's ruling R4 (2026-09-28) set the direction. The dreamer runs in Cloud cells, and its sensing goes to an in-cluster shared plane. No third-party API sees vault text by default. In the owner's words: "i dont see why we need to compromise".

Each measure below states what a server-side job has to achieve to ship. A job that misses its measure has a defect to fix. The measure is not a reason to stop proposing the job.

The authority-and-effects rule still bounds what any model may touch. Instruments measure into the readings ledger. The deterministic dreamer models over those readings. The agent alone authors canon, through governed writers. The normative statement lives in the `frozen-verifiers` and `sensed-epistemic-model` capability specs and the `add-sensed-epistemic-model` change.

## How the benchmark encodes the comparison

- Run manifests carry a `reasoning` axis. `client` is the value today, and `hosted` is reserved. The same corpus and the same scorers apply to both; only the profile changes.
- A hosted row reads `blocked-until-implemented` until its hosted job exists.

## Candidate server-side jobs and their acceptance measures

Each measure refers to membench dimensions. It is judged per dimension, with no aggregate, on the public seeded corpus, recommended profiles and quiesced reference hardware. "Client baseline" means the shipped client-side path, measured by the same suite.

Every job also carries a privacy note: what content crosses which boundary, and under which governance projection. Vault text stays inside the cluster unless the tenant opts in to an API placement.

| Job | What it does | Acceptance measure |
|---|---|---|
| Background compilation | Compile captured sources into notes off-device | Track D capture fidelity and current-state correctness at or above the client baseline. Manual steps fall by at least 40%. Provenance retention unchanged: 100% of compiled notes cite their sources. |
| Contradiction/staleness sensing | Sense pair relations beyond the cosine band into the readings ledger | Recall of at least 0.8 on planted contradiction pairs where the client-side band scores below 0.5. Sweep latency at p95 that the client cannot reach on reference hardware. The fixture set is green at the plane's exact pin. |
| Pack-specific extraction | Domain-pack entity and relation extraction | Connection-discovery precision and recall both at least the client baseline +0.2 on predeclared hidden-link sets. Decoy rejection no worse. |
| Deep synthesis for thin clients | Multi-note synthesis where the client model is small | Blind pairwise human preference of at least 70% over the client baseline on Track D synthesis rubrics. No deterministic-gate regressions. |
| Policy-aware redaction (closes the L4 gap) | Span-level redacted excerpts | L4 renders as a true redacted excerpt, with 0 leak-gate failures across the governance family. |
| First-party mobile/web chat | A hosted answerer over governed retrieval | The Track C mode-13 human acceptance script passes. Leak gates: 0 failures. Latency p95 within a predeclared budget. |
| Multimodal extraction | OCR/ASR for clients without media extras | PNG- and PDF-only facts become answerable (factual_qa goes from unsupported or fail to pass) with citation identity intact. |
| Batch evaluation | This benchmark's judge phases, run server-side | Judge N-sample variance unchanged against desk-side backends. No credential ever enters the runner (the file handshake is preserved). |

## Instruments: the rule every model-backed job runs under

The frozen stance verifier (claim polarity on the contradiction review queue) was the first model admitted. It becomes the first instrument of the sensed epistemic model. Every model-backed job, local or hosted, takes the same shape:

- **Closed questions.** An instrument answers one repository question with probabilities over a closed label set plus abstain. It never produces sampled text. A generative instrument places vault text only in the delimited data slots of a versioned template.
- **A pinned identity.** A repository pin names:
  - the model and its exact upstream revision;
  - the ordered artifact manifest, and the sha256 digest of those declared names and bytes;
  - the label-map version;
  - the fixture set that verified the pair.

  Extra cache files and revisions do not alter that identity. No runtime configuration, environment value or vault content may add, select or alter a pin.
- **API instruments.** An API instrument is an opt-in placement per vault or per tenant, off by default. It is identified by provider, model id and snapshot date, and flagged `unpinned_weights`.
- **Refusal degrades to absence.** A gate that is unset, a digest mismatch, missing weights, a missing dependency or a red fixture set means no reading. It never means a differently produced label wearing the instrument's name.
- **Recorded, not decided.** Each judgement is appended to the per-vault readings ledger. The deterministic dreamer models over the readings. Readings never enter note canon, decisions, retrieval, ranking, policy or any synchronous write path, except through a governed write the agent authors and that cites the reading.

The admitted local checkpoint is multilingual, but Exomem's acceptance claim is deliberately narrower than its model card. The production fixture set checks English, German, French, Estonian, and mixed English/Estonian pairs. The neutral fallback means only that the NLI head did not establish contradiction or entailment at the reviewed thresholds. It does not establish topical unrelatedness.

## What the Cloud plane must measure before the NLI pin serves cells

Today the Hosted image adds no `nli` extra or verifier weights, carries no verifier capability grant, and passes no `EXOMEM_CLAIM_POLARITY_NLI` gate to a cell. The sizes to plan around:

- The selected safetensors checkpoint is about 532 MiB before Torch/runtime overhead.
- The available full ONNX export is about 1,064 MiB.
- The upstream quantized ONNX export is smaller, at about 323 MiB, but it failed a genuine-contradiction fixture. Its two directional contradiction probabilities were about 0.29 and 0.50, far below the reviewed 0.93 symmetric threshold. It is not an admissible capacity shortcut.

The shared plane must pass the exact multilingual fixture set, and it is accepted on measured values, on the actual cell runtime, for:

- image size;
- cold and warm latency;
- peak RSS;
- cells served per node;
- idle reclamation;
- scheduling;
- failure isolation.

Local admission does not grant Hosted admission. The plane is its own placement, with its own measured acceptance.

See the `frozen-verifiers` and `sensed-epistemic-model` capability specs for the normative statement.

## Standing costs every hosted job prices in

Every hosted job prices in:

- Added latency and infrastructure cost.
- A larger privacy surface. Content that leaves the user's machine must ride the existing governed egress: audience projections, withhold notices, disclosure receipts.
- Operational burden: cells, updates, attestation.

Local placement stays the default wherever it works.

## Non-goals

- No server-side reasoning in retrieval, ranking or policy decisions.
- No third-party API sees vault text by default.
- No model writes, rewrites or summarises into the vault.
