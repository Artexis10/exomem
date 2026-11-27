# Design: replay every production failure from one permanent set

## Sources

The decision follows the published evaluation guidance for agents:

- Langfuse's evaluation documentation describes the improvement loop: production trace, failure label, dataset, experiment, evaluator, regression gate.
- Anthropic's "Demystifying evals for AI agents" and OpenAI's agent evaluation guide cover grading a trace on real models and comparing a change on the same cases.

This change builds that loop on the runner the repository already has. A production failure becomes a permanent case. A fix PR replays it on two arms. The case's checks grade the trace, and its admission line records the result.

## Decision: extend the incident runner, do not add a registry

The incident runner already has most of what the set needs:

- the rule that a case is never removed or held out;
- arms that are checkouts, with their commit and dirty state in the manifest;
- multi-turn sessions that resume one session per turn, a follow-up session and a blind judge;
- a store vault, an HTTP transport, transport faults and handed files;
- write tools read from the arm (`arm_side.py`).

A new registry with its own front end over both engines was the other shape considered. It would leave two engines to keep in step and a third place for cases. Extending one engine keeps one home, one schema and one runner.

## What the external keys need

The 17 external incident keys use these fields. Each maps to a runner check:

| Key field | Runner check |
|---|---|
| `reference` | The blind judge's reference (`judge`, existing) |
| `recap` | A recap page required, forbidden or optional: pages created (new); a forbidden recap is also a write |
| `expected_reads`, `trigger_turn`, `read_before_answer` | A named page surfaced by a read before the named turn's answer (new) |
| `read_before_write` | A named page surfaced by a read before the first write (new) |
| `methods` | The method pages, as named pages surfaced by a read (new, same check) |
| `must_change`, `must_not_change`, `new_pages` | Pages changed, unchanged or created after the session (new, generalising `seed_corrected`) |
| `destination` | The Records collection that receives the rows: `collection_row` (existing) |
| `expected_tools`, `turn_tools` | A named tool or door operation called on a named turn (new) |
| `forbidden_writes` | `no_write` (existing) |
| `expected_rows` | `collection_row` (existing) |
| `answer_must_state`, `answer_must_not_state` | Criteria in the judge's reference, not word matches |

A negative case that must make no read uses the "named turn makes no read" check. It counts only reads whose result surfaces a page or row of the vault, so reading the operating instructions does not fail a guard. A blind evaluator judged `answer_must_state` and `answer_must_not_state` on 2026-11-27, and the port keeps them as criteria for the judge.

"Surfaced by a read" means that the page's id or path appears in the result of a call that the arm classes as a read. The arm classes a call by the `readOnlyHint` annotation that its server gives the tool, which covers `recall_context`, a command without an entry in the typed tool table. For a door call it uses the effect of the named operation in the arm's operation registry. A recall that returns other pages does not satisfy the check, because a recall miss is the failure these cases replay.

## Admission record

The admission record is a committed JSON-lines file beside the case files. Each line holds:

- the case id, its digest over the case, its seed directory and its checks;
- its kind: positive, guard or legacy;
- for a guard, the positive case that it protects;
- the arms' commits, the client class, the model and the repeats;
- the per-arm passed, failed and unknown counts.

An admission run is the one run that may play a case with no matching line. It prints the line at the end, and the author commits it. Every other run refuses a case whose digest has no line, so a case cannot change without a new admission. A seed directory shared by several cases is part of each case's digest, so editing it needs an admission run for each of those cases. A fix PR shows its replays in its diff: it adds the line with the counts on the arm without the fix and on the fix arm. The run root itself stays local, under `/var/tmp`.

A positive case must fail on at least two of three repeats on the arm without the fix. Agent failures are random, so a rule of every repeat would refuse most real intermittent failures. A check that always fails would pass that test, so a fix PR's line must also show the targeted checks passing on at least two of three repeats on the fix arm. Where the counts are close, the README's rule applies: rerun with more repeats before the merge.

A guard case is not expected to fail without the fix, because it checks that a fix does not over-correct, for example that a general question gets no lookup. A guard names its positive case and must pass on at least two of three repeats on both arms. The 35 existing cases get legacy lines that name the commit that added them, because nobody recorded their pre-fix arms.

## Privacy

