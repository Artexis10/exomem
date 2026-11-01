"""The CLI sends a folder as windows against a synthetic Cloud API and a synthetic cell (task 5.4.7).

Both fakes are scripted: the cell answers `adoption_studio status` from a plain state object, and the API keeps the
imports it receives, decrypts each uploaded archive and lists its members. Time is a counter, so a wait costs nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import httpx2
import pytest

from exomem import import_stage
from exomem.cli_progress import Progress
from exomem.cloud_import_client import CloudImportClientConfig, CloudImportError
from exomem.cloud_import_windows import Cell, import_folder

CONFIG = CloudImportClientConfig("https://cloud.example/mcp", "https://cloud.example")
needs_age = pytest.mark.skipif(
    not shutil.which("age") or not shutil.which("age-keygen"), reason="real age executables required")
pytestmark = needs_age

# The classification inputs of the synthetic cell: an artifact of 800 bytes is offloadable, one of 3000 is resident.
HYDRATION, VOLUME = 2000, 1_000_000
BIG = 10**9


class FakeCell(Cell):
    """`adoption_studio status` and `acknowledge`, from a state that a test sets."""

    def __init__(self, rig: Rig):
        self.rig = rig
        self.older = False
        self.budget: int | str = BIG
        self.reachable: int | str = BIG
        self.pinned: int | str = 0
        self.effective: int | str = BIG
        self.allowance: int | str = BIG
        self.used = 0
        self.windows: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.acknowledged: list[tuple[str, list[str], int]] = []
        self.refill: int | None = None
        self.windows_unknown: str | None = None
        self.largest: int = 960
        self.inline: int = 4 << 20
        self.before_reply = lambda call: None

    async def status(self, *, series_id=None, next_window_bytes=None, after=None) -> dict:
        call = {"at": self.rig.now, "series_id": series_id, "next_window_bytes": next_window_bytes, "number": len(self.calls)}
        self.calls.append(call)
        self.before_reply(call)
        return self.reply()

    async def acknowledge(self, import_id: str, paths: list[str]) -> dict:
        self.acknowledged.append((import_id, paths, len(self.rig.cloud.completed)))
        return {}

    def figure(self, value) -> dict:
        return {"unknown": value} if isinstance(value, str) else {"bytes": value}

    def reply(self) -> dict:
        windows = list(self.windows.values())
        unfiled = sum(w.get("unfiled", {}).get("count", 0) for w in windows if w["state"] == "landed")
        residual = sum(len(w.get("members", [])) for w in windows if w["state"] == "discarded")
        keep = {"keep": bool(unfiled or residual),
                "message": f"Keep the source folder: {unfiled} unfiled and {residual} discarded, not filed." if unfiled or residual else None}
        # The real `report()` leaves `import_windows` out when the cell holds no window record.
        reply = {"import_windows": {"counts": {}, "windows": windows, "keep_source": keep, "next_after": None}} if windows else {}
        if self.windows_unknown:  # The cell cannot list its records: no `windows` key at all, as the real reply has it.
            reply = {"import_windows": {"unknown": self.windows_unknown, "keep_source": {"keep": True, "message": "Keep the source folder."}}}
        if not self.older:
            allowance = ({"unknown": self.allowance} if isinstance(self.allowance, str)
                         else {"allowance_bytes": BIG, "remaining_bytes": self.allowance})
            reply["window_budget"] = {
                "window_budget": self.figure(self.budget), "reachable_budget": self.figure(self.reachable),
                "job_pinned": self.figure(self.pinned), "effective_file_limit": self.figure(self.effective),
                "classification": {"object_bound": None, "hydration_bytes": HYDRATION, "volume_fraction": 0.25,
                                   "volume_bytes": VOLUME, "inline_text_limit": self.inline, "largest_offloadable_file": self.largest},
                "media_allowance": allowance, "volume": {"used_bytes": self.used}}
        return reply

    def landed(self, import_id: str, count: int) -> None:
        """The cell's receipt for a window that has landed and is not filed yet."""
        self.windows[import_id] = {"import_id": import_id, "state": "landed", "unfiled": {"count": count}}
        if self.refill is not None:
            self.budget = self.reachable = self.refill

    def discard(self, import_id: str, residual: list[str]) -> None:
        """The window is discarded and its residual record lists `residual`."""
        self.windows[import_id] = {"import_id": import_id, "state": "discarded", "members": [
            {"path": path, "bytes": 800, "state": "discarded, not filed", "code": "conflict"} for path in residual]}


