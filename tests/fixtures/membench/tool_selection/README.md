# Recorded tool-selection sessions

Each pair of `.transcript.jsonl` and `.tap.jsonl` files is one real session of the
tool-selection evaluation against the grouped tool surface. The transcript is the
client's `stream-json` output. The tap log is what the MCP tap saw. The
`.end-states.json` file holds the case and the vault state before and after the
session.

Recorded on 2026-10-24 with Claude Code 2.1.310, model `claude-sonnet-5-5` and tool
search off. Two sessions: a Planning write that meets one argument refusal
(`plan-triage-item-s101`) and a Records read (`records-range-total-s101`).

Edits made after recording, all mechanical:

1. The run directory is replaced by `/run`, and the user id in a socket path by `uid`.
2. In the read session, each pair of escaped blank lines in tool-result text gets one
   space between the two. The privacy gate reads that pair as a network path.

To re-record, run one grouped session with `python -m membench.utility.tool_selection run`
and replace these files with the session's `transcript.jsonl`, `runtime/tap.jsonl` and
end states.
