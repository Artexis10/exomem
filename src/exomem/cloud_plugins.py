"""Build and check the public Exomem Cloud provider packages."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path

import jsonschema

from . import package_skills, workflow_skills

_SCHEMA_BASE = "https://agent-plugins.org/schemas/1.0.0/"


def _definition(root: Path) -> dict:
    return json.loads((root / "plugins/cloud/definition.json").read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_identity(root: Path) -> dict:
    root = Path(root)
    definition = _definition(root)
    corpus = root / "plugins/cloud/evals/cases.json"
    return {
        "version": tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ],
        "skill_contract": workflow_skills.skill_contract(),
        "corpus_digest": _sha256(corpus.read_bytes()),
        "profile": definition["profile"],
        "resource": definition["resource"],
    }


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _zip_tree(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )


def _validate_openai(root: Path, package: Path) -> None:
    for stem, filename in (("plugin", "plugin.json"), ("mcp", "mcp.json")):
        schema = json.loads(
            (root / "plugins/cloud/schemas" / f"{stem}.schema.json").read_text(encoding="utf-8")
        )
        value = json.loads((package / filename).read_text(encoding="utf-8"))
        jsonschema.validate(value, schema)


def build_packages(root: Path, output: Path | None = None) -> dict:
    root = Path(root)
    definition = _definition(root)
    identity = release_identity(root)
    workflow_skills.validate_skill_contract()
    destination = Path(output) if output is not None else root / "plugins/cloud/generated"
    destination.mkdir(parents=True, exist_ok=True)
    for provider in ("claude", "openai"):
        target = destination / provider
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        skills = target / "skills"
        for name in ("exomem", *(str(item["name"]) for item in workflow_skills.list_skills())):
            for relative, content in package_skills.skill_payload(name).items():
                path = skills / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
        assets = target / "assets"
        assets.mkdir()
        shutil.copyfile(root / "plugins/cloud/assets/icon.svg", assets / "icon.svg")
        shutil.copyfile(root / "LICENSE", target / "LICENSE")
        readme = (
            "# Exomem Cloud\n\n"
            "Exomem Cloud gives assistants governed long-term memory across conversations. "
            "It keeps original sources and evidence separate from compiled conclusions, "
            "and connects related knowledge without treating a summary as its source. "
            "The bundled skills explain recall, continuity, capture and review. "
            "Connect the remote MCP server through your provider's authorization flow. "
            "The live bootstrap response determines available tools and current policy. "
            "Publisher: Substrate Systems OÜ. Support: " + definition["support"] + "\n"
        )
        (target / "README.md").write_text(readme, encoding="utf-8", newline="\n")
        if provider == "claude":
            manifest = {
                "name": definition["name"],
                "description": definition["description"],
                "version": identity["version"],
                "author": definition["author"],
                "homepage": definition["homepage"],
                "repository": definition["repository"],
                "privacyPolicyUrl": definition["privacy"],
                "license": definition["license"],
                "keywords": definition["keywords"],
            }
            path = target / ".claude-plugin/plugin.json"
            path.parent.mkdir()
            path.write_bytes(_json_bytes(manifest))
            (target / ".mcp.json").write_bytes(
                _json_bytes(
                    {"mcpServers": {"exomem": {"type": "http", "url": definition["resource"]}}}
                )
            )
        else:
            from .cloud_plugin_evals import directory_cases

            shutil.copyfile(root / "plugins/cloud/assets/icon.png", assets / "icon.png")
            interface = {
                "displayName": definition["display_name"],
                "shortDescription": "Memory across conversations",
                "longDescription": definition["description"],
                "developerName": definition["author"]["name"],
                "category": "Productivity",
                "capabilities": ["Read", "Write"],
                "websiteURL": definition["homepage"],
                "supportURL": definition["support"],
                "privacyPolicyURL": definition["privacy"],
                "termsOfServiceURL": definition["terms"],
                "composerIcon": "./assets/icon.png",
                "logo": "./assets/icon.png",
            }
            manifest = {
                "$schema": _SCHEMA_BASE + "plugin.schema.json",
                "name": definition["name"],
                "version": identity["version"],
                "description": definition["description"],
                "author": definition["author"],
                "homepage": definition["homepage"],
                "repository": definition["repository"],
                "license": definition["license"],
                "keywords": definition["keywords"],
                "extensions": {
                    "com.openai": {
                        "interface": interface,
                        "review": {
                            "test_cases": directory_cases(root),
                            "commerce": False,
                            "commerce_description": (
                                "Connects an existing account. No sales, checkout, "
                                "subscription initiation or upgrade promotion in the plugin."
                            ),
                        },
                        "publication": {"countries": []},
                    }
                },
            }
            (target / "plugin.json").write_bytes(_json_bytes(manifest))
            (target / "mcp.json").write_bytes(
                _json_bytes(
                    {
                        "$schema": _SCHEMA_BASE + "mcp.schema.json",
                        "mcpServers": {
                            "exomem": {"type": "streamable-http", "url": definition["resource"]}
                        },
                    }
                )
            )
            _validate_openai(root, target)
        _zip_tree(target, destination / f"{provider}.zip")
    files = {
        path.relative_to(destination).as_posix(): _sha256(path.read_bytes())
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path.name != "release.json"
    }
    release = {**identity, "files": files}
    (destination / "release.json").write_bytes(_json_bytes(release))
    return release


def check_packages(root: Path, output: Path | None = None) -> dict:
    root = Path(root)
    actual = Path(output) if output is not None else root / "plugins/cloud/generated"
    issues: list[str] = []
    with tempfile.TemporaryDirectory(prefix="exomem-cloud-check-") as temporary:
        expected = Path(temporary)
        try:
            build_packages(root, expected)
        except FileNotFoundError as error:
            name = Path(error.filename).name if error.filename else "build resource"
            return {"ok": False, "issues": [f"missing input: {name}"]}
        actual_entries = list(actual.rglob("*")) if actual.exists() else []
        expected_directories = {
            path.relative_to(expected).as_posix() for path in expected.rglob("*") if path.is_dir()
        }
        for path in actual_entries:
            name = path.relative_to(actual).as_posix()
            if path.is_symlink():
                issues.append(f"symlink: {name}")
            elif path.is_dir() and name not in expected_directories:
                issues.append(f"extra: {name}/")
        actual_files = {
            path.relative_to(actual).as_posix(): path
            for path in actual_entries
            if path.is_file() and not path.is_symlink()
        }
        expected_files = {
            path.relative_to(expected).as_posix(): path
            for path in expected.rglob("*")
            if path.is_file()
        }
        for name in sorted(actual_files.keys() | expected_files.keys()):
            if name not in actual_files:
                issues.append(f"missing: {name}")
            elif name not in expected_files:
                issues.append(f"extra: {name}")
            elif actual_files[name].read_bytes() != expected_files[name].read_bytes():
                issues.append(f"drift: {name}")
    return {"ok": not issues, "issues": issues}
