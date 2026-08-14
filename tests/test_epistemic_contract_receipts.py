from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "benchmarks/epistemic/PREREGISTRATION.md"
RECEIPT = ROOT / "benchmarks/epistemic/contracts/ratification.v1.json"
APPROVED_SHA256 = "21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3"
RATIFICATION_REVISION = "7cd15e6d6c67eb914e4f57bd943f98f7d1894b7f"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ratification_payload() -> dict[str, object]:
    return {
        "artifact_type": "preregistration-ratification-receipt.v1",
        "schema_version": 1,
        "sequence": 0,
        "contract_path": "benchmarks/epistemic/PREREGISTRATION.md",
        "contract_sha256": APPROVED_SHA256,
        "decision": "ratified",
        "ratifier": "Hugo Ander Kivi",
        "ratified_on": "2026-08-11",
        "repository_revision": RATIFICATION_REVISION,
        "amendment_contract": {
            "artifact_type": "preregistration-amendment-receipt.v1",
            "schema_version": 1,
            "ordering": "strict_contiguous_sequence",
            "parent_binding": "previous_whole_document_sha256",
            "applicability": "repository_ancestry_through_contract_revision",
        },
    }


def _amendment(sequence: int, parent: str, amended: bytes, revision: str) -> bytes:
    payload = {
        "artifact_type": "preregistration-amendment-receipt.v1",
        "schema_version": 1,
        "sequence": sequence,
        "contract_path": "benchmarks/epistemic/PREREGISTRATION.md",
        "parent_contract_sha256": parent,
        "contract_sha256": _sha(amended),
        "repository_revision": revision,
        "amended_on": "2026-08-12",
        "affected_sections": ["§6 Strategy decision gates"],
        "rationale": "A later policy clarification with no retroactive identity effect.",
        "effective_policy": "Applies to publication decisions at and after this revision.",
    }
    return (json.dumps(payload, sort_keys=True) + "\n").encode()


def test_approved_preregistration_bytes_remain_unchanged_and_receipt_is_exact() -> None:
    from protocol.contracts import RatificationReceipt

    assert _sha(PREREG.read_bytes()) == APPROVED_SHA256
    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert payload == _ratification_payload()
    assert RatificationReceipt.model_validate(payload).contract_sha256 == APPROVED_SHA256


def test_identity_is_derived_from_the_receipt_and_git_contract_bytes() -> None:
    from protocol.contracts import derive_preregistration_identity

    identity = derive_preregistration_identity(ROOT, contract_revision=RATIFICATION_REVISION)
    assert identity.contract_revision == RATIFICATION_REVISION
    assert identity.original.path == "benchmarks/epistemic/PREREGISTRATION.md"
    assert identity.original.sha256 == APPROVED_SHA256
    assert identity.original.repository_revision == RATIFICATION_REVISION
    assert identity.ratification.receipt_path.endswith("ratification.v1.json")
    assert identity.amendments == ()


