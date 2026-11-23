---
exomem_id: ab7188b0-bf55-57e0-b62b-7c4d053e2e3e
name: technology-signal-lifecycle
description: "Follows a newly found vendor feature, tool, library or technique against one of the user's own projects, from first sighting through a scoped trial to a verdict. Use when the user weighs such an idea for a project, agrees to a trial of it, or reports what a trial showed, so every idea keeps one entry per project and is never rediscovered or tested twice."
status: active
created: 2026-09-05
updated: 2026-10-27
---

# Technology signal lifecycle

This is my way of handling a technology idea for one of my own projects. It keeps a single entry per idea and project in [[Technology Signals]], so that I always know what I have seen, what I decided, and what the evidence says.

## When it applies

Use it when all three hold:

1. The thing is concrete: a vendor feature, a tool, a library, a build or database technique.
2. It is tied to one of my projects, such as Pebble Ledger or Fernwatch, with a use I could state in one sentence.
3. The conversation is in one of three stages: I am weighing it, I have agreed to try it, or I am telling you how the trial went.

Work out the stage from what I say. I will not use labels for the stages.

## When it does not apply

- General interest, history, trivia or something a friend works on, with no project of mine attached. Keep that to the conversation.
- A book or article I read and want notes on. That is reading notes.
- A request to dig into a niche topic and verify sources. That is research.
- A routine version bump of a dependency I already use. That is an upgrade check.
- A code change someone wants reviewed.

When unsure whether a project is involved, ask one short question: "Is this for one of your projects?" Do not write anything until the answer is yes.

## Stage 1: I notice something

1. Say in one line which project and which use case the idea serves.
2. Look in [[Technology Signals]] for the same idea and project, including items marked `rejected`, `duplicate` or `superseded`. If one exists, update it. Never create a second item. If the earlier item was rejected, tell me why it was rejected and ask whether anything changed.
3. Create or update the item. Use `observed` when I only saw the idea, and `assessed` when I or you judged its value without testing it. Fill in `title`, `discovered_on`, `project`, `marginal_value` and a short `decision_rationale` using my reasons.
4. Keep the original. Capture the announcement or documentation page as a Source and put its address in `source_url`. Do not paste it into the item.
5. Write a compiled Insight only when something durable has been learned about the feature that would outlast this one trial, and link it in `compiled_insight`. A first sighting seldom earns one.
6. Do not create a Planning item. Do not start a test. An observed or assessed idea is not a candidate.
7. Reply with one sentence about what was saved and its state. Add a pointer to the test question only if I seem ready for it.

## Stage 2: I agree to a trial

1. A go decision has to be explicit and scoped: what part of the project, on what data, for how long. "That looks promising" is not a go. If the scope is missing, ask for it in one question.
2. If the idea has no item yet because we only discussed it in this conversation, create it at `assessed` first.
3. Move the item to `ready_to_test`. Write my scope in `test_scope` in my own terms, and put the reason in `decision_rationale`.
4. Add an item to [[Engineering Experiments]] with the scope, the time box and what result would count as success. Link it in `related_plan`. Set the commitment only because I said go.
5. Move the signal to `testing` only when I tell you the trial has started or evidence of it exists. Agreeing to a trial is not the trial.
6. Reply with the scope and the time box as you saved them, so that I can correct them.

## Stage 3: I report the outcome

1. Treat my report as the evidence. Copy the figures, durations, environments and failures I gave into `outcome_evidence`. Do not round them up, and do not add numbers I did not give.
2. Write `outcome_summary` in one or two sentences.
3. Choose the state from what I decided:
   - `rejected` when I drop it, with my reason in `decision_rationale`.
   - `adopted` when I keep it or roll it out.
   - `tested` when results are in and I have not decided yet.
4. If no item exists because the trial happened before anything was saved, create the item directly in its final state. Do not invent the earlier stages.
5. Close the linked item in [[Engineering Experiments]] to match: done when the trial ran, cancelled when it did not.
6. Write a compiled Insight when the outcome teaches something that holds beyond this project, for example a platform failure, and keep it to what the evidence shows. Link it in `compiled_insight`.
7. If the outcome changes a standing choice of the project, offer to write the Decision. Do not write it unasked.

## Rules for every stage

- Never infer `testing`, `tested` or `adopted` from an announcement, a vendor benchmark or my enthusiasm. Only my report or real artifacts count.
- Keep `rejected` and `duplicate` items. They are how a dropped idea stays dropped.
- One item per idea per project. The same feature for another project is another item.
- Do not edit this page, the collection contract or any existing note about the project because of a signal. Established decisions change only when I decide that.
- Say what was saved, where, and in which state. Do not mention internal fields, keys or storage unless I ask.

## Where results go

| Result | Destination |
| --- | --- |
| The signal and its state | [[Technology Signals]] |
| The trial intent and time box | [[Engineering Experiments]] |
| The original page or announcement | Sources |
| Durable knowledge | compiled Insight under Notes/Insights |
