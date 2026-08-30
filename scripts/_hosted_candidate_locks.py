"""Locate a release's generated Hosted plugin locks for one candidate profile.

`exomem.hosted_plugins` writes the default candidate's artifacts directly under
`plugins/hosted/generated`, and every later candidate under
`plugins/hosted/generated/candidates/<profile>`. The operator scripts read those
files out of `--repo`, so they need the same rule.

It is restated here rather than imported because `--repo` is routinely a
different checkout from the one the script runs out of -- the harness is run
from `main` while `--repo` points at a worktree of the released tag -- and an
import would resolve against whichever tree is on `sys.path`. Only the layout is
duplicated; every value still comes from the files under `--repo`.

Reading the wrong profile's locks is not a loud failure. The digests simply
belong to a different command surface, and the server-side joins that consume
them answer a bare 500 or a silent false, inside the promotion window. So
`read_lock` refuses a lock whose own `profile` field disagrees with the profile
that was asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The one candidate whose artifacts live at the generated root rather than
#: under `candidates/`. Mirrors `exomem.hosted_plugins.DEFAULT_CANDIDATE`.
DEFAULT_CANDIDATE_PROFILE = "hosted-alpha-agent-v1"


def candidate_generated_root(repo: Path, profile: str) -> Path:
    """Directory holding `<platform>.lock.json` for `profile` inside `repo`."""
    generated = repo / "plugins" / "hosted" / "generated"
    if profile == DEFAULT_CANDIDATE_PROFILE:
        return generated
    return generated / "candidates" / profile


def read_lock(repo: Path, profile: str, name: str) -> dict:
    """Read one lock file, refusing a profile the file itself disagrees with."""
    path = candidate_generated_root(repo, profile) / name
    if not path.is_file():
        raise SystemExit(
            f"{path} does not exist; {repo} does not carry generated artifacts for "
            f"profile {profile}. Point --repo at a worktree of the release the "
            f"candidate was cut from, and --profile at that candidate's profile."
        )
    lock = json.loads(path.read_text())
    # The `.zip.lock.json` files carry only an archive digest, so absence is fine;
    # a present-and-different profile is not.
    declared = lock.get("profile")
    if declared is not None and declared != profile:
        raise SystemExit(
            f"{path} declares profile {declared}, not the requested {profile}; "
            "refusing to promote one profile's candidate on another's digests."
        )
    return lock
