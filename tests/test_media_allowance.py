"""Allowance reads custody headers, without retaining extracted transcript bodies."""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import media_allowance, originals, reserved_paths, runtime_resources, trash_metadata
from exomem import vault as vault_module


@pytest.fixture
def custody(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    page = vault / "Knowledge Base/Evidence/clip.bin.md"
    page.parent.mkdir(parents=True)
    reference = originals.StoredObject(1, "test-store", "1", "a" * 64, "version", 50, "b" * 64)
    binary = originals.BinaryBinding("Knowledge Base/Evidence/clip.bin", "c" * 64, 23)
    fields = originals.reference_companion_fields(binary, reference)
    header = vault_module.render_frontmatter_document(vault_module.serialize_frontmatter(fields), "").encode()
    page.write_bytes(header)
    config = tmp_path / "media.json"
    config.write_text(json.dumps({"vaults": {str(vault): {"store": "test-store"}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    return vault, page, header


@pytest.mark.parametrize("allowance", [None, True, -1, 1.5])
def test_admission_requires_an_explicit_whole_allowance(allowance):
    usage = media_allowance.ReferenceUsage({}, "observed")
    binary = originals.BinaryBinding("Knowledge Base/Evidence/sample.bin", "a" * 64, 23)
    decision = media_allowance.admission(usage, binary, None, allowance)
    assert decision.code == "MEDIA_ALLOWANCE_UNKNOWN"
    assert decision.allowance_bytes is None
    assert decision.observed_at == "observed"


@pytest.mark.parametrize(
    ("allowance", "reuse", "projected", "code"),
    [(23, True, 23, None), (46, False, 46, None), (45, False, 46, "MEDIA_ALLOWANCE_EXCEEDED")],
)
def test_admission_counts_a_version_once_and_accepts_the_exact_limit(allowance, reuse, projected, code):
    reference = originals.StoredObject(1, "test-store", "1", "a" * 64, "version", 50, "b" * 64)
    binary = originals.BinaryBinding("Knowledge Base/Evidence/sample.bin", "c" * 64, 23)
    usage = media_allowance.ReferenceUsage({("test-store", "a" * 64, "version"): (binary, reference)}, "observed")
    decision = media_allowance.admission(usage, binary, reference if reuse else None, allowance)
    assert decision.used_bytes == 23
    assert decision.projected_bytes == projected
    assert decision.code == code


def test_admission_does_not_publish_a_projection_from_unknown_or_conflicting_custody():
    reference = originals.StoredObject(1, "test-store", "1", "a" * 64, "version", 50, "b" * 64)
    binary = originals.BinaryBinding("Knowledge Base/Evidence/sample.bin", "c" * 64, 23)
    unknown = media_allowance.admission(media_allowance.ReferenceUsage(None, "observed"), binary, None, 46)
    assert unknown.code == "MEDIA_ALLOWANCE_UNKNOWN" and unknown.projected_bytes is None
    usage = media_allowance.ReferenceUsage({("test-store", "a" * 64, "version"): (binary, reference)}, "observed")
    conflict = media_allowance.admission(usage, originals.BinaryBinding(binary.path, binary.sha256, 24), reference, 46)
    assert conflict.code == "MEDIA_ALLOWANCE_UNKNOWN" and conflict.projected_bytes is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"---\nkey: value\n---\n\xffbody", b"---\nkey: value\n---\n"),
        (b"\xef\xbb\xbf---\r\nkey: value\r\n---\r\nbody", b"\xef\xbb\xbf---\r\nkey: value\r\n---\r\n"),
        (b"---\nkey: value\n---", b"---\nkey: value\n---"),
        (b"---\n\n---\nbody", b"---\n\n---\n"),
        (b"---\nkey: value\n---not-a-fence\n---\nbody", b"---\nkey: value\n---not-a-fence\n---\n"),
        (b"---\nkey: value\n----\n---\nbody", b"---\nkey: value\n----\n---\n"),
        (b"ordinary" * 20000, b""),
        (b"---ordinary" * 20000, b""),
    ],
    ids=["lf", "bom-crlf", "eof", "empty", "dash-prefix", "four-dashes", "long-prose", "long-dash-prose"],
)
def test_header_reader_preserves_the_existing_fence_envelope(raw: bytes, expected: bytes):
    header = vault_module.read_frontmatter_header(io.BytesIO(raw), 16384)
    assert header == expected
    if header:
        assert vault_module.parse_frontmatter(header.decode())[2] == vault_module.parse_frontmatter(raw.decode(errors="replace"))[2]


@pytest.mark.parametrize("raw", [b"---\n---\n", b"---\nkey: value\n---not-a-fence", b"---\nunclosed"])
def test_unclosed_headers_never_look_like_absent_frontmatter(raw: bytes):
    with pytest.raises(vault_module.FrontmatterError):
        vault_module.read_frontmatter_header(io.BytesIO(raw), 16384)


def test_allowance_counts_large_transcripts_once_with_bounded_body_prefetch(custody, monkeypatch):
    vault, page, header = custody
    header = header.replace(b"binary_size:", b"description: " + b"x" * 9000 + b"\nbinary_size:")
    trash_root = "Knowledge Base/_trash/bundle"
    trashed = vault / trash_root / page.name
    trashed.parent.mkdir(parents=True)
    for target in (page, trashed):
        with target.open("wb") as stream:
            stream.write(header + b"\xffbody")
            stream.truncate(32 * 1024 * 1024)
    metadata = vault / (trash_root + trash_metadata.METADATA_SUFFIX)
    metadata.write_text(json.dumps({
        "original_path": "Knowledge Base/Evidence", "frontmatter_snapshot": {"description": "x" * 100000},
        "inbound_snapshots": [{"path": "Knowledge Base/Notes/related.md"}],
    }))
    read_header = vault_module.read_frontmatter_header
    read_sizes = []
    descriptor_read_sizes = []

    def observe(stream, *args, **kwargs):
        start = stream.tell()
        descriptor_start = os.lseek(stream.fileno(), 0, os.SEEK_CUR)
        result = read_header(stream, *args, **kwargs)
        read_sizes.append(stream.tell() - start)
        descriptor_read_sizes.append(os.lseek(stream.fileno(), 0, os.SEEK_CUR) - descriptor_start)
        return result

    original_path = trash_metadata.original_path
    retained_keys = []

    def observe_placement(fields, **kwargs):
        retained_keys.append(set(fields))
        return original_path(fields, **kwargs)

    monkeypatch.setattr(vault_module, "read_frontmatter_header", observe)
    monkeypatch.setattr(trash_metadata, "original_path", observe_placement)
    usage = media_allowance.reference_usage(vault)
    assert usage.used_bytes == 23
    assert len(usage.versions) == 1
    assert usage.live_paths == {("test-store", "a" * 64, "version"): "Knowledge Base/Evidence/clip.bin"}
    assert read_sizes == [len(header), len(header)]
    assert all(len(header) <= size <= len(header) + io.DEFAULT_BUFFER_SIZE for size in descriptor_read_sizes)
    assert retained_keys and all(keys == {"original_path"} for keys in retained_keys)
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


@pytest.mark.parametrize(
    "invalid",
    [
        b"---\nbinary_size: 1\nbinary_size: 2\n---\n",
        b"---\n- a scalar sequence\n---\n",
        b"---\nvalue: [\n---\n",
        b"---\nunterminated: header\n",
    ],
)
def test_invalid_header_makes_the_whole_allowance_unknown(custody, invalid):
    vault, page, header = custody
    (page.parent / "invalid.md").write_bytes(invalid)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


def test_replaced_header_cannot_publish_an_earlier_total(custody, monkeypatch):
    vault, page, header = custody
    parse = media_allowance.parse_frontmatter

    def replace_after_parse(*args, **kwargs):
        fields = parse(*args, **kwargs)
        replacement = page.with_suffix(".replacement")
        replacement.write_bytes(header.replace(b"binary_size: 23", b"binary_size: 99"))
        replacement.replace(page)
        return fields

    monkeypatch.setattr(media_allowance, "parse_frontmatter", replace_after_parse)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


@pytest.mark.parametrize("existing", [False, True], ids=["new-name", "earlier-plain-header"])
def test_reference_added_while_a_later_header_is_read_makes_usage_unknown(custody, monkeypatch, existing):
    vault, page, header = custody
    page.unlink()
    if existing:
        page.write_text("An ordinary note.\n")
    marker = page.with_name("z-marker.md")
    marker.write_text("---\ncensus_marker: true\n---\n")
    parse = media_allowance.parse_frontmatter
    changed = []

    def add_reference(text, **kwargs):
        parsed = parse(text, **kwargs)
        if parsed[0].get("census_marker") and not changed:
            page.write_bytes(header)
            changed.append(True)
        return parsed

    monkeypatch.setattr(media_allowance, "parse_frontmatter", add_reference)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert changed
    assert media_allowance.reference_usage(vault).used_bytes == 23
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


def test_yaml_expansion_and_capacity_failures_leave_the_allowance_unknown(custody, monkeypatch):
    vault, page, header = custody
    nodes = ["base: &a0 {x: y}"]
    nodes.extend(f"level{i}: &a{i} {{<<: [*a{i - 1}, *a{i - 1}]}}" for i in range(1, 21))
    page.write_bytes(header.replace(b"binary_size:", ("\n".join(nodes) + "\nbinary_size:").encode()))
    assert media_allowance.reference_usage(vault).used_bytes is None
    page.write_bytes(header)

    def busy(*args, **kwargs):
        raise runtime_resources.PreparationCapacityBusy("another parser holds capacity")

    monkeypatch.setattr(runtime_resources.SemanticPreparation, "reserve", busy)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


def test_oversized_trash_metadata_is_refused_before_its_bytes_are_read(custody, monkeypatch):
    vault, page, header = custody
    trashed = vault / "Knowledge Base/_trash/clip.bin.md"
    trashed.parent.mkdir()
    page.rename(trashed)
    metadata = trashed.with_name("clip.bin.meta.json")
    with metadata.open("wb") as stream:
        stream.truncate(runtime_resources.SEMANTIC_SOURCE_MAX_BYTES + 1)
    open_binary = reserved_paths.open_generic_binary

    @contextmanager
    def observe(*args, **kwargs):
        with open_binary(*args, **kwargs) as source:
            def must_not_read(*args, **kwargs):
                pytest.fail("unreserved metadata bytes were read")

            monkeypatch.setattr(source.stream, "read", must_not_read)
            yield source

    monkeypatch.setattr(reserved_paths, "open_generic_binary", observe)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


def test_replaced_trash_metadata_cannot_publish_its_previous_placement(custody, monkeypatch):
    from exomem import relation_review

    vault, page, header = custody
    trashed = vault / "Knowledge Base/_trash/clip.bin.md"
    trashed.parent.mkdir()
    page.rename(trashed)
    metadata = trashed.with_name("clip.bin.meta.json")
    metadata.write_text(json.dumps({"original_path": "Knowledge Base/Evidence/clip.bin"}))
    assert media_allowance.reference_usage(vault).used_bytes == 23
    parse = relation_review.parse_exact_json_object

    def replace_after_parse(*args, **kwargs):
        result = parse(*args, **kwargs)
        metadata.write_text(json.dumps({"original_path": "Knowledge Base/Evidence/other.bin"}))
        return result

    monkeypatch.setattr(relation_review, "parse_exact_json_object", replace_after_parse)
    assert media_allowance.reference_usage(vault).used_bytes is None
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0


def test_early_tree_exit_closes_held_descriptors_and_preserves_tuple_callers(custody):
    vault, page, header = custody
    descriptors = Path("/proc/self/fd")
    if not descriptors.is_dir():
        pytest.skip("descriptor census requires procfs")
    # Initialize the existing process-owned registry before checking walk handles.
    reserved_paths.list_generic_names(vault, "Knowledge Base")
    before = set(os.listdir(descriptors))
    with reserved_paths.open_generic_tree(vault, "Knowledge Base/Evidence", markdown_header_limit=16384) as entries:
        assert next(entries).markdown == header
        assert len(os.listdir(descriptors)) > len(before)
    assert set(os.listdir(descriptors)) == before
    assert runtime_resources.semantic_preparation_status()["reserved_bytes"] == 0
    page.write_bytes(header + b"Transcript body.\n")
    entries = reserved_paths.list_generic_tree(vault, "Knowledge Base/Evidence")
    assert isinstance(entries, tuple)
    assert entries[0].markdown == header + b"Transcript body.\n"
