# Exomem

Exomem is a living knowledge base for your AI assistants, not a dump of chat history. It preserves decisions, sources, evidence, relationships, plans, and useful conclusions as organised knowledge you can revisit, correct, and build on.

As you work, Exomem helps your assistant bring relevant context forward, connect ideas, identify what has changed, and preserve lasting outcomes when your settings and assistant support it. Answers can point back to the notes and evidence behind them. Outdated information can be reviewed or superseded instead of silently piling up.

Pick up projects in another conversation or across supported AI assistants connected to the same Exomem account. Exomem does not automatically import your past conversations or access arbitrary files on your device. Connect an existing Exomem Cloud account; registration, invitations, and billing are handled separately on the website.

## Getting started

Install the plugin and connect your Exomem account. Check the account shown on the sign-in page before approving access. Then chat normally. When something is worth keeping, ask Exomem to remember it; in a new chat, ask about what you saved or where you left off.

For example:

- "Where did we leave off on this project, and what changed?"
- "What have we decided, and what evidence supports it?"
- "Review this work and preserve the conclusions that matter."

Give the assistant the information to save or name the project you mean. Exomem can also help during ordinary work according to your chosen settings. It does not automatically import every old conversation.

## Data handling

MCP tool arguments, including user turns sent to `recall_context`, reach the authenticated Exomem Cloud service. Reads return authorized account content; successful writes persist governed memory in that account. OAuth selects the account, and this package reaches Exomem only through that admitted MCP connection. It does not import a local vault or share authorization between accounts. Your AI provider also processes the conversation and tool results under its policies.

## Surface support

Skills and the remote MCP connector carry the portable operating contract. Trusted lifecycle hooks support Claude Code and Cowork; they do not run in Claude Chat. Hooks request native MCP activation rather than injecting a context packet.

## Release notes

Updates the Dot E branding and memory workflow guidance. Trusted lifecycle hooks support activation, governed capture and local continuation on supported Claude Code and Cowork installations. Claude Chat uses the MCP connection and skills without command hooks.

Publisher: Substrate Systems OÜ.

[Documentation](https://github.com/Artexis10/exomem/blob/main/QUICKSTART.md) · [Privacy policy](https://substratesystems.io/exomem/privacy) · [Terms of service](https://substratesystems.io/exomem/terms) · [Support](https://substratesystems.io/exomem/support)

## Claude lifecycle hooks

On supported Cowork/Claude Code surfaces, trust the plugin's command hooks through the normal client approval flow. They invoke canonical Python scripts directly and require a real `python3` executable on the execution host; Python dependencies are not installed by this package. Hooks use only the admitted MCP connection. Retrieval requests context activation; capture asks the assistant to preserve governed outcomes. Neither bypasses the live server's write policy. Local hooks inspect the client transcript and workspace metadata for cadence and structural continuation, keeping bounded state in `CLAUDE_PLUGIN_DATA`, not inside the versioned package. Hook logs contain metadata only, not prompt or assistant snippets. Hooks do not execute in Claude Chat.
