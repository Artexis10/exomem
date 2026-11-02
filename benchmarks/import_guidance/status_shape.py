"""Scripted replies in the shape the product returns, built in one place.

`read_import` status is `import_windows.report` merged into `adoption_run.status`. The window listing, the counts and
`keep_source` come from the product's own `import_windows` code, so a scenario cannot show a window state that the tool
never returns. The budget figures are `{"bytes": N}` or `{"unknown": why}`, as `import_windows._figure` writes them.

`observe_transfer` follows the envelope in `openspec/changes/add-cloud-transfer-tools/specs/cloud-transfer-tools/spec.md`.
The fields inside an import view and inside a route are not in this repository: the Cloud gateway owns them, so they
are unverified.

A scenario file writes `{"$status": {...}}`, `{"$transfer_status": {...}}` or `{"$import_handoff": {...}}` where it
needs one of these replies, and `expand` builds it when the scenario loads. `exomem` is imported only when a reply is
built, so a scenario with no such reply starts the server without it.

The builders call private helpers of `import_windows` (`_Reading`, `_drain` and the like) on purpose: the product
exposes no public way to build one section of a status, and a copy of its rules here would drift. A rename there
fails this probe's parity test.
"""

from __future__ import annotations

from typing import Any

GIB = 1 << 30
AS_OF = "2026-10-30T09:12:00+00:00"
# The spec's export half for a deployment that offers no exports.
EXPORTS_OFF = {"state": "unavailable", "code": "EXPORTS_NOT_OFFERED"}


def _figure(value: Any) -> dict:
    """An int is a known figure; a dict (`{"unknown": why}`) is written as given."""
    return value if isinstance(value, dict) else {"bytes": value}


def _windows(specs: list[dict]) -> list:
    from exomem import adoption_consume, import_stage, import_windows

    # A member that no run scanned: the code every residual entry needs, and a closed result key of the product.
    code = adoption_consume.OUTSIDE_RUN
    landed, discarded, unreadable = (
        import_stage.WINDOW_LANDED,
        import_stage.WINDOW_DISCARDED,
        import_stage.WINDOW_UNREADABLE,
    )
    windows = []
    for spec in specs:
        state = spec["state"]
        if state not in (landed, discarded, unreadable):
            raise ValueError(
                f"window state {state!r} is not one of {landed}, {discarded}, {unreadable}"
            )
        members = tuple(
            {"path": m["path"], "bytes": m["bytes"], "code": m.get("code", code)}
            | ({"staged": m.get("staged", True)} if state == landed else {})
            for m in spec.get("members", ())
        )
        acknowledged = tuple(
            {
                "path": m["path"],
                "bytes": m["bytes"],
                "code": m.get("code", code),
                "at": m.get("at", AS_OF),
            }
            for m in spec.get("acknowledged", ())
        )
        windows.append(
            import_windows.Window(
                spec["import_id"],
                state,
                members,
                acknowledged,
                spec.get("problem"),
                # A discard removes the stage; a landed window or a record that cannot be read still has one.
                spec.get("staged", state != discarded),
                tuple(spec.get("renames_pending", ())),
            )
        )
    return windows


def _budget(spec: dict, windows: list) -> dict:
    """The `window_budget` section of a cell that has room. `spec` overrides a figure, or sets it unknown."""
    from exomem import import_windows as iw
    from exomem import media_capacity, media_store

    bounds = media_capacity.Bounds(
        object_bytes=10 * GIB, hydration_bytes=9 * GIB, volume_bytes=100 * GIB, volume_fraction=0.25
    )
    figures = {"window_budget": 14 * GIB, "reachable_budget": 20 * GIB, "job_pinned": 0} | {
        name: spec[name]
        for name in ("window_budget", "reachable_budget", "job_pinned")
        if name in spec
    }
    reachable, pinned = figures["reachable_budget"], figures["job_pinned"]
    if "effective_file_limit" in spec:
        effective = spec["effective_file_limit"]
    elif isinstance(reachable, int) and isinstance(pinned, int):
        effective = max(0, min(bounds.largest_offloadable(), reachable + pinned))
    else:
        raise ValueError(
            "a budget with an unknown reachable or pinned figure must say effective_file_limit"
        )
    occupancy = (
        iw._Reading(reason=pinned["unknown"])
        if isinstance(pinned, dict)
        else iw._Reading(media_store.Occupancy(evictable_bytes=0, pending_offloads=0, pending_offload_bytes=0,
                                                pinned_bytes=pinned))
    )
    unfiled = iw._read(lambda: iw._unfiled(iw._Reading(windows), iw._Reading(bounds)))
    allowance = spec.get(
        "media_allowance",
        {
            "allowance_bytes": 200 * GIB,
            "used_bytes": 31 * GIB,
            "remaining_bytes": 169 * GIB,
            "observed_at": AS_OF,
        },
    )
    return {
        "window_budget": _figure(figures["window_budget"]),
        "reachable_budget": _figure(reachable),
        "job_pinned": _figure(pinned),
        "effective_file_limit": _figure(effective),
        "classification": iw._classification(iw._Reading(bounds)),
        "volume": {"used_bytes": 40 * GIB},
        "media_allowance": allowance,
        # Eviction is on: its idle age is the value of the third reading.
        "drain": iw._drain(unfiled, occupancy, iw._Reading(3600.0)),
    }


def status_reply(spec: dict) -> dict:
    """`read_import` status without a run ID. `spec`: `windows` (the `Window` fields) and `budget` (see `_budget`)."""
    from exomem import import_windows as iw

    windows = _windows(spec.get("windows", []))
    reply: dict = {"mode": "adoption", "mutated": False, "runs": []}
    # `report` leaves the listing out when the cell holds no window record.
    if windows:
        reply["import_windows"] = iw._windows_section(windows, None)
    reply["window_budget"] = _budget(spec.get("budget", {}), windows)
    return reply


def transfer_status(spec: dict) -> dict:
    """`observe_transfer` status. `spec["import"]` is the import view, or null for no import."""
    return {
        "success": True,
        "action": "status",
        "as_of": AS_OF,
        "import": spec.get("import"),
        "export": EXPORTS_OFF,
    }


def import_handoff(spec: dict) -> dict:
    """`observe_transfer` import_handoff. `spec["routes"]` is the ordered route list."""
    return {
        "success": True,
        "action": "import_handoff",
        "open_import": spec.get("open_import"),
        "routes": spec["routes"],
    }


BUILDERS = {
    "$status": status_reply,
    "$transfer_status": transfer_status,
    "$import_handoff": import_handoff,
}


def expand(reply: Any) -> Any:
    """Build a reply that a scenario names with one `$` key; a list is a sequence of replies."""
    if isinstance(reply, list):
        return [expand(item) for item in reply]
    if isinstance(reply, dict) and len(reply) == 1:
        [(key, spec)] = reply.items()
        if key in BUILDERS:
            return BUILDERS[key](spec)
    return reply
