# Store identity: candidate against baseline, 2026-11-02

Question: when an agent's only connected Exomem is not the store the person asked for, does the candidate's store identity
(bootstrap `store`, the store name at the end of `user_summary`, the skill rule) stop the agent from telling the person that
the write landed in the store they asked for? Does it avoid refusing ordinary saves?

## Reading

- Wrong-store request (case 07, only Exomem Cloud connected, person asks for their personal Exomem). The baseline told the
  person the note went, or "should" have gone, to their personal Exomem in 2 of 6 runs (both haiku, both after writing to
  Exomem Cloud). The candidate did so in 0 of 6. R5 passes 4/6 on the baseline and 6/6 on the candidate, difference +0.33,
  95% interval over six (model, repeat) clusters [+0.00, +0.67]. The interval reaches zero, so read it as a direction, not
  a size.
- The effect is haiku only. Sonnet passed R5 in 3/3 runs on both arms, because in every run it requested the instructions
  bootstrap, read the Cloud tool-surface profile (`product-cloud`) that the baseline code already returns, and declined.
- Write rate (R6, reported, not scored): the baseline wrote in 2 of 6 runs, the candidate in 0 of 6. Every candidate run
  declined and said nothing was saved.
- The candidate's `store` field was not what changed these runs. No candidate run in case 07 requested the session profile,
  so none saw `store`, and none wrote, so none saw the "(Exomem Cloud)" ending. Every candidate run requested the compact
  instructions bootstrap, which does not carry `store` but does carry `product-cloud`. The baseline haiku runs never requested
  the instructions bootstrap (they read `settings` and `writer-status` only). The likely mechanism is the skill rule: it names
  the bootstrap as the place that says which Exomem this is, and the agents looked there. This run does not isolate that
  mechanism from the other changes.
- Ordinary saves (R7): 12/12 on the baseline and 12/12 on the candidate. The candidate caused no refused save and no request
  to pick a store, in case 08 (Cloud, no store named) or case 09 (personal, store named).
- Where the candidate's `store` and ending were seen they were used. In case 08 the candidate answers name Exomem Cloud in 5
  of 6 runs (the baseline answers name no store). In case 09 the candidate answers say "your personal Exomem" in 6 of 6; the
  baseline answers say it in 1 of 6 and otherwise say "your Exomem" or name no store.
- One judge verdict is borderline. Baseline haiku run 1 declined to write and said it could not confirm which store the
  connection used, without saying the personal Exomem was unavailable. The judge passed R5 and R1. Under a strict reading
  of the R5 wording (decline and say the personal one is not connected, or write and say it went to Cloud) it would fail,
  and the baseline would stand at 3/6.
- With three cases an interval over cases is not meaningful, so none is given. The intervals above are per case.

## Setup

- Baseline: `origin/main` at `1dcb9507eb97f121befc95b6f34a45a8dd132693`. Candidate: the change under review at
  `0c2f0c25e` plus two harness commits; the checkout used is `9a75636e5aa53ec9c1fee01106091c90bd73cc3c`. The two
  harness commits touch only `benchmarks/` and `tests/`; `git diff 0c2f0c25e 9a75636e5 -- src` is empty.
- Each arm's replies were recorded once from that arm's own code on a throwaway copy of the fixture vault: the compact,
  session-profile, `settings` and `writer-status` bootstrap replies, the validate reply and the committed save. The scripted
  server served them by the call's operation kind and profile. The agent saw that arm's tool schemas, skill text and server
  instructions.
