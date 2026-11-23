#!/usr/bin/env python3
"""List the shared libraries in a container image, with their licences.

Two commands, each over one platform of one image:

  collect  Read an image (a registry reference or a `docker save` archive) and write
           one JSON record: the operating-system packages with their licences
           (trivy), and the shared libraries that Python wheels bundle.
  render   Turn the records of several images into the Markdown record in
           docs/licensing/image-shared-libraries.md.

Why this exists: trivy reads each Python distribution's declared licence and each
operating-system package, but it does not see a library that a wheel bundles
(`*.libs/`, a vendored `libpdfium.so`). `collect` finds those in the image layers,
without a pull into the local Docker daemon: a registry image streams layer by layer
through `oras`. `scripts/image-licences/library-upstreams.yaml` names the upstream
project and licence of each bundled library, and `render` marks a library that file
does not name as UNMAPPED, so a new bundled library cannot enter unrecorded.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime
import io
import json
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from email.parser import Parser
from pathlib import Path

import yaml

# A shared object is named by the soname convention: `.so`, then dotted version numbers.
SHARED_OBJECT = re.compile(r"\.so(\.\d+)*$")
# A Python extension module carries an interpreter-defined suffix (PEP 3149, PEP 384).
EXTENSION_MODULE = re.compile(r"\.(cpython-\d+[a-z]*-[a-z0-9_-]+|abi3)\.so$")
# auditwheel appends `-<8 hex digits>` to the name of each library it vendors.
VENDOR_HASH = re.compile(r"(-[0-9a-f]{8})+$")
# `<site-packages>/<name>-<version>.dist-info/<file>`: the wheel installation layout.
DIST_INFO = re.compile(r"^(?P<root>.*site-packages)/(?P<dir>[^/]+\.dist-info)/(?P<rest>.+)$")
# The files that the wheel format itself defines in a `.dist-info` directory. The build
# tool puts every other file there, apart from the SBOM directory: licence and notice
# files, and sometimes its own files such as `zip-safe`.
WHEEL_FORMAT_FILES = {
    "METADATA", "RECORD", "WHEEL", "INSTALLER", "REQUESTED",
    "entry_points.txt", "top_level.txt", "direct_url.json",
}
# The closed set of classes that library-upstreams.yaml may use.
CLASSES = (
    "permissive", "public-domain", "weak-copyleft", "linking-exception",
    "strong-copyleft", "proprietary",
)
TRIVY_OS_ARGS = ["--scanners", "license", "--pkg-types", "os", "--list-all-pkgs"]
# The licence classes (`forbidden` = GPL and AGPL, `restricted` = LGPL) live in the image
# licence check's trivy settings, so the operating-system layer is judged by the same lists.
TRIVY_SETTINGS = Path(__file__).parent / "image-licences" / "trivy.yaml"


def stem(file_name: str) -> str:
    """`libyajl-9393e5bd.so.2.1.1` -> `libyajl`."""
    return VENDOR_HASH.sub("", SHARED_OBJECT.sub("", file_name))


def normal_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503 name normalisation


def dpkg_path(path: str) -> str:
    """dpkg lists `/lib/...` where a merged-usr image stores `usr/lib/...`."""
    path = path.lstrip("/")
    return path[4:] if path.startswith("usr/") else path


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


# --- reading an image ----------------------------------------------------------------


class Source:
    """The manifest, config and layer streams of one single-platform image."""

    def __init__(self, registry_manifest: str | None, archive: str | None):
        self.registry_manifest = registry_manifest
        self.archive = tarfile.open(archive) if archive else None
        if self.archive:
            entry = json.load(self.archive.extractfile("manifest.json"))[0]
            self.layers = entry["Layers"]
            self.config = json.load(self.archive.extractfile(entry["Config"]))
            self.image_id = "sha256:" + posixpath.basename(entry["Config"])
        else:
            repo = registry_manifest.split("@")[0]
            manifest = json.loads(run(["oras", "manifest", "fetch", registry_manifest]).stdout)
            self.layers = [f"{repo}@{layer['digest']}" for layer in manifest["layers"]]
            self.config = json.loads(
                run(["oras", "blob", "fetch", f"{repo}@{manifest['config']['digest']}",
                     "--output", "-"]).stdout
            )

    @contextlib.contextmanager
    def open_layer(self, layer: str):
        if self.archive:
            yield self.archive.extractfile(layer)
            return
        process = subprocess.Popen(
            ["oras", "blob", "fetch", layer, "--output", "-"], stdout=subprocess.PIPE
        )
        try:
            yield process.stdout
            while process.stdout.read(1 << 20):  # drain the trailing padding
                pass
        finally:
            process.stdout.close()
            if process.wait() != 0:
                raise RuntimeError(f"oras could not fetch layer {layer}")


class Overlay:
    """The files of the image's final filesystem that the inventory needs.

    Layers apply in order. A later layer replaces a path, and a whiteout entry removes
    it, so each tracked table follows that rule.
    """

    def __init__(self) -> None:
        self.shared_objects: set[str] = set()
        self.dist_info_files: set[str] = set()
        self.metadata: dict[str, str] = {}  # `<dist-info dir>` -> METADATA text
        # `<dist-info dir>` -> the shared objects its RECORD lists, and the files it lists
        # at the top of each package directory (`<package>/LICENSE`)
        self.records: dict[str, tuple[set[str], set[str]]] = {}
        self.dpkg_lists: dict[str, set[str]] = {}  # list file -> shared objects it lists
        self.dpkg_status: str | None = None

    def forget(self, path: str, *, contents_only: bool = False) -> None:
        """Remove what the tracked tables hold at `path` and below it."""
        for table in (self.shared_objects, self.dist_info_files, self.metadata,
                      self.records, self.dpkg_lists):
            for key in [k for k in table if k.startswith(path + "/") or (k == path and not contents_only)]:
                if isinstance(table, set):
                    table.discard(key)
                else:
                    del table[key]
        if self.dpkg_status is not None and (
                path == "var/lib/dpkg/status" or "var/lib/dpkg/status".startswith(path + "/")):
            self.dpkg_status = None

    def apply(self, layer: tarfile.TarFile) -> None:
        for member in layer:
            name = posixpath.normpath(member.name)
            parent, base = posixpath.split(name)
            if base == ".wh..wh..opq":  # the directory's earlier content is gone
                self.forget(parent, contents_only=True)
                continue
            if base.startswith(".wh."):
                self.forget(posixpath.join(parent, base[4:]))
                continue
            if not member.isdir():  # a file or link replaces whatever the path held
                self.forget(name)
            if not (member.isfile() or member.islnk()):
                continue
            # A hard link is another name for a file, so a hard-linked library counts. Its
            # content is not in the layer stream: the files whose text is read must be plain.
            if SHARED_OBJECT.search(base):
                self.shared_objects.add(name)
            match = DIST_INFO.match(name)
            if match:
                directory = f"{match['root']}/{match['dir']}"
                self.dist_info_files.add(name)
                if match["rest"] == "METADATA":
                    self.metadata[directory] = self.text(layer, member)
                elif match["rest"] == "RECORD":
                    self.records[directory] = self.record_files(match["root"], self.text(layer, member))
            elif name == "var/lib/dpkg/status":
                self.dpkg_status = self.text(layer, member)
            elif name.startswith("var/lib/dpkg/info/") and name.endswith(".list"):
                paths = self.text(layer, member).splitlines()
                self.dpkg_lists[name] = {dpkg_path(p) for p in paths if SHARED_OBJECT.search(p)}

    @staticmethod
    def text(layer: tarfile.TarFile, member: tarfile.TarInfo) -> str:
        if member.islnk():
            raise SystemExit(f"{member.name} is a hard link to {member.linkname}: its text is not in the layer")
        return layer.extractfile(member).read().decode("utf-8", "replace")

    @staticmethod
    def record_files(root: str, record: str) -> tuple[set[str], set[str]]:
        """The shared objects and the package-top files that a wheel's RECORD lists."""
        objects, package_files = set(), set()
        for row in csv.reader(io.StringIO(record)):  # RECORD is CSV: `path,hash,size`
            if not row:
                continue
            path = posixpath.normpath(row[0])
            if SHARED_OBJECT.search(path):
                objects.add(posixpath.normpath(f"{root}/{path}"))
            parts = path.split("/")
            # `<package>/<file>`: not a `../` script path, `.dist-info` (read separately) or `.libs`
            if len(parts) == 2 and not parts[0].startswith("..") and not parts[0].endswith((".dist-info", ".libs")):
                package_files.add(path)
        return objects, package_files


