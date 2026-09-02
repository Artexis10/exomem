"""Red-first tests for the closed benchmarks/suites/ LOCKFILE-or-GAP registry.

Mirrors `lme/providers/registry.py`'s closed-set idiom (unknown name raises;
`registered_provider_names()`) and `memorybench/setup.py`'s required-key and
digest validation, applied to the suite-level LOCKFILE.json schema instead of
a single pinned checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path

import pytest
from suites.registry import (
    SuiteRegistryError,
    registered_suite_names,
    suite_lockfile,
    validate_all,
)


def _write_lockfile(directory: Path, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "LOCKFILE.json").write_text(json.dumps(data), encoding="utf-8")


def _minimal_runnable(name: str, **overrides: object) -> dict:
    base = {
        "suite": name,
        "paper": "arXiv:0000.00000",
        "repo_url": f"https://example.invalid/{name}",
        "commit_sha": "a" * 40,
        "upstream_last_commit": "2026-01-01",
        "license_spdx": "MIT",
        "checkout_env_var": f"SUITE_{name.upper().replace('-', '_')}_HOME",
        "runability": "runnable",
        "verified_at_utc": "2026-01-01",
        "notes": "synthetic fixture",
    }
    base.update(overrides)
    return base


def test_the_five_pinned_suite_lockfiles_validate_unchanged() -> None:
    """I1: the real benchmarks/suites/ tree, untouched, must validate clean.

    Four suites (stale, memops, memoryagentbench, oida-corpora) predate this
    lane; lme_v1 is the fifth, added by this lane under D3'.
    """

    entries = validate_all()

    assert set(entries) == {"stale", "memops", "memoryagentbench", "oida-corpora", "lme_v1"}
    assert registered_suite_names() == (
        "lme_v1",
        "memops",
        "memoryagentbench",
        "oida-corpora",
        "stale",
    )


def test_a_suite_directory_without_a_lockfile_fails_validate_all(tmp_path: Path) -> None:
    (tmp_path / "orphan-suite").mkdir()

    with pytest.raises(SuiteRegistryError, match="orphan-suite"):
        validate_all(root=tmp_path)


def test_gap_without_gap_reason_is_refused_and_with_reason_is_valid(tmp_path: Path) -> None:
    directory = tmp_path / "gap-suite"
    lockfile = _minimal_runnable("gap-suite", runability="gap")
    del lockfile["commit_sha"]
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="gap_reason"):
        validate_all(root=tmp_path)

    lockfile["gap_reason"] = "upstream repository is unreleased"
    _write_lockfile(directory, lockfile)

    entries = validate_all(root=tmp_path)
    assert entries["gap-suite"]["gap_reason"] == "upstream repository is unreleased"


def test_gap_forbids_a_commit_sha_based_runnability_claim(tmp_path: Path) -> None:
    directory = tmp_path / "gap-suite"
    lockfile = _minimal_runnable("gap-suite", runability="gap")
    lockfile["gap_reason"] = "upstream repository is unreleased"
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="commit_sha"):
        validate_all(root=tmp_path)


def test_a_missing_required_key_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "incomplete-suite"
    lockfile = _minimal_runnable("incomplete-suite")
    del lockfile["paper"]
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="paper"):
        validate_all(root=tmp_path)


def test_an_unknown_key_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "extra-key-suite"
    lockfile = _minimal_runnable("extra-key-suite")
    lockfile["unexpected_field"] = "surprise"
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="unexpected_field"):
        validate_all(root=tmp_path)


def test_a_malformed_license_digest_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "bad-digest-suite"
    lockfile = _minimal_runnable("bad-digest-suite", license_sha256="not-a-digest")
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="license_sha256"):
        validate_all(root=tmp_path)


def test_a_corrupted_commit_sha_fails_validation_naming_the_suite_and_key(
    tmp_path: Path,
) -> None:
    """H1: commit_sha is digest-checked, not just license_sha256."""

    directory = tmp_path / "bad-commit-suite"
    lockfile = _minimal_runnable("bad-commit-suite", commit_sha="deadbeef")
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="bad-commit-suite.*commit_sha"):
        validate_all(root=tmp_path)


def test_every_real_suite_commit_sha_that_is_present_is_a_forty_hex_digest() -> None:
    """H1: the digest check actually holds for all five pinned lockfiles."""

    entries = validate_all()
    for name, data in entries.items():
        commit_sha = data.get("commit_sha")
        if commit_sha is not None:
            assert re.fullmatch(r"[0-9a-f]{40}", commit_sha), name


def test_a_non_hashable_runability_fails_closed_as_a_registry_error(tmp_path: Path) -> None:
    """F1: a non-hashable runability must not escape as a bare TypeError."""

    directory = tmp_path / "weird-type-suite"
    lockfile = _minimal_runnable("weird-type-suite", runability=["runnable"])
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="runability"):
        validate_all(root=tmp_path)


@pytest.mark.parametrize("key,value", [("notes", None), ("repo_url", "")])
def test_a_null_or_empty_required_string_key_fails_validation(
    tmp_path: Path, key: str, value: object
) -> None:
    """F2: required string keys must be non-empty strings, not null or ''."""

    directory = tmp_path / "blank-key-suite"
    lockfile = _minimal_runnable("blank-key-suite", **{key: value})
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match=key):
        validate_all(root=tmp_path)


def test_an_explicit_null_evaluate_qa_is_refused_for_any_suite_not_only_lme_v1(
    tmp_path: Path,
) -> None:
    """NEW-5: `"evaluate_qa": null` must fail type validation for every suite,
    not only the suites _EVALUATE_QA_REQUIRED_SUITES happens to name."""

    directory = tmp_path / "other-suite"
    lockfile = _minimal_runnable("other-suite", evaluate_qa=None)
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="other-suite.*must be a JSON object"):
        validate_all(root=tmp_path)


def test_a_suite_field_mismatched_with_its_directory_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "renamed-suite"
    lockfile = _minimal_runnable("original-name")
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="renamed-suite"):
        validate_all(root=tmp_path)


def test_an_unrecognized_runability_value_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "weird-runability-suite"
    lockfile = _minimal_runnable("weird-runability-suite", runability="totally-fine-trust-me")
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="totally-fine-trust-me"):
        validate_all(root=tmp_path)


def test_gap_reason_on_a_non_gap_entry_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "confused-suite"
    lockfile = _minimal_runnable("confused-suite", gap_reason="should not be here")
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="gap_reason"):
        validate_all(root=tmp_path)


def test_a_non_gap_entry_without_commit_sha_fails_validation(tmp_path: Path) -> None:
    directory = tmp_path / "unpinned-suite"
    lockfile = _minimal_runnable("unpinned-suite")
    del lockfile["commit_sha"]
    _write_lockfile(directory, lockfile)

    with pytest.raises(SuiteRegistryError, match="commit_sha"):
        validate_all(root=tmp_path)


def test_unknown_suite_name_is_refused_by_the_closed_registry() -> None:
    with pytest.raises(SuiteRegistryError, match="unknown suite"):
        suite_lockfile("not-a-real-suite")


def test_suite_lockfile_returns_the_validated_entry_for_a_registered_suite() -> None:
    data = suite_lockfile("oida-corpora")

    assert data["suite"] == "oida-corpora"
    assert data["runability"] == "runnable"


def test_lme_v1_lockfile_pins_a_positional_evaluate_qa_interface_with_no_flags() -> None:
    """R4 (part 1): the lockfile itself records what the pinned script actually is."""

    lockfile = suite_lockfile("lme_v1")
    evaluate_qa = lockfile["evaluate_qa"]

    assert lockfile["runability"] == "runnable-with-cost"
    assert evaluate_qa["invocation"] == "positional"
    assert evaluate_qa["arity"] == 3
    assert evaluate_qa["argv_template"] == ["<metric_model_short>", "<hyp_file>", "<ref_file>"]
    assert "flags" not in evaluate_qa
    assert "--" not in json.dumps(evaluate_qa)


def test_lme_v1_lockfile_records_no_log_derivation() -> None:
    """H4: the pinned script writes no .log file; the README claim is wrong upstream."""

    evaluate_qa = suite_lockfile("lme_v1")["evaluate_qa"]

    assert "log_derivation" not in evaluate_qa


def _lme_v1_with_evaluate_qa(evaluate_qa: object, tmp_path: Path) -> Path:
    lockfile = dict(suite_lockfile("lme_v1"))
    if evaluate_qa is _MISSING:
        del lockfile["evaluate_qa"]
    else:
        lockfile["evaluate_qa"] = evaluate_qa
    directory = tmp_path / "lme_v1"
    _write_lockfile(directory, lockfile)
    return tmp_path


_MISSING = object()


_VALID_EVALUATE_QA_BASE = {
    "path": "src/evaluation/evaluate_qa.py",
    "sha256": "e" * 64,
    "invocation": "positional",
    "arity": 3,
    "argv_template": ["<metric_model_short>", "<hyp_file>", "<ref_file>"],
    "result_file_derivation": "<hyp_file>.eval-results-<metric_model_short>",
    "readme_example": "python3 evaluate_qa.py gpt-4o hyp ref",
}


@pytest.mark.parametrize(
    "evaluate_qa,match",
    [
        pytest.param(
            {
                "path": "src/evaluation/evaluate_qa.py",
                "sha256": "e" * 64,
                "invocation": "positional",
                "arity": 3,
                "argv_template": ["<metric_model_short>", "<hyp_file>", "<ref_file>"],
                "result_file_derivation": "<hyp_file>.eval-results-<metric_model_short>",
                "readme_example": "python3 evaluate_qa.py gpt-4o hyp ref",
                "flags": ["--dataset_file"],
            },
            "unknown keys",
            id="flags-key",
        ),
        pytest.param(
            {
                "path": "src/evaluation/evaluate_qa.py",
                "sha256": "e" * 64,
                "invocation": "positional",
                "arity": 4,
                "argv_template": ["<a>", "<b>", "<c>", "<d>"],
                "result_file_derivation": "<hyp_file>.eval-results-<metric_model_short>",
                "readme_example": "python3 evaluate_qa.py gpt-4o hyp ref",
            },
            "arity",
            id="arity-4",
        ),
        pytest.param("positional, three args", "must be a JSON object", id="bare-string"),
        # NEW-5: branching on key presence means an explicit null now reaches
        # the same type check as bare-string, not the required-suites check.
        pytest.param(None, "must be a JSON object", id="null"),
        pytest.param({}, "missing", id="empty-object"),
        # NEW-1: evaluate_qa.path must stay a safe relative POSIX path.
        pytest.param(
            {**_VALID_EVALUATE_QA_BASE, "path": "/abs/evaluate_qa.py"},
            "not a safe relative path",
            id="absolute-path",
        ),
        pytest.param(
            {**_VALID_EVALUATE_QA_BASE, "path": "../../../etc/passwd"},
            "not a safe relative path",
            id="parent-traversal",
        ),
        pytest.param(
            {**_VALID_EVALUATE_QA_BASE, "path": "src/../../escape.py"},
            "not a safe relative path",
            id="traversal-via-relative-segments",
        ),
    ],
)
def test_evaluate_qa_rejects_every_malformed_shape_naming_the_suite(
    tmp_path: Path, evaluate_qa: object, match: str
) -> None:
    """H3: deep-validate evaluate_qa; each of five malformed shapes is refused."""

    root = _lme_v1_with_evaluate_qa(evaluate_qa, tmp_path)

    with pytest.raises(SuiteRegistryError, match=match) as exc_info:
        validate_all(root=root)
    assert "lme_v1" in str(exc_info.value)


def test_deleting_evaluate_qa_from_lme_v1_fails_validation_not_a_keyerror(
    tmp_path: Path,
) -> None:
    """H3: a missing evaluate_qa block on lme_v1 fails validate_all, never a
    bare KeyError surfacing later at render time."""

    root = _lme_v1_with_evaluate_qa(_MISSING, tmp_path)

    with pytest.raises(SuiteRegistryError, match="lme_v1.*evaluate_qa"):
        validate_all(root=root)


def test_official_judge_commands_use_exactly_three_positional_arguments_and_no_flags(
    tmp_path: Path,
) -> None:
    """R4 (part 2): the emitted command has no flags and no UNVERIFIED marker."""

    from lme.judge_io import LANE_FILES, official_judge_commands, verified_judge_banner

    text = official_judge_commands(tmp_path, judge_model="gpt-4o")

    assert "UNVERIFIED" not in text
    assert "--" not in text
    assert text.splitlines()[0] == f"# {verified_judge_banner()}"

    lockfile = suite_lockfile("lme_v1")
    evaluate_qa = lockfile["evaluate_qa"]
    expected_script = f"${lockfile['checkout_env_var']}/{evaluate_qa['path']}"

    command_lines = [line for line in text.splitlines() if line.startswith("python3 ")]
    assert len(command_lines) == len(LANE_FILES)
    for line in command_lines:
        tokens = shlex.split(line)
        # H2: the script path comes from the lockfile, never a fourth typed
        # copy -- if the lockfile's path or checkout_env_var drifts, this
        # must drift with it rather than silently keep passing.
        assert tokens[1] == expected_script
        # Exactly three positional arguments follow the interpreter and script.
        assert len(tokens) - 2 == 3


def test_official_judge_commands_script_is_double_quoted_not_shlex_quoted(
    tmp_path: Path,
) -> None:
    """NEW-2 (part 1): a raw-text check on the emitted line.

    shlex.split() strips quote characters regardless of style, so a
    token-comparison test alone cannot tell single-quoted
    (`shlex.quote()`, which would suppress $VAR expansion) from
    double-quoted (which preserves it) apart -- both parse to the identical
    token string. Reverting `script` to `shlex.quote(...)` must fail this
    assertion even though it would still pass the token-comparison test
    above.
    """

    from lme.judge_io import official_judge_commands

    lockfile = suite_lockfile("lme_v1")
    text = official_judge_commands(tmp_path, judge_model="gpt-4o")

    expected_open = f'"${lockfile["checkout_env_var"]}/'
    assert expected_open in text


def test_the_double_quoted_script_path_stays_one_argv_element_with_a_space_in_checkout(
    tmp_path: Path,
) -> None:
    """NEW-2 (part 2): a real bash expansion check, not just shlex.

    The double-quoted script path must stay one argv element even when the
    checkout directory itself contains a space -- the property double
    quoting (over leaving the expression bare) actually exists to protect.
    """

    import subprocess

    from lme.judge_io import official_judge_commands

    lockfile = suite_lockfile("lme_v1")
    env_var = lockfile["checkout_env_var"]

    text = official_judge_commands(tmp_path, judge_model="gpt-4o")
    command_line = next(line for line in text.splitlines() if line.startswith("python3 "))

    checkout_with_space = "/tmp/a directory/checkout"
    bash_script = (
        "set -u\n"
        f"{env_var}={shlex.quote(checkout_with_space)}\n"
        'python3() { echo "$#"; }\n'
        f"{command_line}\n"
    )
    result = subprocess.run(
        ["bash", "-c", bash_script], capture_output=True, text=True, check=True
    )
    # 4 argv elements: script path (one, despite the embedded space),
    # judge_model, hypothesis path, dataset path.
    assert result.stdout.strip() == "4"


def test_official_judge_commands_result_comment_matches_the_lockfile_template(
    tmp_path: Path,
) -> None:
    """R4 (part 3): the derivation comment equals the lockfile template, substituted."""

    from lme.judge_io import LANE_FILES, _substitute_template, official_judge_commands

    run_dir = tmp_path.resolve()
    text = official_judge_commands(run_dir, judge_model="gpt-4o")
    result_template = suite_lockfile("lme_v1")["evaluate_qa"]["result_file_derivation"]

    comment_lines = [line for line in text.splitlines() if line.startswith("# writes results to ")]
    assert len(comment_lines) == len(LANE_FILES)
    for (input_name, _output_name), comment_line in zip(
        LANE_FILES.values(), comment_lines, strict=True
    ):
        hyp_path = str(run_dir / input_name)
        expected = _substitute_template(
            result_template, hyp_file=hyp_path, metric_model_short="gpt-4o"
        )
        assert comment_line == f"# writes results to {shlex.quote(expected)}"


def test_report_banner_renders_the_same_verified_sentence_as_judge_io() -> None:
    """R4 (part 4): report.py's banner has no UNVERIFIED marker and matches judge_io."""

    from lme.dataset import load_dataset
    from lme.judge_io import verified_judge_banner
    from lme.report import render_report

    dataset = load_dataset(Path("benchmarks/lme/fixtures/mini.json"))
    report = render_report(
        dataset, labels={}, ceiling_question_ids=set(), floor_question_ids=set()
    )

    assert "UNVERIFIED" not in report
    assert f"> {verified_judge_banner()}" in report