def test_complete_ordered_amendment_chain_is_derived_at_the_pin() -> None:
    from protocol.contracts import derive_identity_from_receipt_bytes

    original = b"ratified bytes\n"
    amended_1 = original + b"amendment one\n"
    amended_2 = amended_1 + b"amendment two\n"
    ratification = _ratification_payload()
    ratification["contract_sha256"] = _sha(original)
    ratification["repository_revision"] = "a" * 40
    receipts = (
        ("ratification.v1.json", (json.dumps(ratification, sort_keys=True) + "\n").encode()),
        ("amendment-0001.v1.json", _amendment(1, _sha(original), amended_1, "c" * 40)),
        ("amendment-0002.v1.json", _amendment(2, _sha(amended_1), amended_2, "e" * 40)),
    )
    artifacts = {
        ("a" * 40, ratification["contract_path"]): original,
        (
            "b" * 40,
            "benchmarks/epistemic/contracts/ratification.v1.json",
        ): receipts[0][1],
        ("c" * 40, ratification["contract_path"]): amended_1,
        (
            "d" * 40,
            "benchmarks/epistemic/contracts/amendment-0001.v1.json",
        ): receipts[1][1],
        ("e" * 40, ratification["contract_path"]): amended_2,
        (
            "f" * 40,
            "benchmarks/epistemic/contracts/amendment-0002.v1.json",
        ): receipts[2][1],
        ("f" * 40, ratification["contract_path"]): amended_2,
    }
    introductions = {
        "benchmarks/epistemic/contracts/ratification.v1.json": ("b" * 40,),
        "benchmarks/epistemic/contracts/amendment-0001.v1.json": ("d" * 40,),
        "benchmarks/epistemic/contracts/amendment-0002.v1.json": ("f" * 40,),
    }

    identity = derive_identity_from_receipt_bytes(
        receipts,
        contract_revision="f" * 40,
        read_git_artifact=lambda revision, path: artifacts[(revision, path)],
        is_revision_applicable=lambda revision, pin: revision <= pin,
        receipt_introduction_revisions=lambda path, _pin: introductions[path],
    )

    assert [item.sequence for item in identity.amendments] == [1, 2]
    assert identity.effective.sha256 == _sha(amended_2)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda payload: payload.update(contract_sha256="f" * 64), "digest"),
        (lambda payload: payload.update(repository_revision="f" * 40), "repository"),
        (lambda payload: payload.update(ratifier="Someone Else"), "ratifier"),
    ],
)
def test_ratification_digest_revision_or_ratifier_substitution_is_refused(mutation, match) -> None:
    from protocol.contracts import ContractIdentityError, derive_identity_from_receipt_bytes

    original = b"ratified bytes\n"
    payload = _ratification_payload()
    payload["contract_sha256"] = _sha(original)
    payload["repository_revision"] = "a" * 40
    mutation(payload)
    with pytest.raises(ContractIdentityError, match=match):
        derive_identity_from_receipt_bytes(
            (("ratification.v1.json", (json.dumps(payload) + "\n").encode()),),
            contract_revision=str(payload["repository_revision"]),
            read_git_artifact=lambda _revision, _path: original,
            is_revision_applicable=lambda _revision, _pin: True,
            receipt_introduction_revisions=lambda _path, _pin: (),
            expected_ratifier="Hugo Ander Kivi",
            expected_repository_revision="a" * 40,
        )


@pytest.mark.parametrize(
    "receipts",
    [
        lambda rat, a1, a2: (rat, a2),
        lambda rat, a1, a2: (rat, a2, a1),
        lambda rat, a1, a2: (rat, a1, _NamedBytes("amendment-0001-copy.v1.json", a2.data)),
    ],
)
def test_incomplete_out_of_order_or_duplicate_amendment_chain_is_refused(receipts) -> None:
    from protocol.contracts import ContractIdentityError, derive_identity_from_receipt_bytes

    original, amended_1, amended_2 = b"v0", b"v1", b"v2"
    rat_payload = _ratification_payload()
    rat_payload["contract_sha256"] = _sha(original)
    rat_payload["repository_revision"] = "a" * 40
    rat = _NamedBytes("ratification.v1.json", (json.dumps(rat_payload) + "\n").encode())
    a1 = _NamedBytes("amendment-0001.v1.json", _amendment(1, _sha(original), amended_1, "b" * 40))
    a2 = _NamedBytes("amendment-0002.v1.json", _amendment(2, _sha(amended_1), amended_2, "c" * 40))
    artifacts = {"a" * 40: original, "b" * 40: amended_1, "c" * 40: amended_2}
    selected = receipts(rat, a1, a2)

    with pytest.raises(ContractIdentityError, match="ordered|sequence|duplicate"):
        derive_identity_from_receipt_bytes(
            tuple((item.name, item.data) for item in selected),
            contract_revision="a" * 40,
            read_git_artifact=lambda revision, _path: artifacts[revision],
            is_revision_applicable=lambda _revision, _pin: True,
            receipt_introduction_revisions=lambda path, _pin: ()
            if path.endswith("ratification.v1.json")
            else ("d" * 40,),
        )


class _NamedBytes:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self.data = data