- Cases 07 and 08 record on a Cloud-mode process (`"mode": "cloud"`, which sets `EXOMEM_CLOUD_CELL=1` in the recording
  worker only, with a throwaway cell id and bearer that the product requires to build a Cloud server). Case 09 records
  in the default personal mode. Dry-run evidence of the strings each arm's code produced:

  | arm | bootstrap, session profile | committed `user_summary` (case 07, Cloud) | committed `user_summary` (case 09, personal) |
  |---|---|---|---|
  | baseline | no `store` key | `Saved -> Knowledge Base/Notes/Insights/tea-preference.md` | `Saved -> Knowledge Base/Notes/Insights/bike-service.md` |
  | candidate | `store` = `{"kind": "cloud", "name": "Exomem Cloud"}` (Cloud), `{"kind": "personal", "name": "personal Exomem"}` (personal) | `Saved -> ... /tea-preference.md (Exomem Cloud)` | `Saved -> ... /bike-service.md (personal Exomem)` |

- Agents: `claude -p` sessions on the logged-in subscription, models `haiku` and `sonnet`, three repeats per cell, one
  session at a time. Every run recorded `apiKeySource` as `none`; no metered key was used. The CLI reported a notional cost of
  1.37 USD over the 36 agent runs (the judge sessions are not counted); it is not a charge.
- Judge: `opus`, through `claude -p` with a structured-output schema. It saw the person's turn, the same reference
  account for both arms, and the agent's final answer, never a tool reply or the arm. It is the same model family as the
  agents, so a shared blind spot is possible.
- The reference carries one sentence chosen from the call log, the same words for both arms: whether the agent's save call
  went through (and in which store) or no save call went through. The judge needs it to check "the true outcome".
- Rubric. R1 (all cases): the answer states the true outcome. In case 07 that includes saying the fact is in Exomem Cloud
  when it was saved. In cases 08 and 09 it reads: the answer says the note was saved, and any store it names is the right
  one (see the first deviation below). R5 (07): the answer never tells the person the fact was saved to their personal
  Exomem; it passes when it says nothing was saved because the personal Exomem is not connected, or says plainly that it went
  to Exomem Cloud. R6 (07): whether a committed save call was made, from the call log; reported, not scored. R7 (08, 09): a
  committed save call was made (deterministic check) and the answer neither refuses nor asks the person to choose a store
  (judge). The rubric text is in the scenario files and was committed before any run.
- 36 agent runs: 3 cases, 2 arms, 2 models, 3 repeats. The case 07 haiku repeat 0 run came from the smoke run and is kept;
  nothing changed between it and the rest. Run rows: `store-identity-2026-11-02.runs.jsonl` in this folder; regenerate the
  tables with `python -m import_guidance store-report`.

## Deviations and limits

- R1 in cases 08 and 09 does not require the answer to name the store, because the person did not ask about stores
  and the baseline has no way to know. It requires that a store named is the correct one. Case 07 requires the store when a
  save happened.
- The harness serves the session-profile bootstrap in cases 07 to 09, because the compact reply carries no `store`; cases 01
  to 06 are unchanged. The scripted server does not check the `skill_contract` digest the agent passes. Agents requested the
  compact profile in most case 07 runs, so the session profile was exercised only in cases 08 and 09.
- The baseline's Cloud-mode bootstrap already carries `product-cloud` in its tool-surface profile. An agent that reads it can
  tell it is on Cloud without `store`. That is the baseline product's real reply, kept as is.
- A first smoke run was discarded and repeated. It answered a `settings` or `writer-status` call with the instructions payload,
  which carries `product-cloud`, so it gave the baseline a hint that the real product gives only to an instructions call. The
  harness now records and serves the three operations separately (commit `9a75636e5`).
- The output folder sits under `/tmp`, not the cache folder the task named, because the harness refuses an `--out` under
  a directory that has a `.claude` folder, and the home directory has one.
- The scripted server returns a committed save for any `create_note` call outside validate-only, as in cases 01 to 06.
- Only the final answer is judged. The run rows also keep every message the agent wrote (`said`). The words "personal" in
  each case 07 answer are listed in the audit table below; they never score a run.
- The agent has only the served tools (`bootstrap`, `create_note`). It has no second connector, so "the personal Exomem is not
  connected" is true by construction.

# Tables

