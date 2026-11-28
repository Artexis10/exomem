# Design

The MCP wrapper already knows the public tool that the caller used. Apply the default reply shape at that boundary after the normal egress checks. This uses the same path for file and store collections and leaves the release checks on rows, sources and links in place.

Omit only `rendered` when the public tool is `query_records` and the request omits a format. An explicit JSON, Markdown or CSV format keeps its current output for queries that support it. Typed and derived queries retain their existing format checks and reply shapes. Other tools and direct Records commands retain their current replies. Keep the existing query and rendering bounds, so this change cannot increase the number of rows or bytes the query admits.

The structured reply already contains the data needed for follow-up reads and edits. Reuse it to keep the default result small, as described in [Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents).