def test_later_amendment_does_not_retroactively_invalidate_historical_identity() -> None:
    from protocol.contracts import derive_identity_from_receipt_bytes, validate_preregistration_identity

    original, amended = b"v0", b"v1"
    rat_payload = _ratification_payload()
    rat_payload["contract_sha256"] = _sha(original)
    rat_payload["repository_revision"] = "a" * 40
    rat = ("ratification.v1.json", (json.dumps(rat_payload) + "\n").encode())
    amendment = ("amendment-0001.v1.json", _amendment(1, _sha(original), amended, "c" * 40))
    artifacts = {
        ("a" * 40, rat_payload["contract_path"]): original,
        ("b" * 40, rat_payload["contract_path"]): original,
        (
            "b" * 40,
            "benchmarks/epistemic/contracts/ratification.v1.json",
        ): rat[1],
        ("c" * 40, rat_payload["contract_path"]): amended,
        (
            "d" * 40,
            "benchmarks/epistemic/contracts/amendment-0001.v1.json",
        ): amendment[1],
        ("d" * 40, rat_payload["contract_path"]): amended,
    }
    applicable = lambda revision, pin: revision <= pin
    introductions = {
        "benchmarks/epistemic/contracts/ratification.v1.json": ("b" * 40,),
        "benchmarks/epistemic/contracts/amendment-0001.v1.json": ("d" * 40,),
    }
    historical = derive_identity_from_receipt_bytes(
        (rat,), contract_revision="b" * 40,
        read_git_artifact=lambda revision, path: artifacts[(revision, path)],
        is_revision_applicable=applicable,
        receipt_introduction_revisions=lambda path, _pin: introductions[path],
    )
    current = derive_identity_from_receipt_bytes(
        (rat, amendment), contract_revision="d" * 40,
        read_git_artifact=lambda revision, path: artifacts[(revision, path)],
        is_revision_applicable=applicable,
        receipt_introduction_revisions=lambda path, _pin: introductions[path],
    )

    validate_preregistration_identity(
        historical,
        receipts=(rat,),
        read_git_artifact=lambda revision, path: artifacts[(revision, path)],
        is_revision_applicable=applicable,
        receipt_introduction_revisions=lambda path, _pin: introductions[path],
    )
    assert historical.amendments == ()
    assert current.amendments[0].sequence == 1


def test_receipt_visible_at_pin_with_nonancestor_bound_revision_is_refused() -> None:
    from protocol.contracts import ContractIdentityError, derive_identity_from_receipt_bytes

    original, amended = b"v0", b"v1"
    rat_payload = _ratification_payload()
    rat_payload["contract_sha256"] = _sha(original)
    rat_payload["repository_revision"] = "a" * 40
    receipts = (
        ("ratification.v1.json", (json.dumps(rat_payload) + "\n").encode()),
        ("amendment-0001.v1.json", _amendment(1, _sha(original), amended, "c" * 40)),
    )
    artifacts = {
        ("a" * 40, str(rat_payload["contract_path"])): original,
        ("b" * 40, str(rat_payload["contract_path"])): original,
        (
            "b" * 40,
            "benchmarks/epistemic/contracts/ratification.v1.json",
        ): receipts[0][1],
    }

    with pytest.raises(ContractIdentityError, match="ancestor|applicable|repository|pin"):
        derive_identity_from_receipt_bytes(
            receipts,
            contract_revision="b" * 40,
            read_git_artifact=lambda revision, path: artifacts[(revision, path)],
            is_revision_applicable=lambda revision, pin: (revision, pin)
            in {("a" * 40, "b" * 40)},
            receipt_introduction_revisions=lambda path, _pin: ("b" * 40,)
            if path.endswith("ratification.v1.json")
            else ("d" * 40,),
        )


def test_git_tree_pin_always_requires_exact_approved_ratification_receipt_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.contracts as contracts

    payload = json.loads(RECEIPT.read_text(encoding="utf-8"))
    payload["ratified_on"] = "2026-08-10"
    mutated = (json.dumps(payload, sort_keys=True) + "\n").encode()
    assert _sha(mutated) != contracts.RATIFICATION_RECEIPT_SHA256
    monkeypatch.setattr(
        contracts,
        "_receipt_bytes_at_pin",
        lambda _root, _pin: (("ratification.v1.json", mutated),),
    )
    monkeypatch.setattr(
        contracts,
        "_git_show",
        lambda _root, _revision, path: PREREG.read_bytes()
        if path == contracts.CONTRACT_PATH
        else mutated,
    )
    monkeypatch.setattr(contracts, "_git_applicable", lambda *_args: True)

    with pytest.raises(contracts.ContractIdentityError, match="receipt.*digest|immutable"):
        contracts.derive_preregistration_identity(
            ROOT,
            contract_revision=RATIFICATION_REVISION,
        )


