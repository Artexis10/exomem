# Exomem

Exomem gives assistants governed long-term memory across conversations through its hosted service. It keeps original sources and evidence separate from compiled conclusions, and connects related knowledge without treating a summary as its source. The bundled skills explain recall, continuity, capture and review. Connect the remote MCP server through your provider's authorization flow. The live bootstrap response determines available tools and current policy.

## Getting started

Connect your existing Exomem Cloud account through OAuth. Use the bundled Exomem skill to call `bootstrap`, then `activate_context` with the raw user turn when context is needed. Reuse context already supplied for this turn; do not activate twice. The same canonical skills govern recall, capture and review on every supported surface. Workflow skills are user-invocable; separate commands and agents are not required copies of those workflows.

## Data handling

MCP tool arguments, including user turns sent to `activate_context`, reach the authenticated Exomem Cloud service. Reads return authorized account content; successful writes persist governed memory in that account. OAuth selects the account. This package does not import a local vault, read local service credentials, or share authorization between accounts. Your AI provider also processes the conversation and tool results under its policies.

## Surface support

Skills and the remote MCP connector carry the portable operating contract. Lifecycle hooks run only where the client supports and trusts them; they do not run in ordinary Claude Chat or ChatGPT conversations. The public OpenAI directory package deliberately contains no lifecycle hooks: current submission rules forbid them, although local Codex configurations support hooks. Local profile hook installation is a separate release-owned path. A delegated MCP activation is not a pre-injected context packet.

Publisher: Substrate Systems OÜ.

[Documentation](https://github.com/Artexis10/exomem/blob/main/QUICKSTART.md) · [Privacy policy](https://substratesystems.io/exomem/privacy) · [Terms of service](https://substratesystems.io/exomem/terms) · [Support](https://substratesystems.io/exomem/support)
