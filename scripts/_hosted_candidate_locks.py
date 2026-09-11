"""Locate a release's generated Hosted plugin locks for one candidate.

`exomem.hosted_plugins` writes the default candidate's artifacts directly under
`plugins/hosted/generated`, and every later candidate under
`plugins/hosted/generated/candidates/<candidate>`. The operator scripts read
those files out of `--repo`, so they need the same rule.

It is restated here rather than imported because `--repo` is routinely a
different checkout from the one the script runs out of -- the harness is run
from `main` while `--repo` points at a worktree of the released tag -- and an
import would resolve against whichever tree is on `sys.path`. Only the layout
and the candidate-to-profile map are duplicated; every digest still comes from
the files under `--repo`.

Reading the wrong candidate's locks is not a loud failure. The digests simply
belong to a different command surface, and the server-side joins that consume
them answer a bare 500 or a silent false, inside the promotion window. So
`read_lock` refuses a lock whose own `profile` field disagrees with the profile
of the candidate that was asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The one candidate whose artifacts live at the generated root rather than
#: under `candidates/`. Mirrors `exomem.hosted_plugins.DEFAULT_CANDIDATE`.
DEFAULT_CANDIDATE_PROFILE = "hosted-alpha-agent-v1"

#: Candidate name -> the agent profile its locks declare. Mirrors
#: `exomem.hosted_plugins.CANDIDATE_PROFILES`; `tests/test_reviewer_bootstrap_cli.py`
#: pins the two maps equal. A candidate is a generated directory, and more than
#: one candidate can carry the same profile -- the command-binding variant of v4
#: declares `hosted-alpha-agent-v4` -- so the directory name is not what a lock
#: declares and must not be compared against it.
CANDIDATE_PROFILES = {
    "hosted-alpha-agent-v1": "hosted-alpha-agent-v1",
    "hosted-alpha-agent-v2": "hosted-alpha-agent-v2",
    "hosted-alpha-agent-v3": "hosted-alpha-agent-v3",
    "hosted-alpha-agent-v4": "hosted-alpha-agent-v4",
    "hosted-alpha-agent-v5": "hosted-alpha-agent-v5",
    "hosted-alpha-agent-v4-command-binding-v1": "hosted-alpha-agent-v4",
}


def candidate_generated_root(repo: Path, candidate: str) -> Path:
    """Directory holding `<platform>.lock.json` for `candidate` inside `repo`."""
    generated = repo / "plugins" / "hosted" / "generated"
    if candidate == DEFAULT_CANDIDATE_PROFILE:
        return generated
    return generated / "candidates" / candidate


def read_lock(repo: Path, candidate: str, name: str) -> dict:
    """Read one lock file, refusing a profile the file itself disagrees with."""
    path = candidate_generated_root(repo, candidate) / name
    if not path.is_file():
        raise SystemExit(
            f"{path} does not exist; {repo} does not carry generated artifacts for "
            f"candidate {candidate}. Point --repo at a worktree of the release the "
            f"candidate was cut from, and --profile at that candidate."
        )
    lock = json.loads(path.read_text())
    # The `.zip.lock.json` files carry only an archive digest, so absence is fine;
    # a present-and-different profile is not.
    expected = CANDIDATE_PROFILES.get(candidate, candidate)
    declared = lock.get("profile")
    if declared is not None and declared != expected:
        raise SystemExit(
            f"{path} declares profile {declared}, not {expected} (the profile of "
            f"candidate {candidate}); refusing to promote one profile's candidate on "
            "another's digests."
        )
    return lock
