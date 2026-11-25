"""The release mode runs the platform runbook's commands as written, so it must find exactly those commands."""

from __future__ import annotations

import pytest

from cloud_rehearsal.report import StepFailure
from cloud_rehearsal.runbook_commands import (
    FORWARD_ONLY_HOLD,
    PAUSED_ROLLOUT,
    ROTATE_KEY,
    RUNBOOK,
    RunbookRunner,
    runbook_blocks,
)


def test_the_shipped_runbook_yields_the_blocks_the_release_mode_runs() -> None:
    # A runbook edit that renames a heading, or adds, drops or reorders a block, would otherwise fail a cluster run
    # late, or run the wrong command against the cluster.
    blocks = runbook_blocks(RUNBOOK.read_text())
    assert {heading: len(found) for heading, found in blocks.items()} == {
        PAUSED_ROLLOUT: 4, FORWARD_ONLY_HOLD: 2, ROTATE_KEY: 1}
    read, grep, fix_forward, clear = blocks[PAUSED_ROLLOUT]
    assert "/cloud-release/rollout" in read and read.endswith("| jq .view\n")
    assert "grep -E 'cloud-rollout-paused" in grep
    assert "{cellImage: $image, release: $release}" in fix_forward
    assert "--data '{\"clearRolloutPause\":true}'" in clear
    # These two sit inside a list item: the parser takes the fence out of its indentation and keeps the shell intact.
    own_image, clear_own_image = blocks[FORWARD_ONLY_HOLD]
    assert own_image.startswith("jq -n --arg cell") and "cellDesiredImage: $image}" in own_image
    assert "-H @<(printf 'Authorization: Bearer %s\\n' \"$EXOMEM_ADMIN_TOKEN\")" in own_image
    assert clear_own_image.startswith("jq -n --arg cell") and "cellDesiredImage: null}" in clear_own_image
    assert blocks[ROTATE_KEY][0].startswith("kubectl get pods -A -l app.kubernetes.io/name=exomem-cell")


def test_a_block_belongs_to_the_heading_above_it_until_the_next_h3_h2_or_h1_heading() -> None:
    text = """\
### First

Intro.

1. A step:

   ```bash
   echo nested
   ```

```bash
# ### Not a heading, only a shell comment
echo top
```

```text
echo not bash
```

#### A deeper heading stays inside the section

```bash
echo deeper
```

## A higher heading ends it

```bash
echo after
```

### Second

```bash
echo second
```
"""
    blocks = runbook_blocks(text, {"First": 3, "Second": 1})
    assert blocks == {"First": ["echo nested\n", "# ### Not a heading, only a shell comment\necho top\n", "echo deeper\n"],
                      "Second": ["echo second\n"]}


def test_a_missing_heading_or_a_different_block_count_fails_the_step_by_name() -> None:
    text = "### Present\n\n```bash\necho present\n```\n"
    with pytest.raises(StepFailure, match="no `###` heading .*Absent"):
        runbook_blocks(text, {"Present": 1, "Absent": 1})
    with pytest.raises(StepFailure, match=r"per heading are \{'Present': 1\}, not \{'Present': 2\}"):
        runbook_blocks(text, {"Present": 2})


def test_the_runbooks_shell_sees_only_the_loaded_variables_and_its_evidence_holds_no_token(monkeypatch, tmp_path) -> None:
    # An operator's own proxy, kubeconfig or token in this process must not decide whether a command works,
    # and the report is uploaded, so a token a command prints must not reach it.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("EXOMEM_ADMIN_TOKEN", "the-operators-own-token")
    block = 'printf "%s %s %s\\n" "${HTTPS_PROXY-unset}" "$EXOMEM_ADMIN_TOKEN" "Authorization: Bearer $FIXED_TAG"'
    runner = RunbookRunner({"h": [block]}, tmp_path / "kubeconfig", tmp_path / "home", "/usr/bin:/bin", ("loaded-token",))
    ran = runner.run("h", 1, {"EXOMEM_ADMIN_TOKEN": "loaded-token", "FIXED_TAG": "another-credential"})
    assert ran.stdout == "unset loaded-token Authorization: Bearer another-credential\n"
    assert ran.evidence(runner.secrets)["stdout"] == "unset [redacted] Authorization: Bearer [redacted]"
