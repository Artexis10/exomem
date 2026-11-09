"""automate-cell-storage-deletion D1, D3, D5: the media edge's purge pass hides a deleted cell's media only on
a verified deletion grant, only once the loaded set no longer admits the cell, and only after purge_after.

Each case runs the real edge over the test provider, which gives the edge key no list capability and the list
key nothing else.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from exomem.cell_deletion_grant import load_public_keys, verify_grant
from exomem.media_edge import MediaEdge
from exomem.media_edge_b2 import B2
from tests.fake_b2 import LIST_KEY_ID
from tests.test_media_edge import CELL_A, CELL_B, Harness, name_of

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "infra/contracts/cell-deletion-grants-v1.json").read_text(encoding="utf-8"))
SUBSTRATE = Ed25519PrivateKey.generate()
DAY = 86_400
IAT = int(time.time()) - 10 * DAY


def _grant(cell_id: str, *, purge_after: int = IAT + 7 * DAY, key: Ed25519PrivateKey = SUBSTRATE) -> str:
    return jwt.encode({"aud": "exomem-cell-delete", "iat": IAT, "jti": f"grant-{cell_id}", "cell_id": cell_id,
                       "purge_after": purge_after}, key, algorithm="EdDSA")


class Deleted:
    """The edge after cellctl revoked CELL_A, which stored `stored` names while it was live."""

    def __init__(self, tmp_path: Path, *, stored: int = 2, held: list[str] | None = None):
        self.grants_file = tmp_path / "grants.json"
        self.hold_file = tmp_path / "hold-cells.json"
        self.hold_file.write_text(json.dumps(held or []))
        keys_file = tmp_path / "grant-public-keys.json"
        keys_file.write_text(json.dumps([SUBSTRATE.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()]))
        self.harness = Harness(tmp_path / "edge", list_key_id=LIST_KEY_ID, list_application_key="list-secret",
                               grants_file=self.grants_file, grant_public_keys_file=keys_file,
                               hold_list_file=self.hold_file)
        self.fake = self.harness.fake
        self.names = [f"cells/{CELL_A}/{name_of(self.data(index))}" for index in range(stored)]
        for index in range(stored):
            assert self.harness.put(self.data(index)).status_code == 201
        self.harness.publish(2, {CELL_B: 1}, collectors={CELL_B: 1})
        self.edge = self.restart()

    @staticmethod
    def data(index: int) -> bytes:
        return f"cell a original {index}".encode()

    def restart(self) -> MediaEdge:
        client = httpx.AsyncClient(transport=self.fake.transport())
        lister = B2(client, key_id=LIST_KEY_ID, application_key="list-secret", bucket=self.fake.bucket)
        self.edge = MediaEdge(self.harness.config, B2(client, key_id="key", application_key="secret",
                                                      bucket=self.fake.bucket), lister)
        self.harness.edge = self.edge
        return self.edge

    def relay(self, *grants: object) -> None:
        self.grants_file.write_text(json.dumps(list(grants)))

    def run(self) -> None:
        asyncio.run(self.edge.purge_pass())

    def hidden(self) -> list[str]:
        """Each name with a hide marker, once per marker."""
        return sorted(item["name"] for item in self.fake.files.values() if item["action"] == "hide")


@pytest.mark.parametrize("case", FIXTURE["grants"], ids=[case["case"] for case in FIXTURE["grants"]])
def test_the_edge_verifier_accepts_and_refuses_the_shared_grants(case: dict) -> None:
    keys = load_public_keys(json.dumps(FIXTURE["public_keys"]).encode())
    grant = verify_grant(case["token"], keys)
    accepted = None if grant is None else {"cell_id": grant.cell_id, "iat": grant.iat,
                                           "purge_after": grant.purge_after}
    assert accepted == case["accepted"]


@pytest.mark.parametrize("token", [None, _grant(CELL_A, key=Ed25519PrivateKey.generate()), _grant(CELL_B)],
                         ids=["missing", "forged", "names another cell"])
def test_a_grant_that_does_not_verify_for_the_cell_hides_nothing(tmp_path: Path, token: str | None) -> None:
    """Defect: a relayed entry that Substrate did not sign for this cell starts the purge of its media."""
    deleted = Deleted(tmp_path)
    deleted.relay(*([] if token is None else [token]))

    deleted.run()

    assert deleted.hidden() == []


def test_a_malformed_entry_is_skipped_and_the_grant_beside_it_acts(tmp_path: Path) -> None:
    """Defect: one bad entry in the relayed list stops every purge."""
    deleted = Deleted(tmp_path)
    deleted.relay(42, "not-a-token", _grant(CELL_A))

    deleted.run()

    assert deleted.hidden() == sorted(deleted.names)


def test_a_held_cell_is_left_alone_and_a_released_hold_resumes_on_the_same_grant(tmp_path: Path) -> None:
    """Defect: the operator's hold during the grace does not stop the media purge."""
    deleted = Deleted(tmp_path, held=[CELL_A])
    deleted.relay(_grant(CELL_A))

    deleted.run()
    assert deleted.hidden() == []

    deleted.hold_file.write_text("[]")
    deleted.run()
    assert deleted.hidden() == sorted(deleted.names)