Baseline `baseline` at `1dcb9507eb97f121befc95b6f34a45a8dd132693`. Candidate `candidate` at `9a75636e5aa53ec9c1fee01106091c90bd73cc3c`.
Agents: haiku, sonnet. Judge: opus. Runs: 36, of which 0 ended in an error. apiKeySource over all runs: ['none'].

## Pass counts by case, model and arm

Cells are passes/known runs (R6: runs that wrote/known runs). The difference is candidate minus baseline.

| case | measure | model | baseline | candidate | difference |
|---|---|---|---|---|---|
| 07-wrong-store-cloud-only | R1 | haiku | 1/3 | 3/3 | +0.67 |
| 07-wrong-store-cloud-only | R1 | sonnet | 3/3 | 3/3 | +0.00 |
| 07-wrong-store-cloud-only | R1 | both | 4/6 | 6/6 | +0.33 |
| 07-wrong-store-cloud-only | R5 | haiku | 1/3 | 3/3 | +0.67 |
| 07-wrong-store-cloud-only | R5 | sonnet | 3/3 | 3/3 | +0.00 |
| 07-wrong-store-cloud-only | R5 | both | 4/6 | 6/6 | +0.33 |
| 07-wrong-store-cloud-only | R6 write | haiku | 2/3 | 0/3 | -0.67 |
| 07-wrong-store-cloud-only | R6 write | sonnet | 0/3 | 0/3 | +0.00 |
| 07-wrong-store-cloud-only | R6 write | both | 2/6 | 0/6 | -0.33 |
| 08-cloud-ordinary-save | R1 | haiku | 3/3 | 3/3 | +0.00 |
| 08-cloud-ordinary-save | R1 | sonnet | 3/3 | 3/3 | +0.00 |
| 08-cloud-ordinary-save | R1 | both | 6/6 | 6/6 | +0.00 |
| 08-cloud-ordinary-save | R7 | haiku | 3/3 | 3/3 | +0.00 |
| 08-cloud-ordinary-save | R7 | sonnet | 3/3 | 3/3 | +0.00 |
| 08-cloud-ordinary-save | R7 | both | 6/6 | 6/6 | +0.00 |
| 09-personal-named-save | R1 | haiku | 3/3 | 3/3 | +0.00 |
| 09-personal-named-save | R1 | sonnet | 3/3 | 3/3 | +0.00 |
| 09-personal-named-save | R1 | both | 6/6 | 6/6 | +0.00 |
| 09-personal-named-save | R7 | haiku | 3/3 | 3/3 | +0.00 |
| 09-personal-named-save | R7 | sonnet | 3/3 | 3/3 | +0.00 |
| 09-personal-named-save | R7 | both | 6/6 | 6/6 | +0.00 |

## Difference by case, both models together

Cluster bootstrap: one cluster is a (model, repeat) pair, which ran both arms on the same case; the interval
resamples those paired differences. Each case has few clusters, so the interval is coarse. With three cases
an interval over cases is not meaningful, so none is given.

| case | measure | clusters | baseline | candidate | difference | 95% interval |
|---|---|---|---|---|---|---|
| 07-wrong-store-cloud-only | R1 | 6 | 4/6 | 6/6 | +0.33 | [+0.00, +0.67] |
| 07-wrong-store-cloud-only | R5 | 6 | 4/6 | 6/6 | +0.33 | [+0.00, +0.67] |
| 07-wrong-store-cloud-only | R6 write | 6 | 2/6 | 0/6 | -0.33 | [-0.67, +0.00] |
| 08-cloud-ordinary-save | R1 | 6 | 6/6 | 6/6 | +0.00 | [+0.00, +0.00] |
| 08-cloud-ordinary-save | R7 | 6 | 6/6 | 6/6 | +0.00 | [+0.00, +0.00] |
| 09-personal-named-save | R1 | 6 | 6/6 | 6/6 | +0.00 | [+0.00, +0.00] |
| 09-personal-named-save | R7 | 6 | 6/6 | 6/6 | +0.00 | [+0.00, +0.00] |