class FakeCloud:
    """The Cloud import API and its storage, as two in-process transports; it keeps every import it receives."""

    def __init__(self, rig: Rig, recipient: str, identity: Path):
        self.rig, self.recipient, self.identity = rig, recipient, identity
        self.rows: dict[str, dict] = {}
        self.parts: dict[str, dict[int, bytes]] = {}
        self.members: dict[str, dict[str, bytes]] = {}
        self.completed: list[str] = []
        self.registered_at: list[float] = []
        self.registered_calls: list[int] = []
        self.limits: dict | None = None
        self.refuse_when = lambda members: False
        self.refuse_registration = False
        self.refusal_code = "STORAGE_ALLOWANCE_EXCEEDED"
        self.holding = False  # A landing stays `uploaded` until `release()`, or until the third row read once `read_count` is 0.
        self.held: list[str] = []
        self.fail_next_read = False
        self.read_count: int | None = None
        self.fail_prepare_once = False
        self.short_parts_once = False
        self.creates = 0  # Every create request, including the ones that Cloud refuses.
        self.cancelled: list[str] = []  # The import IDs that the cancel route received.
        self.on_read = lambda import_id: None  # Runs before Cloud answers a read of one import, which may not exist.

    @staticmethod
    def holds_cell(row: dict) -> bool:
        """Substrate's unique index of open imports (migration 0061): a cell holds at most one such row."""
        closed = row["observed_state"] in ("done", "failed", "expired", "discarded")
        return not closed or (row["requested_state"] == "discard_requested" and row["observed_state"] != "discarded")

    def open_another_import(self) -> None:
        """An import that this series did not create, such as an upload from Home, is open on the cell."""
        self.rows["another-import"] = {"declaration": {}, "import_id": "another-import", "requested_state": "requested",
                                       "observed_state": "key_ready", "recipient": self.recipient}

    def release(self) -> None:
        """Cloud finishes every import that it held."""
        for import_id in self.held:
            self.rows[import_id].update(observed_state="done")
            self.completed.append(import_id)
            self.rig.cell.landed(import_id, len([name for name in self.members[import_id] if name != import_stage.SERIES_FILE]))
        self.held.clear()

    def _api(self, request):
        path = request.url.path
        base = "/api/exomem/cloud/imports"
        if request.method == "GET" and path == base:
            return httpx2.Response(200, json={"imports": [], "limits": self.limits} if self.limits else {"imports": []})
        if request.method == "POST" and path == base:
            if self.refuse_registration:
                return httpx2.Response(503, json={"error": {"code": "STORE_UNAVAILABLE"}})
            self.creates += 1
            body = json.loads(request.content)
            known = self.rows.get(body["import_id"])
            if known is not None:  # Cloud replays a known ID whatever its state, and refuses a changed declaration.
                declared = {key: body[key] for key in ("kind", "declared_bytes", "declared_files")}
                if declared != known["declaration"]:
                    return httpx2.Response(409, json={"error": {"code": "IMPORT_CONFLICT"}})
                return httpx2.Response(200, json={"import": known})
            if any(self.holds_cell(row) for row in self.rows.values()):  # The insert violates the open-import index.
                return httpx2.Response(409, json={"error": {"code": "IMPORT_CONFLICT"}})
            self.rows[body["import_id"]] = {"declaration": {key: body[key] for key in ("kind", "declared_bytes", "declared_files")},
                                            "import_id": body["import_id"], "requested_state": "requested",
                                            "observed_state": "key_ready", "recipient": self.recipient}
            self.registered_at.append(self.rig.now)
            self.registered_calls.append(len(self.rig.cell.calls))
            return httpx2.Response(200, json={"import": self.rows[body["import_id"]]})
        import_id = path.removeprefix(base + "/").split("/")[0]
        if request.method == "GET":
            self.on_read(import_id)
        if import_id not in self.rows:
            return httpx2.Response(404, json={"error": {"code": "IMPORT_NOT_FOUND"}})
        row = self.rows[import_id]
        if request.method == "GET" and row["requested_state"] == "cancelled" and self.holds_cell(row):
            # Cloud ends a cancelled import over two passes: it cleans up, then the row is `discarded`.
            row["observed_state"] = "discarded" if row["observed_state"] == "cleanup_running" else "cleanup_running"
        if request.method == "POST" and path.endswith("/cancel"):
            # Substrate's cancel: a repeat is a replay, and an import that ended or is being discarded refuses it.
            if row["requested_state"] != "cancelled":
                if row["requested_state"] == "discard_requested" or not self.holds_cell(row):
                    return httpx2.Response(409, json={"error": {"code": "IMPORT_CONFLICT"}})
                row["requested_state"] = "cancelled"
                self.cancelled.append(import_id)
            return httpx2.Response(200, json={"import": row})
        if request.method == "GET" and import_id in self.held:
            if self.fail_next_read:
                self.fail_next_read = False
                raise httpx2.ConnectError("interrupted")
            if self.read_count is not None:
                self.read_count += 1
                if self.read_count >= 3:
                    self.release()
        if request.method == "POST" and row["observed_state"] == "failed":
            return httpx2.Response(409, json={"error": {"code": "IMPORT_CONFLICT"}})
        if request.method == "POST" and path.endswith("/prepare-upload"):
            if self.fail_prepare_once:
                self.fail_prepare_once = False
                raise httpx2.ConnectError("interrupted")
            size = json.loads(request.content)["ciphertext_bytes"]
            row["requested_state"] = "requested"  # Substrate keeps `requested` until the upload completes.
            row["part_urls"] = [{"part_number": n + 1, "part_size_bytes": min(5000, size - offset),
                                 "url": f"https://storage.example/{import_id}/{n + 1}"} for n, offset in enumerate(range(0, size, 5000))]
            if self.short_parts_once:  # The issued parts do not cover the encrypted copy.
                self.short_parts_once = False
                row["part_urls"][0]["part_size_bytes"] -= 1
        elif request.method == "POST" and path.endswith("/complete"):
            self._land(import_id, row)
        return httpx2.Response(200, json={"import": row})

    def _land(self, import_id: str, row: dict) -> None:
        cipher = b"".join(self.parts[import_id][n] for n in sorted(self.parts[import_id]))
        plain = subprocess.run(["age", "-d", "-i", str(self.identity)], input=cipher, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(plain)) as archive:
            self.members[import_id] = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}
        if self.refuse_when(self.members[import_id]):
            row.update(observed_state="failed", error_code=self.refusal_code)
            return
        if self.holding:
            row.update(requested_state="uploaded", observed_state="uploaded")
            self.held.append(import_id)
            return
        row.update(requested_state="uploaded", observed_state="done")
        self.completed.append(import_id)
        self.rig.cell.landed(import_id, len([name for name in self.members[import_id] if name != import_stage.SERIES_FILE]))

    def _storage(self, request):
        import_id, part = request.url.path.strip("/").split("/")
        self.parts.setdefault(import_id, {})[int(part)] = request.content
        return httpx2.Response(200, headers={"etag": f'"part-{part}"'})

    def cost(self, import_id: str) -> int:
        """What a window takes of the budget: artifacts once, text twice (the series file is text)."""
        return sum(len(data) * (2 if name.endswith((".md", ".json")) else 1) for name, data in self.members[import_id].items())

    def sent(self, import_id: str) -> set[str]:
        return set(self.members[import_id]) - {import_stage.SERIES_FILE}

    def all_sent(self) -> list[str]:
        return sorted(name for import_id in self.completed for name in self.sent(import_id))


