"""The organisation replay adapter: f34 ``organisation_autonomy_replay``.

Per situation and arm it builds the seed vault through the arm's own public MCP
doors with a model-free scripted client, runs a capture session and a
fresh-recall session through the switch-evaluation session executor, reads the
end state from files between them, and writes one run directory. The input
and run-file contract is ``SCHEMA.md`` beside this file.

The adapter reads situation files only. It never opens a key file, an arm map
or a custody manifest's other entries, and it never passes a situation's key
anywhere: a case file that carries one is refused.
"""