def dpkg_packages(status: str | None) -> list[dict]:
    packages = []
    for paragraph in (status or "").split("\n\n"):
        fields = Parser().parsestr(paragraph, headersonly=True)  # deb822 is header syntax
        if fields["Package"] and (fields["Status"] or "").endswith(" installed"):
            packages.append({
                "name": fields["Package"], "version": fields["Version"],
                "section": fields["Section"] or "", "architecture": fields["Architecture"],
            })
    return sorted(packages, key=lambda p: p["name"])


def licence_fields(metadata: str) -> dict:
    headers = Parser().parsestr(metadata, headersonly=True)
    free_text = (headers.get("License") or "").strip().splitlines()
    return {
        "name": headers.get("Name"), "version": headers.get("Version"),
        "expression": headers.get("License-Expression"),
        "free_text": free_text[0][:120] if free_text else None,
        "classifiers": [c for c in headers.get_all("Classifier") or [] if c.startswith("License ::")],
    }


def inventory(source: Source) -> dict:
    overlay = Overlay()
    for layer_name in source.layers:
        with source.open_layer(layer_name) as stream, tarfile.open(fileobj=stream, mode="r|*") as layer:
            overlay.apply(layer)

    owner = {path: directory for directory, (objects, _) in overlay.records.items() for path in objects}
    dpkg_owned = {path for paths in overlay.dpkg_lists.values() for path in paths}
    wheels: dict[str, dict] = {}
    unowned, other_libraries = [], []
    other_modules: dict[str, int] = defaultdict(int)
    operating_system_objects = 0
    for path in sorted(overlay.shared_objects):
        base = posixpath.basename(path)
        directory = owner.get(path)
        if directory:
            wheel = wheels.setdefault(directory, {"modules": 0, "libraries": []})
            if EXTENSION_MODULE.search(base):
                wheel["modules"] += 1
            else:
                wheel["libraries"].append({"path": path, "stem": stem(base)})
        elif "/site-packages/" in path:
            unowned.append({"path": path})
        elif dpkg_path(path) in dpkg_owned:
            operating_system_objects += 1
        elif EXTENSION_MODULE.search(base):
            other_modules[posixpath.dirname(path)] += 1
        else:
            other_libraries.append({"path": path, "stem": stem(base)})

    records = []
    for directory, wheel in sorted(wheels.items()):
        fields = licence_fields(overlay.metadata.get(directory, ""))
        prefix = directory + "/"
        files = sorted(
            relative for path in overlay.dist_info_files if path.startswith(prefix)
            for relative in [path[len(prefix):]]
            if relative not in WHEEL_FORMAT_FILES and not relative.startswith("sboms/")
        )
        records.append({**fields, "licence_files": files,
                        "package_files": sorted(overlay.records[directory][1]), **wheel})

    env = dict(e.split("=", 1) for e in source.config["config"].get("Env", []))
    return {
        "image": {
            "architecture": source.config["architecture"], "os": source.config["os"],
            "created": source.config.get("created"),
            "labels": source.config["config"].get("Labels") or {},
            "variant": env.get("EXOMEM_CONTAINER_VARIANT"),
        },
        "layers": len(source.layers),
        "python_distributions": len(overlay.metadata),
        "wheels": records,
        "unowned": unowned,
        "other_libraries": other_libraries,
        "other_extension_modules": dict(sorted(other_modules.items())),
        "operating_system_objects": operating_system_objects,
        "dpkg": dpkg_packages(overlay.dpkg_status),
    }


