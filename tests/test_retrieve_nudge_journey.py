"""Task 5.1 — the shipped hook's working-set journey, end to end, for free.

Everything else about this mode is tested against loaded functions. This runs the
script that `install-hook` actually copies into a client's hook directory, as a
SUBPROCESS, over real HTTP on loopback, across a three-turn session — because the
things that break in the field are the seams those unit tests hold still: the
env the client passes, the transport, the shape the service really answers with,
and the token file surviving between two processes that never share memory.

No paid model session is involved, and no model at all: the service is a stub
`http.server` bound to 127.0.0.1 that answers `/api/activate_context` from a
script of packets. Nothing outside loopback is contacted and no state is written
outside `tmp_path` — `HOME`, `EXOMEM_HOOK_HOME` and `EXOMEM_CONFIG_PATH` all
point into it, so the real `~/.cache` is never touched.

The journey, in order:

1. a resolved packet REPLACES the reminder, and the first call carries no token;
2. an `unresolved` turn with two worded candidates and three retrieval-only ones
   injects the two, the `anchor` instruction and then the reminder — the real
   shape a turn about a Planning item produces — and carries turn 1's token;
3. a follow-up still carries that token, because an abstention mints none;
4. a `PreCompact` through the shipped CHECKPOINT hook clears the token;
5. the next call therefore carries none;
6. with the service gone, the hook degrades to the reminder inside its budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from benchmark_capabilities import has_posix_file_modes

import exomem
from exomem._hooks import exomem_continuation_checkpoint as checkpoint

HOOKS = Path(exomem.__file__).parent / "_hooks"
RETRIEVE_SCRIPT = HOOKS / "exomem_retrieve_nudge.py"
CHECKPOINT_SCRIPT = HOOKS / "exomem_continuation_checkpoint.py"

SESSION = "journey-session"
API_KEY = "journey-key"
ROUTE = "/api/activate_context"

TURN_ONE = "Remind me what the Cargo Sled is rated for before I load it this week."
TURN_TWO = "What did we decide about the winter schedule for the northern corridor?"
TURN_THREE = "And how much depot stock does that leave us with for the run north?"
TURN_FOUR = "Anything else recorded about the corridor run before I commit to it?"
TURN_FIVE = "One more question about the depot ledger before I close this out."

REMINDER_HEAD = "[Exomem retrieval check]"
HEADER_HEAD = "[Exomem working set"


# --------------------------------------------------------------------------- #
# The stub service
# --------------------------------------------------------------------------- #


class _Stub:
    """A loopback stand-in for the personal REST facade's activation route.

    Answers from a script, records every request, and 409s anything unscripted so
    a stray call is a visible failure rather than a silent default.
    """

    def __init__(self, packets: list[dict]) -> None:
        self.packets = list(packets)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {"unparsable": raw[:200].decode("utf-8", "replace")}
                stub.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                if self.path != ROUTE or not stub.packets:
                    self.send_response(409)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {"success": True, "data": stub.packets.pop(0)}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:  # noqa: A003 - silence the stub
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> _Stub:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


# --------------------------------------------------------------------------- #
# Packets the stub serves
# --------------------------------------------------------------------------- #


def _generation() -> dict:
    return {
        "freshness_key": "journey",
        "index_generation": 4,
        "roles_hash": "journey-roles",
        "continuity": "absent",
    }


def _resolved(token: str | None) -> dict:
    packet = {
        "anchors": [
            {
                "ref": "Knowledge Base/Products/Cargo Sled.md",
                "path": "Knowledge Base/Products/Cargo Sled.md",
                "title": "Cargo Sled",
                "kind": "resource",
                "lifecycle": "active",
                "status": "resolved",
                "evidence": ["exact_alias"],
            }
        ],
        "roles": [{"id": "resources", "source": "anchor_default", "lane": "units"}],
        "units": [
            {
                "ref": "Knowledge Base/Products/Cargo Sled.md#u1",
                "role": "resources",
                "text": "Never exceed 400 kg.",
                "lifecycle": "active",
                "updated": "2026-09-02",
                "provenance": {"path": "Knowledge Base/Products/Cargo Sled.md"},
            }
        ],
        "pointers": [],
        "current_state": [
            {
                "anchor": "Knowledge Base/Systems/Depot Ledger.md",
                "source": "records",
                "as_of": "2026-09-10",
                "statement": "depot stock: 180 kg",
            }
        ],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 60},
        "generation": _generation(),
        "abstained": False,
    }
    if token is not None:
        packet["continuity"] = token
    return packet


#: The measured real-vault shape: a Planning item and a Records collection the
#: turn's own words reached, beside three pages only recall surfaced.
WORDED = (
    (
        "Knowledge Base/Planning/Corridor/_collection.md",
        "Winter schedule for the northern corridor",
        "plan",
        ["lexical_overlap"],
    ),
    (
        "Knowledge Base/Records/Depot Stock/_collection.md",
        "Depot stock",
        "collection",
        ["claims_match", "retrieval"],
    ),
)
RETRIEVAL_ONLY = (
    ("Knowledge Base/Notes/Insights/northern-corridor-hub.md", "Northern corridor", "hub"),
    ("Knowledge Base/Entities/People/Marit Solheim.md", "Marit Solheim", "entity"),
    ("Knowledge Base/Systems/Depot Ledger.md", "Depot Ledger", "resource"),
)


def _unresolved() -> dict:
    anchors = [
        {
            "ref": ref,
            "path": ref,
            "title": title,
            "kind": kind,
            "lifecycle": "active",
            "status": "partial",
            "evidence": evidence,
        }
        for ref, title, kind, evidence in WORDED
    ] + [
        {
            "ref": ref,
            "path": ref,
            "title": title,
            "kind": kind,
            "lifecycle": "active",
            "status": "partial",
            "evidence": ["retrieval"],
        }
        for ref, title, kind in RETRIEVAL_ONLY
    ]
    return {
        "anchors": anchors,
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {**_generation(), "continuity": "applied"},
        "abstained": True,
        "abstention": {"reason": "unresolved"},
    }


# --------------------------------------------------------------------------- #
# Driving the shipped script
# --------------------------------------------------------------------------- #


def _env(home: Path, tmp_path: Path, port: int) -> dict[str, str]:
    """A deliberately small environment. Nothing EXOMEM_* is inherited, so a real
    key or a real prominence setting on the developer's box cannot reach the
    subprocess, and every path that could escape tmp_path is pinned inside it."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_HOOK_CLIENT": "claude",
        "EXOMEM_RETRIEVE_INJECT": "working_set",
        "EXOMEM_RETRIEVE_INJECT_CLI": "0",
        "EXOMEM_REST_API_KEY": API_KEY,
        "EXOMEM_HOST": "127.0.0.1",
        "EXOMEM_REST_PORT": str(port),
        "EXOMEM_PROMINENCE": "balanced",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": "0",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": "0",
        "EXOMEM_SERVICE_ENV": str(tmp_path / "absent-service.env"),
        "EXOMEM_CONFIG_PATH": str(tmp_path / "absent-config.json"),
    }
    return {key: value for key, value in env.items() if value != ""}


