#!/usr/bin/env python3
"""Recover release bytes and producer evidence before publishing discovery tags.

Conflicting immutable bytes stop publication to prevent replacement. A false
conflict delays release recovery; the release maintainer resolves the evidence.
Registry outages remain retryable errors and never block a dry run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import hosted_image_candidate as candidate_tool
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version
from verify_hosted_release import runtime_candidate_assets


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project(tag: str) -> dict:
    return tomllib.loads(run("git", "show", f"{tag}:pyproject.toml"))["project"]


def distribution_kind(filename: str, package: str, version: str) -> str | None:
    try:
        if filename.endswith(".whl"):
            name, parsed, _, _ = parse_wheel_filename(filename)
            kind = "wheel"
        else:
            name, parsed = parse_sdist_filename(filename)
            kind = "sdist"
    except (InvalidWheelFilename, InvalidSdistFilename):
        return None
    return kind if name == canonicalize_name(package) and parsed == Version(version) else None


def distributions_complete(names, package: str, version: str) -> bool:
    # These are the two distribution formats required by the release contract.
    return {distribution_kind(name, package, version) for name in names} >= {"wheel", "sdist"}


def missing_distributions(files: list[Path], urls: list[dict]) -> list[Path]:
    published = {row["filename"]: row["digests"]["sha256"] for row in urls}
    missing = []
    for path in files:
        if path.name not in published:
            missing.append(path)
        elif published[path.name] != sha256(path):
            raise ValueError(f"immutable distribution differs: {path.name}")
    return missing


def pypi_files(package: str, version: str) -> list[dict]:
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{package}/{version}/json", timeout=30
        ) as response:
            return json.load(response)["urls"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


def cloud_files(tag: str) -> dict[str, tuple[str, str]]:
    source = "plugins/cloud/generated"
    metadata = run("git", "show", f"{tag}:{source}/release.json")
    # These filenames are the existing public Cloud release asset contract.
    mapping = {
        "claude.zip": "exomem-cloud-claude.zip",
        "openai.zip": "exomem-cloud-openai.zip",
        "release.json": "exomem-cloud-release.json",
    }
    hashes = json.loads(metadata)["files"]
    return {
        target: (
            f"{source}/{name}",
            hashes[name]
            if name != "release.json"
            else hashlib.sha256(
                subprocess.check_output(["git", "show", f"{tag}:{source}/{name}"])
            ).hexdigest(),
        )
        for name, target in mapping.items()
    }


def release_assets(tag: str) -> list[dict]:
    return json.loads(run("gh", "release", "view", tag, "--json", "assets"))["assets"]


def download_release(tag: str, names: list[str], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if names:
        args = ["gh", "release", "download", tag, "--dir", str(directory)]
        for name in names:
            args.extend(("--pattern", name))
        run(*args)


def merge_evidence(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if not path.is_file():
            continue
        target = destination / path.name
        if target.exists() and sha256(target) != sha256(path):
            raise ValueError(f"immutable retained evidence differs: {path.name}")
        if not target.exists():
            shutil.copyfile(path, target)


def producer_artifacts(
    tag: str,
    commit: str,
    prefixes: tuple[str, ...],
    destination: Path,
    *,
    package: dict | None = None,
) -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    pages = json.loads(
        run(
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/actions/workflows/release-please.yml/runs?head_sha={commit}&per_page=100",
        )
    )
    for page in pages:
        for producer in page["workflow_runs"]:
            if (
                producer["head_sha"] != commit
                or producer["head_branch"] != tag
                or producer["event"] != "workflow_dispatch"
            ):
                continue
            artifacts = json.loads(
                run(
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    f"repos/{repository}/actions/runs/{producer['id']}/artifacts?per_page=100",
                )
            )
            for batch in artifacts:
                for artifact in batch["artifacts"]:
                    if artifact["expired"] or not artifact["name"].startswith(prefixes):
                        continue
                    if package is not None:
                        if artifact["name"] != "python-distributions":
                            continue
                        jobs = json.loads(
                            run(
                                "gh",
                                "api",
                                "--paginate",
                                "--slurp",
                                f"repos/{repository}/actions/runs/{producer['id']}/jobs?filter=all&per_page=100",
                            )
                        )
                        # GitHub's closed step conclusions distinguish attempted publication
                        # from dry runs, including old runs with identical artifact names.
                        publication = any(
                            job["name"] == "build-artifacts"
                            and job["started_at"] <= artifact["created_at"] <= job["completed_at"]
                            and any(
                                step["name"] == "Upload missing artifacts to the GitHub release"
                                and step["conclusion"] in ("success", "failure", "cancelled")
                                for step in job["steps"]
                            )
                            for page in jobs
                            for job in page["jobs"]
                            if job.get("started_at") and job.get("completed_at")
                        )
                        if not publication:
                            continue
                    with tempfile.TemporaryDirectory() as temporary:
                        archive = Path(temporary) / "artifact.zip"
                        source = Path(temporary) / "files"
                        # Download the selected immutable artifact ID; a rerun may reuse its name.
                        with archive.open("wb") as output:
                            subprocess.run(
                                [
                                    "gh",
                                    "api",
                                    f"repos/{repository}/actions/artifacts/{artifact['id']}/zip",
                                ],
                                stdout=output,
                                check=True,
                            )
                        shutil.unpack_archive(archive, source, format="zip")
                        if package is not None:
                            files = [
                                p
                                for p in source.iterdir()
                                if distribution_kind(p.name, package["name"], package["version"])
                            ]
                            if not distributions_complete(
                                [p.name for p in files], package["name"], package["version"]
                            ):
                                continue
                            if any(
                                (destination / p.name).exists()
                                and sha256(destination / p.name) != sha256(p)
                                for p in files
                            ):
                                continue
                            for path in files:
                                if not (destination / path.name).exists():
                                    shutil.copyfile(path, destination / path.name)
                            return
                        merge_evidence(source, destination)


def prepare_distributions(tag: str, directory: Path, *, dry_run: bool) -> None:
    package = project(tag)
    version = package["version"]
    if Version(version) != Version(tag.removeprefix("v")):
        raise ValueError("package version differs from release tag")
    directory.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        assets = release_assets(tag)
        names = [
            a["name"] for a in assets if distribution_kind(a["name"], package["name"], version)
        ]
        download_release(tag, names, directory)
        for row in pypi_files(package["name"], version):
            if not distribution_kind(row["filename"], package["name"], version):
                continue
            target = directory / row["filename"]
            if not target.exists():
                urllib.request.urlretrieve(row["url"], target)
            if sha256(target) != row["digests"]["sha256"]:
                raise ValueError(f"immutable distribution differs: {target.name}")
        if not distributions_complete(
            [p.name for p in directory.iterdir()], package["name"], version
        ):
            producer_artifacts(
                tag,
                run("git", "rev-parse", f"{tag}^{{commit}}"),
                ("python-distributions",),
                directory,
                package=package,
            )
        if any(directory.iterdir()) and not distributions_complete(
            [p.name for p in directory.iterdir()], package["name"], version
        ):
            raise ValueError(
                "original publication distributions are incomplete; recover the producer artifact"
            )
    kinds = {distribution_kind(p.name, package["name"], version) for p in directory.iterdir()}
    # uv owns the actual wheel/sdist filenames; never infer Python build tags.
    for kind in ("wheel", "sdist"):
        if kind not in kinds:
            run("uv", "build", f"--{kind}", "--out-dir", str(directory))
    files = [p for p in directory.iterdir() if distribution_kind(p.name, package["name"], version)]
    if not distributions_complete([p.name for p in files], package["name"], version):
        raise ValueError("build did not produce both release distributions")
    if not dry_run:
        missing_distributions(files, pypi_files(package["name"], version))


def stage_cloud(tag: str, directory: Path) -> None:
    for target, (source, digest) in cloud_files(tag).items():
        if sha256(Path(source)) != digest:
            raise ValueError(f"Cloud archive differs from committed metadata: {source}")
        shutil.copyfile(source, directory / target)


def upload_missing(tag: str, directory: Path) -> None:
    assets = {a["name"]: a for a in release_assets(tag)}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.name in assets:
            with tempfile.TemporaryDirectory() as temporary:
                download_release(tag, [path.name], Path(temporary))
                if sha256(Path(temporary) / path.name) != sha256(path):
                    raise ValueError(f"immutable release asset differs: {path.name}")
        else:
            run("gh", "release", "upload", tag, str(path))


def asset_status(tag: str) -> dict:
    package = project(tag)
    assets = release_assets(tag)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        expected_cloud = cloud_files(tag)
        names = [
            a["name"]
            for a in assets
            if a["name"] in expected_cloud
            or distribution_kind(a["name"], package["name"], package["version"])
        ]
        download_release(tag, names, directory)
        distributions = [
            p
            for p in directory.iterdir()
            if distribution_kind(p.name, package["name"], package["version"])
        ]
        complete = distributions_complete(
            [p.name for p in distributions], package["name"], package["version"]
        )
        cloud_complete = all(
            (directory / name).is_file() and sha256(directory / name) == digest
            for name, (_, digest) in expected_cloud.items()
        )
        try:
            urls = pypi_files(package["name"], package["version"])
            pypi_complete = complete and not missing_distributions(distributions, urls)
            pypi_state = "present" if pypi_complete else "missing"
        except (OSError, ValueError) as error:
            pypi_state = f"unknown ({error})"
        return {
            "assets": "present" if complete and cloud_complete else "missing",
            "pypi": pypi_state,
        }


def image_digest(reference: str) -> str | None:
    repository, tag = reference.removeprefix("ghcr.io/").rsplit(":", 1)
    credentials = base64.b64encode(
        f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()
    ).decode()
    request = urllib.request.Request(
        f"https://ghcr.io/token?scope=repository:{repository}:pull",
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["token"]
    # OCI and Docker define this closed set of manifest media types.
    media_types = (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{repository}/manifests/{tag}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": ",".join(media_types)},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            digest = response.headers["Docker-Content-Digest"]
            candidate_tool._digest(digest, label="registry manifest digest")
            return digest
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def verify_build_target(image: str, name: str) -> None:
    inspected = json.loads(
        run(
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            '{"images":{{json .Image}},"provenance":{{json .Provenance}}}',
        )
    )
    images, provenance = inspected["images"], inspected["provenance"]
    # Buildx exposes one SLSA object for a single platform, or a platform-keyed map.
    if isinstance(images, dict) and "architecture" in images and "os" in images:
        proofs = [provenance]
    elif (
        isinstance(images, dict)
        and images
        and isinstance(provenance, dict)
        and images.keys() == provenance.keys()
    ):
        proofs = list(provenance.values())
    else:
        raise ValueError(f"BuildKit provenance is missing an image platform: {image}")
    # cellctl's Dockerfile has an unnamed final stage; all other release targets are named.
    expected = "" if name == "cellctl" else name
    for proof in proofs:
        if not isinstance(proof, dict) or not isinstance(proof.get("SLSA"), dict):
            raise ValueError(f"BuildKit target provenance is unavailable: {image}")
        parameters = proof["SLSA"].get("invocation", {}).get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError(f"BuildKit target parameters are unavailable: {image}")
        arguments = parameters.get("args")
        # BuildKit omits an empty argument map when the unnamed final stage is selected.
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict) or arguments.get("target", "") != expected:
            raise ValueError(f"BuildKit target differs from {name}: {image}")


def verify_image_bundle(bundle: Path, tag: str, commit: str, repository: str, name: str) -> str:
    envelope = json.loads(bundle.read_text())["dsseEnvelope"]
    statement = json.loads(base64.b64decode(envelope["payload"]))
    subject = statement["subject"][0]
    digest = "sha256:" + subject["digest"]["sha256"]
    target = f"{repository}@{digest}"
    verified = json.loads(
        run(
            "gh",
            "attestation",
            "verify",
            f"oci://{target}",
            "--bundle",
            str(bundle),
            "--repo",
            os.environ["GITHUB_REPOSITORY"],
            "--signer-workflow",
            f"{os.environ['GITHUB_REPOSITORY']}/.github/workflows/release-please.yml",
            "--signer-digest",
            commit,
            "--source-digest",
            commit,
            "--source-ref",
            f"refs/tags/{tag}",
            "--deny-self-hosted-runners",
            "--predicate-type",
            candidate_tool.SLSA_PREDICATE,
            "--format",
            "json",
        )
    )
    for proof in candidate_tool._verified_statements(verified):
        candidate_tool._require_verified_subject(proof, repository, digest, label="release image")
        invocation = urlsplit(proof["predicate"]["runDetails"]["metadata"]["invocationId"])
        prefix = f"/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
        # The GitHub SLSA invocation URL is a fixed protocol, not a filename claim.
        if (
            invocation.scheme != "https"
            or invocation.netloc != "github.com"
            or not invocation.path.startswith(prefix)
            or invocation.query
            or invocation.fragment
        ):
            raise ValueError("signed producer invocation is not a repository Actions run")
        parts = invocation.path.removeprefix(prefix).split("/")
        if len(parts) != 3 or parts[1] != "attempts":
            raise ValueError("signed producer invocation has no exact attempt")
        run_id = candidate_tool._run_number(parts[0], label="signed producer run")
        attempt = candidate_tool._run_number(parts[2], label="signed producer attempt")
        expected = f"exomem-image-{name}-{commit}-{digest.removeprefix('sha256:')}-{run_id}-{attempt}.sigstore.json"
        if bundle.name != expected:
            raise ValueError("receipt locator differs from the signed producer invocation")
    verify_build_target(target, name)
    return digest


def image_evidence(tag: str, commit: str, directory: Path) -> None:
    # Prefixes identify the existing hosted triplet and image Sigstore receipts.
    names = [
        a["name"]
        for a in release_assets(tag)
        if a["name"].startswith(("exomem-hosted-runtime-", "exomem-image-"))
    ]
    download_release(tag, names, directory)
    producer_artifacts(
        tag, commit, ("hosted-runtime-candidate-", "release-image-evidence-"), directory
    )


def recovered_image(
    name: str, repository: str, tag: str, commit: str, directory: Path
) -> tuple[str | None, list[Path]]:
    found: dict[str, list[Path]] = {}
    if name == "hosted":
        for path in directory.glob("*.candidate-v1.json"):
            candidate = candidate_tool.load_candidate(path)
            if candidate["source"]["commit"] != commit or candidate["release"]["tag"] != tag:
                continue
            stem = path.name.removesuffix(".candidate-v1.json")
            bundle = directory / f"{stem}.sigstore.json"
            signature = directory / f"{stem}.candidate.sigstore.json"
            if not bundle.is_file() or not signature.is_file():
                continue
            runtime_candidate_assets(
                [
                    {"name": item.name, "size": item.stat().st_size}
                    for item in (path, bundle, signature)
                ]
            )
            candidate_tool.verify_candidate(path, bundle=bundle, candidate_bundle=signature)
            verify_build_target(candidate["image"]["reference"], name)
            found[candidate["image"]["digest"]] = [path, bundle, signature]
    else:
        for path in directory.glob(f"exomem-image-{name}-{commit}-*.sigstore.json"):
            digest = verify_image_bundle(path, tag, commit, repository, name)
            found[digest] = [path]
    if len(found) > 1:
        # Maintainers resolve conflicting producer receipts; guessing could retag an immutable release.
        raise ValueError(f"multiple verified digests for {name}; release remains incomplete")
    return next(iter(found.items()), (None, []))


def inspect_images(tag: str, images: dict, directory: Path, *, dry_run: bool) -> dict:
    if dry_run:
        return {
            name: {**entry, "publish": True, "state": "dry run"} for name, entry in images.items()
        }
    commit = run("git", "rev-parse", f"{tag}^{{commit}}")
    image_evidence(tag, commit, directory)
    for name, entry in images.items():
        repository = entry["ref"].rsplit(":", 1)[0]
        digest, files = recovered_image(name, repository, tag, commit, directory)
        aliases = {reference: image_digest(reference) for reference in entry["immutable_tags"]}
        entry.update(digest=digest, evidence=[str(p) for p in files], publish=False)
        if digest:
            if any(existing and existing != digest for existing in aliases.values()):
                raise ValueError(
                    f"immutable discovery tag differs from original evidence: {entry['ref']}"
                )
            complete = all(existing == digest for existing in aliases.values())
            if (
                entry.get("floating_tag")
                and run(
                    "gh",
                    "api",
                    f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/latest",
                    "--jq",
                    ".tag_name",
                )
                == tag
            ):
                complete = complete and image_digest(entry["floating_tag"]) == digest
            assets = {a["name"] for a in release_assets(tag)}
            entry["state"] = (
                "present" if complete and all(p.name in assets for p in files) else "missing"
            )
            requested = os.environ.get("RECORDS_TARGET")
            if name == "hosted" and requested:
                candidate = candidate_tool.load_candidate(files[0])
                claim = candidate.get("recordsCompatibility", {})
                if claim.get("runtimeTarget") != json.loads(requested):
                    entry["state"] = "incomplete: requested rollback claim needs a new release"
                    entry["blocked"] = True
        elif any(aliases.values()):
            entry["state"] = "incomplete: immutable tag has no recoverable producer evidence"
        else:
            # An incomplete hosted receipt proves a prior push; never replace its lost proof with a rebuild.
            partial = name == "hosted" and any(directory.glob("*.candidate-v1.json"))
            entry["state"] = "incomplete: original evidence is partial" if partial else "missing"
            entry["publish"] = not partial
    return images


def finish_images(tag: str, images: dict, directory: Path) -> None:
    commit = run("git", "rev-parse", f"{tag}^{{commit}}")
    incomplete = []
    recovered = {}
    aliases = {}
    # Check every image and alias before the first upload or tagging write.
    for name, entry in images.items():
        repository = entry["ref"].rsplit(":", 1)[0]
        digest, files = recovered_image(name, repository, tag, commit, directory)
        aliases[name] = {
            reference: image_digest(reference) for reference in entry["immutable_tags"]
        }
        if digest and any(existing and existing != digest for existing in aliases[name].values()):
            raise ValueError(
                f"immutable discovery tag differs from original evidence: {entry['ref']}"
            )
        if entry.get("blocked") or not digest:
            incomplete.append(name)
        if digest:
            recovered[name] = (digest, files)
    for name, (digest, files) in recovered.items():
        entry = images[name]
        repository = entry["ref"].rsplit(":", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            for path in files:
                shutil.copyfile(path, Path(temporary) / path.name)
            upload_missing(tag, Path(temporary))
        for reference in entry["immutable_tags"]:
            if aliases[name][reference] is None:
                run(
                    "docker",
                    "buildx",
                    "imagetools",
                    "create",
                    "--prefer-index=false",
                    "--tag",
                    reference,
                    f"{repository}@{digest}",
                )
            if image_digest(reference) != digest:
                raise ValueError(f"discovery tag digest differs: {reference}")
        if (
            entry.get("floating_tag")
            and run(
                "gh",
                "api",
                f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/latest",
                "--jq",
                ".tag_name",
            )
            == tag
        ):
            reference = entry["floating_tag"]
            run(
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--prefer-index=false",
                "--tag",
                reference,
                f"{repository}@{digest}",
            )
            if image_digest(reference) != digest:
                raise ValueError(f"floating tag digest differs: {reference}")
    if incomplete:
        raise ValueError(
            f"release incomplete; original producer evidence unavailable: {', '.join(incomplete)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Commands are this CLI's closed protocol, not authored vocabulary.
    parser.add_argument(
        "command",
        choices=("assets", "distributions", "cloud", "upload", "pypi", "images", "finish-images"),
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.command == "assets":
        print(
            json.dumps(
                {"assets": "dry run", "pypi": "dry run"} if args.dry_run else asset_status(args.tag)
            )
        )
    elif args.command == "distributions":
        prepare_distributions(args.tag, args.directory, dry_run=args.dry_run)
    elif args.command == "cloud":
        stage_cloud(args.tag, args.directory)
    elif args.command == "upload":
        upload_missing(args.tag, args.directory)
    elif args.command == "pypi":
        package = project(args.tag)
        files = list(args.directory.iterdir())
        missing = missing_distributions(files, pypi_files(package["name"], package["version"]))
        destination = args.directory / "missing"
        destination.mkdir()
        for path in missing:
            shutil.copyfile(path, destination / path.name)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"publish={'true' if missing else 'false'}\n")
    elif args.command == "images":
        print(
            json.dumps(
                inspect_images(
                    args.tag, json.loads(os.environ["IMAGES"]), args.directory, dry_run=args.dry_run
                )
            )
        )
    else:
        finish_images(args.tag, json.loads(os.environ["IMAGES"]), args.directory)


if __name__ == "__main__":
    main()