class Rig:
    def __init__(self, tmp_path: Path):
        identity = tmp_path / "identity"
        subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
        recipient = subprocess.run(["age-keygen", "-y", str(identity)], capture_output=True, check=True).stdout.decode().strip()
        self.tmp, self.now, self.sleeps = tmp_path, 0.0, 0
        self.cell = FakeCell(self)
        self.cloud = FakeCloud(self, recipient, identity)
        self.lines: list[str] = []
        self.source = tmp_path / "Field Notes"
        self.source.mkdir()

    def files(self, **sizes: int) -> None:
        """Each keyword is a file name with `_` for `.`, and its size; the bytes differ from file to file."""
        for name, size in sizes.items():
            name = name.replace("__", ".")
            (self.source / name).write_bytes((hashlib.sha256(name.encode()).hexdigest().encode() * (size // 64 + 1))[:size])

    async def _sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        assert self.sleeps < 1000, "the command waits for ever"

    def run(self, **kwargs) -> dict:
        async def go():
            headers = {"Authorization": "Bearer example-session"}
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(self.cloud._api), headers=headers) as api:
                async with httpx2.AsyncClient(transport=httpx2.MockTransport(self.cloud._storage)) as uploads:
                    return await import_folder(
                        self.source, CONFIG, self.tmp / "checkpoint", cell=self.cell, api=api, uploads=uploads,
                        progress=Progress(self.lines.append), poll_seconds=kwargs.pop("poll_seconds", 30),
                        sleep=self._sleep, clock=lambda: self.now, **kwargs)
        return asyncio.run(go())

    def said(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def nine(rig: Rig) -> None:
    rig.files(**{f"a{n:02}__bin": 800 for n in range(1, 10)})


NOT_IN_EXOMEM = "not in Exomem"


# -- Windows and the budget


def test_three_times_the_budget_becomes_three_or_more_windows_each_within_the_budget(rig):
    """Catches a window sized past the budget that the cell reports."""
    nine(rig)
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 2000

    rig.run()

    assert len(rig.cloud.completed) >= 3
    assert all(rig.cloud.cost(import_id) <= 2000 for import_id in rig.cloud.completed)
    assert rig.cloud.all_sent() == sorted(f"a{n:02}.bin" for n in range(1, 10))


def test_each_window_stays_within_the_published_admission_limit(rig):
    """Catches a window that would make admission grow the cell: used + factor x bytes + reserve above its size."""
    nine(rig)
    rig.cloud.limits = {"max_members": 100000, "reserve_bytes": 1000, "space_factor": 2}
    rig.cell.used = VOLUME - 3000

    rig.run()

    for import_id in rig.cloud.completed:
        plain = sum(len(data) for data in rig.cloud.members[import_id].values())
        assert rig.cell.used + 2 * plain + 1000 <= VOLUME
    assert len(rig.cloud.completed) == 9


def test_an_unknown_budget_waits_and_reports_the_reason(rig):
    """Catches an unknown budget read as 0 (a stop) or as room (a send)."""
    rig.files(a01__bin=800)
    rig.cell.budget = "the volume's use cannot be read"
    rig.cell.before_reply = lambda call: setattr(rig.cell, "budget", BIG) if rig.now >= 90 else None

    rig.run()

    assert rig.cloud.registered_at and min(rig.cloud.registered_at) >= 90
    assert "the volume's use cannot be read" in rig.said()


def test_a_window_that_another_series_landing_no_longer_fits_is_not_created(rig):
    """Catches a create that trusts the first reading: the CLI reads the budget again right before it."""
    rig.files(a01__bin=800)
    # Calls: the first read (0), the resume read (1), the sizing read (2), then the read before the create (3).
    gate = 3

    def another_series_lands(call):
        if call["number"] == gate:
            rig.cell.budget = 100
        elif rig.now >= 30:
            rig.cell.budget = BIG

    rig.cell.before_reply = another_series_lands
    rig.cell.budget = BIG

    rig.run()

    assert rig.cloud.registered_calls[0] > gate + 1
    assert min(rig.cloud.registered_at) >= 30


def test_a_waiting_cli_renews_its_demand_at_least_every_five_minutes(rig):
    """Catches a waiter that lets its demand lapse; the lease lasts 15 minutes."""
    rig.files(a01__bin=800)
    rig.cell.budget = 100
    rig.cell.before_reply = lambda call: setattr(rig.cell, "budget", BIG) if rig.now >= 1500 else None

    rig.run(poll_seconds=900)

    times = [call["at"] for call in rig.cell.calls if call["next_window_bytes"] is not None and call["number"] > 2]
    assert len(times) >= 4
    assert max(later - earlier for earlier, later in zip(times, times[1:], strict=False)) <= 300


def test_a_window_that_the_reachable_budget_covers_waits_and_sends_its_bytes_as_the_demand(rig):
    """Catches a waiter that records no demand, so eviction never makes room."""
    rig.files(a01__bin=800)
    rig.cell.budget = 100
    rig.cell.before_reply = lambda call: setattr(rig.cell, "budget", BIG) if rig.now >= 300 else None

    result = rig.run()

    demands = [call for call in rig.cell.calls if call["next_window_bytes"] is not None and call["at"] < 300]
    assert rig.cloud.cost(result["import_id"]) in {call["next_window_bytes"] for call in demands}
    assert {call["series_id"] for call in demands} != {None}
    assert min(rig.cloud.registered_at) >= 300


# -- Stops


def test_band_too_small_stops_and_reports_the_four_figures(rig):
    """Catches a stop that omits the budget, the reachable budget, the job-pinned bytes or the member's bytes."""
    rig.files(a01__bin=800)
    # The effective file limit shrank after the preflight, as notes and derived state grow, so the series starts and then stops.
    rig.cell.budget, rig.cell.reachable, rig.cell.pinned = 321, 500, 222

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert "band too small" in stop.value.message
    assert all(figure in stop.value.message for figure in ("321", "500", "222", "800"))
    assert not rig.cloud.registered_at


def test_a_next_member_that_only_the_job_pinned_bytes_make_room_for_waits_instead_of_stopping(rig):
    """Catches a stop where the design says to wait for the claimed jobs to end."""
    rig.files(a01__bin=800)
    rig.cell.budget = rig.cell.reachable = 700
    rig.cell.pinned = 600

    def jobs_end(call):
        if rig.now >= 120:
            rig.cell.budget = rig.cell.reachable = BIG
            rig.cell.pinned = 0

    rig.cell.before_reply = jobs_end

    rig.run()

    assert min(rig.cloud.registered_at) >= 120
    assert "claimed jobs" in rig.said()


def _after_one_window(rig: Rig, **cell_state) -> None:
    """Window 1 lands, then the cell reports `cell_state`."""
    original = rig.cell.landed

    def landed(import_id, count):
        original(import_id, count)
        for name, value in cell_state.items():
            setattr(rig.cell, name, value)

    rig.cell.landed = landed


@pytest.mark.parametrize(("state", "phrase"), [
    ({"allowance": 0}, "allowance exhausted"),
    ({"allowance": "the stored versions cannot be counted"}, "media usage unknown"),
])
def test_allowance_exhausted_and_media_usage_unknown_stop_and_report(rig, state, phrase):
    """Catches a series that goes on creating windows once the allowance cannot take them."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cell.budget = rig.cell.reachable = 1200
    _after_one_window(rig, **state)

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert phrase in stop.value.message
    assert len(rig.cloud.completed) == 1


@pytest.mark.parametrize("stop", ["band", "allowance", "usage", "storage"])
def test_every_stop_says_the_members_not_yet_sent_are_not_in_exomem_and_must_be_kept(rig, stop):
    """Catches a stop that leaves the person thinking the unsent members are safe in Exomem."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cell.budget = rig.cell.reachable = 1200
    if stop == "band":
        _after_one_window(rig, budget=300, reachable=300, pinned=0)
    elif stop == "allowance":
        _after_one_window(rig, allowance=0)
    elif stop == "usage":
        _after_one_window(rig, allowance="the stored versions cannot be counted")
    else:
        rig.cloud.refuse_registration = True

    with pytest.raises(CloudImportError) as raised:
        rig.run()

    assert NOT_IN_EXOMEM in raised.value.message and "must keep" in raised.value.message


# -- Refusals before upload


def test_a_series_whose_resident_bytes_exceed_the_reachable_budget_is_refused_before_upload_with_its_files_listed(rig):
    """Catches a series that uploads first and finds that its resident files cannot fit."""
    rig.files(big_one__bin=3000, big_two__bin=3000, note__md=100)
    rig.cell.budget = rig.cell.reachable = 5000

    with pytest.raises(CloudImportError) as refused:
        rig.run()

    assert "big_one.bin" in refused.value.message and "big_two.bin" in refused.value.message
    assert not rig.cloud.registered_at


def test_a_series_with_an_offloadable_member_above_the_effective_file_limit_is_refused_before_upload(rig):
    """Catches a series that would stop at the member only after earlier windows landed."""
    rig.files(a01__bin=800, a02__bin=900)
    rig.cell.effective = 850

    with pytest.raises(CloudImportError) as refused:
        rig.run()

    assert "a02.bin (900 bytes)" in refused.value.message and "850" in refused.value.message
    assert "a01.bin" not in refused.value.message
    assert not rig.cloud.registered_at


def test_a_source_folder_with_the_series_file_at_its_top_level_is_refused_before_upload(rig):
    """Catches a source file that the cell would read as the series' own."""
    rig.files(a01__bin=800)
    (rig.source / import_stage.SERIES_FILE).write_text("{}")

    with pytest.raises(CloudImportError) as refused:
        rig.run()

    assert refused.value.code == "SERIES_FILE_IN_SOURCE"
    assert not rig.cloud.registered_at


# -- The series file


def test_each_window_carries_the_series_file_with_the_label_and_every_path_relative_to_the_stage(rig):
    """Catches a window without the file, or with paths that differ between windows."""
    rig.files(a01__bin=800, a02__bin=800, a03__bin=800, n01__md=100)
    (rig.source / "sub").mkdir()
    (rig.source / "sub" / "deep.md").write_text("deep")
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200

    rig.run()

    files = {import_id: json.loads(rig.cloud.members[import_id][import_stage.SERIES_FILE]) for import_id in rig.cloud.completed}
    assert len(files) >= 3
    assert {json.dumps(value, sort_keys=True) for value in files.values()}.__len__() == 1
    series = next(iter(files.values()))
    assert series["label"] == "Field Notes"
    assert sorted(series["paths"]) == ["a01.bin", "a02.bin", "a03.bin", "n01.md", "sub/deep.md"]


# -- Resuming


def _two_windows_then_stop(rig: Rig) -> None:
    rig.files(**{f"a{n}__bin": 800 for n in range(1, 5)})
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200


def test_a_restart_after_window_two_resends_nothing(rig):
    """Catches a rerun that sends the landed windows again."""
    _two_windows_then_stop(rig)
    state = {"landed": 0}
    original = rig.cell.landed

    def landed(import_id, count):
        original(import_id, count)
        state["landed"] += 1
        if state["landed"] == 2:
            rig.cell.allowance = 0

    rig.cell.landed = landed
    with pytest.raises(CloudImportError):
        rig.run()
    first_two = list(rig.cloud.completed)
    rig.cell.allowance = BIG

    rig.run()

    assert rig.cloud.completed[:2] == first_two
    assert rig.cloud.all_sent() == ["a1.bin", "a2.bin", "a3.bin", "a4.bin"]
    assert len(rig.cloud.rows) == 4  # No import was registered for a landed window a second time.


def test_a_window_without_a_receipt_is_sent_again_before_the_next_one(rig):
    """Catches a rerun that trusts its record after a cell restore lost a landed window."""
    _two_windows_then_stop(rig)
    original = rig.cell.landed

    def landed(import_id, count):
        original(import_id, count)
        if len(rig.cloud.completed) == 2:
            rig.cell.allowance = 0

    rig.cell.landed = landed
    with pytest.raises(CloudImportError):
        rig.run()
    lost = rig.cloud.completed[1]
    lost_members = rig.cloud.sent(lost)
    del rig.cell.windows[lost]  # The restore from backup kept no receipt for window 2.
    rig.cell.allowance = BIG

    rig.run()

    assert rig.cloud.sent(rig.cloud.completed[2]) == lost_members
    assert rig.cloud.all_sent().count(sorted(lost_members)[0]) == 2


def test_a_window_that_the_job_refuses_for_space_is_planned_again_smaller(rig):
    """Catches a retry of the same window, which the Job would refuse again."""
    rig.files(a01__bin=800, a02__bin=800, a03__bin=800)
    rig.cloud.refuse_when = lambda members: len(members) > 3  # The series file and two members.

    rig.run()

    refused = [import_id for import_id, row in rig.cloud.rows.items() if row["observed_state"] == "failed"]
    assert len(refused) == 1
    assert max(len(rig.cloud.members[import_id]) for import_id in rig.cloud.completed) < len(rig.cloud.members[refused[0]])
    assert rig.cloud.all_sent() == ["a01.bin", "a02.bin", "a03.bin"]


# -- Older cells


def test_an_older_cell_gets_one_import_with_no_series_file(rig):
    """Catches windows against a cell that reports no budget, which could not size them."""
    nine(rig)
    rig.cell.older = True

    rig.run()

    assert len(rig.cloud.completed) == 1
    assert import_stage.SERIES_FILE not in rig.cloud.members[rig.cloud.completed[0]]
    assert len(rig.cloud.sent(rig.cloud.completed[0])) == 9


def test_an_older_cell_refuses_a_folder_above_its_known_size_before_any_upload(rig):
    """Catches an upload that the cell's size would refuse only at the end."""
    rig.files(a01__bin=800)
    with (rig.source / "huge.bin").open("wb") as handle:
        handle.truncate((1 << 30) + 1)
    rig.cell.older = True
    rig.cloud.limits = {"max_members": 100000, "reserve_bytes": 0, "max_storage_gib": 1}

    with pytest.raises(CloudImportError) as refused:
        rig.run()

    assert str(1 << 30) in refused.value.message and str((1 << 30) + 1 + 800) in refused.value.message
    assert not rig.cloud.registered_at


# -- Keeping the source


def test_the_cli_tells_the_person_to_keep_the_source_while_a_window_has_an_unfiled_member(rig):
    """Catches a done report that lets the person delete the source while members are still unfiled."""
    rig.files(a01__bin=800)

    rig.run()

    assert "Keep the source folder" in rig.said()
    assert "no longer need" not in rig.said()


def test_the_cli_stops_telling_the_person_to_keep_the_source_once_nothing_is_unfiled_or_residual(rig):
    """Catches a warning that never ends, so the person keeps a folder that Exomem no longer needs."""
    rig.files(a01__bin=800)
    rig.run()
    for import_id in list(rig.cell.windows):
        rig.cell.discard(import_id, [])  # Every member is filed, and the window is discarded with an empty residual.
    rig.lines.clear()

    rig.run()

    assert "no longer need to keep the source folder" in rig.said()
    assert "Keep the source folder:" not in rig.said()


# -- Resending the residual


def _series_with_a_residual(rig: Rig) -> list[str]:
    """Four one-member windows; window 2 and window 4 are discarded with a residual of their member."""
    rig.files(**{f"a{n}__bin": 800 for n in range(1, 5)})
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200
    rig.run()
    return list(rig.cloud.completed)


def _window_of(rig: Rig, path: str) -> str:
    return next(import_id for import_id in rig.cloud.completed if path in rig.cloud.sent(import_id))


def test_a_resend_reads_the_residual_records_of_discarded_windows_and_carries_the_first_series_file(rig):
    """Catches a resend that sends the whole folder, or one whose file differs from the first series'."""
    first = _series_with_a_residual(rig)
    first_file = rig.cloud.members[first[0]][import_stage.SERIES_FILE]
    rig.cell.discard(_window_of(rig, "a2.bin"), ["a2.bin"])
    rig.cell.discard(_window_of(rig, "a4.bin"), ["a4.bin"])

    rig.run(resend_residual=True)

    resent = [import_id for import_id in rig.cloud.completed if import_id not in first]
    assert sorted(name for import_id in resent for name in rig.cloud.sent(import_id)) == ["a2.bin", "a4.bin"]
    assert {rig.cloud.members[import_id][import_stage.SERIES_FILE] for import_id in resent} == {first_file}


def test_a_resend_skips_a_member_that_changed_at_the_source_and_clears_the_resent_ones_once_the_window_lands(rig):
    """Catches a resend of a changed file, or an old record cleared before the new window landed."""
    first = _series_with_a_residual(rig)
    rig.cell.discard(_window_of(rig, "a2.bin"), ["a2.bin"])
    rig.cell.discard(_window_of(rig, "a4.bin"), ["a4.bin"])
    (rig.source / "a4.bin").write_bytes(b"changed after the series began")
    old_two, landed_before = _window_of(rig, "a2.bin"), len(rig.cloud.completed)

    rig.run(resend_residual=True)

    resent = [import_id for import_id in rig.cloud.completed if import_id not in first]
    assert [rig.cloud.sent(import_id) for import_id in resent] == [{"a2.bin"}]
    assert "a4.bin" in rig.said()
    assert rig.cell.acknowledged == [(old_two, ["a2.bin"], landed_before + 1)]


def test_a_resend_sends_no_member_of_a_window_that_is_not_discarded_yet(rig):
    """Catches a resend of a member whose staged copy can still be filed."""
    _series_with_a_residual(rig)
    landed = len(rig.cloud.completed)
    rig.cell.windows[_window_of(rig, "a3.bin")]["members"] = [{"path": "a3.bin", "bytes": 800, "state": "unfiled"}]

    rig.run(resend_residual=True)

    assert len(rig.cloud.completed) == landed
    assert rig.cell.acknowledged == []


# -- Review round 1


def _stopped_after_two_windows(rig: Rig, count: int = 4) -> list[str]:
    """`count` one-member windows; the run stops once two have landed. Returns the two landed import IDs."""
    rig.files(**{f"a{n}__bin": 800 for n in range(1, count + 1)})
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200
    original = rig.cell.landed

    def landed(import_id, size):
        original(import_id, size)
        if len(rig.cloud.completed) == 2:
            rig.cell.allowance = 0

    rig.cell.landed = landed
    with pytest.raises(CloudImportError):
        rig.run()
    rig.cell.landed = original
    rig.cell.allowance = BIG
    return list(rig.cloud.completed)


def test_a_rerun_waits_while_the_cell_cannot_list_its_receipts_and_resends_no_landed_window(rig):
    """Catches a rerun that reads an unknown receipt listing as "no receipts" and sends every landed window again."""
    _stopped_after_two_windows(rig)
    rig.cell.windows_unknown = "the window records cannot be listed"
    rerun_starts = len(rig.cell.calls)
    rig.cell.before_reply = lambda call: setattr(rig.cell, "windows_unknown", None) if call["number"] > rerun_starts + 3 else None

    rig.run()

    assert rig.cloud.all_sent() == ["a1.bin", "a2.bin", "a3.bin", "a4.bin"]
    assert len(rig.cloud.rows) == 4
    assert "cannot be listed" in rig.said()


def test_a_rerun_stops_with_the_notice_when_the_receipt_listing_stays_unknown(rig):
    """Catches a rerun that never stops on a listing that stays unknown."""
    _stopped_after_two_windows(rig)
    rig.cell.windows_unknown = "the window records cannot be listed"

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert NOT_IN_EXOMEM in stop.value.message
    assert len(rig.cloud.rows) == 2


def test_a_resend_stops_when_the_cell_cannot_list_the_residual_records(rig):
    """Catches a resend that reads an unknown listing as "nothing to resend" and ends ok."""
    _series_with_a_residual(rig)
    rig.cell.windows_unknown = "the window records cannot be listed"

    with pytest.raises(CloudImportError) as stop:
        rig.run(resend_residual=True)

    assert "cannot be listed" in stop.value.message and "keep" in stop.value.message.lower()


def test_a_resume_on_a_cell_that_reports_no_window_budget_stops_instead_of_sending_the_folder_as_one_import(rig):
    """Catches a resume that takes the one-import path and sends every member again."""
    _stopped_after_two_windows(rig)
    rig.cell.older = True

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert NOT_IN_EXOMEM in stop.value.message
    assert len(rig.cloud.rows) == 2


def test_a_failed_status_call_after_a_window_landed_stops_with_the_notice(rig):
    """Catches a mid-series error that says nothing about the members that are not in Exomem."""
    from fastmcp.exceptions import ToolError

    rig.files(a1__bin=800, a2__bin=800)
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200

    def fail(call):
        if rig.cloud.completed:
            raise ToolError("boom")

    rig.cell.before_reply = fail

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert NOT_IN_EXOMEM in stop.value.message


def test_a_failed_acknowledge_call_in_a_resend_stops_with_the_notice(rig):
    """Catches a failed clearing call that says nothing about the members that are not in Exomem."""
    from fastmcp.exceptions import ToolError

    _series_with_a_residual(rig)
    rig.cell.discard(_window_of(rig, "a2.bin"), ["a2.bin"])

    async def broken(import_id, paths):
        raise ToolError("boom")

    rig.cell.acknowledge = broken

    with pytest.raises(CloudImportError) as stop:
        rig.run(resend_residual=True)

    assert NOT_IN_EXOMEM in stop.value.message


def test_a_changed_or_vanished_unsent_member_is_skipped_and_listed_and_the_rest_goes_on(rig):
    """Catches a rerun that stops for ever on a member that changed, with a restart that would send everything again."""
    _stopped_after_two_windows(rig, count=5)
    (rig.source / "a3.bin").write_bytes(b"changed after the series began")
    (rig.source / "a4.bin").unlink()

    rig.run()
    rig.run()  # A second rerun neither stops nor sends anything.

    assert rig.cloud.all_sent() == ["a1.bin", "a2.bin", "a5.bin"]
    assert "a3.bin" in rig.said() and "a4.bin" in rig.said()


def test_a_stop_right_after_a_window_lands_does_not_send_that_window_again(rig):
    """Catches a rerun that sends a window twice when the stop came before the record said it landed."""
    from exomem.cloud_import_windows import Series

    rig.files(a1__bin=800, a2__bin=800)
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200
    write, failed = Series.write, []

    def flaky(self, path):
        if not failed and any(window.state == "landed" for window in self.windows):
            failed.append(True)
            raise OSError("the disk is full")
        write(self, path)

    Series.write = flaky
    try:
        with pytest.raises(CloudImportError):
            rig.run()
    finally:
        Series.write = write

    rig.run()

    assert rig.cloud.all_sent() == ["a1.bin", "a2.bin"]
    assert len(rig.cloud.rows) == 2


@pytest.mark.parametrize("limits", [{"space_factor": "2", "reserve_bytes": 1000}, {"space_factor": 2, "reserve_bytes": 1.5}])
def test_a_published_limit_that_cannot_be_read_waits_as_unknown_instead_of_meaning_no_limit(rig, limits):
    """Catches an unreadable published limit that is read as "no limit" and sends the window."""
    rig.files(a1__bin=800)
    rig.cloud.limits = limits

    with pytest.raises(CloudImportError):
        rig.run()

    assert rig.cloud.completed == []
    assert "limit" in rig.said()


def test_the_cli_classifies_text_with_the_inline_limit_that_the_cell_reports(rig):
    """Catches a classification by the CLI's own copy of the rule: a 500-byte note that the cell does not read as text."""
    rig.files(a01__md=500, b01__bin=800)
    rig.cell.inline = 100
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 1200

    rig.run()

    assert rig.cloud.sent(rig.cloud.completed[0]) == {"a01.md"}  # Offloadable files go first; with the local rule it is text and goes last.


def test_the_cli_classifies_artifacts_with_the_largest_offloadable_file_that_the_cell_reports(rig):
    """Catches a classification by the CLI's own copy of the rule: a 3000-byte file that the cell would offload."""
    rig.files(a01__bin=3000)
    rig.cell.largest = 5000
    rig.cell.allowance = 0

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert "allowance exhausted" in stop.value.message


def test_a_series_that_the_preflight_refuses_records_no_demand(rig):
    """Catches a demand that a refused series leaves behind at the cell."""
    rig.files(a1__bin=800)
    rig.cell.effective = 100

    with pytest.raises(CloudImportError):
        rig.run()

    assert [call["next_window_bytes"] for call in rig.cell.calls if call["next_window_bytes"] is not None] == []


# -- Review round 2


def test_a_rerun_after_a_restore_that_lost_every_receipt_sends_the_landed_windows_again(rig):
    """Catches an absent window listing read as unknown: the rerun waits two hours and stops instead of sending again."""
    _stopped_after_two_windows(rig)
    rig.cell.windows.clear()  # The cell holds no window record, so its reply has no `import_windows` key.

    rig.run()

    assert rig.cloud.all_sent() == ["a1.bin", "a1.bin", "a2.bin", "a2.bin", "a3.bin", "a4.bin"]


def _fail_the_first_landing(rig: Rig) -> CloudImportError:
    rig.files(a01__bin=800, a02__bin=800)
    refused = []
    rig.cloud.refusal_code = "ARCHIVE_UNREADABLE"
    rig.cloud.refuse_when = lambda members: not refused and not refused.append(True)
    with pytest.raises(CloudImportError) as stop:
        rig.run()
    assert stop.value.code == "ARCHIVE_UNREADABLE"
    return stop.value


def test_a_window_whose_attempt_ended_failed_is_sent_again_under_a_new_import_and_lands_once(rig):
    """Catches a window pinned to the import ID of a dead attempt, which Cloud replays on every rerun."""
    assert _fail_the_first_landing(rig).resume, "the stop must say that a rerun goes on"

    rig.run()

    assert rig.cloud.all_sent() == ["a01.bin", "a02.bin"]
    assert len(rig.cloud.rows) == 2


def test_a_window_whose_member_changed_after_a_failed_attempt_is_sent_again_under_a_new_import(rig):
    """Catches a rerun that gets IMPORT_CONFLICT for a changed declaration under the old import ID."""
    _fail_the_first_landing(rig)
    (rig.source / "a02.bin").write_bytes(b"changed before the retry")

    rig.run()

    assert rig.cloud.all_sent() == ["a01.bin"]
    assert "a02.bin" in rig.said()


def test_a_window_whose_copy_was_deleted_while_cloud_still_imports_it_lands_once(rig):
    """Catches a window sent again under a new import while Cloud still imports the first one."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.holding = rig.cloud.fail_next_read = True
    with pytest.raises(CloudImportError) as first:
        rig.run(poll_seconds=0.001)
    assert first.value.code == "NETWORK_ERROR"
    for held in rig.tmp.glob("checkpoint/window-*/archive.age"):
        held.unlink()  # The person follows the CLI's own advice to delete the encrypted copy.
    rig.cloud.read_count = 0  # Cloud finishes the first import on the third read of its row.

    for _ in range(3):  # Each rerun is the same command.
        try:
            rig.run(poll_seconds=0.001)
            break
        except CloudImportError:
            continue
    rig.cloud.release()

    assert rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


def test_a_window_whose_encrypted_copy_changed_before_complete_keeps_its_import_and_says_the_rerun_replays_it(rig):
    """Catches a client abandon that gives the window a new import while the old one may still be open."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.fail_prepare_once = True
    with pytest.raises(CloudImportError):
        rig.run()
    for held in rig.tmp.glob("checkpoint/window-*/archive.age"):
        held.write_bytes(held.read_bytes() + b"x")
    with pytest.raises(CloudImportError) as abandoned:
        rig.run()
    assert abandoned.value.code == "CIPHERTEXT_CHANGED"
    assert abandoned.value.resume and "start a new run" not in abandoned.value.message

    rig.run()

    assert len(rig.cloud.rows) == 1 and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


def test_a_member_change_does_not_give_a_new_import_to_a_window_that_cloud_is_still_importing(rig):
    """Catches a renewal after a changed member while the old import is live, which lands the window twice."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.holding = rig.cloud.fail_next_read = True
    with pytest.raises(CloudImportError):
        rig.run(poll_seconds=0.001)
    for held in rig.tmp.glob("checkpoint/window-*/*"):
        held.unlink()
    (rig.source / "a02.bin").write_bytes(b"changed before the retry")

    rig.cloud.read_count = 0  # Cloud finishes what it holds on the third read of a row.

    for _ in range(3):  # Each rerun is the same command.
        try:
            rig.run(poll_seconds=0.001)
            break
        except CloudImportError:
            continue
    rig.cloud.release()

    assert len(rig.cloud.rows) == 1 and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


def test_a_resumed_run_whose_first_call_fails_still_says_what_is_not_in_exomem(rig):
    """Catches a failure on the first network call of a rerun that carries no notice."""
    from fastmcp.exceptions import ToolError

    _stopped_after_two_windows(rig)

    def fail(call):
        raise ToolError("boom")

    rig.cell.before_reply = fail

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert NOT_IN_EXOMEM in stop.value.message


def test_a_skipped_member_adds_to_the_cells_keep_the_source_message_and_does_not_replace_it(rig):
    """Catches a final message that drops what the cell says about unfiled members."""
    _stopped_after_two_windows(rig, count=5)
    (rig.source / "a3.bin").write_bytes(b"changed after the series began")

    rig.run()

    assert "unfiled" in rig.said() and "1 files were not sent" in rig.said()


# -- Review round 4


def _stop_with_cloud_importing_slowly_then_change_a_member(rig: Rig) -> None:
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.holding = rig.cloud.fail_next_read = True
    with pytest.raises(CloudImportError):
        rig.run(poll_seconds=0.001)
    for folder in rig.tmp.glob("checkpoint/window-*"):
        (folder / "checkpoint.json").unlink()
    (rig.source / "a02.bin").write_bytes(b"changed after the upload")


# -- Review round 5: one open import per cell, and the window's identity rules (a) to (d) in cloud_import_windows.py


def _first_window(rig: Rig) -> dict:
    """The first window of the series record: its import ID and the IDs of its earlier attempts."""
    return json.loads((rig.tmp / "checkpoint" / "series.json").read_text())["windows"][0]


def test_rule_d_a_window_whose_create_meets_another_open_import_stops_at_once_and_goes_on_once_that_import_ends(rig):
    """R1 and rule (d): catches a conflict read as a dead attempt, which renews and creates again until RecursionError."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.open_another_import()

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert stop.value.code == "IMPORT_CONFLICT" and stop.value.resume
    assert "another import is open; cancel it in Home or wait for it to end" in stop.value.message
    assert NOT_IN_EXOMEM in stop.value.message
    assert rig.cloud.creates == 1
    first = _first_window(rig)["import_id"]
    rig.cloud.rows["another-import"].update(observed_state="done")
    assert rig.run()["import_ids"] == [first] and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


@pytest.mark.parametrize("requested, observed", [("uploaded", "importing"), ("discard_requested", "failed")])
def test_rule_a_a_window_keeps_its_import_while_cloud_holds_its_earlier_attempt_open_or_done(rig, requested, observed):
    """Rule (a): catches a new import ID while the earlier attempt is open or done, which lands the window twice or loops."""
    _stop_with_cloud_importing_slowly_then_change_a_member(rig)
    before = _first_window(rig)["import_id"]
    rig.cloud.rows[before].update(requested_state=requested, observed_state=observed)  # The cell lists no receipt for it.

    with pytest.raises(CloudImportError) as stop:
        rig.run(poll_seconds=0.001)

    assert stop.value.resume and "earlier attempt of this window" in stop.value.message
    assert _first_window(rig)["import_id"] == before and _first_window(rig)["attempts"] == []
    assert len(rig.cloud.rows) == 1


def test_rule_b_a_window_gets_one_new_import_per_ended_attempt_and_retries_its_send_once(rig):
    """Rule (b): catches a renewal that repeats: a retry or rerun that meets another open import keeps the new ID."""
    _stop_with_cloud_importing_slowly_then_change_a_member(rig)
    old = _first_window(rig)["import_id"]
    rig.cloud.rows[old].update(observed_state="failed")
    rig.cloud.open_another_import()

    for creates in (2, 3):  # The rerun renews on its read of the failed row and creates once, then a rerun keeps the new ID.
        with pytest.raises(CloudImportError) as stop:
            rig.run(poll_seconds=0.001)
        assert "another import is open" in stop.value.message and stop.value.resume
        assert old not in stop.value.message  # The failed attempt no longer holds the cell, so the stop never names it as the open import.
        assert rig.cloud.creates == creates and _first_window(rig)["attempts"] == [old]


@pytest.mark.parametrize("observed", ["failed", "expired", "discarded"])
def test_rule_c_after_cloud_ends_the_earlier_attempt_the_next_run_sends_the_window_once_under_a_new_import(rig, observed):
    """Rule (c): catches an ended attempt that is not read as over, so the window stays pinned to it and never goes on."""
    _stop_with_cloud_importing_slowly_then_change_a_member(rig)
    with pytest.raises(CloudImportError):
        rig.run(poll_seconds=0.001)  # Cloud still holds the earlier attempt.
    old = _first_window(rig)["import_id"]
    rig.cloud.rows[old].update(observed_state=observed)
    rig.cloud.holding = False  # Cloud lands the new import at once.

    result = rig.run(poll_seconds=0.001)

    assert rig.cloud.all_sent() == ["a01.bin"]
    assert len(rig.cloud.rows) == 2 and old not in result["import_ids"]


# -- Task 5.4.20: the CLI settles its own stalled attempts (rules (c), (e) and (f) in cloud_import_windows.py)


def _network_error(*_) -> None:
    raise httpx2.ConnectError("interrupted")


def _stop_after_complete_while_cloud_imports(rig: Rig) -> str:
    """The window is uploaded and Cloud imports it, and the run stops on a dropped read. Returns its import ID."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.holding = rig.cloud.fail_next_read = True
    with pytest.raises(CloudImportError) as stop:
        rig.run(poll_seconds=0.001)
    assert stop.value.code == "NETWORK_ERROR"
    rig.cloud.holding = False
    return _first_window(rig)["import_id"]


def _checkpoints(rig: Rig) -> list[Path]:
    return list(rig.tmp.glob("checkpoint/window-*/checkpoint.json"))


def test_rule_e_a_checkpoint_whose_import_cloud_has_no_row_for_is_dropped_and_the_window_lands_once(rig):
    """Rule (e): catches a stale checkpoint that a rerun resumes, so it waits on a new row that never gets an upload."""
    old = _stop_after_complete_while_cloud_imports(rig)
    del rig.cloud.rows[old]  # Cloud has no row for the window's import ID.
    rig.cloud.held.clear()
    unsent = []

    def cloud_expires_an_import_that_gets_no_upload(import_id):
        row = rig.cloud.rows.get(import_id)
        if row is not None and "part_urls" not in row:
            unsent.append(import_id)
            if len(unsent) >= 3:
                row.update(observed_state="expired", error_code="IMPORT_EXPIRED")

    rig.cloud.on_read = cloud_expires_an_import_that_gets_no_upload

    result = rig.run(poll_seconds=0.001)

    assert result["import_ids"] == [old] and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


def test_rule_e_a_network_error_never_drops_a_checkpoint(rig):
    """Rule (e): catches a failed read taken as "Cloud cannot show a row", which drops a live checkpoint."""
    old = _stop_after_complete_while_cloud_imports(rig)
    del rig.cloud.rows[old]
    rig.cloud.held.clear()
    creates = rig.cloud.creates
    rig.cloud.on_read = _network_error

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert stop.value.resume and NOT_IN_EXOMEM in stop.value.message
    assert len(_checkpoints(rig)) == 1 and rig.cloud.creates == creates


def test_verify_a_member_change_after_a_stopped_attempt_lands_once_with_the_new_member_set_and_no_manual_cancel(rig):
    """Verify of 5.4.20, rule (f): catches a never-uploaded attempt that holds the cell until the person presses Cancel in Home."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.short_parts_once = True
    with pytest.raises(CloudImportError) as abandoned:
        rig.run()
    assert abandoned.value.code == "PARTS_INVALID"
    old = _first_window(rig)["import_id"]
    (rig.source / "a02.bin").write_bytes(b"changed after the abandon")

    result = rig.run()

    assert rig.cloud.cancelled == [old] and rig.cloud.rows[old]["observed_state"] == "discarded"
    assert rig.cloud.all_sent() == ["a01.bin"] and len(rig.cloud.rows) == 2
    assert old not in result["import_ids"] and "a02.bin" in rig.said()


def test_rule_f_a_member_change_that_empties_a_stopped_window_cancels_its_attempt_and_lands_the_rest_once(rig):
    """Rule (f): catches an emptied window that leaves the record while its never-uploaded attempt holds the cell."""
    rig.files(a01__bin=800, a02__bin=800, a03__bin=800)
    rig.cell.budget = rig.cell.reachable = rig.cell.refill = 2000  # Window 1 is a01.bin and a02.bin.
    rig.cloud.short_parts_once = True
    with pytest.raises(CloudImportError) as abandoned:
        rig.run()
    assert abandoned.value.code == "PARTS_INVALID"
    old = _first_window(rig)["import_id"]
    for name in ("a01.bin", "a02.bin"):
        (rig.source / name).write_bytes(b"changed after the abandon")

    result = rig.run()

    assert rig.cloud.cancelled == [old] and old not in result["import_ids"]
    assert rig.cloud.all_sent() == ["a03.bin"] and result["not_sent"] == ["a01.bin", "a02.bin"]


def test_rule_f_a_network_error_never_cancels_an_attempt(rig):
    """Rule (f): catches a cancel that a failed row read reaches, which ends an attempt on no evidence."""
    rig.files(a01__bin=800, a02__bin=800)
    rig.cloud.short_parts_once = True
    with pytest.raises(CloudImportError):
        rig.run()
    old = _first_window(rig)["import_id"]
    (rig.source / "a02.bin").write_bytes(b"changed after the abandon")
    creates = rig.cloud.creates
    # Cloud stops answering reads once the changed declaration conflicts.
    rig.cloud.on_read = lambda import_id: _network_error() if rig.cloud.creates > creates else None

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert stop.value.resume and NOT_IN_EXOMEM in stop.value.message
    assert rig.cloud.cancelled == [] and rig.cloud.rows[old]["requested_state"] == "requested"
    assert len(rig.cloud.rows) == 1


def test_verify_a_restored_cell_that_lost_a_windows_receipt_sends_that_window_again_exactly_once(rig):
    """Verify of 5.4.20, rule (c): catches a done attempt whose receipt a restore lost read as landed, so its members never arrive."""
    old = _stop_after_complete_while_cloud_imports(rig)
    rig.cloud.release()  # Cloud finishes the import, and the cell records its receipt.
    rig.cell.windows.clear()  # A restore of the cell loses the receipt.

    result = rig.run()

    assert rig.cloud.all_sent() == ["a01.bin", "a01.bin", "a02.bin", "a02.bin"]
    assert len(rig.cloud.completed) == 2 and _first_window(rig)["attempts"] == [old]
    assert old not in result["import_ids"]


def test_rule_c_a_done_attempt_whose_receipt_the_cell_lists_once_cloud_finishes_is_not_sent_again(rig):
    """Rule (c): catches a lost receipt judged from a listing read before the row, while Cloud was still finishing the import."""
    old = _stop_after_complete_while_cloud_imports(rig)
    rig.cloud.on_read = lambda import_id: rig.cloud.release()  # Cloud finishes while the CLI reads the row.

    result = rig.run()

    assert result["import_ids"] == [old] and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]


def test_rule_c_an_unknown_listing_never_sends_a_done_window_again(rig):
    """Rule (c): catches an unknown listing taken as a lost receipt, which lands the window twice."""
    old = _stop_after_complete_while_cloud_imports(rig)
    rig.cloud.release()
    rig.cell.windows.clear()
    # The listing is known when the rerun starts and turns unknown once the CLI reads the done row.
    rig.cloud.on_read = lambda import_id: setattr(rig.cell, "windows_unknown", "the window records cannot be listed")

    with pytest.raises(CloudImportError) as stop:
        rig.run()

    assert stop.value.resume and NOT_IN_EXOMEM in stop.value.message
    assert rig.cloud.completed == [old] and len(rig.cloud.rows) == 1
    assert f"import_discard, import_id {old}" in stop.value.message


def test_rule_c_a_home_cancel_of_an_importing_attempt_whose_discard_leaves_a_record_files_each_member_once(rig):
    """Rule (c): catches an ended attempt read as over while the cell holds its record, so a rerun and a resend file it twice."""
    old = _stop_after_complete_while_cloud_imports(rig)
    rig.cloud.release()  # The Job imports the window before cellctl stops it.
    del rig.cell.windows[old]
    rig.cloud.rows[old].update(requested_state="cancelled", observed_state="discarded")  # The person pressed Cancel in Home.
    # The discard's residual record appears after the rerun lists the receipts and before it reads the row.
    rig.cloud.on_read = lambda import_id: rig.cell.discard(old, ["a01.bin", "a02.bin"]) if import_id == old else None

    rig.run()
    rig.run()
    rig.run(resend_residual=True)

    # The old import carries both members and its discard files neither, so the resend files each member once.
    assert rig.cloud.all_sent() == ["a01.bin", "a01.bin", "a02.bin", "a02.bin"]
    assert _first_window(rig)["import_id"] == old and _first_window(rig)["attempts"] == []


def test_a_window_that_lands_on_the_cells_record_at_a_rerun_leaves_no_encrypted_copy(rig):
    """Catches a window that lands on the cell's record without the client, which keeps a full encrypted copy on disk."""
    old = _stop_after_complete_while_cloud_imports(rig)
    assert list(rig.tmp.glob("checkpoint/window-*/archive.age"))  # The stall keeps the copy for the rerun.
    rig.cloud.release()  # Cloud finishes the import, and the cell lists its receipt.

    result = rig.run()

    assert result["import_ids"] == [old] and rig.cloud.all_sent() == ["a01.bin", "a02.bin"]
    assert not list(rig.tmp.glob("checkpoint/window-*/archive.age")) and not _checkpoints(rig)