def test_git_tree_receipt_discovery_rejects_unrecognized_contract_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.contracts as contracts

    listing = (
        f"{contracts.CONTRACTS_DIR}/ratification.v1.json\n"
        f"{contracts.CONTRACTS_DIR}/unrecognized.v1.json\n"
    ).encode()
    monkeypatch.setattr(
        contracts,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=listing, stderr=b""
        ),
    )
    monkeypatch.setattr(
        contracts,
        "_git_show",
        lambda _root, _revision, _path: RECEIPT.read_bytes(),
    )

    with pytest.raises(contracts.ContractIdentityError, match="unrecognized|receipt.*artifact"):
        contracts._receipt_bytes_at_pin(ROOT, RATIFICATION_REVISION)


def test_manifest_v2_requires_derived_typed_identity_and_v1_is_historical_untrusted(tmp_path: Path) -> None:
    from protocol.manifest import ManifestError, load_manifest, start_manifest

    started = start_manifest(
        tmp_path / "v2", run_id="run-v2", contract_revision=RATIFICATION_REVISION,
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1},
        started_at="2026-08-11T00:00:00Z",
    )
    assert started.schema_version == 2
    assert started.preregistration_identity.original.sha256 == APPROVED_SHA256

    old = started.model_dump(mode="json")
    old["schema_version"] = 1
    old.pop("preregistration_identity")
    old["pre_registration_sha256"] = APPROVED_SHA256
    (tmp_path / "v1").mkdir()
    (tmp_path / "v1/manifest.json").write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(ManifestError, match="historical-untrusted"):
        load_manifest(tmp_path / "v1")


def test_caller_cannot_substitute_preregistration_identity() -> None:
    from protocol.manifest import start_manifest

    assert "preregistration_identity" not in start_manifest.__annotations__


# ---------------------------------------------------------------------------
# Independent final recheck: the effective bytes and revision chain reach pin.
# ---------------------------------------------------------------------------


def test_recheck3_unreceipted_descendant_contract_edit_is_refused() -> None:
    from protocol.contracts import ContractIdentityError, derive_identity_from_receipt_bytes

    original = b"ratified\n"
    edited_at_pin = b"ratified\nunreceipted edit\n"
    payload = _ratification_payload()
    payload["contract_sha256"] = _sha(original)
    payload["repository_revision"] = "a" * 40
    ratification = ("ratification.v1.json", (json.dumps(payload) + "\n").encode())
    artifacts = {
        ("a" * 40, payload["contract_path"]): original,
        ("b" * 40, payload["contract_path"]): original,
        (
            "b" * 40,
            "benchmarks/epistemic/contracts/ratification.v1.json",
        ): ratification[1],
        ("c" * 40, payload["contract_path"]): edited_at_pin,
    }

    with pytest.raises(ContractIdentityError, match="pin|effective|digest|receipt"):
        derive_identity_from_receipt_bytes(
            (ratification,),
            contract_revision="c" * 40,
            read_git_artifact=lambda revision, path: artifacts[(revision, path)],
            is_revision_applicable=lambda revision, pin: revision <= pin,
            receipt_introduction_revisions=lambda _path, _pin: ("b" * 40,),
        )


def test_recheck3_genuinely_unchanged_descendant_preserves_historical_validity() -> None:
    from protocol.contracts import derive_identity_from_receipt_bytes

    original = b"ratified\n"
    payload = _ratification_payload()
    payload["contract_sha256"] = _sha(original)
    payload["repository_revision"] = "a" * 40
    path = str(payload["contract_path"])
    receipt = ("ratification.v1.json", (json.dumps(payload) + "\n").encode())
    artifacts = {
        ("a" * 40, path): original,
        ("b" * 40, path): original,
        (
            "b" * 40,
            "benchmarks/epistemic/contracts/ratification.v1.json",
        ): receipt[1],
        ("c" * 40, path): original,
    }

    identity = derive_identity_from_receipt_bytes(
        (receipt,),
        contract_revision="c" * 40,
        read_git_artifact=lambda revision, contract_path: artifacts[(revision, contract_path)],
        is_revision_applicable=lambda revision, pin: revision <= pin,
        receipt_introduction_revisions=lambda _path, _pin: ("b" * 40,),
    )
    assert identity.effective.sha256 == _sha(original)
    assert identity.amendments == ()


# ---------------------------------------------------------------------------
# Final Git-history correction: document commits precede receipt introductions.
# ---------------------------------------------------------------------------


def _real_git(repo: Path, *args: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Receipt Test",
            "GIT_AUTHOR_EMAIL": "receipt@example.invalid",
            "GIT_COMMITTER_NAME": "Receipt Test",
            "GIT_COMMITTER_EMAIL": "receipt@example.invalid",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
    )
    return completed.stdout.strip()


