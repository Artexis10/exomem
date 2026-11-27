"""The import request modes and the fields each one takes.

This module imports nothing, so the startup coverage check and the lease can read
the modes without loading the store's database libraries.
"""

from __future__ import annotations

#: Modes that write nothing. Lease and egress classify these as reads and every other mode as a mutation.
READ_ONLY_MODES = frozenset({"preview", "status"})
#: Each mode's required and optional request fields.
MODE_FIELDS = {
    "preview": ({"source_ref", "format"}, {"mapping", "members", "check_sum"}),
    "start": ({"source_ref", "format", "mapping"}, {"members", "reimport"}),
    "status": ({"continuation"}, set()),
    "cancel": ({"continuation"}, set()),
}
#: Every mode an import request may name; the Records tool entries must cover each one.
MODES = frozenset(MODE_FIELDS)
#: Formats whose source paths are single header names, not dotted JSON paths: a header may contain a dot.
# nosemgrep: ep-word-set -- The request grammar fixes these source formats.
HEADER_FORMATS = frozenset({"csv", "markdown-table"})