def _prompt(script: Path, env: dict[str, str], prompt: str) -> str:
    """One UserPromptSubmit through the shipped script. Returns additionalContext."""
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": SESSION,
        "prompt": prompt,
    }
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def _lifecycle(env: dict[str, str], event: str, **fields) -> None:
    """One lifecycle event through the shipped CHECKPOINT script."""
    payload = {"hook_event_name": event, "session_id": SESSION, **fields}
    proc = subprocess.run(
        [sys.executable, str(CHECKPOINT_SCRIPT), "--client", "claude"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# The journey
# --------------------------------------------------------------------------- #


def test_the_shipped_hook_walks_a_working_set_session(tmp_path: Path) -> None:
    home = tmp_path / "home"
    token_path = checkpoint.activation_token_path(home, "claude", SESSION)
    stub = _Stub([_resolved("TOKEN-1"), _unresolved(), _resolved("TOKEN-3"), _resolved(None)])

    with stub:
        env = _env(home, tmp_path, stub.port)

        # --- 1. a resolved packet replaces the reminder ----------------------
        first = _prompt(RETRIEVE_SCRIPT, env, TURN_ONE)

        assert first.startswith(HEADER_HEAD)
        assert REMINDER_HEAD not in first
        assert "- state: depot stock: 180 kg [Knowledge Base/Systems/Depot Ledger.md]" in first
        assert "- unit: Never exceed 400 kg." in first
        assert stub.requests[0]["path"] == ROUTE
        assert stub.requests[0]["authorization"] == f"Bearer {API_KEY}"
        assert stub.requests[0]["body"]["turn"] == TURN_ONE
        assert "continuity" not in stub.requests[0]["body"], "nothing to carry yet"
        assert token_path.read_text(encoding="utf-8") == "TOKEN-1"

        # --- 2. an unresolved turn hands over its worded candidates ----------
        second = _prompt(RETRIEVE_SCRIPT, env, TURN_TWO)
        lines = second.splitlines()

        assert lines[0].startswith(HEADER_HEAD)
        assert lines[1] == (
            "- plan: Winter schedule for the northern corridor "
            "[Knowledge Base/Planning/Corridor/_collection.md]"
        )
        assert lines[2] == (
            "- collection: Depot stock "
            "[Knowledge Base/Records/Depot Stock/_collection.md]"
        )
        assert "`anchor`" in lines[3] and "activate_context" in lines[3]
        # The reminder FOLLOWS, because nothing resolved and ordinary recall may
        # still be wanted.
        assert second.rstrip().endswith("skip silently.")
        assert REMINDER_HEAD in second
        assert second.index(HEADER_HEAD) < second.index(REMINDER_HEAD)
        # None of the pages only recall surfaced.
        for ref, title, _kind in RETRIEVAL_ONLY:
            assert ref not in second, ref
            assert title not in second, title
        assert "- unit:" not in second
        # Turn 1's token travelled to the second call.
        assert stub.requests[1]["body"]["continuity"] == "TOKEN-1"

        # --- 3. an abstention mints nothing, so the token still stands -------
        third = _prompt(RETRIEVE_SCRIPT, env, TURN_THREE)

        assert third.startswith(HEADER_HEAD)
        assert stub.requests[2]["body"]["continuity"] == "TOKEN-1"
        assert token_path.read_text(encoding="utf-8") == "TOKEN-3"

        # --- 4. a lifecycle event through the shipped checkpoint hook --------
        _lifecycle(env, "PreCompact", trigger="manual")

        assert not token_path.exists()

        # --- 5. so the next call carries none --------------------------------
        _prompt(RETRIEVE_SCRIPT, env, TURN_FOUR)

        assert "continuity" not in stub.requests[3]["body"]
        assert stub.packets == [], "every scripted packet was consumed"

    # --- 6. with the service gone, the reminder is the floor -----------------
    fifth = _prompt(RETRIEVE_SCRIPT, _env(home, tmp_path, stub.port), TURN_FIVE)

    assert fifth.startswith(REMINDER_HEAD)
    assert HEADER_HEAD not in fifth


@pytest.mark.skipif(not has_posix_file_modes(), reason="mode bits are synthesized here")
def test_the_journey_leaves_the_shared_state_tree_private(tmp_path: Path) -> None:
    """The regression that started this: the retrieve hook creates the tree the
    checkpoint hook shares, and the checkpoint hook requires it to be exactly
    0700. Asserted here through the two SHIPPED scripts under the umask that
    exposed it, because the unit test for it monkeypatches neither."""
    import stat

    home = tmp_path / "home"
    stub = _Stub([_resolved("TOKEN-1")])
    previous = os.umask(0o002)
    try:
        with stub:
            env = _env(home, tmp_path, stub.port)
            _prompt(RETRIEVE_SCRIPT, env, TURN_ONE)
            # The checkpoint hook must still be able to write after that.
            _lifecycle(env, "SessionEnd")
    finally:
        os.umask(previous)

    root = home / ".cache" / "exomem-continuation"
    for level in (home / ".cache", root, root / "claude"):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700, level
    checkpoints = list((root / "claude").glob("*/*"))
    assert checkpoints, "the checkpoint hook wrote through the shared tree"


def test_the_shipped_hook_stays_silent_behind_the_prominence_gate(
    tmp_path: Path,
) -> None:
    """The gates are the same ones stub mode has, asserted against the shipped
    script so an env-handling slip cannot hide behind a loaded function."""
    home = tmp_path / "home"
    stub = _Stub([_resolved("TOKEN-1")])

    with stub:
        env = _env(home, tmp_path, stub.port)
        env["EXOMEM_PROMINENCE"] = "off"

        assert _prompt(RETRIEVE_SCRIPT, env, TURN_ONE) == ""
        assert stub.requests == [], "no transport behind a closed gate"


def test_the_mode_is_off_unless_the_switch_says_working_set(tmp_path: Path) -> None:
    """Default off, and a plain truthy value is stub mode — which asks
    `ask_memory`, a route this stub does not serve, so the activation route must
    see nothing at all."""
    home = tmp_path / "home"
    stub = _Stub([_resolved("TOKEN-1")])

    with stub:
        env = _env(home, tmp_path, stub.port)
        del env["EXOMEM_RETRIEVE_INJECT"]

        assert _prompt(RETRIEVE_SCRIPT, env, TURN_ONE).startswith(REMINDER_HEAD)
        assert [item for item in stub.requests if item["path"] == ROUTE] == []
