"""The suite must not read whatever vault the developer's shell points at.

EXOMEM_VAULT_PATH and EXOMEM_LOG_DIR are the two documented ways to aim a real
Exomem at real data, and both are exported on a configured machine. Both also
serve as fallbacks deep inside the product -- `LeaseManager._receipt_vault_root`
and `query_log._target` -- reached whenever a call names no destination of its
own. An unscoped test on such a machine therefore read and wrote the operator's
live vault and log directory.

CI never sees this: the variables are unset there, so the suite is green while
being wrong for everyone running it on a working install. #570 fixed the log
directory after three tests wrote into it; the vault path is the same defect one
layer deeper, and it made five test_writer_lease.py tests fail in isolation on a
checkout with nothing wrong in it.

Asserted here rather than left to the conftest comment, because the symptom is
invisible in the only environment that gates merges.
"""

from __future__ import annotations

import os

import pytest

#: Every variable that names a real destination outside the test's tmp_path.
#: A test that wants one sets it explicitly; autouse setup runs before
#: test-requested fixtures, so `monkeypatch.setenv` in a fixture still wins.
AMBIENT_DESTINATION_VARS = ("EXOMEM_VAULT_PATH", "EXOMEM_LOG_DIR")


@pytest.mark.parametrize("name", AMBIENT_DESTINATION_VARS)
def test_no_ambient_destination_reaches_a_test(name: str) -> None:
    assert os.environ.get(name) is None, (
        f"{name} leaked into the suite; a test would read or write a real "
        "destination instead of its tmp_path"
    )


def test_a_test_that_asks_for_a_vault_still_gets_one(vault) -> None:
    """The isolation must not break the fixture it exists to protect.

    `vault` sets EXOMEM_VAULT_PATH deliberately, and that has to survive the
    autouse delenv -- otherwise clearing the ambient value would silently
    un-configure every test that actually wants a vault.
    """
    assert os.environ["EXOMEM_VAULT_PATH"] == str(vault)
