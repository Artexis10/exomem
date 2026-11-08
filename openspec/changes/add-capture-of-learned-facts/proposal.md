## Why

Exomem as shipped does not keep what an agent learns while it works. On the CL-Bench database task (development split, 2026-11-08), the shipped plugin saved nothing in 50 sessions. The result held under the `balanced` block, the `maximal` block, an added save sentence in the system prompt, and Stop and retrieve hook cues. The live-capture trial of 2026-11-07 found the same gap in hookless clients: they made no Exomem call and no write.

The same two sentences worked when they reached the agent as the user's own instruction. In a workspace `CLAUDE.md`, a save sentence produced 13 writes on 10 questions, where the system prompt had produced 0. A recall-and-save pair produced 62 writes and a recall in every one of 20 questions. Exomem's score then rose by 0.18 over the shipped arm and matched a plain memory folder. Claude Code delivers `CLAUDE.md` and `AGENTS.md` beside the user's message, not in the system prompt. Hosted clients do the same with account-level custom instructions.

So the missing piece is a short learning instruction in the user's voice, placed where each client reads user instructions. Exomem does not ship one today. Its copyable block states save policy for clients without skills, and `exomem setup` writes no instruction file.

## What Changes

- Exomem ships one **learning instruction**. It is two or three sentences in the user's voice: recall what Exomem holds about the work before you start, and save each reusable fact you establish as one concise observation, updating an existing note when one covers it. The text has one source in the package.
- `exomem setup` offers to place the learning instruction in the user-level instruction file of each client that it wires: `~/.claude/CLAUDE.md` for Claude Code and `~/.codex/AGENTS.md` for Codex. It writes the text between named markers, so a re-run updates it in place and `--uninstall` removes only that span. Interactive setup asks first. A non-interactive run places it only with an explicit flag. Setup never offers it while the effective prominence is `off` or `light`.
- The assistant guide, the remote quickstart and the prominence guide give the same text as paste-in text for ChatGPT, claude.ai and other hosted clients, ahead of the existing copyable block.
- The CL-Bench P arm ("Exomem as shipped") lays the learning instruction where setup puts it. The harness uses project settings only, so P writes it to the workspace `CLAUDE.md`, which reaches the agent through the same channel as the user-level file.

## Impact

- **Users:** a new setup step changes a file the user owns. The step asks first, shows the text, and is reversible. Users who already keep their own instructions can decline it.
- **Agents:** sessions recall and save more. That adds tool calls per task and exposes the write refusals that the write-path work already tracks.
- **Benchmarks:** the f35 deciding run waits for this change (amendment sequence 10). P then measures the shipped product with the instruction. PP is P plus the benchmark's own memory sentence, so it now carries the instruction too, and a PP run after this change is not pooled with an earlier one.
- **Unverified:** the hosted-client effect. No harness drives ChatGPT or claude.ai, so the paste-in route ships on the evidence that hosted custom instructions use the same user-side channel.
- No vault data, registry or tool-surface change.