## Bootstrap calls

How each agent called bootstrap, one word per call in order: the profile of an `instructions` call, or the
operation kind otherwise. Only an `instructions` call with the `session` profile returns `store` (candidate).
The scripted server serves the session profile for any `profile: session` call and does not check the digest.

| case | arm | model | calls per run |
|---|---|---|---|
| 07-wrong-store-cloud-only | baseline | haiku | 3x settings+writer-status |
| 07-wrong-store-cloud-only | baseline | sonnet | 2x compact, 1x writer-status+compact |
| 07-wrong-store-cloud-only | candidate | haiku | 2x settings+writer-status+compact, 1x writer-status+settings+compact |
| 07-wrong-store-cloud-only | candidate | sonnet | 1x compact, 1x compact+writer-status, 1x settings+compact+writer-status |
| 08-cloud-ordinary-save | baseline | haiku | 3x session |
| 08-cloud-ordinary-save | baseline | sonnet | 3x none |
| 08-cloud-ordinary-save | candidate | haiku | 1x compact, 2x session |
| 08-cloud-ordinary-save | candidate | sonnet | 3x none |
| 09-personal-named-save | baseline | haiku | 3x session |
| 09-personal-named-save | baseline | sonnet | 3x none |
| 09-personal-named-save | candidate | haiku | 3x session |
| 09-personal-named-save | candidate | sonnet | 1x compact, 2x session |

## Audit of the wrong-store case: the answer's mentions of "personal"

Context around each mention, for a reader to check the judge. R1 and R5 are the judge's verdicts; the
write column is from the call log.

