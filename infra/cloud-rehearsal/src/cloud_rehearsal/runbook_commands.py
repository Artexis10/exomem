"""Run the platform runbook's own commands against the rehearsal, byte for byte.

automate-cloud-releases task 1.13 verifies that each runbook command runs as written. The release mode reads the
fenced bash blocks out of the runbook with a CommonMark parser, runs each with `bash -o pipefail -c` and only the
variables the runbook tells an operator to load, and records what each printed. A command that needs another
namespace, label or variable than the rehearsal has is a runbook finding: the step fails and names it, and the
command is never adapted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from . import images
from .http import Resolver
from .report import StepFailure
from .shell import run

RUNBOOK = images.REPO_ROOT / "docs/runbooks/cloud-platform-deploy.md"
PAUSED_ROLLOUT = "Read and clear a paused rollout"
FORWARD_ONLY_HOLD = "End a forward-only hold"
ROTATE_KEY = "Rotate a key under upgrade windows"
# The `###` headings whose commands the release mode runs, and how many bash blocks each shows. A renamed heading
# or an added or dropped block is a runbook change the rehearsal must be told about, so the step fails on it.
SECTIONS = {PAUSED_ROLLOUT: 4, FORWARD_ONLY_HOLD: 2, ROTATE_KEY: 1}
BLOCK_TIMEOUT_SECONDS = 120
# What an operator reads off a block: the head and tail of its output, each line cut, so a long log stays a record.
KEPT_LINES = 5
LINE_WIDTH = 300
STDERR_WIDTH = 500


def runbook_blocks(text: str, sections: Mapping[str, int] = SECTIONS) -> dict[str, list[str]]:
    """The bash blocks under each named `###` heading, in document order.

    A `###`, `##` or `#` heading ends the section; a deeper heading stays inside it, and a block nested in a list
    item belongs to the heading above it. A missing heading, a repeated heading or a block count that differs
    from `sections` fails by name.
    """

    found: dict[str, list[str]] = {}
    current: str | None = None
    tokens = MarkdownIt("commonmark").parse(text)
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and int(token.tag.removeprefix("h")) <= 3:
            title = tokens[index + 1].content
            current = title if token.tag == "h3" and title in sections else None
            if current is not None:
                if current in found:
                    raise StepFailure(f"the runbook repeats the heading {current!r}")
                found[current] = []
        elif token.type == "fence" and current is not None and token.info.split()[:1] == ["bash"]:
            found[current].append(token.content)
    missing = [heading for heading in sections if heading not in found]
    if missing:
        raise StepFailure(f"the runbook has no `###` heading {missing}")
    wrong = {heading: len(blocks) for heading, blocks in found.items() if len(blocks) != sections[heading]}
    if wrong:
        expected = {heading: sections[heading] for heading in wrong}
        raise StepFailure(f"the runbook's bash blocks per heading are {wrong}, not {expected}")
    return found


def redact(text: str, secrets: Iterable[str]) -> str:
    """The text with every known secret and every bearer credential replaced, so that the report holds no credential."""

    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    # A credential's format, not a meaning: it catches a bearer value that no secret in `secrets` names.
    return re.sub(r"(?i)\b(bearer)\s+\S+", r"\1 [redacted]", text)


def bounded(text: str) -> str:
    lines = [line[:LINE_WIDTH] for line in text.splitlines()]
    if len(lines) > 2 * KEPT_LINES:
        lines = [*lines[:KEPT_LINES], f"... {len(lines) - 2 * KEPT_LINES} lines omitted ...", *lines[-KEPT_LINES:]]
    return "\n".join(lines)


@dataclass
class BlockRun:
    heading: str
    block: int
    exit_code: int
    stdout: str
    stderr: str

    def evidence(self, secrets: Iterable[str]) -> dict[str, Any]:
        return {
            "heading": self.heading, "block": self.block, "exit_code": self.exit_code,
            "stdout_lines": len(self.stdout.splitlines()),
            "stdout": redact(bounded(self.stdout), secrets),
            "stderr": redact(self.stderr.strip()[-STDERR_WIDTH:], secrets),
        }


@dataclass
class RunbookRunner:
    """Runs the runbook's blocks with the variables an operator loads, and nothing else from this process.

    `home` is the shell's HOME and curl's config directory: its `.curlrc` points curl at the run's CA and local
    port for the Substrate name, so the command runs unedited against a cluster with no public DNS.
    """

    blocks: dict[str, list[str]]
    kubeconfig: Path
    home: Path
    path: str
    secrets: tuple[str, ...]
    runs: list[BlockRun] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)

    def configure_curl(self, resolver: Resolver, host: str) -> None:
        """Point curl at the run's CA and at the local address that the resolver gives `host`."""

        address, port = resolver.routes[(host, 443)]
        # --connect-to moves the connection to the local port, while the certificate is still checked for `host`.
        (self.home / ".curlrc").write_text(
            f'cacert = "{resolver.ca_path}"\nconnect-to = "{host}:443:{address}:{port}"\n', encoding="utf-8")

    def environment(self, variables: Mapping[str, str]) -> dict[str, str]:
        return {**variables, "KUBECONFIG": str(self.kubeconfig), "PATH": self.path, "HOME": str(self.home),
                "CURL_HOME": str(self.home)}

    def run(self, heading: str, block: int, variables: Mapping[str, str]) -> BlockRun:
        """Block `block` (counted from 1) under `heading`, run as written."""

        script = self.blocks[heading][block - 1]
        result = run(["bash", "-o", "pipefail", "-c", script], cwd=self.home, env=self.environment(variables),
                     check=False, timeout=BLOCK_TIMEOUT_SECONDS)
        ran = BlockRun(heading, block, result.returncode, result.stdout, result.stderr)
        self.runs.append(ran)
        return ran
