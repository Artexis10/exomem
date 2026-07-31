"""Continuation-checkpoint round-trip driver (Track C).

Drives the INSTALLED ``exomem_continuation_checkpoint.py`` hook through the
same command a client would run (settings entry ``bash
"<hooks>/exomem-continuation-checkpoint.sh" --client <c>`` for claude,
``python3 "<hooks>/exomem_continuation_checkpoint.py" --client codex`` for
codex; install_hook.py lines 261-275), with event JSON on stdin.

Contract facts encoded here (line references are into
src/exomem/_hooks/exomem_continuation_checkpoint.py):

- Event envelope (lines 52-62, 80-89, 232-279): PreCompact requires
  ``trigger`` in {manual, auto} and NO ``source``; SessionStart requires
  ``source`` in {compact, resume} and NO ``trigger``; ``session_id`` is
  mandatory; claude additionally supports SessionEnd. Keys may be snake_case
  or camelCase.
- Home resolution (lines 304-313): EXOMEM_HOOK_HOME wins over
  CODEX_HOME / CLAUDE_CONFIG_DIR.
- State layout (lines 316-324): checkpoints live at
  ``<home>/.cache/exomem-continuation/<client>/<safe>-<digest>/current.json``
  with schema_version 1 (line 26) and a hard 64 KiB bound (line 29,
  encode_checkpoint lines 883-887).
- What a checkpoint captures (build_checkpoint, lines 890-950): the
  TRANSCRIPT is profiled only structurally — size/mtime/offset plus the
  sha256 of its last 64 KiB slice (lines 375-412); no content is parsed out
  of it. Git identity (root name, branch, HEAD, dirty paths) comes from the
  event ``cwd`` via read-only git probes (lines 497-599). Task lines come
  from a CLOSED artifact allowlist relative to the git root — fixed paths
  ``.superpowers/sdd/progress.md``, ``.task/TASK.md``, ``.task/RESULT.md``
  plus ``openspec/changes/<name>/tasks.md`` — with checkbox counting and
  incomplete line numbers (lines 609-629, 774-807).
- Restore (SessionStart): select_checkpoint requires same client, same
  session, same per-client state-root binding, retention window, and a
  transcript binding re-check (lines 2835-2891); the render surfaces
  workspace root/branch/head, transcript binding (incl. slice sha256),
  artifact paths with incomplete counts/line numbers, and dirty paths
  (lines 957-1037), emitted as ``hookSpecificOutput.additionalContext``
  with hookEventName SessionStart (lines 3827-3843).
- CROSS-CLIENT: a checkpoint written by client A is contractually INVISIBLE
  to client B even in a shared EXOMEM_HOOK_HOME — the state roots are
  per-client (lines 316-317) and _candidate_matches rejects on client and
  state_root_binding (lines 2845-2850). The cross-client score is therefore
  isolation-respected plus client B's own round trip in the same shared home.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from membench.trackc.hook_home import (
    HookHome,
    create_hook_home,
    ensure_isolated,
    make_workdir,
)

SCHEMA_VERSION = 1  # exomem_continuation_checkpoint.py line 26
MAX_CHECKPOINT_BYTES = 64 * 1024  # line 29
HOOK_TIMEOUT_SECONDS = 30.0
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Membench",
    "GIT_AUTHOR_EMAIL": "membench@example.invalid",
    "GIT_COMMITTER_NAME": "Membench",
    "GIT_COMMITTER_EMAIL": "membench@example.invalid",
}

WORKSPACE_NAME = "membench-workspace"
BRANCH_MARKER = "membench-marker-branch"
OPENSPEC_CHANGE = "membench-marker-change"
DIRTY_MARKER = "notes/marker-dirty-file.md"
TASK_ARTIFACT = ".task/TASK.md"
OPENSPEC_ARTIFACT = f"openspec/changes/{OPENSPEC_CHANGE}/tasks.md"


@dataclass
class PlantedWorkspace:
    """A fabricated git workspace + transcript carrying structural markers."""

    root: Path
    transcript: Path
    head: str
    slice_sha256: str
    markers: dict[str, str] = field(default_factory=dict)

    def marker_values(self) -> dict[str, str]:
        return dict(self.markers)


@dataclass
class RoundTripResult:
    client_a: str
    client_b: str
    checkpoint_path: Path | None
    checkpoint_bytes: int
    schema_version: int | None
    restored_context: str
    markers: dict[str, str]
    recalled: list[str]
    missing: list[str]
    recall: float
    cross_client: bool
    isolation_respected: bool | None  # None for same-client runs
    own_roundtrip: "RoundTripResult | None" = None  # client_b's own trip (cross-client)

    def as_dict(self) -> dict:
        return {
            "client_a": self.client_a,
            "client_b": self.client_b,
            "checkpoint_path": str(self.checkpoint_path) if self.checkpoint_path else None,
            "checkpoint_bytes": self.checkpoint_bytes,
            "schema_version": self.schema_version,
            "recall": self.recall,
            "recalled": self.recalled,
            "missing": self.missing,
            "cross_client": self.cross_client,
            "isolation_respected": self.isolation_respected,
            "own_roundtrip": self.own_roundtrip.as_dict() if self.own_roundtrip else None,
        }


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args],
        env={"PATH": "/usr/bin:/bin", **_GIT_IDENTITY},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def plant_workspace(base: Path) -> PlantedWorkspace:
    """Fabricate the marker workspace + a small JSONL transcript.

    Markers are planted where the hook actually reads them (see module
    docstring): git identity in the repo itself, task lines in the allowlisted
    artifact files, a dirty path as an untracked file, and the transcript's
    contribution as its last-64KiB slice sha256 (the hook hashes, never
    parses, transcript content — lines 375-412).
    """
    ws = base / WORKSPACE_NAME
    ws.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", BRANCH_MARKER, str(ws)],
        env={"PATH": "/usr/bin:/bin", **_GIT_IDENTITY},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    (ws / "README.md").write_text("membench checkpoint seed\n", encoding="utf-8")
    _git(ws, "add", "README.md")
    _git(ws, "commit", "-q", "-m", "membench seed")
    head = _git(ws, "rev-parse", "HEAD")

    (ws / ".task").mkdir()
    (ws / TASK_ARTIFACT).write_text(
        "# Task\n\n- [x] done marker\n- [ ] alpha open marker\n- [ ] beta open marker\n",
        encoding="utf-8",
    )
    (ws / "openspec" / "changes" / OPENSPEC_CHANGE).mkdir(parents=True)
    (ws / OPENSPEC_ARTIFACT).write_text(
        "# Tasks\n\n- [x] finished step\n- [ ] gamma open step\n",
        encoding="utf-8",
    )
    (ws / "notes").mkdir()
    (ws / DIRTY_MARKER).write_text("membench dirty marker\n", encoding="utf-8")

    transcript = base / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "membench transcript marker"}) + "\n",
        encoding="utf-8",
    )
    raw = transcript.read_bytes()
    slice_sha = hashlib.sha256(raw[-MAX_CHECKPOINT_BYTES:]).hexdigest()

    markers = {
        # Every value below is contract-promised in render_continuation
        # (lines 991-1028) for an un-truncated checkpoint.
        "workspace_name": WORKSPACE_NAME,
        "branch": BRANCH_MARKER,
        "head": head,
        "task_artifact": TASK_ARTIFACT,
        "openspec_artifact": OPENSPEC_ARTIFACT,
        "dirty_path": DIRTY_MARKER,
        "transcript_slice_sha256": slice_sha,
    }
    return PlantedWorkspace(
        root=ws, transcript=transcript, head=head, slice_sha256=slice_sha, markers=markers
    )


def continuation_command(home: HookHome, client: str, event: str) -> str:
    """The wired continuation command for ``event`` from the client's config
    file in this home (claude: settings.json; codex: hooks.json)."""
    config_path = home.home / ("hooks.json" if client == "codex" else "settings.json")
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    for group in settings["hooks"][event]:
        for entry in group.get("hooks", []):
            command = str(entry.get("command", ""))
            if "continuation" in command and f"--client {client}" in command:
                return command
    raise AssertionError(f"no continuation command wired for {client}/{event}")


def run_checkpoint_hook(
    home: HookHome, client: str, event: dict, *, event_name: str
) -> subprocess.CompletedProcess:
    env = home.base_env()
    ensure_isolated(env)
    return subprocess.run(
        ["bash", "-c", continuation_command(home, client, event_name)],
        input=json.dumps(event),
        env=env,
        capture_output=True,
        text=True,
        timeout=HOOK_TIMEOUT_SECONDS,
    )


def _state_root(home: HookHome, client: str) -> Path:
    return home.home / ".cache" / "exomem-continuation" / client  # lines 316-317


def _find_checkpoint(home: HookHome, client: str) -> Path | None:
    files = sorted(_state_root(home, client).rglob("current.json"))
    return files[0] if files else None


def _parse_restore(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""
    payload = json.loads(text)
    inner = payload["hookSpecificOutput"]
    if inner.get("hookEventName") != "SessionStart":  # lines 3838-3843
        raise AssertionError(f"unexpected restore envelope: {payload!r}")
    return str(inner.get("additionalContext") or "")


def _score(markers: dict[str, str], context: str) -> tuple[list[str], list[str], float]:
    recalled = [name for name, value in markers.items() if value in context]
    missing = [name for name in markers if name not in recalled]
    recall = len(recalled) / len(markers) if markers else 1.0
    return recalled, missing, recall


def _one_trip(
    home: HookHome,
    write_client: str,
    read_client: str,
    planted: PlantedWorkspace,
    session_id: str,
) -> tuple[Path | None, int, int | None, str]:
    pre_event = {
        "hook_event_name": "PreCompact",
        "session_id": session_id,
        "trigger": "manual",  # required for PreCompact (lines 52-62, 253-257)
        "cwd": str(planted.root),
        "transcript_path": str(planted.transcript),
    }
    proc = run_checkpoint_hook(home, write_client, pre_event, event_name="PreCompact")
    if proc.returncode != 0:
        raise RuntimeError(f"PreCompact hook rc={proc.returncode}: {proc.stderr[:300]}")
    checkpoint_path = _find_checkpoint(home, write_client)
    checkpoint_bytes = checkpoint_path.stat().st_size if checkpoint_path else 0
    schema = None
    if checkpoint_path is not None:
        schema = json.loads(checkpoint_path.read_text(encoding="utf-8")).get("schema_version")
    start_event = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "source": "compact",  # required for SessionStart (lines 56, 258-262)
        "cwd": str(planted.root),
        "transcript_path": str(planted.transcript),
    }
    restore = run_checkpoint_hook(home, read_client, start_event, event_name="SessionStart")
    return checkpoint_path, checkpoint_bytes, schema, _parse_restore(restore.stdout)


def round_trip(
    client_a: str,
    client_b: str,
    *,
    home: HookHome | None = None,
    workdir: Path | None = None,
) -> RoundTripResult:
    """PreCompact via ``client_a``'s installed hook, SessionStart(compact) via
    ``client_b``'s, in ONE shared EXOMEM_HOOK_HOME.

    Same-client: score = planted-marker recall over the restored context.
    Cross-client: the contract PROMISES isolation (see module docstring), so
    the score is (a) the claude/codex checkpoint stays invisible to the other
    client and (b) client_b's OWN round trip in the same shared home recalls
    its planted markers — proving one shared home serves both installs.
    """
    owned = workdir is None
    if workdir is None:
        workdir = make_workdir(f"ckpt-{client_a}-{client_b}")
    try:
        if home is None:
            home = create_hook_home(client_a, base=workdir)
            if client_b != client_a:
                # Install the second client into the SAME shared hook home.
                create_hook_home(client_b, base=workdir, home=home.home)
        planted = plant_workspace(workdir)
        session = f"membench-{client_a}-to-{client_b}"
        checkpoint_path, checkpoint_bytes, schema, context = _one_trip(
            home, client_a, client_b, planted, session
        )
        cross = client_a != client_b
        if not cross:
            recalled, missing, recall = _score(planted.markers, context)
            return RoundTripResult(
                client_a=client_a,
                client_b=client_b,
                checkpoint_path=checkpoint_path,
                checkpoint_bytes=checkpoint_bytes,
                schema_version=schema,
                restored_context=context,
                markers=planted.markers,
                recalled=recalled,
                missing=missing,
                recall=recall,
                cross_client=False,
                isolation_respected=None,
            )
        isolation_respected = context == ""  # contract lines 2845-2850, 316-317
        # client_b's own round trip in the same shared home.
        own_path, own_bytes, own_schema, own_context = _one_trip(
            home, client_b, client_b, planted, f"membench-own-{client_b}"
        )
        own_recalled, own_missing, own_recall = _score(planted.markers, own_context)
        own = RoundTripResult(
            client_a=client_b,
            client_b=client_b,
            checkpoint_path=own_path,
            checkpoint_bytes=own_bytes,
            schema_version=own_schema,
            restored_context=own_context,
            markers=planted.markers,
            recalled=own_recalled,
            missing=own_missing,
            recall=own_recall,
            cross_client=False,
            isolation_respected=None,
        )
        return RoundTripResult(
            client_a=client_a,
            client_b=client_b,
            checkpoint_path=checkpoint_path,
            checkpoint_bytes=checkpoint_bytes,
            schema_version=schema,
            restored_context=context,
            markers=planted.markers,
            recalled=[],
            missing=sorted(planted.markers),
            recall=0.0,
            cross_client=True,
            isolation_respected=isolation_respected,
            own_roundtrip=own,
        )
    finally:
        if owned:
            from membench.trackc.hook_home import cleanup_workdir

            cleanup_workdir(workdir)