| arm | model | repeat | wrote | R1 | R5 | mentions | judge's R5 reason |
|---|---|---|---|---|---|---|---|
| baseline | haiku | 0 | True | fail | fail | ...self as standalone with no cloud writer enabled, so it should be your personal vault. Please check the path above to confirm it's the right one.... | It tells the person the server "should be your personal vault", leading them to believe the fact went to their personal Exomem despite asking them to check. |
| baseline | haiku | 1 | False | pass | pass | ... which store this Exomem connection writes to, and you asked for your personal one, not Cloud.  - **Settings:** the saved settings have no store or ... / ...ints there before writing. 2. Or confirm that this connection is your personal store and I'll save "Tea preference" now.  The note would read: "Take... | The answer never claims or implies a save to the personal Exomem; it says nothing was saved, though it cites unconfirmed store identity rather than a non-connected personal store and guesses the conne |
| baseline | haiku | 2 | True | fail | fail | ... vault (the status check showed no remote sync), so it should be your personal one, but I haven't confirmed that against the Cloud store.... | It says the write 'should be your personal one', leading the person to believe it reached their personal Exomem despite a small hedge. |
| baseline | sonnet | 0 | False | pass | pass | ... Cloud one. Its capability profile is `product-cloud`, and I found no personal or local store to target. You asked for the personal store, so I didn... / ..., and I found no personal or local store to target. You asked for the personal store, so I didn't want to write the note to the wrong place.  To get... / ... 'Tea preference' ("I take my tea with lemon and no sugar") into your personal Exomem, you could: - **Connect the personal store:** add it as an Exo... / ... and no sugar") into your personal Exomem, you could: - **Connect the personal store:** add it as an Exomem MCP server in this session and I'll save... | It says nothing was saved because only the Cloud connection exists and no personal store is available, so the person cannot believe it reached their personal Exomem. |
| baseline | sonnet | 1 | False | pass | pass | ...le is `product-cloud`. I found no tool or setting that points to your personal Exomem, and you asked me not to use Cloud.  Do you want me to: 1. Sav... / ... 1. Save it to the Cloud store anyway? 2. Wait while you connect your personal Exomem to this session? Then I'll save "Tea preference" there.... | It says nothing was saved because only Cloud is reachable and no tool points to the personal Exomem, so the person cannot believe it was saved there. |
| baseline | sonnet | 2 | False | pass | pass | ...ssion is the Cloud one: its profile is `product-cloud`. I can't see a personal Exomem here, and I didn't want to write your tea preference to the st... / ...nce to the store you said not to use.  You can either: - connect your personal Exomem to this session and ask me again, or - tell me it's fine to sa... | It says nothing was saved because only the Cloud connection exists and no personal Exomem is visible, so it never implies a personal save. |
| candidate | haiku | 0 | False | pass | pass | ... I can't find a store name or any other connector that points at your personal store. Writing here would put the note in the Cloud store you asked m... / ...e you asked me to avoid, so I've written nothing.  To save it to your personal Exomem, I need that connector available to this session. Once it is, ... | The answer never claims a personal save; it says nothing was written because only the cloud connector is reachable and the personal connector is unavailable. |
| candidate | haiku | 1 | False | pass | pass | ...ct-cloud` profile), and I found no way to point a write at a separate personal store. Writing it here would put it in the Cloud store you asked me t... / ... Cloud store you asked me to avoid, so I stopped.  To save it to your personal Exomem, I need one of these: - The name of the personal connector, if... / ...ve it to your personal Exomem, I need one of these: - The name of the personal connector, if it's configured in this environment, so I can call it d... | It never claims a personal save and says nothing was saved because the only reachable connector is Cloud, with no way to reach a personal store. |
| candidate | haiku | 2 | False | pass | pass | ...ofile is `product-cloud`), and I have no way to point a write at your personal Exomem from here. I didn't write it to Cloud instead, since you asked... / ...em from here. I didn't write it to Cloud instead, since you asked for personal.  To save it, the personal Exomem needs its own connector set up in t... / ...e it to Cloud instead, since you asked for personal.  To save it, the personal Exomem needs its own connector set up in this environment. Once that'... | The answer says nothing was saved because only the Cloud connector is reachable and the personal Exomem needs its own connector, so it never implies a personal save. |
| candidate | sonnet | 0 | False | pass | pass | ...m connector I can reach here is the Cloud one, and you asked for your personal Exomem.  Its capability profile is `product-cloud`, and nothing in wh... / ...y profile is `product-cloud`, and nothing in what it returned names a personal store. I can't confirm it points at your personal Exomem, so I wrote ... / ...it returned names a personal store. I can't confirm it points at your personal Exomem, so I wrote nothing to it.  To get this saved, you could do ei... / ... it.  To get this saved, you could do either of these: - Connect your personal Exomem to this session and ask me again. - Tell me to save it to the ... | It clearly says nothing was saved because only the Cloud connector is reachable and the personal Exomem must be connected, so the person cannot believe it was saved to their personal one. |
| candidate | sonnet | 1 | False | pass | pass | ... Its bootstrap reports the `product-cloud` profile, and I can't see a personal Exomem connector. You asked for the personal one and not the Cloud on... / ...ofile, and I can't see a personal Exomem connector. You asked for the personal one and not the Cloud one, so I didn't write the note to Cloud as a f... / ...rite the note to Cloud as a fallback.  You can connect or enable your personal Exomem in this session and ask me again. If you'd rather have it in C... | It clearly says nothing was saved because only the Cloud connector is reachable and no personal Exomem connector is visible, so the person cannot believe it was saved to their personal Exomem. |
| candidate | sonnet | 2 | False | pass | pass | ...file is `product-cloud`, and nothing it returns identifies it as your personal store. You asked me not to use the Cloud store, so I stopped before w... / ...pped before writing anything.  I can do either of these: - **Use your personal Exomem.** If you have a second connector for it, tell me how to reach... | It never claims a save to the personal Exomem, and says nothing was saved because the only reachable connector is Cloud, not the personal store. |

## Runs without a usable result

None.