# --- collect -------------------------------------------------------------------------


def collect(args: argparse.Namespace) -> None:
    trivy_version = run(["trivy", "--version"]).stdout.splitlines()[0].removeprefix("Version: ")
    classes = Path(args.trivy_cache) / "licence-classes.yaml"
    classes.write_text(yaml.safe_dump(
        {"license": yaml.safe_load(TRIVY_SETTINGS.read_text(encoding="utf-8"))["license"]}))
    trivy = ["trivy", "image", "--skip-version-check", "--quiet", "--format", "json",
             "--config", str(classes), "--cache-dir", args.trivy_cache, *TRIVY_OS_ARGS]
    meta = {"id": args.id, "tag": args.tag, "source": args.source, "platform": args.platform,
            "scan_date": datetime.date.today().isoformat(), "trivy": trivy_version,
            "arguments": ["--id", args.id, "--tag", args.tag, "--source", args.source,
                          "--platform", args.platform,
                          *(["--archive", "IMAGE.tar"] if args.archive else ["--registry", args.registry])]}
    if args.archive:
        trivy += ["--input", args.archive]
        source = Source(None, args.archive)
        meta["image_id"] = source.image_id
    else:
        repo, index_digest = args.registry.split("@")
        index = json.loads(run(["oras", "manifest", "fetch", args.registry]).stdout)
        architecture = args.platform.split("/", 1)[1]
        chosen = [m for m in index["manifests"]
                  if m.get("platform", {}).get("architecture") == architecture
                  and m["platform"].get("os") == "linux"]
        if len(chosen) != 1:
            raise SystemExit(f"{args.registry} has no single {args.platform} manifest")
        meta |= {"reference": repo, "index_digest": index_digest,
                 "manifest_digest": chosen[0]["digest"],
                 "oras": run(["oras", "version"]).stdout.split()[1]}
        source = Source(f"{repo}@{chosen[0]['digest']}", None)
        trivy += ["--image-src", "remote", "--platform", args.platform, args.registry]
    report = json.loads(run(trivy).stdout)
    category = {
        (found["PkgName"], found["Name"]): found["Category"]
        for result in report.get("Results", []) for found in result.get("Licenses") or []
    }
    packages = [
        {"name": p["Name"], "version": p["Version"],
         "licences": [{"name": name, "category": category.get((p["Name"], name), "unknown")}
                      for name in p.get("Licenses") or []]}
        for result in report.get("Results", []) for p in result.get("Packages") or []
    ]
    operating_system = (report.get("Metadata") or {}).get("OS") or {}
    record = {"meta": meta,
              "os": {"family": operating_system.get("Family"), "name": operating_system.get("Name"),
                     "packages": sorted(packages, key=lambda p: p["name"])},
              "inventory": inventory(source)}
    Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# --- render --------------------------------------------------------------------------


