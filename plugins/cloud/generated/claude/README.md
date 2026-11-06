# Exomem

Exomem is a living knowledge base for your AI assistants, not a dump of chat history. It preserves decisions, sources, evidence, relationships, plans, and useful conclusions as organised knowledge you can revisit, correct, and build on.

As you work, Exomem helps your assistant bring relevant context forward, connect ideas, identify what has changed, and preserve lasting outcomes when your settings and assistant support it. Answers can point back to the notes and evidence behind them. Outdated information can be reviewed or superseded instead of silently piling up.

Pick up projects in another conversation or across supported AI assistants connected to the same Exomem account. Exomem does not automatically import your past conversations or access arbitrary files on your device. Connect an existing Exomem account; registration, invitations, and billing are handled separately on the website.

## Getting started

Install the plugin and connect your Exomem account. Check the account shown on the sign-in page before approving access. Then chat normally. When something is worth keeping, ask Exomem to remember it; in a new chat, ask about what you saved or where you left off.

For example:

- "Where did we leave off on this project, and what changed?"
- "What have we decided, and what evidence supports it?"
- "Review this work and preserve the conclusions that matter."

Give the assistant the information to save or name the project you mean. Exomem can also help during ordinary work according to your chosen settings. It does not automatically import every old conversation.

## Data handling

MCP tool arguments, including user turns sent to `recall_context`, reach the authenticated Exomem service. Reads return authorized account content; successful writes persist governed memory in that account. OAuth selects the account, and this package reaches Exomem only through that admitted MCP connection. It does not import a local vault or share authorization between accounts. Your AI provider also processes the conversation and tool results under its policies.

## Surface support

Skills and the remote MCP connector carry the portable operating contract. This public directory package contains skills and a remote MCP connection, without lifecycle hooks.

Lifecycle hooks are available in the full Exomem plugin. They add automatic recall, capture checks and continuation on Claude Code and Cowork. To use it in Claude Code, run `/plugin install exomem-cloud --marketplace Artexis10/exomem` instead of installing this edition.

## Release notes

Ships skills and the remote MCP connection for Claude Chat, Cowork and Claude Code, without command hooks or local scripts. Connect an existing Exomem account through OAuth. The full Exomem plugin adds lifecycle hooks.

Publisher: Substrate Systems OÜ.

[Documentation](https://github.com/Artexis10/exomem/blob/main/QUICKSTART.md) · [Privacy policy](https://substratesystems.io/exomem/privacy) · [Terms of service](https://substratesystems.io/exomem/terms) · [Support](https://substratesystems.io/exomem/support)
