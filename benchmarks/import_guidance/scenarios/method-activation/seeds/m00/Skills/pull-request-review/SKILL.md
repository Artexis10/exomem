---
exomem_id: 894e0784-2a0a-5e21-bae9-a6b7343c6a8c
name: pull-request-review
description: "Reviews a code change for correctness, tests, risk and readability and returns findings ordered by severity with a clear call on whether it is ready to merge. Use when the user shares a diff or pull request, or asks whether a change is ready."
status: active
created: 2026-03-19
updated: 2026-09-27
---

# Pull request review

How I want a code change reviewed.

## When it applies

I paste a diff, link a pull request or ask whether a change is safe to merge. It applies to my own side projects as much as to shared code.

## When it does not apply

- Choosing which tool or library to use in the first place.
- Planning future work.
- Writing the change itself.

## Steps

1. Read the description first and state in one line what the change claims to do.
2. Read the diff for correctness: boundary cases, error handling, data that could be lost.
3. Check that the tests cover the change and fail without it.
4. Check for risk: migrations, anything that cannot be undone, anything that touches secrets.
5. List findings in order of severity. Give each a file, a line, the problem and the suggested fix.
6. Finish with a plain verdict: ready, ready after the named fixes, or not ready.

## Rules

- Separate real defects from taste. Mark taste as optional.
- Do not rewrite the change unless I ask.
- Do not approve what you did not read.

## Where results go

The findings appear in the reply. A recurring lesson becomes an Insight only if I ask.

## Format of the review

Start with the one-line claim and the verdict. Then the findings, most serious first, each as a short paragraph with the location. End with the questions that only the author can answer. Keep the whole review within one screen when the change is small.

## Common mistakes

- Spending the whole review on naming and formatting while a data-loss bug sits in the diff.
- Praising a test that cannot fail.
- Asking for a rewrite when a small fix would do.
- Skipping the migration file because it looks mechanical.

## Example verdicts

- Ready after the named fixes: the logic is sound, but the error path drops a row and the test does not exercise it.
- Not ready: the change alters stored data and ships with no way back.