def load_upstreams(path: Path) -> tuple[dict, dict, dict]:
    """The wheels by normalised distribution name, the other libraries and the
    operating-system packages."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = [e for wheel in data["wheels"].values() for e in wheel["libraries"].values()]
    for entry in [*entries, *data["other_libraries"], *data["operating_system"]]:
        if entry["class"] not in CLASSES:
            raise SystemExit(f"{path}: {entry}: class must be one of {CLASSES}")
        # `OR` is the SPDX operator for a choice, and the record names the licence elected.
        if " OR " in entry["licence"] and "elected" not in entry:
            raise SystemExit(f"{path}: {entry}: a licence offered as a choice needs `elected`")
    return (
        {normal_distribution(name): wheel for name, wheel in data["wheels"].items()},
        {e["stem"]: e for e in data["other_libraries"]},
        {e["package"]: e for e in data["operating_system"]},
    )


def wheel_licence(wheel: dict) -> str:
    """What the wheel's metadata declares: the SPDX expression when it has one, else the
    free-text field and the licence classifiers."""
    if wheel["expression"]:
        return wheel["expression"]
    classifiers = [c.removeprefix("License :: ").removeprefix("OSI Approved :: ")
                   for c in wheel["classifiers"]]
    declared = dict.fromkeys([*([wheel["free_text"]] if wheel["free_text"] else []), *classifiers])
    return "; ".join(declared) or "none declared"


def look_up(wheels: dict, wheel: dict, library: dict) -> dict | None:
    """The entry for one bundled library, with the evidence file that applies to it."""
    known = wheels.get(normal_distribution(wheel["name"]))
    if known is None:
        return None
    entry = known["libraries"].get(library["stem"]) or known["libraries"].get("*")
    if entry is None:
        return None
    licence = wheel_licence(wheel) if entry["licence"] == "declared" else entry["licence"]
    return {**entry, "licence": licence, "evidence": entry.get("evidence", known["evidence"])}


def cell(text: object) -> str:
    return str(text).replace("|", "\\|")


def shared_library_rows(record: dict, wheels: dict) -> tuple[list[str], list[dict], Counter]:
    """Rows for the table of bundled libraries, the entries that need a decision, and the
    number of libraries in each class."""
    rows, findings, classes = [], [], Counter()
    for wheel in record["inventory"]["wheels"]:
        for library in wheel["libraries"]:
            entry = look_up(wheels, wheel, library)
            location = library["path"].split("site-packages/", 1)[-1]
            who = f"{wheel['name']} {wheel['version']}"
            if entry is None:
                rows.append(f"| `{location}` | {who} | UNMAPPED | | | |")
                findings.append({"kind": "unmapped", "library": location, "wheel": who})
                continue
            known = {f.rsplit("/", 1)[-1] for f in [*wheel["licence_files"], *wheel["package_files"]]}
            evidence = entry["evidence"]
            if evidence == "none":
                shown = "none shipped"
            elif evidence in known:
                shown = evidence
            else:
                shown = f"{evidence} (NOT IN WHEEL)"
                findings.append({"kind": "evidence", "library": location, "wheel": who})
            classes[entry["class"]] += 1
            if entry["class"] == "strong-copyleft":
                findings.append({"kind": "strong-copyleft", "library": location, "wheel": who})
            elected = f" (elected: {entry['elected']})" if "elected" in entry else ""
            rows.append(f"| `{location}` | {who} | {cell(entry['upstream'])} "
                        f"| {cell(entry['licence'])}{cell(elected)} | {entry['class']} | {cell(shown)} |")
    return rows, findings, classes


def other_library_entries(record: dict, other: dict) -> list[tuple[dict, dict | None]]:
    """Each shared library outside any package manager and wheel, with its entry if mapped."""
    return [(library, other.get(library["stem"])) for library in record["inventory"]["other_libraries"]]


def os_groups(record: dict) -> dict[str, list[dict]]:
    """The operating-system packages in two groups, by dpkg section: system libraries
    (`libs`) and programs and data (every other section)."""
    section = {p["name"]: p["section"] for p in record["inventory"]["dpkg"]}
    groups: dict[str, list[dict]] = {"libs": [], "programs": []}
    for package in record["os"]["packages"]:
        groups["libs" if section.get(package["name"]) == "libs" else "programs"].append(package)
    return groups


def digests(meta: dict) -> str:
    if "manifest_digest" in meta:
        return f"index `{meta['index_digest']}`, platform manifest `{meta['manifest_digest']}`"
    return f"local build, image config digest `{meta['image_id']}`"


def render(args: argparse.Namespace) -> None:
    wheels, other, system = load_upstreams(Path(args.upstreams))
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(args.directory).glob("*.json"))]
    records.sort(key=lambda r: r["meta"]["id"])
    identity = ["| Record | Source | Platform | Tag | Manifest digest | Scanned |",
                "| --- | --- | --- | --- | --- | --- |"]
    counts = ["| Record | OS packages (Part C) | OS system libraries (Part C) "
              "| Wheel libraries (Part A) | Other libraries (Part B) | Unmapped (A, B) "
              "| Strong copyleft (A, B) | Weak copyleft (A, B) | Linking exception (A, B) "
              "| Proprietary (A, B) |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    findings_text: list[str] = []
    sections: list[str] = []
    shown_tables: dict[str, str] = {}  # Part C tables -> the record that shows them in full
    for record in records:
        meta = record["meta"]
        rows, findings, classes = shared_library_rows(record, wheels)
        others = other_library_entries(record, other)
        for library, entry in others:
            if entry is None:
                findings.append({"kind": "unmapped", "library": library["path"], "wheel": "no wheel"})
                continue
            classes[entry["class"]] += 1
            if entry["class"] == "strong-copyleft":
                findings.append({"kind": "strong-copyleft", "library": library["path"], "wheel": "no wheel"})
        groups = os_groups(record)
        digest = meta.get("manifest_digest") or f"local build, image config digest {meta['image_id']}"
        identity.append(f"| `{meta['id']}` | {meta['source']} | {meta['platform']} | `{meta['tag']}` "
                        f"| `{digest}` | {meta['scan_date']} (trivy {meta['trivy']}) |")
        copyleft = [f for f in findings if f["kind"] == "strong-copyleft"]
        counts.append(
            f"| `{meta['id']}` | {len(record['os']['packages'])} | {len(groups['libs'])} | {len(rows)} "
            f"| {len(others)} | {sum(f['kind'] == 'unmapped' for f in findings)} | {classes['strong-copyleft']} "
            f"| {classes['weak-copyleft']} | {classes['linking-exception']} | {classes['proprietary']} |")
        if copyleft:
            by_wheel = defaultdict(list)
            for f in copyleft:
                by_wheel[f["wheel"]].append(posixpath.basename(f["library"]))
            findings_text.append(f"- `{meta['id']}` bundles strong-copyleft libraries: " + "; ".join(
                f"{wheel}: {', '.join(libraries)}" for wheel, libraries in by_wheel.items()) + ".")
        else:
            findings_text.append(f"- `{meta['id']}` bundles no library of class strong-copyleft.")
        for f in findings:
            if f["kind"] in ("unmapped", "evidence"):
                findings_text.append(f"- `{meta['id']}`: {f['kind']}: {f['library']} ({f['wheel']}).")
        sections.append(render_image(record, rows, others, system, groups, shown_tables))
    header = args.header.read_text(encoding="utf-8").rstrip()
    out = [header, "", "## Images scanned", "", *identity, "", "## Counts, derived from the records", "",
           *counts, "", "## Findings, derived from the records", "", *findings_text, ""] + sections
    sys.stdout.write("\n".join(out) + "\n")


def os_tables(groups: dict[str, list[dict]], system: dict) -> str:
    """Part C's tables: the system libraries under a linking exception, then each dpkg group."""
    exempt = [p["name"] for group in groups.values() for p in group
              if system.get(p["name"], {}).get("class") == "linking-exception"]
    lines = ["System libraries under a linking exception: " + (", ".join(exempt) or "none") + ".", ""]
    for heading, group in (("System libraries (dpkg section `libs`)", groups["libs"]),
                           ("Programs and data (every other dpkg section)", groups["programs"])):
        lines += [f"<details><summary>{heading}: {len(group)}</summary>", "",
                  "| Package | Version | Licences read by trivy (class) |", "| --- | --- | --- |"]
        for package in group:
            note = system.get(package["name"])
            licences = ", ".join(f"{x['name']} ({x['category']})" for x in package["licences"]) or (
                f"none read; recorded as {note['licence']} ({note['class']}), evidence {note['evidence']}"
                if note else "none read")
            lines.append(f"| {package['name']} | {cell(package['version'])} | {cell(licences)} |")
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def render_image(record: dict, rows: list[str], others: list, system: dict,
                 groups: dict[str, list[dict]], shown_tables: dict[str, str]) -> str:
    meta, inv, operating_system = record["meta"], record["inventory"], record["os"]
    oras = f" and oras {meta['oras']}" if "oras" in meta else ""
    lines = [f"## `{meta['id']}`", "",
             f"- Reference: `{meta.get('reference', 'local build')}`; tag `{meta['tag']}`; source: {meta['source']}.",
             f"- Digests: {digests(meta)}.",
             "- Regenerate: `uv run --frozen python scripts/image-library-inventory.py collect "
             f"{shlex.join(meta['arguments'])} --trivy-cache CACHE --out RECORD.json`.",
             f"- Scanned {meta['scan_date']} with trivy {meta['trivy']}{oras}; image built {inv['image']['created']}; "
             f"layers: {inv['layers']}; Python distributions: {inv['python_distributions']}.", "",
             f"### Part A: shared libraries inside Python wheels (`{meta['id']}`)", ""]
    if inv["wheels"]:
        lines += ["Wheels that carry shared objects:", "",
                  "| Wheel | Declared licence | Other files in its `.dist-info` (count: first names) | Bundled libraries | Extension modules |",
                  "| --- | --- | --- | --- | --- |"]
        for wheel in inv["wheels"]:
            names = list(dict.fromkeys(f.rsplit("/", 1)[-1] for f in wheel["licence_files"]))
            files = (f"{len(wheel['licence_files'])}: " + ", ".join(names[:4])
                     + (", ..." if len(names) > 4 else "")) if names else "none"
            lines.append(f"| {wheel['name']} {wheel['version']} | {cell(wheel_licence(wheel))} | {cell(files)} "
                         f"| {len(wheel['libraries'])} | {wheel['modules']} |")
        lines += [""]
    if rows:
        lines += ["| Library | Wheel | Upstream project | Licence | Class | Evidence: licence file in the wheel |",
                  "| --- | --- | --- | --- | --- | --- |", *rows]
    else:
        lines += ["No Python wheel in this image bundles a shared library."]
    if inv["unowned"]:
        lines += ["", "Shared objects in `site-packages` that no wheel record lists:",
                  *[f"- `{u['path']}`" for u in inv["unowned"]]]
    lines += ["", f"### Part B: other shared libraries, outside any package manager (`{meta['id']}`)", ""]
    if others:
        lines += ["| Library | Upstream project | Licence | Class | Evidence: file in the image |",
                  "| --- | --- | --- | --- | --- |"]
        for library, entry in others:
            lines.append(f"| `/{library['path']}` | {entry['upstream'] if entry else 'UNMAPPED'} "
                         f"| {entry['licence'] if entry else ''} | {entry['class'] if entry else ''} "
                         f"| {entry['evidence'] if entry else ''} |")
    else:
        lines += ["None."]
    lines += ["", f"### Part C: operating-system layer (`{meta['id']}`): {operating_system['family']} "
              f"{operating_system['name']}", "",
              f"{len(operating_system['packages'])} packages own {inv['operating_system_objects']} shared "
              "objects between them. They are separate programs and system libraries in the image, "
              "aggregated with the product and not combined with it. Licence class is trivy's, "
              "from the lists in `scripts/image-licences/trivy.yaml`: `forbidden` is GPL and AGPL, "
              "`restricted` is LGPL.", ""]
    tables = os_tables(groups, system)
    first = shown_tables.setdefault(tables, meta["id"])
    if first == meta["id"]:
        lines += [tables]
    else:
        lines += [f"The tables are the same as in Part C of `{first}`.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("collect")
    one.add_argument("--id", required=True, help="record name, such as lean-amd64")
    one.add_argument("--tag", required=True)
    one.add_argument("--source", required=True, help="where the image came from, in words")
    one.add_argument("--platform", required=True, help="linux/amd64 or linux/arm64")
    where = one.add_mutually_exclusive_group(required=True)
    where.add_argument("--registry", help="REPOSITORY@sha256:INDEX_DIGEST")
    where.add_argument("--archive", help="a `docker save` archive")
    one.add_argument("--trivy-cache", required=True)
    one.add_argument("--out", required=True)
    one.set_defaults(run=collect)
    many = commands.add_parser("render")
    many.add_argument("directory", help="the directory of collect records")
    many.add_argument("--upstreams", required=True)
    many.add_argument("--header", type=Path, required=True, help="the Markdown that opens the record")
    many.set_defaults(run=render)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
