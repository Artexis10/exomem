# Write narration: candidate against baseline, 2026-11-02

Question: after the plain-write-outcomes change, does an agent tell the person the outcome of a write in plain words, without
the codes, ids, retry history or sync state that the tool replies carry?

## Reading

- The candidate leaked less. Over the four cases that test for leaks, the mean per-case difference is -0.250
  (candidate minus baseline). The 95% bootstrap interval over cases is [-0.333, -0.083] and excludes zero. The interval
  that also resamples the runs is [-0.500, -0.042].
- The interval rests on four cases, and three of them show the same drop (-0.333), so it is coarse. Read it as a direction,
  not a size. Case 01 shows no leak on either arm.
- Case by case, no interval excludes zero. Resampling the runs inside each case gives [-0.83, +0.17] for case 02,
  [-0.67, +0.00] for case 04 and [-0.83, +0.17] for case 06. Only the interval over the four cases pooled excludes zero.
- Each model alone has the same mean (-0.250), and its interval over cases touches zero ([-0.500, +0.000]).
- The leaks that remain are judged R2 failures. In case 04 the agent says that the graph
  index is still rebuilding: 6 of 6 baseline runs, 4 of 6 candidate runs. In cases 02 and 06 the agent says the first save
  was rejected and retried: 6 of 12 baseline runs, 2 of 12 candidate runs.
- The exact-string check (scripted ids and codes in the final answer) found no hit on either arm after the fix described
  below. Every leak counted here is a plain-language description of a refusal, a retry or an indexing state.
- R1 (true outcome), R3 (blocker stated plainly, case 03) and R4 (request id given when asked, case 05) pass in every run
  on both arms (36/36, 6/6, 6/6). The candidate costs nothing on those.
- The run does not show that the candidate removes sync-state narration in case 04. A sync state remains in 4 of 6
  candidate answers there.

## Setup

- Baseline: the merge base of the change (`c11f9f740898a22d72a2c3b54c62968f3462059f`). Candidate: the branch tip with the
  harness (`57f5dfca8bd09dac5a3f317f2e6df8ef248199b7`).
- Each arm's tool replies were recorded once from that arm's own code on a throwaway copy of the fixture vault, and the
  scripted server served them. The agent saw that arm's tool schemas, skill text and server instructions.
- Agents: `claude -p` sessions on the logged-in subscription, models `haiku` and `sonnet`, three repeats per cell, one
  session at a time. Every run recorded `apiKeySource` as `none`; no metered key was used.
- Judge: `opus`, through `claude -p` with a structured-output schema. It sees the person's turn, the same reference
  account of the true events for both arms, and the agent's final answer. It never sees a tool reply or the arm. The judge
  is the same model family as the agents, so a shared blind spot is possible.
- A leak is a judge R2 failure or an exact-string hit on a scripted id or code in the final answer. Rates exclude a run
  with an error or an unknown result; there were none.
- 72 agent runs: 6 cases, 2 arms, 2 models, 3 repeats. Run rows: `write-narration-2026-11-02.runs.jsonl` in this folder;
  regenerate this table with `python -m import_guidance report`.

## Limits

- In cases 02 and 06 the first commit is refused whatever arguments the agent sends, and the second succeeds. The refusal
  text is the arm's own: the old text on the baseline, the new text on the candidate.
- The agent has only the served tools (`bootstrap` and the one write tool). The compact bootstrap reply stands in for the
  session profile.
- In case 03, 4 of 6 baseline runs and 3 of 6 candidate runs refused without calling `edit_note`, from the skill text
  alone. In those runs the refusal reply was not exercised.
- Case 03 refusal text carries an incidental suffix `(missing: ['path'])` on both arms. It is old behaviour, not part of the
  change.
- Only the final answer is judged. The run rows also keep every message the agent wrote (`said`).
- An earlier run of 12 agent runs used a check that matched the first characters of a long code. It counted the plain
  words "relation review" as a leaked code, so those runs were discarded and the whole run was repeated from empty with
  the fixed check. Nothing else was changed between the two.

Baseline `baseline` at `c11f9f740898a22d72a2c3b54c62968f3462059f`. Candidate `candidate` at `57f5dfca8bd09dac5a3f317f2e6df8ef248199b7`.
Agents: haiku, sonnet. Judge: opus. Runs: 72, of which 0 ended in an error.

## Leak rate by case and model

A leak is an R2 failure or an exact-string hit in the final answer. Cells are leaks/known runs. The difference is candidate minus baseline; negative means fewer leaks.

| case | model | baseline | candidate | difference |
|---|---|---|---|---|
| 01-ordinary-save | haiku | 0/3 | 0/3 | +0.00 |
| 01-ordinary-save | sonnet | 0/3 | 0/3 | +0.00 |
| 02-review-needed-then-saved | haiku | 2/3 | 1/3 | -0.33 |
| 02-review-needed-then-saved | sonnet | 1/3 | 0/3 | -0.33 |
| 04-saved-indexing-pending | haiku | 3/3 | 1/3 | -0.67 |
| 04-saved-indexing-pending | sonnet | 3/3 | 3/3 | +0.00 |
| 06-health-task-recoverable-error | haiku | 0/3 | 0/3 | +0.00 |
| 06-health-task-recoverable-error | sonnet | 3/3 | 1/3 | -0.67 |

## Leak rate by case, both models together

The interval resamples the runs of each arm inside the case (the same 95% bootstrap as below).

| case | baseline | candidate | difference | 95% interval |
|---|---|---|---|---|
| 01-ordinary-save | 0/6 | 0/6 | +0.00 | [+0.00, +0.00] |
| 02-review-needed-then-saved | 3/6 | 1/6 | -0.33 | [-0.83, +0.17] |
| 04-saved-indexing-pending | 6/6 | 4/6 | -0.33 | [-0.67, +0.00] |
| 06-health-task-recoverable-error | 3/6 | 1/6 | -0.33 | [-0.83, +0.17] |

## Overall leak difference

Mean of the per-case differences over 4 cases: **-0.250**.

- 95% bootstrap interval over cases (10000 resamples of the cases): [-0.333, -0.083].
- 95% interval resampling the cases and then the runs inside each case: [-0.500, -0.042].

An interval that includes zero means no change.
- haiku alone: -0.250, 95% interval over cases [-0.500, +0.000].
- sonnet alone: -0.250, 95% interval over cases [-0.500, +0.000].

## Pass rates

| criterion | case | baseline | candidate |
|---|---|---|---|
| R1 | 01-ordinary-save | 6/6 | 6/6 |
| R1 | 02-review-needed-then-saved | 6/6 | 6/6 |
| R1 | 03-blocked-nothing-saved | 6/6 | 6/6 |
| R1 | 04-saved-indexing-pending | 6/6 | 6/6 |
| R1 | 05-debug-wants-request-id | 6/6 | 6/6 |
| R1 | 06-health-task-recoverable-error | 6/6 | 6/6 |
| R1 | all | 36/36 | 36/36 |
| R3 | 03-blocked-nothing-saved | 6/6 | 6/6 |
| R3 | all | 6/6 | 6/6 |
| R4 | 05-debug-wants-request-id | 6/6 | 6/6 |
| R4 | all | 6/6 | 6/6 |

## Runs without a usable result

None.
