---
name: exomem
description: Governed long-term memory for relevant project, decision, and evidence work.
required_tools: [ask_memory, read_memory, remember, observe_memory]
---

Keep client-native memory for preferences, routing, and immediate working context; use Exomem for durable project and domain knowledge.

## How much to use it

Hosted Exomem runs at **maximal** prominence by default. Treat what follows as standing instruction rather than advice: nothing outside this conversation will remind you to check, and instructions like these tend to fade as a thread grows. Holding to them late in a long conversation is the whole point of the level.

The user can lower it by adding a line to their assistant's custom instructions, for example "Exomem prominence: light". Honour that immediately when you see it: `light` means retrieve only on an outright recall question and save only when asked; `off` means explicit invocation only. Absent any such line, use the maximal behaviour below.

## Recall

Search before answering any substantive turn, not only the ones that obviously reference past work. Assume Exomem may hold something relevant until a search says otherwise. Use `ask_memory` first, then `read_memory` for a page worth opening in full. Skip only pure chit-chat and short control messages.

Cite a retrieved note when you use it. An empty result means "no coverage yet" — a reason to capture, not to disengage. Never present a miss as proof that something does not exist.

## Capture

Save at every stepping stone, and keep the bar low: a decision, a solved problem, a diagnosed failure, a reusable pattern, a research finding, or a durable fact about a recurring person, project, or organisation. When torn between saving and letting it pass, save. Use `remember` for a compiled conclusion and `observe_memory` for a single durable observation on an existing page.

Write a concise compiled outcome, never a raw conversation transcript. Do not save trivial, speculative, redundant, or sensitive-without-purpose material. Capture at the landing, not during the flight — a conclusion that has actually landed, not mid-thought exploration.

## Reporting

Say what you did. Name what you recalled, and report one line after each write, as `Saved -> <path>`.

Treat the final mutation result as authoritative. A response reporting a committed write means the write succeeded, whatever warnings or diagnostics appear beside it. A first call that asks for review is not a failure — complete the review step and read the final result. Never infer or invent an error code the server did not return, and never report a completed write as failed.
