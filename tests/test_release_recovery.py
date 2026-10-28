"""Release retries preserve immutable files and original producer evidence."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_recovery", ROOT / "infra/scripts/release_recovery.py"
)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.mark.parametrize(
    ("published", "verification_attempt"),
    [("success", "1"), ("skipped", "1"), ("success", "2")],
)
def test_python_qualification_uses_exact_producer_while_images_are_running(
    tmp_path, monkeypatch, published, verification_attempt
):
    """Official bytes can qualify before unrelated image jobs or the workflow complete."""
    source = "a" * 40
    names = ["sample-1.2.3-py3-none-any.whl", "sample-1.2.3.tar.gz"]
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setattr(recovery, "project", lambda tag: {"name": "sample", "version": "1.2.3"})
    build = {
        "id": 91,
        "run_attempt": 1,
        "name": "build-artifacts",
        "conclusion": "success",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z",
        "steps": [
            {"name": "Upload missing artifacts to the GitHub release", "conclusion": "success"}
        ],
    }
    producer = {
        "id": 12,
        "run_attempt": 1,
        "run_started_at": "2026-01-01T00:00:00Z",
        "path": ".github/workflows/release-please.yml",
        "event": "workflow_dispatch",
        "head_branch": "v1.2.3",
        "head_sha": source,
        "repository": {"full_name": "example/project"},
        "head_repository": {"full_name": "example/project"},
        "status": "in_progress",
    }
    artifact = {
        "id": 99,
        "name": "python-distributions",
        "expired": False,
        "created_at": "2026-01-01T00:00:30Z",
        "workflow_run": {"id": 12, "head_sha": source},
    }

    def run(*args):
        if args[:2] == ("git", "rev-parse"):
            return source
        endpoint = args[-1]
        if endpoint.endswith("/attempts/1"):
            return json.dumps(producer)
        if endpoint.endswith("/attempts/2"):
            return json.dumps({**producer, "run_attempt": 2, "run_started_at": "2026-01-01T00:02:00Z"})
        if "/jobs?" in endpoint:
            # GitHub copies a successful job into later attempts with a new ID,
            # but preserves the original execution timestamps and artifact.
            copied_build = {**build, "id": 92, "run_attempt": 2}
            publication = (
                "failure" if verification_attempt == "2" and "/attempts/1/" in endpoint else published
            )
            return json.dumps(
                [
                    {
                        "jobs": [
                            copied_build if "/attempts/2/" in endpoint else build,
                            {"name": "publish-pypi", "conclusion": publication},
                            {"name": "publish-images", "conclusion": None},
                        ]
                    }
                ]
            )
        if "/artifacts?" in endpoint:
            return json.dumps([{"artifacts": [artifact]}])
        raise AssertionError(args)

    def download(tag, selected, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for name in selected:
            (directory / name).write_bytes(name.encode())

    monkeypatch.setattr(recovery, "run", run)
    monkeypatch.setattr(recovery, "release_assets", lambda tag: [{"name": name} for name in names])
    monkeypatch.setattr(recovery, "download_release", download)
    monkeypatch.setattr(
        recovery,
        "download_artifact",
        lambda artifact, repo, directory: download("v1.2.3", names, directory),
    )
    urls = [
        {
            "filename": name,
            "url": name,
            "digests": {"sha256": recovery.hashlib.sha256(name.encode()).hexdigest()},
        }
        for name in names
    ]
    observations = iter([[], urls])
    monkeypatch.setattr(recovery, "pypi_files", lambda *args: next(observations))
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        recovery.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(url.encode())
    )
    receipt = recovery.qualify_python(
        "v1.2.3", source, "12", "1", tmp_path, verification_attempt=verification_attempt
    )
    assert receipt["producer"] == {
        "repository": "example/project",
        "run_id": "12",
        "run_attempt": "1",
        "artifact_id": 99,
    }
    assert receipt["verification"] == {
        "repository": "example/project", "run_id": "12", "run_attempt": verification_attempt,
    }
    assert {row["filename"] for row in receipt["distributions"]} == set(names)
    assert receipt["python"]["state"] == "qualified"
    assert receipt["images"]["state"] == "unqualified"
    assert receipt["consumer_verification"]["state"] == "unverified"

    if verification_attempt == "2":
        with pytest.raises(ValueError, match="predates.*producer attempt"):
            recovery.qualify_python("v1.2.3", source, "12", "2", tmp_path)

    # A conflicting public digest is permanent; it must not trigger another registry retry.
    urls[0]["digests"]["sha256"] = "0" * 64
    monkeypatch.setattr(recovery, "pypi_files", lambda *args: urls)
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: pytest.fail("conflict retried"))
    with pytest.raises(ValueError, match="immutable distribution"):
        recovery.qualify_python(
            "v1.2.3", source, "12", "1", tmp_path, verification_attempt=verification_attempt
        )

    producer["head_sha"] = "b" * 40
    with pytest.raises(ValueError, match="producer identity"):
        recovery.qualify_python(
            "v1.2.3", source, "12", "1", tmp_path, verification_attempt=verification_attempt
        )


def test_distribution_completeness_requires_matching_package_and_both_kinds():
    assert not recovery.distributions_complete(
        ["other-1.2.3-py3-none-any.whl", "sample-1.2.3.tar.gz"], "sample", "1.2.3"
    )
    assert not recovery.distributions_complete(
        ["sample-1.2.2-py3-none-any.whl", "sample-1.2.3.tar.gz"], "sample", "1.2.3"
    )
    assert recovery.distributions_complete(
        ["sample-1.2.3-py3-none-any.whl", "sample-1.2.3.tar.gz"], "sample", "1.2.3"
    )


def test_pypi_retry_keeps_matching_bytes_and_rejects_conflicting_immutable_file(tmp_path):
    wheel = tmp_path / "sample-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"original wheel")
    row = {"filename": wheel.name, "digests": {"sha256": recovery.sha256(wheel)}}
    assert recovery.missing_distributions([wheel], [row]) == []
    row["digests"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="immutable"):
        recovery.missing_distributions([wheel], [row])


def test_upload_publishes_release_assets_without_build_metadata(tmp_path, monkeypatch):
    """Build metadata in the distribution directory must never become a release asset."""
    cloud_names = ("sample-claude.zip", "sample-openai.zip", "sample-release.json")
    expected = {
        name: name.encode()
        for name in ("sample-1.2.3-py3-none-any.whl", "sample-1.2.3.tar.gz", *cloud_names)
    }
    for name, data in {**expected, ".gitignore": b"*", "unrelated.txt": b"build metadata"}.items():
        (tmp_path / name).write_bytes(data)
    monkeypatch.setattr(recovery, "project", lambda tag: {"name": "sample", "version": "1.2.3"})
    monkeypatch.setattr(recovery, "cloud_files", lambda tag: dict.fromkeys(cloud_names))
    monkeypatch.setattr(recovery, "release_assets", lambda tag: [])
    monkeypatch.setattr(
        "sys.argv", ["release-recovery", "upload", "--tag", "v1.2.3", "--directory", str(tmp_path)]
    )
    uploaded = {}

    def upload(*args):
        assert args[:4] == ("gh", "release", "upload", "v1.2.3")
        path = Path(args[4])
        uploaded[path.name] = path.read_bytes()
        return ""

    monkeypatch.setattr(recovery, "run", upload)
    recovery.main()
    assert uploaded == expected


def test_partial_release_receipts_can_be_completed_from_original_artifact(tmp_path):
    release = tmp_path / "release"
    artifact = tmp_path / "artifact"
    release.mkdir()
    artifact.mkdir()
    receipt = "exomem-image-cloud-original.sigstore.json"
    (release / receipt).write_bytes(b"original signed cloud receipt")
    for name, data in (
        (receipt, b"original signed cloud receipt"),
        ("exomem-image-cellctl-original.sigstore.json", b"original signed cellctl receipt"),
    ):
        (artifact / name).write_bytes(data)
    recovery.merge_evidence(artifact, release)
    assert sorted(p.name for p in release.iterdir()) == sorted(p.name for p in artifact.iterdir())
    assert (release / receipt).read_bytes() == b"original signed cloud receipt"
    (artifact / receipt).write_bytes(b"retry identity")
    with pytest.raises(ValueError, match="immutable"):
        recovery.merge_evidence(artifact, release)


def test_recovered_image_without_version_tag_finishes_each_tag_without_building(
    tmp_path, monkeypatch
):
    """A producer can retain evidence and stop before any discovery tag exists."""
    commit = "a" * 40
    digest = "sha256:" + "b" * 64
    repository = "ghcr.io/example/runtime"
    version_tag = repository + ":1.2.3-cloud"
    commit_tag = repository + ":" + commit + "-cloud"
    receipt = tmp_path / "original.sigstore.json"
    receipt.write_bytes(b"original signed producer bytes")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(recovery, "recovered_image", lambda *args: (digest, [receipt]))
    monkeypatch.setattr(recovery, "image_evidence", lambda *args: None)
    monkeypatch.setattr(recovery, "release_assets", lambda *args: [])
    tags = {commit_tag: digest}
    monkeypatch.setattr(recovery, "image_digest", lambda ref: tags.get(ref))
    uploaded = {}

    def command(*args):
        if args[0] == "git":
            return commit
        if args[:4] == ("gh", "release", "upload", "v1.2.3"):
            path = Path(args[4])
            uploaded[path.name] = path.read_bytes()
            return ""
        if args[:4] == ("docker", "buildx", "imagetools", "create"):
            assert args[-1] == repository + "@" + digest
            assert "--prefer-index=false" in args
            tags[args[args.index("--tag") + 1]] = digest
            return ""
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(recovery, "run", command)
    images = {"cloud": {"ref": version_tag, "immutable_tags": [version_tag, commit_tag]}}
    plan = recovery.inspect_images("v1.2.3", images, tmp_path, dry_run=False)
    assert plan["cloud"]["publish"] is False
    assert plan["cloud"]["state"] == "missing"
    recovery.finish_images("v1.2.3", plan, tmp_path)
    assert tags == {version_tag: digest, commit_tag: digest}
    assert uploaded == {receipt.name: b"original signed producer bytes"}


def test_dry_run_does_not_consult_an_unavailable_registry(tmp_path, monkeypatch):
    def unavailable(*args):
        raise AssertionError("dry run must not depend on registry availability")

    monkeypatch.setattr(recovery, "image_evidence", unavailable)
    monkeypatch.setattr(recovery, "image_digest", unavailable)
    plan = recovery.inspect_images(
        "v1.2.3", {"cloud": {"ref": "ghcr.io/example/runtime:1.2.3-cloud"}}, tmp_path, dry_run=True
    )
    assert plan["cloud"]["publish"] is True


@pytest.mark.parametrize("other_target", ["lean", None, "cloud"])
def test_raw_bundle_cannot_bless_another_target_or_a_platform_without_provenance(
    tmp_path, monkeypatch, other_target
):
    import base64
    import json

    commit = "a" * 40
    digest = "b" * 64
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-123-1.sigstore.json"
    statement = {
        "subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}],
        "predicate": {
            "runDetails": {
                "metadata": {
                    "invocationId": "https://github.com/example/runtime/actions/runs/123/attempts/1"
                }
            }
        },
    }
    bundle.write_text(
        json.dumps(
            {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}}
        )
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    inspection = {
        "images": {"linux/amd64": {}, "linux/arm64": {}},
        "provenance": {
            "linux/amd64": {"SLSA": {"invocation": {"parameters": {"args": {"target": "cloud"}}}}}
        },
    }
    if other_target is not None:
        inspection["provenance"]["linux/arm64"] = {
            "SLSA": {"invocation": {"parameters": {"args": {"target": other_target}}}}
        }
    monkeypatch.setattr(
        recovery,
        "run",
        lambda *args: (
            json.dumps([{"verificationResult": {"statement": statement}}])
            if args[0] == "gh"
            else json.dumps(inspection)
        ),
    )
    if other_target == "cloud":
        assert (
            recovery.verify_image_bundle(
                bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud"
            )
            == "sha256:" + digest
        )
    else:
        with pytest.raises(ValueError, match="target|platform"):
            recovery.verify_image_bundle(
                bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud"
            )


def test_raw_bundle_uses_signed_producer_attempt_instead_of_filename_claim(tmp_path, monkeypatch):
    import base64
    import json

    commit = "a" * 40
    digest = "b" * 64
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-123-2.sigstore.json"
    statement = {
        "subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}],
        "predicate": {
            "runDetails": {
                "metadata": {
                    "invocationId": "https://github.com/example/runtime/actions/runs/123/attempts/1"
                }
            }
        },
    }
    bundle.write_text(
        json.dumps(
            {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}}
        )
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(
        recovery,
        "run",
        lambda *args: json.dumps([{"verificationResult": {"statement": statement}}]),
    )
    with pytest.raises(ValueError, match="producer"):
        recovery.verify_image_bundle(bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud")


@pytest.mark.parametrize(
    "verification", [[], {}, [None], [{}], [{"verificationResult": None}],
                     [{"verificationResult": {"statement": []}}]]
)
def test_image_recovery_rejects_empty_or_malformed_verification_results(
    tmp_path, monkeypatch, verification
):
    """A successful verifier process must still authenticate a usable statement."""
    import base64
    import json

    commit, digest = "a" * 40, "b" * 64
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-123-1.sigstore.json"
    statement = {"subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}]}
    bundle.write_text(json.dumps({
        "dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}
    }))
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(recovery, "run", lambda *args: json.dumps(verification))

    with pytest.raises(ValueError, match="verification"):
        recovery.verify_image_bundle(bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud")


@pytest.mark.parametrize(
    "subjects", [None, [], [None],
                 [{"name": "ghcr.io/example/other", "digest": {"sha256": "b" * 64}}],
                 [{"name": "ghcr.io/example/runtime", "digest": {"sha256": "c" * 64}}],
                 [{"name": "ghcr.io/example/runtime", "digest": {"sha256": "b" * 64}}] * 2]
)
def test_image_recovery_rejects_a_different_or_additional_verified_subject(
    tmp_path, monkeypatch, subjects
):
    """A retained receipt must authenticate exactly the image that recovery tags."""
    import base64
    import json

    commit, digest = "a" * 40, "b" * 64
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-123-1.sigstore.json"
    statement = {"subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}]}
    bundle.write_text(json.dumps({
        "dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}
    }))
    statement["subject"] = subjects
    verification = [{"verificationResult": {"statement": statement}}]
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(recovery, "run", lambda *args: json.dumps(verification))

    with pytest.raises(ValueError, match="subject"):
        recovery.verify_image_bundle(bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud")


@pytest.mark.parametrize("digest", ["", "b" * 63, "B" * 64, None])
def test_image_recovery_rejects_a_malformed_digest_before_accepting_a_receipt(
    tmp_path, monkeypatch, digest
):
    """Malformed receipt subjects must never become registry references."""
    import base64
    import json

    commit = "a" * 40
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-123-1.sigstore.json"
    statement = {
        "subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}],
        "predicate": {"runDetails": {"metadata": {
            "invocationId": "https://github.com/example/runtime/actions/runs/123/attempts/1"
        }}},
    }
    bundle.write_text(json.dumps({
        "dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}
    }))
    verification = [{"verificationResult": {"statement": statement}}]
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(recovery, "run", lambda *args: json.dumps(verification))
    monkeypatch.setattr(recovery, "verify_build_target", lambda *args: None)

    with pytest.raises(ValueError, match="digest"):
        recovery.verify_image_bundle(bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud")


@pytest.mark.parametrize("run_id,attempt", [("0", "1"), ("123", "01"), ("abc", "1")])
def test_image_recovery_rejects_an_invalid_signed_producer_run_or_attempt(
    tmp_path, monkeypatch, run_id, attempt
):
    """Receipt filenames cannot legitimize invalid signed producer identities."""
    import base64
    import json

    commit, digest = "a" * 40, "b" * 64
    bundle = tmp_path / f"exomem-image-cloud-{commit}-{digest}-{run_id}-{attempt}.sigstore.json"
    statement = {
        "subject": [{"name": "ghcr.io/example/runtime", "digest": {"sha256": digest}}],
        "predicate": {"runDetails": {"metadata": {
            "invocationId": f"https://github.com/example/runtime/actions/runs/{run_id}/attempts/{attempt}"
        }}},
    }
    bundle.write_text(json.dumps({
        "dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode()}
    }))
    verification = [{"verificationResult": {"statement": statement}}]
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    monkeypatch.setattr(recovery, "run", lambda *args: json.dumps(verification))

    with pytest.raises(ValueError, match="positive decimal"):
        recovery.verify_image_bundle(bundle, "v1.2.3", commit, "ghcr.io/example/runtime", "cloud")


def test_commit_alias_without_version_or_evidence_prevents_rebuild(tmp_path, monkeypatch):
    version = "ghcr.io/example/runtime:1.2.3-cloud"
    commit_tag = "ghcr.io/example/runtime:commit-cloud"
    monkeypatch.setattr(recovery, "run", lambda *args: "a" * 40)
    monkeypatch.setattr(recovery, "image_evidence", lambda *args: None)
    monkeypatch.setattr(recovery, "recovered_image", lambda *args: (None, []))
    monkeypatch.setattr(
        recovery, "image_digest", lambda ref: "sha256:" + "b" * 64 if ref == commit_tag else None
    )
    result = recovery.inspect_images(
        "v1.2.3",
        {"cloud": {"ref": version, "immutable_tags": [version, commit_tag]}},
        tmp_path,
        dry_run=False,
    )
    assert not result["cloud"]["publish"]
    assert result["cloud"]["state"].startswith("incomplete:")


def test_late_alias_conflict_prevents_all_tag_writes(tmp_path, monkeypatch):
    digest = "sha256:" + "b" * 64
    receipt = tmp_path / "original.sigstore.json"
    receipt.write_bytes(b"signed original")
    monkeypatch.setattr(
        recovery,
        "run",
        lambda *args: (
            "a" * 40 if args[0] == "git" else pytest.fail("tag write before complete preflight")
        ),
    )
    monkeypatch.setattr(
        recovery, "upload_missing", lambda *args: pytest.fail("upload before complete preflight")
    )
    monkeypatch.setattr(recovery, "recovered_image", lambda *args: (digest, [receipt]))
    monkeypatch.setattr(
        recovery,
        "image_digest",
        lambda ref: "sha256:" + "c" * 64 if ref.endswith(":commit-cloud") else None,
    )
    images = {
        "lean": {
            "ref": "ghcr.io/example/runtime:1.2.3",
            "immutable_tags": ["ghcr.io/example/runtime:1.2.3"],
        },
        "cloud": {
            "ref": "ghcr.io/example/runtime:1.2.3-cloud",
            "immutable_tags": [
                "ghcr.io/example/runtime:1.2.3-cloud",
                "ghcr.io/example/runtime:commit-cloud",
            ],
        },
    }
    with pytest.raises(ValueError, match="immutable"):
        recovery.finish_images("v1.2.3", images, tmp_path)


def test_complete_published_distributions_ignore_unrelated_dry_run_artifacts(tmp_path, monkeypatch):
    wheel = "sample-1.2.3-py3-none-any.whl"
    sdist = "sample-1.2.3.tar.gz"
    published = {wheel: b"published wheel", sdist: b"published sdist"}
    monkeypatch.setattr(recovery, "project", lambda tag: {"name": "sample", "version": "1.2.3"})
    monkeypatch.setattr(
        recovery, "release_assets", lambda tag: [{"name": name} for name in published]
    )
    monkeypatch.setattr(
        recovery,
        "download_release",
        lambda tag, names, directory: [
            (directory / name).write_bytes(published[name]) for name in names
        ],
    )
    monkeypatch.setattr(recovery, "pypi_files", lambda *args: [])
    monkeypatch.setattr(
        recovery,
        "producer_artifacts",
        lambda *args, **kwargs: pytest.fail("published pair must not consult dry-run artifacts"),
    )
    recovery.prepare_distributions("v1.2.3", tmp_path, dry_run=False)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == published


def test_partial_distributions_select_original_publication_and_exclude_dry_run(
    tmp_path, monkeypatch
):
    import json

    commit = "a" * 40
    wheel = "sample-1.2.3-py3-none-any.whl"
    sdist = "sample-1.2.3.tar.gz"
    original = {wheel: b"published wheel", sdist: b"original sdist"}
    (tmp_path / wheel).write_bytes(original[wheel])
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/runtime")
    producers = [
        {"id": run_id, "head_sha": commit, "head_branch": "v1.2.3", "event": "workflow_dispatch"}
        for run_id in (200, 100)
    ]

    def command(*args):
        endpoint = args[-1]
        if "/workflows/" in endpoint:
            return json.dumps([{"workflow_runs": producers}])
        if "/artifacts?" in endpoint:
            return json.dumps(
                [
                    {
                        "artifacts": [
                            {
                                "id": 200 if "/200/" in endpoint else 100,
                                "name": "python-distributions",
                                "expired": False,
                                "created_at": "2026-01-01T00:02:00Z",
                            }
                        ]
                    }
                ]
            )
        if "/jobs?" in endpoint:
            conclusion = "skipped" if "/200/" in endpoint else "failure"
            return json.dumps(
                [
                    {
                        "jobs": [
                            {
                                "name": "build-artifacts",
                                "started_at": "2026-01-01T00:00:00Z",
                                "completed_at": "2026-01-01T00:03:00Z",
                                "steps": [
                                    {
                                        "name": "Upload missing artifacts to the GitHub release",
                                        "conclusion": conclusion,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            )
        raise AssertionError(args)

    import zipfile

    def download(args, *, stdout, check):
        assert args[-1].endswith("/artifacts/100/zip"), "select the original artifact ID"
        with zipfile.ZipFile(stdout, "w") as archive:
            for name, data in original.items():
                archive.writestr(name, data)

    monkeypatch.setattr(recovery.subprocess, "run", download)
    monkeypatch.setattr(recovery, "run", command)
    recovery.producer_artifacts(
        "v1.2.3",
        commit,
        ("python-distributions",),
        tmp_path,
        package={"name": "sample", "version": "1.2.3"},
    )
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original


@pytest.mark.parametrize(
    "name,targets", [("cloud", ["cloud"]), ("ml", ["ml", "ml"]), ("cellctl", [None])]
)
def test_buildkit_target_verification_accepts_all_matching_platforms(monkeypatch, name, targets):
    import json

    proofs = [
        {"SLSA": {"invocation": {"parameters": {"args": {"target": target} if target else None}}}}
        for target in targets
    ]
    if len(proofs) == 1:
        inspection = {"images": {"architecture": "amd64", "os": "linux"}, "provenance": proofs[0]}
    else:
        platforms = ("linux/amd64", "linux/arm64")
        inspection = {
            "images": dict.fromkeys(platforms, {}),
            "provenance": dict(zip(platforms, proofs, strict=True)),
        }
    monkeypatch.setattr(recovery, "run", lambda *args: json.dumps(inspection))
    recovery.verify_build_target("ghcr.io/example/runtime@sha256:" + "b" * 64, name)


@pytest.mark.parametrize(
    "invalid", [None, "missing-platform", "duplicate-platform", "bad-descriptor"]
)
def test_native_index_join_writes_only_the_validated_digest(tmp_path, monkeypatch, invalid):
    """A partial native build must never become a tagged or attested release image."""
    import hashlib
    import json

    repository = "registry.example/runtime"
    children = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    for architecture, digest in zip(("amd64", "arm64"), children, strict=True):
        (tmp_path / f"cloud-{architecture}.digest").write_text(digest)
    manifests = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "size": 123,
            "platform": {"os": "linux", "architecture": architecture},
        }
        for architecture, digest in zip(("amd64", "arm64"), children, strict=True)
    ]
    if invalid == "missing-platform":
        manifests.pop()
    elif invalid == "duplicate-platform":
        manifests.append(manifests[0])
    elif invalid == "bad-descriptor":
        manifests[0]["digest"] = "invalid"
    # Keep indentation: a reserialization changes the content address.
    raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": manifests,
        },
        indent=2,
    ).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    pushed = []

    def check_output(args):
        if "--dry-run" in args:
            assert args[-2:] == [repository + "@" + child for child in children]
        else:
            assert "inspect" in args and args[-1] == repository + "@" + digest
        # Buildx appends one display newline to both JSON outputs.
        return raw + b"\n"

    monkeypatch.setattr(recovery.subprocess, "check_output", check_output)
    monkeypatch.setattr(recovery, "run", lambda *args: pushed.append(args) or "")
    verified = []
    monkeypatch.setattr(recovery, "verify_build_target", lambda *args: verified.append(args))
    if invalid:
        with pytest.raises(ValueError, match="platform|digest"):
            recovery.join_image(repository, "cloud", tmp_path)
        assert not pushed
        assert not verified
    else:
        assert recovery.join_image(repository, "cloud", tmp_path) == digest
        assert pushed == [
            (
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                repository + "@" + digest,
                *[repository + "@" + child for child in children],
            )
        ]
        assert verified == [(repository + "@" + digest, "cloud")]


def test_native_index_join_rejects_missing_child_without_registry_writes(tmp_path, monkeypatch):
    (tmp_path / "cloud-amd64.digest").write_text("sha256:" + "a" * 64)
    monkeypatch.setattr(recovery, "run", lambda *args: pytest.fail("partial pair must not push"))
    with pytest.raises(FileNotFoundError, match="cloud-arm64.digest"):
        recovery.join_image("registry.example/runtime", "cloud", tmp_path)


def test_missing_native_role_finishes_verified_roles_and_reports_incomplete(tmp_path, monkeypatch):
    """One failed native role must not hide or suppress another role's publication."""
    digest = "sha256:" + "b" * 64
    receipt = tmp_path / "original.sigstore.json"
    receipt.write_bytes(b"original receipt")
    tags = {}
    uploaded = []
    monkeypatch.setattr(recovery, "recovered_image", lambda name, *args: (None, []) if name == "cloud" else (digest, [receipt]))
    monkeypatch.setattr(recovery, "image_digest", lambda ref: tags.get(ref))
    monkeypatch.setattr(recovery, "upload_missing", lambda *args: uploaded.append(args[0]))

    def command(*args):
        if args[0] == "git":
            return "a" * 40
        tags[args[args.index("--tag") + 1]] = digest
        return ""

    monkeypatch.setattr(recovery, "run", command)
    images = {
        "cloud": {"ref": "registry.example/runtime:1.2.3-cloud", "immutable_tags": ["registry.example/runtime:1.2.3-cloud"]},
        "cellctl": {"ref": "registry.example/controller:1.2.3", "immutable_tags": ["registry.example/controller:1.2.3"]},
    }
    with pytest.raises(ValueError, match="release incomplete.*cloud"):
        recovery.finish_images("v1.2.3", images, tmp_path)
    assert tags == {"registry.example/controller:1.2.3": digest}
    assert uploaded == ["v1.2.3"]