The repository is public. A real incident holds a person's turns and facts about their life and the people around them. The privacy gate checks the shape of a file, not its meaning, so it cannot catch a paraphrase. The defence is the authoring process. A lane that never saw the original turns writes each case from the `incident` mechanism alone. The lane invents the people, places, numbers and life facts. The operator's incident record names the case id, and the link stays in the vault.

A rewrite can lose the mechanism. Admission catches that for positive cases: a rewrite that passes on the arm without the fix is not admitted.

## Client classes

The runner's actors are `claude`, `claude-hookless` and `openai`. The session-close engine has two skills-only modes:

- `deferred-skill` serves the core skill as an uploaded skill.
- `plugin-skills` serves the plugin's skills.

In both modes the server serves no instructions, the tools are deferred behind a tool search, and the surface is claude.ai. A claude.ai incident with an uploaded skill replays on `deferred-skill`. The 2026-11-27 proofs ran on `plugin-skills`, so their ported cases are admitted again on the class that each incident happened on.

The session-close engine serves no instructions by patching a module global in a stdio launcher. The runner's store cases use its HTTP loopback. So the launcher becomes one module that both engines import, and it serves no instructions on both transports. The surface stays a run variable, recorded per unit, because it changes what a recall returns.

## Maintained tools checked

- **Inspect AI with `inspect_swe`** runs the Claude Code CLI with MCP servers and skills. It does not fit, for four reasons:
  - Its model traffic goes through Inspect's provider bridge with a placeholder token, so the CLI's subscription login and transport are not exercised. Its documentation shows no subscription path.
  - Plugins and hooks are not documented.
  - Its examples and tests run in a Docker or Kubernetes sandbox.
  - It documents no scripted list of user turns.

  The runner's custom parts would remain, and the dependency would be added on top.
- **Langfuse datasets and experiments** hold items with object inputs. An external runner can report per-item scores. The core is MIT-licensed and can be self-hosted. Langfuse could store and compare runs, but it does not run a Claude Code session on a seeded vault, so it replaces no part of the runner. It does not support repetitions yet. Adding it as the run store is a separate decision, not taken here.

## Cost and selection

A change replays the cases it can reach, under the eval-loop's selection, failures first:

| What the change alters | Cases it reaches | Arms and repeats |
|---|---|---|
| Skill, guidance, server-instruction or prompt text | Every case on a client class that loads that text | Two arms, one repeat |
| A tool or door operation | The cases that call it or check its effect | Two arms, one repeat |
| The model, the judge or the code that reads the answer | Every case | Two arms, one repeat |
| A fix for a production failure | Its targeted cases, in addition | Two arms, three repeats |

The targeted cases need three repeats on both arms, because the admission line shows at least two of three failing without the fix and at least two of three passing with it. A case that differs between the arms on one repeat is rerun with more repeats before the merge. The release run plays the whole set once on the release candidate, and reruns its failures.

Session cost comes from two samples:

- The session-close engine, which played the external cases: 88 sessions on 2026-11-27 cost a median of $0.18 of notional subscription usage each, with a maximum of $0.32 (`cost_usd` in `/var/tmp/exomem-skills-proof/**/runs.jsonl`).
- The incident runner, which plays the 35 existing cases: 209 sessions cost a median of $0.075, with a maximum of $0.37 (`notional_usd` in `/var/tmp/exomem-outcome-evals/runs/incidents-*/results.jsonl`).

Nothing was billed in either sample. At the higher median, these are the typical costs:

| Run | Sessions | Notional cost |
|---|---|---|
| A fix with three targeted cases and no text change | 18 | about $3 |
| A change to the shipped skills, which nearly every case loads, plus three targeted cases | about 126 | about $23 |
| A change to one workflow skill that only some client classes load, plus three targeted cases | about 68 | about $12 |
| A model or judge change, every case on two arms | 108 | about $20 |
| The release run | 54 | about $10 |

Runs with the `openai` actor are metered and need agreement before they run.

## Risks

- **A rewrite loses the mechanism.** Admission requires a failing pre-fix run for a positive case.
- **Proof outweighs product.** This change has no product code. Porting retires four scorers. The new checks replace the external scorers' page and order checks, so the scoring code moves, not grows.
- **Variance.** Three repeats cannot prove a difference. The README already tells readers to rerun suspect cases with more repeats before a merge.

## What would change this decision

- Most rewrites fail to reproduce on their pre-fix arm. Then a private registry outside the repository serves those cases better.
- Inspect gains a documented subscription login, plugins and hooks for Claude Code on the host.
