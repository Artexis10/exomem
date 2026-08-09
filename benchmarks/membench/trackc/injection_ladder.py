"""CLI-rung integration for the retrieve-nudge inject mode (Track C).

Exercises the shipped transport ladder of
``src/exomem/_hooks/exomem_retrieve_nudge.py`` (line references below are into
that file) against an ISOLATED corpus vault:

- Inject mode is a payload upgrade on the same fire gate: with
  ``EXOMEM_RETRIEVE_INJECT`` truthy the hook fetches compact routing stubs and
  appends them under the ``KB routing stubs`` header (lines 396-408, 337-356;
  header at line 75, 400-char cap at line 76).
- Ladder order (``_gather_hits``, lines 320-334): REST is attempted ONLY when
  ``EXOMEM_REST_API_KEY`` is set (lines 325-329); otherwise, with
  ``EXOMEM_RETRIEVE_INJECT_CLI`` truthy, the CLI rung runs.
- CLI rung resolution (``_fetch_via_cli``, lines 289-317): the hook locates
  the executable via ``shutil.which("exomem") or shutil.which("kb")`` (line
  295) — i.e. PATH lookup, NOT an EXOMEM_COMMAND env var — then runs
  ``<exe> ask_memory --detail compact --limit 3 --mode keyword --json
  <prompt>`` with a hard 5.0s timeout (lines 299-311). It accepts the shared
  ``{"success": true, "data": [...]}`` envelope (``_parse_hits``, lines
  243-254). ANY failure — executable missing, non-zero exit, bad JSON,
  timeout — returns None and the hook falls to the reminder-only floor
  (lines 330-334, 404-408); it never blocks or raises.

Determinism note: the CLI rung queries in keyword mode, which is strict
case-insensitive substring matching over title+body (src/exomem/find.py line
457), so the firing probe prompt must literally appear in a corpus page — a
corpus source TITLE satisfies both the substring requirement and the nudge
gate (>= 20 chars, non-control).

REST rung: intentionally NOT exercised here (loopback availability in the
test sandbox is uncertain); see ``rest_rung_stub`` for the exact manual
command.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from membench.trackc.hook_home import (
    SRC_DIR,
    HookHome,
    ensure_isolated,
    make_workdir,
)
from membench.trackc.nudge_driver import HOOK_TIMEOUT_SECONDS, parse_fired

#: Inject-block header, mirrored from exomem_retrieve_nudge.py line 75.
STUB_HEADER = "KB routing stubs"
#: Reminder prefix, mirrored from lines 60-61.
REMINDER_PREFIX = "[Exomem retrieval check]"

T00_TEMPLATE = "t00_mini_smoke"


@dataclass
class SeededVault:
    """An isolated vault ingested from a deterministic t00 corpus."""

    workdir: Path
    vault: Path
    env: dict[str, str]  # the exact env the vault was built under
    probe_prompt: str  # a source title that passes the nudge gate
    probe_token: str  # distinctive lowercase corpus token for citation asserts
    ingested: int = 0


@dataclass
class InjectionResult:
    fired: bool
    injected: bool  # stub block present (beyond the reminder floor)
    context: str
    raw_stdout: str
    cited_corpus: bool
    result_kind: str = ""  # "injected" | "reminder_floor" | "quiet"
    detail: dict = field(default_factory=dict)


def _gate_passes(prompt: str) -> bool:
    """Check the probe prompt against the hook's OWN gate predicates."""
    from exomem._hooks import exomem_retrieve_nudge as nudge

    if len(prompt.strip()) < 20:  # min-chars default, lines 373, 378
        return False
    return not nudge._is_obvious_control_prompt(prompt, 180)  # lines 178-191


def _distinctive_token(title: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]+", title) if len(w) >= 6]
    if not words:
        raise AssertionError(f"no distinctive token in corpus title {title!r}")
    return max(words, key=len).lower()


def build_seeded_vault(workdir: Path | None = None, *, seed: int = 1) -> SeededVault:
    """Generate a t00 corpus, render native capture ops, ingest via the leaf
    adapter into an isolated vault, and return the env to reach that vault.

    Reuses ExomemLocalAdapter's leaf setup (benchmarks/membench/adapters/
    exomem_local.py lines 96-127) so the vault matches Track A/B provider
    state; the returned ``env`` mirrors exactly the values setup() exported
    (vault/config/lease/log paths + lexical_profile() settings).
    """
    from membench.adapters.exomem_local import ExomemLocalAdapter, lexical_profile
    from membench.generate import generate_corpus
    from membench.native import exomem_kb, load_corpus_view

    if workdir is None:
        workdir = make_workdir("inject-vault")
    corpus_dir = workdir / "corpus" / f"s{seed}"
    generate_corpus(seed, corpus_dir, template_ids=[T00_TEMPLATE])
    view = load_corpus_view(corpus_dir)
    native_dir = workdir / "native"
    exomem_kb.render(view, native_dir)

    provider_dir = workdir / "provider"
    profile = lexical_profile()
    adapter = ExomemLocalAdapter(mode="leaf")
    adapter.setup(provider_dir, profile)
    try:
        results = adapter.ingest(corpus_dir, native_dir)
        failures = [r.detail for r in results if not r.ok]
        if failures:
            raise RuntimeError(f"corpus ingest failed: {failures[:3]}")
        ingested = len(results)
    finally:
        adapter.cleanup()  # restores os.environ; the vault files remain

    vault = provider_dir / "vault"
    env = {
        # Mirror of ExomemLocalAdapter.setup()'s exported env (lines 104-112)
        # — deterministic reconstruction, not os.environ scraping.
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_CONFIG_PATH": str(provider_dir / "exomem-config.json"),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(provider_dir / "leases"),
        "EXOMEM_LOG_DIR": str(provider_dir / "logs"),
        **profile.settings,
    }

    probe = next((s.title for s in view.sources if _gate_passes(s.title)), None)
    if probe is None:
        raise RuntimeError("no t00 source title passes the nudge gate premises")
    return SeededVault(
        workdir=workdir,
        vault=vault,
        env=env,
        probe_prompt=probe,
        probe_token=_distinctive_token(probe),
        ingested=ingested,
    )


