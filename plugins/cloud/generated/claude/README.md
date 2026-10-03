# Exomem

Keep what matters from your conversations and find it again when you need it. Exomem lets you save useful information, pick up a project in a new chat, and revisit what you decided and why. Ask in your own words; you do not need to organise folders or learn special commands. Connect your Exomem account to use the same saved knowledge across supported assistants. Answers can point back to the notes and sources they draw on, and you control how actively Exomem helps.

## Getting started

Install the plugin and connect your Exomem account. Check the account shown on the sign-in page before approving access. Then chat normally. When something is worth keeping, ask Exomem to remember it; in a new chat, ask about what you saved or where you left off.

For example:

- "Remember this for next time."
- "Where did we leave off with my project?"
- "What did we decide, and why?"

Give the assistant the information to save or name the project you mean. Exomem can also help during ordinary work according to your chosen settings. It does not automatically import every old conversation.

## Data handling

MCP tool arguments, including user turns sent to `activate_context`, reach the authenticated Exomem Cloud service. Reads return authorized account content; successful writes persist governed memory in that account. OAuth selects the account. This package does not import a local vault, read local service credentials, or share authorization between accounts. Your AI provider also processes the conversation and tool results under its policies.

## Surface support

Skills and the remote MCP connector carry the portable operating contract. Lifecycle hooks run only where the client supports and trusts them; they do not run in ordinary Claude Chat or ChatGPT conversations. The public OpenAI directory package deliberately contains no lifecycle hooks: current submission rules forbid them, although local Codex configurations support hooks. Local profile hook installation is a separate release-owned path. A delegated MCP activation is not a pre-injected context packet.

Publisher: Substrate Systems OÜ.

[Documentation](https://github.com/Artexis10/exomem/blob/main/QUICKSTART.md) · [Privacy policy](https://substratesystems.io/exomem/privacy) · [Terms of service](https://substratesystems.io/exomem/terms) · [Support](https://substratesystems.io/exomem/support)

## Claude lifecycle hooks

On supported Cowork/Claude Code surfaces, trust the plugin's command hooks through the normal client approval flow. They invoke canonical Python scripts directly and require a real `python3` executable on the execution host; Python dependencies are not installed by this package. Hooks use the admitted MCP connection, never a local service credential. Retrieval requests context activation; capture asks the assistant to preserve governed outcomes. Neither bypasses the live server's write policy. Local hooks inspect the client transcript and workspace metadata for cadence and structural continuation, keeping bounded state in `CLAUDE_PLUGIN_DATA`, not inside the versioned package. Hook logs contain metadata only, not prompt or assistant snippets. Hooks do not execute in Claude Chat.