def _real_commit(repo: Path, message: str) -> str:
    _real_git(repo, "add", "-A")
    _real_git(repo, "commit", "-m", message)
    return _real_git(repo, "rev-parse", "HEAD")


def _real_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    import protocol.contracts as contracts

    repo = tmp_path / "history"
    repo.mkdir()
    _real_git(repo, "init", "-b", "main")
    contract_path = repo / contracts.CONTRACT_PATH
    receipt_dir = repo / contracts.CONTRACTS_DIR
    contract_path.parent.mkdir(parents=True)
    original = b"ratified v0\n"
    contract_path.write_bytes(original)
    ratified_revision = _real_commit(repo, "ratify v0")

    ratification_payload = _ratification_payload()
    ratification_payload["contract_sha256"] = _sha(original)
    ratification_payload["repository_revision"] = ratified_revision
    ratification = (json.dumps(ratification_payload, sort_keys=True) + "\n").encode()
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "ratification.v1.json").write_bytes(ratification)
    ratification_introduction = _real_commit(repo, "add ratification receipt")

    amended = b"ratified v1\n"
    contract_path.write_bytes(amended)
    amended_revision = _real_commit(repo, "amend preregistration")
    amendment = _amendment(1, _sha(original), amended, amended_revision)
    amendment_path = receipt_dir / "amendment-0001.v1.json"
    amendment_path.write_bytes(amendment)
    amendment_introduction = _real_commit(repo, "add amendment receipt")

    monkeypatch.setattr(
        contracts, "RATIFICATION_REPOSITORY_REVISION", ratified_revision
    )
    monkeypatch.setattr(contracts, "RATIFICATION_CONTRACT_SHA256", _sha(original))
    monkeypatch.setattr(contracts, "RATIFICATION_RECEIPT_SHA256", _sha(ratification))
    return {
        "contracts": contracts,
        "repo": repo,
        "original": original,
        "amended": amended,
        "ratification": ratification,
        "ratified_revision": ratified_revision,
        "ratification_introduction": ratification_introduction,
        "amended_revision": amended_revision,
        "amendment": amendment,
        "amendment_path": amendment_path,
        "amendment_introduction": amendment_introduction,
    }


def test_receipt_git_history_derives_document_then_receipt_introductions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    contracts = history["contracts"]

    identity = contracts.derive_preregistration_identity(
        history["repo"], contract_revision=history["amendment_introduction"]
    )

    assert identity.ratification.introduction_revision == history[
        "ratification_introduction"
    ]
    assert identity.amendments[0].contract.repository_revision == history[
        "amended_revision"
    ]
    assert identity.amendments[0].receipt.introduction_revision == history[
        "amendment_introduction"
    ]
    schema = json.loads(
        (ROOT / "benchmarks/protocol/schema/run-manifest.v2.schema.json").read_text()
    )
    introduction = schema["$defs"]["ReceiptIdentity"]["properties"][
        "introduction_revision"
    ]
    assert {branch.get("type") for branch in introduction["anyOf"]} == {
        "string",
        "null",
    }


@pytest.mark.parametrize("field", ["rationale", "effective_policy"])
def test_receipt_git_history_refuses_later_amendment_receipt_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    payload = json.loads(history["amendment"])
    payload[field] = f"later mutation of {field}"
    history["amendment_path"].write_bytes(
        (json.dumps(payload, sort_keys=True) + "\n").encode()
    )
    mutated_revision = _real_commit(history["repo"], f"mutate {field}")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="receipt.*introduction.*bytes|receipt.*changed",
    ):
        history["contracts"].derive_preregistration_identity(
            history["repo"], contract_revision=mutated_revision
        )


def test_receipt_git_history_refuses_mutation_even_after_exact_byte_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    payload = json.loads(history["amendment"])
    payload["rationale"] = "temporary mutation later hidden by an exact restore"
    history["amendment_path"].write_bytes(
        (json.dumps(payload, sort_keys=True) + "\n").encode()
    )
    _real_commit(history["repo"], "temporarily mutate receipt")
    history["amendment_path"].write_bytes(history["amendment"])
    restored_revision = _real_commit(history["repo"], "restore receipt bytes")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="receipt.*changed|receipt.*history|mutation",
    ):
        history["contracts"].derive_preregistration_identity(
            history["repo"], contract_revision=restored_revision
        )


