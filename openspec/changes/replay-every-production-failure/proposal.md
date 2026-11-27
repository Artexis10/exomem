# Replay every production failure from one permanent set

## Why

Production failures of agents that use Evermatter are replayed on real models from two places today:

- The incident runner (`benchmarks/membench/outcome/incidents/`) holds 35 cases in six files. Its README says "A case is never removed or held out".
- 17 incident-derived cases live outside the repository, with their seeds and answer keys. The session-close engine (`benchmarks/import_guidance/session_close.py`) plays them in the skills-only client modes that the incident runner lacks. Four scripts outside the repository score them.

So a fix PR cannot show which production failures it replayed. The external cases cannot be rerun from a clean checkout. The external scorers decide which tools write and which calls were refused from hand-kept lists of tool names and refusal strings. On 2026-11-27, three recall fixes were proven by replaying external incident cases through an uncommitted local bench override. The stored run rows name a dirty hash whose diff no longer exists.

The operator set the direction: one real-model replay set, with every production failure as a permanent case, and the existing incident replays folded into it.

## What Changes

- The incident runner becomes the one replay set for production failures. Its case files are the only home of a production-failure case.
- The runner gains the checks that the external answer keys need. These are:
  - a named page surfaced by a read before a named turn's answer, or before a write;
  - a turn with no read at all;
  - pages changed, unchanged or created after the session;
  - per-turn tool checks that match a door operation, not only a tool name.
- The runner reads which calls read, write or were refused from the arm: the `readOnlyHint` annotation that the arm's server gives each tool (which covers `recall_context`), the effect of each door operation in the arm's operation registry, and the error code in each result. No check that this change adds or ports matches a hand-kept list of tool names or refusal text. Answer content is judged by the blind judge against the case's reference, never by matching words in the answer.
- The runner gains the skills-only client class that the 2026-11-24 to 2026-11-27 incidents happened on, over both of its transports.
- Each case file keeps its existing `incident` line (a date and a neutral mechanism). The runner now refuses a case file without one. The link from a case to a private incident record stays in the operator's vault.
- A committed admission record holds the proof for each case: its digest, the arms' commits, the client class, the model, the repeats and the per-arm counts. The runner refuses a case without a matching admission line. A positive case is admitted when it fails on an arm without the fix. A guard case names the positive case that it protects and is admitted when it passes on both arms.
- Every ported or new case is a fresh rewrite of the failure's mechanism by a lane that never saw the original turns. The rewrite changes the facts of the subject's life, not only the names.
- The 17 external incident-derived cases and the two held-out follow-up cases used while iterating on 2026-11-27 are ported. The four out-of-repository scorers are retired for incident cases.

The nine workflow-skill cases beside the external incident cases are guidance cases, not production failures. They stay out of scope.

## Capabilities

### New Capabilities

- `production-failure-replays`: the one permanent real-model replay set for production failures, with its admission record, client classes and checks.

### Modified Capabilities

None. The session-close engine and its suites keep their contracts. Its skills-only staging moves to a module that both engines import.

## Impact

- Code: `benchmarks/membench/outcome/incidents/` (checks, client class, seed directories, admission record), the client staging in `benchmarks/import_guidance/session_close.py`, the README and their tests. No product code changes.
- Cases: 19 cases ported into `benchmarks/membench/outcome/incidents/cases/`, each with its admission line. The 35 existing cases get a legacy admission line that names the commit that added them, because their pre-fix arms are not recorded.
- Rewrite work: the external cases rest on three seed vaults, about 139 files in the recall folder alone, including skills and collection manifests whose wording some mechanisms depend on. Every seed file is rewritten as well as every turn. This is the largest part of the port.
- Retired: the four out-of-repository scorers for incident cases.
- Cost: the README asks for three repeats. A full run of 54 cases on two arms is 324 sessions. At the README's $0.2 to $1 of notional usage per session, that is $65 to $324 of subscription usage, which is not metered spend. Runs with the `openai` actor are metered and need agreement before they run. Wall-clock time is unverified.
- Not covered: the ChatGPT and Cloud plugin evaluations stay operator-run on their own surfaces. A production failure on those surfaces gets a case here only when an actor of this runner can play its client class.
