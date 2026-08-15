# §4.6c runbook — the 25-case Exomem direct-vs-MemoryBench equivalence gate

The prerequisite for every comparative run. Both lanes retrieve; neither reads
or judges, so this tier is **unmetered** — the ⛳ founder gate at §7.4 covers the
metered reader/judge tier, not this. No `OPENAI_API_KEY` is needed.

## Before you start

**Run only on a quiesced machine.** This ingests 25 LongMemEval-S cases twice —
once in-process, once through MemoryBench's Bun harness against an ephemeral
Exomem REST service. Check first; the programme forbids benchmark runs on a busy
host, and **no latency claim may ever come from this machine**.

```
uptime; free -g | sed -n 2p; ps -eo comm --no-headers | grep -c '^claude$'
```

Preconditions, all currently satisfied:

| Thing | Where | Check |
|---|---|---|
| LongMemEval-S | `<data-dir>/longmemeval-cleaned-98d7416c…/longmemeval_s_cleaned.json` | `python -m benchmarks.lme.fetch verify <path> --sha256 d6f21ea9…` |
| 25-case cohort | `benchmarks/equivalence/subsets/lme-s-25.json` | 25 `target_question_ids`, frozen against the same pin |
| MemoryBench | `<projects-dir>/memorybench` @ `118209a` | pinned in `benchmarks/memorybench/LOCKFILE.json` |
| Bun | on `PATH` | must report `1.3.14` |

## 1. Materialize the pinned overlay

`setup.py` reads the checkout locations from the environment named in the
LOCKFILE, and refuses on any drift.

```
export MEMORYBENCH_HOME="$PROJECTS_DIR/memorybench"
export BASIC_MEMORY_HOME="$PROJECTS_DIR/basic-memory"
python -m benchmarks.memorybench.setup --verify
python -m benchmarks.memorybench.setup --materialize
```

`--restore` puts the checkout back pristine and must be run when you are done,
whether or not the gate passed.

## 2. Left side — the direct lane

`--canonical-selection` binds the run to the frozen 25-case cohort; do not
substitute `--pilot 25`, which is a different, non-comparative selection.

```
EXOMEM_DISABLE_EMBEDDINGS=0 PYTHONPATH=src python -m benchmarks.lme.cli run --dataset "$LME_S" --reader stub --provider exomem-source-only --canonical-selection --top-k 10 --out "$RUNS/direct"
```

Then confirm the run is publishable before comparing anything to it:

```
python -m benchmarks.protocol.cli validate --run-dir "$RUNS/direct/<run-id>" --strict
```

It writes `equivalence.json` itself. A run that is not `VALID` is an
environment fault, never a contender result.

## 3. Right side — the guest lane

Author a `memorybench-run-plan.v1` as an **absolute, owned, mode-0600** file.
Required fields: `run_id`, `upstream_run_id`, `provider` (`exomem`),
`provider_variant`, `benchmark`, `selection`, `harness`, `dataset`,
`dataset_path`, `provider_checkout`, `memorybench_home`, `output_root`,
`guest_work_root`, `guest_evidence_root`, `contract_revision`,
`preregistration_sha256`, `privacy_hmac_key_hex`.

Set `selection` to the explicit ordered ids — the ingest entrypoint passes them
through MemoryBench's own `questionIds` seam:

```
"selection": {"mode": "explicit", "target_question_ids": [ …the 25 ids… ]}
```

Never use `--limit` or sampling: the cohort is the artifact, not a sample size.

```
chmod 600 "$PLAN"
python -m benchmarks.memorybench.export --plan "$PLAN"
```

Exit codes are independent of status: `0` VALID, `1` INVALID, `2` BLOCKED
(pre-provider only), `3` unproved cleanup, `130`/`143` on a caught signal.

## 4. Project the guest export

```
python -m benchmarks.memorybench.equivalence_projection --export "$OUT/memorybench-export.v1.json" --out "$RUNS/guest" --reader stub --reader-model gpt-4o --judge-model gpt-4o
```

The reader/model triple must match what the left run recorded, or
`answer_judge_prompt_model_config` differs for a reason that has nothing to do
with retrieval.

## 5. The gate

```
python -m benchmarks.equivalence.cli gate --left "$RUNS/direct/<run-id>" --right "$RUNS/guest" --mode blocking
```

Exit `1` means a blocking difference. **Expect that on the first run.**

## Reading the first result honestly

Several BLOCKING keys will differ because the two paths *genuinely* differ, not
because either is wrong:

- `session_normalization` — `lme.normalize.render_neutral_session/v1` vs
  `memorybench.longmemeval_to_corpus/v1`
- `namespace` — different derivations
- `ingestion_payloads` — digest of a rendered neutral session vs of a
  `capture_source` body

`retrieved_ids` will differ too (positional `exomem-N` ids vs vault paths), but
that key is REPORTED, not blocking.

Run `--mode report` first to read the full picture without the exit code, then
decide per key whether it is a genuine incomparability — which belongs in the
exceptions register as an **expiring weaker predicate with a written reason** —
or a real disagreement between the two paths, which is a finding.

Do not author exception entries before seeing the measured diff. The register
exists to record known incomparabilities, not to make a red gate green.

## Afterwards

```
python -m benchmarks.memorybench.setup --restore
python -m benchmarks.memorybench.setup --verify
```

Record the outcome against ledger §4.6c in
`openspec/changes/add-competitive-benchmark-programme/tasks.md`.
