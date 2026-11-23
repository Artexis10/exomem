---
exomem_id: fd3c9528-b011-5520-ab2a-a76d39c05ebb
type: collection
title: Technology Signals
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: sqlite
  source: Signals
  format_version: 1
item_schema:
  natural_key: [signal_key, project]
  fields:
    signal_key:
      type: string
      required: true
    title:
      type: string
      required: true
    discovered_on:
      type: date
      required: true
    project:
      type: string
      required: true
    state:
      type: enum
      required: true
      enum: [observed, assessed, ready_to_test, testing, tested, adopted, rejected, duplicate, superseded]
    source_url:
      type: string
    compiled_insight:
      type: link
    related_plan:
      type: link
    marginal_value:
      type: enum
      enum: [high, medium, low, none]
    decision_rationale:
      type: string
    test_scope:
      type: string
    outcome_evidence:
      type: string
    outcome_summary:
      type: string
item_filename:
  version: 1
  fields: [signal_key, project]
item_presentation:
  version: 1
  title: title
  summary: [state, project, discovered_on, marginal_value]
---

# Technology Signals

One item stands for one meaningful vendor feature or technique, seen against one of my projects. The same feature seen for two projects is two items, because its value and its test differ by project. The item follows the signal from first sighting to a verdict, so the same idea is neither rediscovered nor tested twice.

## What belongs here

A signal is a feature, tool, library or technique that could change how one of my projects is built or run. Gossip, general news and background reading are not signals. A signal needs a named project and a use case that I could state in one sentence.

## Where the pieces go

- The original text, announcement or documentation page goes to Sources. The item points to it with `source_url` and does not copy it.
- What I learn that stays true goes to a compiled Insight, linked in `compiled_insight`. Write an Insight only when the knowledge outlasts this one test.
- An executable test goes to Planning as an item with its own scope and horizon, linked in `related_plan`. The signal item never holds the task list.

## Lifecycle rules

- `observed` and `assessed` are not candidates. They mean that I saw the feature, or that I judged its value on paper. Nothing here promises a test.
- `ready_to_test` needs a deliberate, scoped go decision from me, with the scope written in `test_scope`. Interest alone does not qualify.
- `testing`, `tested` and `adopted` need real evidence in `outcome_evidence`. Never infer them from an announcement, a benchmark in a blog post or a promise that something works.
- `rejected` and `duplicate` items stay. They stop the same signal from coming back as news.
- `superseded` names the newer signal that replaced this one.

## Writing an item

Give each signal a short stable `signal_key` that names the feature and not the date. `marginal_value` says how much the feature would add beyond what the project does today. `decision_rationale` holds the reason for the current state in one or two sentences, with the numbers I gave.
