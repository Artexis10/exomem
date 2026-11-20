# Proposal

## Why

A user hands an agent a file, such as a meeting transcript, two screenshots or a PDF, and asks about it. The agent answers but does not keep the file. The user has to ask a second time before the original is preserved.

Real-model incident rounds on 2026-11-17 reproduced this. A per-turn capture-policy line that told agents to keep every handed file kept 1 file of 14 by exact bytes. The same code without the line kept 0 of 6. The agents received the line and declined the file as "a one-off check" or "a disagreement rather than a conclusion".

Two causes explain the result:

- The line reached agents only in server text. A measured Exomem change on 2026-11-08 found that agents treat server text as background. The same save sentence produced writes on 0 of 10 questions from the system prompt and on 19 of 20 from the workspace instruction file.
- The text in the user's own voice contradicts custody. The level blocks say "Never save transcripts" and "tentative claims stay quiet", and they ask for a durable conclusion before a save. The agents' refusals repeat those words.

## What Changes

- **The learning instruction asks for custody.** The packaged learning instruction asks the agent to keep each file the user hands over as a Source, by its exact bytes, and to report it in one line. Keeping a file verifies none of its claims. Files the agent works on in the user's own repository are excluded.
- **The level blocks stop contradicting custody.** "Never save transcripts" becomes "Never save chat transcripts". The quiet list ("one-offs ... and tentative claims") stays out of notes instead of staying quiet. The served baseline doctrine and the Stop-hook capture check take the same wording.
- **A text copy says it is a text copy.** A `save_source` call that stores pasted text reports that it kept the text, not the original file.
- **The incident harness measures what users get.** A run waits until recall stops abstaining with `index_warming`. It allows enough turns for an upload, uses the production upload configuration and adds negative cases.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-memory-onboarding`: the learning instruction asks for custody of handed files.
- `attachment-source-ingestion`: receiving a file makes it eligible for custody under `proactive_capture`, and a text copy is reported as a text copy.
- `attention-queue`: a Source kept for custody only is listed by name, outside the default attention set and the due-state projection.

## Impact

- `src/exomem/prominence.py` (`LEARNING_INSTRUCTION`, `_BASELINE_CAPTURE`) and the three guides that show the learning instruction.
- `docs/prominence.md` (Balanced and Maximal blocks), which stay under the ChatGPT instruction field's size limit.
- `src/exomem/_hooks/exomem_capture_nudge.py` (`REMINDER_SHORT`), the plugin copies and the cloud plugin artifacts.
- The `save_source` reply for content captures.
- `benchmarks/membench/outcome/incidents/` (recall readiness, turn cap, upload configuration, cases).

Existing users get the new instruction by running `exomem setup` again or by pasting it again. The behaviour depends on a model's judgement, so a real-model run decides the release.
