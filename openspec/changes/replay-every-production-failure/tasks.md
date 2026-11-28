# Tasks: replay-every-production-failure

## 1. Runner

- [ ] 1.1 Move the skills-only client staging into one launcher module that both engines import. Serve no instructions on stdio and HTTP.
- [ ] 1.2 Add the `deferred-skill` and `plugin-skills` client classes to the incident runner, with the claude.ai surface. Record the class per unit.
- [ ] 1.3 Attribute each tool call and its result to its user turn.
- [ ] 1.4 Add the checks: page surfaced by a read before a turn's answer or a write; turn with no read; pages changed, unchanged or created; tool or door operation on a turn.
- [ ] 1.5 Expose from `arm_side.py` each tool's `readOnlyHint` and each door operation's effect, and read read and write calls from them and refusals from result error codes in every check this change adds or ports. A test shows that `recall_context` classes as a read.
- [ ] 1.6 Accept a seed directory in the case folder, and include it in the case digest.
- [ ] 1.7 Refuse a case file without an `incident` line.

## 2. Admission

- [ ] 2.1 Add the admission record and refuse a case whose digest has no line. Print the line at the end of a run.
- [ ] 2.2 Add legacy lines for the 35 existing cases, each naming the commit that added it.
- [ ] 2.3 Rewrite the README's steps and cost line: fresh rewrite from the mechanism, positive and guard admission, the admission line in the fix PR, selection by what a change reaches, the release run, and the measured session cost.

## 3. Port

- [ ] 3.1 Port the 17 external incident cases and the two held-out follow-up cases. A lane that never saw the original turns or seeds writes each case and its seed vault from the mechanism alone, keeping any wording the mechanism depends on.
- [ ] 3.2 Admit each ported positive case on its pre-fix arm with three repeats, on the client class its incident happened on. Report a case that does not reproduce; do not port it.
- [ ] 3.3 Retire the four out-of-repository scorers for incident cases.

## 4. Proof and closure

- [ ] 4.1 Run the three 2026-11-27 recall cases on the arm before and after the merged fix with three repeats. Each check that the fix targets fails before and passes after; the open second later-turn lookup still fails and stays reported; each guard passes on both arms.
- [ ] 4.2 Synchronize and archive after merge, with the strict gate before and after.