def test_receipt_git_full_history_refuses_side_branch_mutation_hidden_by_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    repo = history["repo"]
    _real_git(repo, "checkout", "-b", "receipt-mutation")
    payload = json.loads(history["amendment"])
    payload["rationale"] = "side-branch mutation hidden by merge resolution"
    history["amendment_path"].write_bytes(
        (json.dumps(payload, sort_keys=True) + "\n").encode()
    )
    _real_commit(repo, "mutate receipt on side branch")

    _real_git(repo, "checkout", "main")
    (repo / "main-only.txt").write_text("advance main\n", encoding="utf-8")
    _real_commit(repo, "advance main without touching receipt")
    _real_git(repo, "merge", "--no-ff", "--no-commit", "receipt-mutation")
    history["amendment_path"].write_bytes(history["amendment"])
    merge_revision = _real_commit(repo, "merge while retaining original receipt")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="receipt.*changed|receipt.*history|mutation",
    ):
        history["contracts"].derive_preregistration_identity(
            repo, contract_revision=merge_revision
        )


def test_receipt_git_full_history_allows_merge_without_receipt_path_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    repo = history["repo"]
    _real_git(
        repo, "checkout", "-b", "unrelated-side", history["ratified_revision"]
    )
    (repo / "side-only.txt").write_text("side\n", encoding="utf-8")
    _real_commit(repo, "change unrelated side path")

    _real_git(repo, "checkout", "main")
    (repo / "main-only.txt").write_text("main\n", encoding="utf-8")
    _real_commit(repo, "change unrelated main path")
    _real_git(repo, "merge", "--no-ff", "--no-edit", "unrelated-side")
    merge_revision = _real_git(repo, "rev-parse", "HEAD")

    identity = history["contracts"].derive_preregistration_identity(
        repo, contract_revision=merge_revision
    )

    assert identity.amendments[0].receipt.introduction_revision == history[
        "amendment_introduction"
    ]


def test_receipt_git_history_refuses_delete_and_readd_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    history["amendment_path"].unlink()
    _real_commit(history["repo"], "delete amendment receipt")
    history["amendment_path"].write_bytes(history["amendment"])
    readded_revision = _real_commit(history["repo"], "readd amendment receipt")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="unique introduction|delete|re-add|history",
    ):
        history["contracts"].derive_preregistration_identity(
            history["repo"], contract_revision=readded_revision
        )


def test_receipt_git_history_refuses_named_contract_revision_from_side_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    repo = history["repo"]
    _real_git(repo, "checkout", "-b", "side", history["ratification_introduction"])
    side_document = b"side-branch amendment\n"
    (repo / history["contracts"].CONTRACT_PATH).write_bytes(side_document)
    side_revision = _real_commit(repo, "side contract amendment")
    _real_git(repo, "checkout", "main")
    second = _amendment(2, _sha(history["amended"]), side_document, side_revision)
    second_path = repo / history["contracts"].CONTRACTS_DIR / "amendment-0002.v1.json"
    second_path.write_bytes(second)
    pin = _real_commit(repo, "add receipt naming side branch")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="named amended-contract revision.*ancestor|not an ancestor",
    ):
        history["contracts"].derive_preregistration_identity(repo, contract_revision=pin)


def test_receipt_git_history_refuses_contract_revision_before_previous_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = _real_history(tmp_path, monkeypatch)
    repo = history["repo"]
    _real_git(repo, "checkout", "-b", "second-contract", history["amended_revision"])
    second_document = b"ratified v2\n"
    (repo / history["contracts"].CONTRACT_PATH).write_bytes(second_document)
    second_revision = _real_commit(repo, "second contract before first receipt")
    _real_git(repo, "checkout", "main")
    _real_git(repo, "merge", "--no-ff", "--no-edit", "second-contract")
    second = _amendment(
        2, _sha(history["amended"]), second_document, second_revision
    )
    second_path = repo / history["contracts"].CONTRACTS_DIR / "amendment-0002.v1.json"
    second_path.write_bytes(second)
    pin = _real_commit(repo, "add out-of-order second receipt")

    with pytest.raises(
        history["contracts"].ContractIdentityError,
        match="previous receipt introduction.*amended-contract revision|ordered ancestry",
    ):
        history["contracts"].derive_preregistration_identity(repo, contract_revision=pin)
