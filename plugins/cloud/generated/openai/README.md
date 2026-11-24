# Evermatter

Evermatter is a living knowledge base for your AI assistants, not a dump of chat history. It preserves decisions, sources, evidence, relationships, plans, and useful conclusions as organised knowledge you can revisit, correct, and build on.

As you work, Evermatter helps your assistant bring relevant context forward, connect ideas, identify what has changed, and preserve lasting outcomes when your settings and assistant support it. Answers can point back to the notes and evidence behind them. Outdated information can be reviewed or superseded instead of silently piling up.

Pick up projects in another conversation or across supported AI assistants connected to the same Evermatter account. Evermatter does not automatically import your past conversations or access arbitrary files on your device. Connect an existing Evermatter account; registration, invitations, and billing are handled separately on the website.

## Getting started

Install the plugin and connect your Evermatter account. Check the account shown on the sign-in page before approving access. Then chat normally. When something is worth keeping, ask Evermatter to remember it; in a new chat, ask about what you saved or where you left off.

For example:

- "Where did we leave off on this project, and what changed?"
- "What have we decided, and what evidence supports it?"
- "Review this work and preserve the conclusions that matter."

Give the assistant the information to save or name the project you mean. Evermatter can also help during ordinary work according to your chosen settings. It does not automatically import every old conversation.

## Data handling

MCP tool arguments, including user turns sent to `recall_context`, reach the authenticated Evermatter service. Reads return authorized account content; successful writes persist governed memory in that account. OAuth selects the account, and this package reaches Evermatter only through that admitted MCP connection. It does not import a local vault or share authorization between accounts. Your AI provider also processes the conversation and tool results under its policies.

## Surface support

Skills and the remote MCP connector carry the portable operating contract. This public directory package contains skills and a remote MCP connection, without lifecycle hooks.

## Release notes

Updates the Dot E branding and memory workflow guidance. Connect an existing Evermatter account through OAuth. Includes skills and the remote MCP connection, without lifecycle hooks or local service credentials.

Publisher: Substrate Systems OÜ.

[Documentation](https://github.com/substrate-systems/evermatter/blob/main/QUICKSTART.md) · [Privacy policy](https://evermatter.ai/privacy) · [Terms of service](https://evermatter.ai/terms) · [Support](https://evermatter.ai/support)
