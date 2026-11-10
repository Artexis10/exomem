"""A field declares which collection its values refer to; create and revise accept or refuse the declaration.

The fixtures invent three Records collections: sites (natural key `code`), skills (natural key
`slug`) and a diary whose `site`, `sites_seen` and `skills[].skill_id` fields declare references.
"""

import pytest
from test_collection_store_governance import _record_writer
from test_collection_store_writer import store as store
from test_governance_egress import write_rule, write_scope

from exomem import commands
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command

SITES = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SKILLS = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DIARY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GONE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
NORTH = "00000000-0000-4000-8000-0000000000a1"
YARD = "00000000-0000-4000-8000-0000000000a2"
SLUG = "00000000-0000-4000-8000-0000000000b1"
ENTRY = "00000000-0000-4000-8000-0000000000c1"


def path(name):
    return f"Knowledge Base/Records/{name}/_collection.md"


def collection_text(cid, title, natural_key, fields, extra=""):
    return f"""---
type: collection
exomem_id: {cid}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: summary
{extra}storage:
  strategy: sqlite
  source: Items
  format_version: 1
item_schema:
  natural_key: [{natural_key}]
  fields:
{fields}---
"""


def sites_text(**kwargs):
    return collection_text(SITES, "Sites", "code", "    code: {type: string, required: true}\n"
                           "    name: {type: string}\n", **kwargs)


def skills_text():
    return collection_text(SKILLS, "Skills", "slug", "    slug: {type: string, required: true}\n")


def diary_text(fields, cid=DIARY, **kwargs):
    return collection_text(cid, "Diary", "title", "    title: {type: string, required: true}\n" + fields, **kwargs)


SITE_FIELD = f"    site: {{type: string, references: {{collection: {SITES}}}}}\n"
SITES_SEEN_FIELD = (f"    sites_seen: {{type: array, items: {{type: string}}, "
                    f"references: {{collection: {SITES}}}}}\n")
SKILL_FIELD = f"""    skills:
      type: array
      items:
        type: object
        properties:
          skill_id: {{type: string, references: {{collection: {SKILLS}}}}}
          minutes: {{type: integer}}
"""


def create(store, cid, text, name):
    return store.create_collection(path(name), text, why="declare", scaffold=False)


def revise(store, cid, text):
    guards = store.inspect_collection(cid)["lifecycle_guards"]
    return store.revise_collection(
        cid, manifest_text=text, expected_manifest_hash=guards["expected_manifest_hash"],
        expected_container_hash=guards["expected_container_hash"], why="declare a reference")


def put_diary(store, fields, how):
    """Declare `fields` on a diary by creating it, or by revising a diary created without them."""
    if how == "create":
        return create(store, DIARY, diary_text(fields), "Diary")
    create(store, DIARY, diary_text(""), "Diary")
    return revise(store, DIARY, diary_text(fields))


def row(store, cid, key, item):
    return store.append_record(cid, item=item, item_key=key, why="record")


def refused(store, fields, how="revise"):
    with pytest.raises(collections.CollectionError) as error:
        put_diary(store, fields, how)
    return error.value


# --- 1.1 declaration: accepted shapes -----------------------------------------------------------


@pytest.mark.parametrize("how", ["create", "revise"])
@pytest.mark.parametrize(("fields", "path_"), [
    (SITE_FIELD, "site"),
    (SITES_SEEN_FIELD, "sites_seen"),
    (SKILL_FIELD.replace(SKILLS, SITES), "skills.skill_id"),
])
def test_a_field_declares_a_reference_to_a_collection_the_author_reads(store, fields, path_, how):
    """A declaration on a string, an array of strings or an object property is read as written."""
    create(store, SITES, sites_text(), "Sites")
    put_diary(store, fields, how)
    with store.read_collection(DIARY) as manifest:
        declared = {item.dotted: (item.collection, item.many)
                    for item in collections.declared_references(manifest.schema)}
    assert declared == {path_: (SITES, path_ != "site")}


# --- 1.1 declaration: refusals -------------------------------------------------------------------


@pytest.mark.parametrize("how", ["create", "revise"])
def test_a_target_that_does_not_exist_is_refused_with_the_generic_target_error(store, how):
    """A silently accepted declaration would leave every later read of the field refused."""
    error = refused(store, SITE_FIELD.replace(SITES, GONE), how)
    assert error.code == "INVALID_REFERENCE_TARGET"
    assert error.details["field"] == "item_schema.fields.site.references.collection"


def test_validate_of_a_new_manifest_refuses_a_missing_target_as_create_does(store):
    """A preflight that passes what create refuses sends an agent into a create that then fails."""
    fields = SITE_FIELD.replace(SITES, GONE)
    validate = next(command for command in commands.COMMANDS if command.name == "record_memory")
    with request_scope(owner_principal()), preview_store(store.root, store.handle), \
            pytest.raises(OpError) as validated:
        invoke_command(validate, store.root, action="validate", manifest_path=path("Diary"),
                       manifest_text=diary_text(fields))
    created = refused(store, fields, "create")
    assert validated.value.code == created.code == "INVALID_REFERENCE_TARGET"
    assert validated.value.details["field"] == created.details["field"]