def test_an_unreadable_hold_list_purges_nothing(tmp_path: Path) -> None:
    """Defect: a hold list that cannot be read reads as "no hold", and a held cell loses its media."""
    deleted = Deleted(tmp_path, held=[CELL_A])
    deleted.relay(_grant(CELL_A))
    deleted.hold_file.write_text("not json")

    deleted.run()

    assert deleted.hidden() == []


def test_no_name_is_hidden_before_purge_after(tmp_path: Path) -> None:
    """Defect: the purge starts during the grace, when the operator can still stop a wrong deletion."""
    deleted = Deleted(tmp_path)
    deleted.relay(_grant(CELL_A))
    deleted.edge.clock = lambda: float(IAT + 7 * DAY - 1)

    deleted.run()

    assert deleted.hidden() == []


def test_after_purge_after_every_current_name_is_hidden_and_a_rerun_changes_nothing(tmp_path: Path) -> None:
    """Defect: the purge misses names beyond the first listing page, takes an unfinished re-upload for the
    name's newest entry, hides a name twice, or touches a live cell's media."""
    deleted = Deleted(tmp_path, stored=3)
    deleted.fake.list_page = 2
    asyncio.run(deleted.edge.provider.start_large(deleted.names[0], {}))
    assert deleted.harness.put(b"cell b original", cell=CELL_B).status_code == 201
    deleted.relay(_grant(CELL_A))

    deleted.run()
    assert deleted.hidden() == sorted(deleted.names)

    hides = deleted.fake.calls.count("b2_hide_file")
    deleted.run()
    assert deleted.fake.calls.count("b2_hide_file") == hides


@pytest.mark.parametrize("admitted", ["cells", "collectors"])
def test_a_cell_that_the_loaded_set_still_admits_gets_no_hide(tmp_path: Path, admitted: str) -> None:
    """Defect: a grant alone purges a cell that cellctl has not yet revoked, or whose backup Job still runs."""
    deleted = Deleted(tmp_path)
    still = {"cells": {CELL_B: 1}, "collectors": {CELL_B: 1}}
    still[admitted] = {**still[admitted], CELL_A: 1}
    deleted.harness.publish(3, still["cells"], collectors=still["collectors"])
    deleted.relay(_grant(CELL_A))

    deleted.run()

    assert deleted.hidden() == []


def test_the_pass_waits_while_a_write_for_the_cell_is_pending(tmp_path: Path) -> None:
    """Defect: an uncertain upload lands after the purge listed the prefix and leaves a current name."""
    deleted = Deleted(tmp_path, stored=1)
    deleted.harness.publish(3, {CELL_A: 1, CELL_B: 1}, collectors={CELL_B: 1})
    late = b"an upload that lands late"
    deleted.fake.lose("upload_file", "late")
    assert deleted.harness.put(late).status_code == 503
    deleted.harness.publish(4, {CELL_B: 1}, collectors={CELL_B: 1})
    deleted.relay(_grant(CELL_A))

    deleted.run()
    assert deleted.hidden() == []

    deleted.fake.land_late()
    asyncio.run(deleted.edge.recover_pending())
    deleted.run()
    assert deleted.hidden() == sorted([*deleted.names, f"cells/{CELL_A}/{name_of(late)}"])


def test_a_restart_in_the_middle_of_a_purge_resumes_it_and_hides_each_name_once(tmp_path: Path) -> None:
    """Defect: a restart after an uncertain hide either strands the purge or hides a name a second time."""
    deleted = Deleted(tmp_path, stored=3)
    deleted.relay(_grant(CELL_A))
    deleted.fake.lose("b2_hide_file", "late", then_outage=True)

    deleted.run()
    assert deleted.hidden() == []

    deleted.fake.outage = False
    restarted = deleted.restart()
    deleted.run()
    deleted.fake.land_late()
    asyncio.run(restarted.recover_pending())
    deleted.run()

    assert deleted.hidden() == sorted(deleted.names)
    assert restarted.intents == {}
