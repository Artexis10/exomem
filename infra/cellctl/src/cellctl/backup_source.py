"""The backup Job's vault check (move-cloud-cells-to-local-storage D5) and media census (add-cloud-media-store D12).

A served cell restarted onto an emptied volume leaves cell-init refusing over
an empty /data/vault and /data/host. Backed up, they would replace the cell's
last good restore point, so the Job first asks the cell image's own
`exomem.vault._is_vault` whether its source holds a vault. The rule is the
image's, so it never drifts from cell-init's.

The command names a function in another package, so this module imports
only the standard library: the product suite (tests/test_backup_vault_check.py) loads it by path
and runs the command against the real exomem package.

The media census is the same kind of command: it names `exomem cell-media-census`
in the cell image, which prints the record the cell wrote (tests/test_media_drain_record.py runs it,
and checks that the cell's record parses here).
"""

import re
import shlex
from datetime import datetime

# The check's own answer, exit 3: the source holds no vault.
SOURCE_NOT_A_VAULT = "BACKUP_SOURCE_NOT_A_VAULT"
# Any other failure: the check could not run (an import error, a crash, an
# out-of-memory kill), which says nothing about the volume.
VAULT_CHECK_FAILED = "BACKUP_VAULT_CHECK_FAILED"
# The value-free codes a failed backup Job may leave as its termination
# message; cellctl logs no other message.
FAILURE_CODES = frozenset({SOURCE_NOT_A_VAULT, VAULT_CHECK_FAILED})

VAULT_CHECK_COMMAND = (
    "{ python3 -c 'import sys; from pathlib import Path; from exomem.vault import _is_vault; "
    "sys.exit(0 if _is_vault(Path(\"/data/vault\")) else 3)'; status=$?; "
    "[ \"$status\" -eq 0 ] || { "
    f"if [ \"$status\" -eq 3 ]; then code={SOURCE_NOT_A_VAULT}; else code={VAULT_CHECK_FAILED}; fi; "
    "printf '%s' \"$code\" > /dev/termination-log; exit 1; }; }"
)

# The census copies one file, so it needs the seconds that the cell image takes to start, not the minutes that a
# walk of the vault took; a census that does not finish is unknown.
MEDIA_CENSUS_SECONDS = 10
MEDIA_CENSUS_UNKNOWN = "unknown unknown"


def media_census_command(captured_at: str) -> str:
    """Append the media report to the termination message: "<count> <bytes> <census time>" on a line of its own.

    The cell image prints the record that the cell wrote beside its media ledger (D12). The Job mounts the volume
    read-only, so it reads that file and no store, lease or database. A record that is missing, linked, oversize
    or malformed, or a timeout, leaves "unknown unknown" and the hold start, which keeps a turned-off cell's media access.
    """
    unknown = f"{MEDIA_CENSUS_UNKNOWN} {captured_at}"
    return (
        f"{{ census=$(timeout {MEDIA_CENSUS_SECONDS} exomem cell-media-census --vault /data/vault)"
        f" || census={shlex.quote(unknown)}; "
        "printf '\\n%s' \"$census\" >> /dev/termination-log || true; }"
    )


# The report line is a closed protocol format that the cell writes (media_drain_record.format_line) and the Job copies:
# "<evicted count> <evicted bytes> <census time>". MEDIA_CENSUS_UNKNOWN is an incomplete census. A line with one count
# known and one unknown is no census, since a known 0 beside it would read as drained.
_MEDIA_CENSUS = re.compile(
    rf"(?:(?P<evicted>[0-9]{{1,19}}) (?P<bytes>[0-9]{{1,19}})|{re.escape(MEDIA_CENSUS_UNKNOWN)}) (?P<captured>\S+)")


def parse_media_census(line: str) -> tuple[int | None, int | None, datetime] | None:
    """The census a report line carries as (count, bytes, census time), counts None when unknown; None when unreadable."""
    match = _MEDIA_CENSUS.fullmatch(line)
    if match is None:
        return None
    try:
        captured_at = datetime.fromisoformat(match["captured"])
    except ValueError:
        return None
    if captured_at.tzinfo is None:
        return None
    evicted, evicted_bytes = (None if match[key] is None else int(match[key]) for key in ("evicted", "bytes"))
    return evicted, evicted_bytes, captured_at