def make_exomem_shim(bin_dir: Path) -> Path:
    """An ``exomem`` executable for the hook's PATH lookup (line 295) that runs
    THIS worktree's code in the test venv. Vault/profile env is inherited from
    the hook process (``_fetch_via_cli`` passes no explicit env, line 299)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "exomem"
    shim.write_text(
        "#!/bin/sh\n"
        f'export PYTHONPATH="{SRC_DIR}"\n'
        f'exec "{sys.executable}" -m exomem "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def run_injection(
    home: HookHome,
    seeded: SeededVault,
    *,
    state_home: Path,
    break_cli: bool = False,
    prompt: str | None = None,
) -> InjectionResult:
    """Fire the installed retrieve-nudge hook in inject mode.

    ``break_cli=True`` degrades the CLI rung the way the hook actually
    resolves it: the shim directory is left OFF PATH so
    ``shutil.which("exomem"/"kb")`` (line 295) finds nothing and the ladder
    falls to the reminder-only floor (lines 330-334). (There is no
    EXOMEM_COMMAND override in the shipped hook — resolution is PATH-based.)
    """
    state_home = Path(state_home)
    state_home.mkdir(parents=True, exist_ok=True)
    shim_dir = seeded.workdir / "bin"
    make_exomem_shim(shim_dir)
    path = "/usr/bin:/bin" if break_cli else f"{shim_dir}:/usr/bin:/bin"
    env = home.base_env(
        EXOMEM_HOOK_HOME=str(state_home),
        PATH=path,
        # Opt-in inject mode + CLI rung (truthy-parsed flags, lines 113-133).
        EXOMEM_RETRIEVE_INJECT="1",
        EXOMEM_RETRIEVE_INJECT_CLI="1",
        **seeded.env,
    )
    env.pop("EXOMEM_REST_API_KEY", None)  # REST rung stays un-attempted (line 325)
    ensure_isolated(env)
    probe = prompt if prompt is not None else seeded.probe_prompt
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": f"inject-{uuid.uuid4().hex[:8]}",
        "prompt": probe,
    }
    proc = subprocess.run(
        ["bash", "-c", home.wired_command("UserPromptSubmit")],
        input=json.dumps(event),
        env=env,
        capture_output=True,
        text=True,
        timeout=HOOK_TIMEOUT_SECONDS,
    )
    fired = parse_fired(proc.stdout)
    context = ""
    if fired:
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    injected = STUB_HEADER in context
    stub_block = context.split(STUB_HEADER, 1)[1] if injected else ""
    cited = injected and seeded.probe_token in stub_block.lower()
    kind = "injected" if injected else ("reminder_floor" if fired else "quiet")
    return InjectionResult(
        fired=fired,
        injected=injected,
        context=context,
        raw_stdout=proc.stdout,
        cited_corpus=cited,
        result_kind=kind,
        detail={
            "prompt": probe,
            "probe_token": seeded.probe_token,
            "break_cli": break_cli,
            "path": path,
        },
    )


def rest_rung_stub() -> dict:
    """Documented stub for the REST rung (NOT run here: loopback availability
    in this sandbox is uncertain, and the hook only attempts REST when
    EXOMEM_REST_API_KEY is set — lines 257-286, 325-329).

    To exercise it manually on a workstation with the local REST facade
    running, execute the returned ``user_command`` (single line): it starts
    from the same seeded-vault env, sets the API key, and submits the probe
    event to the installed hook; REST reachability then short-circuits the
    CLI rung entirely (line 328: a reachable REST — even with 0 hits — means
    CLI is never tried).
    """
    return {
        "status": "not_run",
        "reason": "REST rung requires a loopback HTTP facade; not assumed in this sandbox",
        "user_command": (
            "EXOMEM_REST_API_KEY=<key> EXOMEM_HOST=127.0.0.1 EXOMEM_RETRIEVE_INJECT=1 "
            "EXOMEM_HOOK_HOME=<isolated-home> bash -c 'printf %s "
            "\"{\\\"session_id\\\": \\\"rest-probe\\\", \\\"prompt\\\": "
            "\\\"<corpus source title>\\\"}\" | bash \"<isolated-home>/hooks/"
            "exomem-retrieve-nudge.sh\"'"
        ),
        "contract": (
            "REST POST http://$EXOMEM_HOST:8765/api/ask_memory with "
            '{"query": <prompt>, "detail": "compact", "limit": 3, "mode": "keyword"} '
            "and Authorization: Bearer $EXOMEM_REST_API_KEY (lines 263-276); any "
            "non-200/timeout/malformed response falls through (lines 277-286)"
        ),
    }