def _lme_v1_checkout_env_var_for_skip() -> str | None:
    """Best-effort env var name for the skip condition only.

    Must never raise at collection time: a malformed real lme_v1 lockfile
    should fail this module's own tests (several of which validate it
    properly and report the exact defect), not abort collection of the
    whole module -- or, worse, the whole pytest session. The test body
    below still re-derives everything from the lockfile for its real
    assertions; this helper exists only to decide whether to skip.
    """

    try:
        return suite_lockfile("lme_v1")["checkout_env_var"]
    except SuiteRegistryError:
        return None


_LME_V1_CHECKOUT_ENV_VAR = _lme_v1_checkout_env_var_for_skip()


@pytest.mark.skipif(
    _LME_V1_CHECKOUT_ENV_VAR is None or _LME_V1_CHECKOUT_ENV_VAR not in os.environ,
    reason=(
        f"set {_LME_V1_CHECKOUT_ENV_VAR} to the pinned LongMemEval checkout to run this"
        if _LME_V1_CHECKOUT_ENV_VAR is not None
        else "lme_v1's LOCKFILE.json does not validate; see the module's other tests"
    ),
)
def test_the_pinned_evaluate_qa_script_matches_the_lockfile_and_stays_positional() -> None:
    """R5 (env-gated): the real checkout at the pin still hashes to the lockfile."""

    lockfile = suite_lockfile("lme_v1")
    evaluate_qa = lockfile["evaluate_qa"]
    checkout = Path(os.environ[lockfile["checkout_env_var"]])
    script = checkout / evaluate_qa["path"]

    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    assert digest == evaluate_qa["sha256"]

    text = script.read_text(encoding="utf-8")
    for token in (
        "len(sys.argv) != 4",
        "sys.argv[1]",
        "sys.argv[2]",
        "sys.argv[3]",
        ".eval-results-",
    ):
        assert token in text