def test_a_target_the_author_cannot_read_is_refused_exactly_like_a_missing_one(store, tmp_path, monkeypatch):
    """The refusal must not tell an author that a collection exists, or what it holds."""
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(""), "Diary")
    write_scope(store.root, paths="Records/Sites/**")
    write_rule(store.root, ceiling=0)
    author = _record_writer(store, tmp_path, monkeypatch)
    errors = []
    for target in (SITES, GONE):
        with request_scope(author), pytest.raises(collections.CollectionError) as error:
            revise(store, DIARY, diary_text(SITE_FIELD.replace(SITES, target)))
        errors.append(error.value)
    withheld, missing = errors
    assert withheld.code == missing.code == "INVALID_REFERENCE_TARGET"
    assert withheld.reason == missing.reason
    strip = lambda error: {name: value for name, value in error.details.items() if name != "received"}  # noqa: E731
    assert strip(withheld) == strip(missing)
    assert SITES not in withheld.reason and SITES not in str(strip(withheld))


def test_a_target_without_a_single_field_key_is_refused_naming_the_key(store):
    """A compound key cannot be matched by one value, so the refusal must say what to change."""
    create(store, SITES, collection_text(
        SITES, "Sites", "code, region", "    code: {type: string, required: true}\n"
        "    region: {type: string, required: true}\n"), "Sites")
    error = refused(store, SITE_FIELD)
    assert error.code == "UNSUPPORTED_REFERENCE_TARGET"
    assert error.details["received"] == ["code", "region"]


def test_a_target_whose_key_is_not_text_or_a_date_is_refused_naming_its_type(store):
    """An integer key would match a value by coercion; only string, date and datetime keys are defined."""
    create(store, SITES, collection_text(SITES, "Sites", "number", "    number: {type: integer, required: true}\n"), "Sites")
    error = refused(store, SITE_FIELD)
    assert error.code == "UNSUPPORTED_REFERENCE_TARGET"
    assert error.details["received"] == "integer"


def test_a_source_field_that_is_not_a_string_is_refused(store):
    """A number cannot name a row, so the declaration would never resolve."""
    create(store, SITES, sites_text(), "Sites")
    error = refused(store, f"    hours: {{type: integer, references: {{collection: {SITES}}}}}\n")
    assert error.code == "INVALID_FIELD_REFERENCE"
    assert error.details["field"] == "item_schema.fields.hours.references"


@pytest.mark.parametrize("fields", [
    pytest.param(f"    notes: {{type: array, items: {{type: string, references: {{collection: {SITES}}}}}}}\n",
                 id="on-array-items"),
    pytest.param(f"    place:\n      type: object\n      properties:\n        site: "
                 f"{{type: string, references: {{collection: {SITES}}}}}\n", id="on-top-level-object-property"),
])
def test_a_declaration_in_an_unsupported_position_is_refused(store, fields):
    """A declaration the reader never looks at would be accepted and then silently ignored."""
    create(store, SITES, sites_text(), "Sites")
    assert refused(store, fields).code == "INVALID_FIELD_REFERENCE"


@pytest.mark.parametrize("declaration", [
    pytest.param("{collection: Sites}", id="path-instead-of-id"),
    pytest.param(f"{{collection: {SITES}, field: code}}", id="extra-key"),
    pytest.param("sites", id="not-a-mapping"),
])
def test_a_reference_names_its_target_by_collection_id_only(store, declaration):
    """A path or name would resolve differently after a move, so only the stable id is accepted."""
    error = refused(store, f"    site: {{type: string, references: {declaration}}}\n")
    assert error.code == "INVALID_FIELD_REFERENCE"
    assert error.details["expected"]


def test_a_declaration_is_refused_on_a_collection_that_keeps_its_rows_in_files(tmp_path):
    """A file-mode collection has no row index to resolve through, so the declaration must not be accepted."""
    from exomem import records

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    text = diary_text(SITE_FIELD).replace("view_mode: summary\n", "").replace(
        "strategy: sqlite", "strategy: markdown-items")
    with pytest.raises(collections.CollectionError) as error:
        records.create_collection(vault, path("Diary"), text, why="declare", scaffold=False)
    assert error.value.code == "INVALID_FIELD_REFERENCE"


def test_a_derived_collection_cannot_declare_a_reference(store):
    """Derived rows live outside the store the resolver reads."""
    create(store, SITES, sites_text(), "Sites")
    with pytest.raises(collections.CollectionError) as error:
        create(store, DIARY, diary_text(SITE_FIELD, extra="derived: true\n"), "Diary")
    assert error.value.code == "INVALID_FIELD_REFERENCE"


def test_a_shared_collection_type_cannot_declare_a_reference():
    """A relation binds two collections that one owner keeps, so a type that declared one would bind every user's."""
    import yaml

    from exomem.collection_store import types

    data = yaml.safe_load(types.declaration_text("records"))
    data["name"] = "linked-records"
    data.pop("wire")
    data["fields"]["site"] = {"type": "string", "references": {"collection": SITES}}
    with pytest.raises(types.CollectionTypeError) as error:
        types.parse_declaration(data, builtin=False)
    assert error.value.code == "INVALID_DECLARATION" and "references" in error.value.reason


def test_an_unchanged_declaration_survives_its_target_leaving_the_authors_view(store, tmp_path, monkeypatch):
    """An unrelated revise must not be blocked because a target is no longer readable."""
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    write_scope(store.root, paths="Records/Sites/**")
    write_rule(store.root, ceiling=0)
    author = _record_writer(store, tmp_path, monkeypatch)
    with request_scope(author):
        revise(store, DIARY, diary_text(SITE_FIELD + "    notes: {type: string}\n"))
        # Pointing the same field at a different, equally unreadable target is a changed declaration.
        with pytest.raises(collections.CollectionError) as error:
            revise(store, DIARY, diary_text(SITE_FIELD.replace(SITES, SKILLS) + "    notes: {type: string}\n"))
    assert error.value.code == "INVALID_REFERENCE_TARGET"
