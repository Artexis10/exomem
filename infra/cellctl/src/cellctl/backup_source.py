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
in the cell image, which the product suite (tests/test_media_store.py) runs.
"""

import shlex

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

# The census must not push the Job past its deadline, so it gets its own; a census that does not finish is unknown.
MEDIA_CENSUS_SECONDS = 120
MEDIA_CENSUS_UNKNOWN = "unknown unknown"


def media_census_command(store: str, captured_at: str) -> str:
    """Append the media report to the termination message: "<count> <bytes> <capture time>" on a line of its own.

    The cell image counts the cell's evicted originals from its ledger. The Job reads the data volume read-only,
    so it gives the image the store identity instead of the policy file. Any failure of the census, including
    a timeout, leaves "unknown unknown", which keeps a turned-off cell's media access (D12).
    """
    return (
        f"{{ census=$(timeout {MEDIA_CENSUS_SECONDS} exomem cell-media-census --vault /data/vault --store {shlex.quote(store)})"
        f" || census={shlex.quote(MEDIA_CENSUS_UNKNOWN)}; "
        f"printf '\\n%s %s' \"$census\" {shlex.quote(captured_at)} >> /dev/termination-log || true; }}"
    )
