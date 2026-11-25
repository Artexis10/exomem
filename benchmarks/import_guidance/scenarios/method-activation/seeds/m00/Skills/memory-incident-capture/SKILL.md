---
exomem_id: e4a2feb3-06e1-55e6-a844-ad47026059b4
name: memory-incident-capture
description: "Logs a failure of the assistant's long-term memory the moment the user points it out: something they asked to keep is missing, was filed in the wrong place, was never saved, or came back stale or wrong. Use when the user says the assistant forgot, lost, misplaced or contradicted something it should have retained, so the occurrence is written down first and diagnosed afterwards."
status: active
created: 2026-09-12
updated: 2026-10-14
---

# Memory incident capture

This is how I want a failure of the assistant's memory handled. Each failure becomes one entry in [[Memory Incidents]]. The entry is written before anyone tries to explain it.

## When it applies

Use it when I say, in any words, that the assistant's memory did not do its job:

- I told it something and later it behaves as if I never did.
- I asked it to keep something and the thing is not there.
- Something was saved, but in the wrong page or under the wrong subject.
- It should have saved something from the conversation and did not.
- It gave back an old value after I had given a newer one.

## When it does not apply

- I want a fact changed because the world changed. Update the page that holds it.
- I changed my mind about a decision.
- My own forgetting: a name, a date, a birthday. That is a human memory and has nothing to do with this ledger.
- A product feature request. Note it in conversation and do not file it as an incident.
- I am talking about a physical object that has the word memory in its name.

## Steps

1. Capture the concrete occurrence first. Write one item with the date, the surface where the failure showed, my words, what I expected and what actually came back. Give the item a short key that names the occurrence, such as the subject and the date.
2. Use my own wording in `symptom`. Where I gave a date, a name or a place, keep it exactly. Leave `severity` empty unless I rate it.
3. Do not fold the new item into an earlier one, even if the same kind of failure happened before. Link them only in the diagnosis.
4. Say plainly to me what was saved: "Logged as an incident: ..." in one sentence, with no storage details.
5. After the item exists, diagnose. Check whether the original was ever saved, where it landed, what the last read returned, and whether a newer value overwrote it. Add what you find to the same item as the diagnosis and set `status` to `diagnosed`.
6. If the lost or misplaced thing can be repaired now, offer the repair to me. Make it only after I say yes. A repair does not replace the incident.
7. If you cannot tell what happened, say so, leave the status `open`, and say what would have to be checked.

## Rules

- The incident is written even when the diagnosis is unfinished, and even when the fix is obvious.
- Do not argue with me about whether I said it. My report is the occurrence; the diagnosis may later show the cause.
- Never edit an earlier incident to make it look like a new one. Add a new one.
- Do not delete incidents once fixed.

## Where results go

| Result | Destination |
| --- | --- |
| The occurrence and its diagnosis | [[Memory Incidents]] |
| A repaired fact | The page that should have held it |
