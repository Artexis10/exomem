"""The promotion evidence script must never let a DSN reach output or argv.

`query()` shells out to psql, and psql quotes the whole connection string back —
password included — in errors like `invalid connection option`. That stderr used
to be raised verbatim, which put a live database password into whatever log,
terminal or agent transcript the operator was running under. It happened.

The DSN also arrives in a shape libpq cannot parse: the provisioner stores its
URL for SQLAlchemy, so it reads `postgresql+asyncpg://...?ssl=require`. That
mismatch is what produced the error that leaked the password in the first place,
so the normalisation and the redaction are tested together.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/promotion_evidence.py"

SQLALCHEMY_DSN = (
    "postgresql+asyncpg://exomem_runtime:s3cr3t-p455w0rd"
    "@ep-lively-queen.eu-central-1.aws.neon.tech/neondb?ssl=require"
)
PASSWORD = "s3cr3t-p455w0rd"


@pytest.fixture(scope="module")
def evidence():
    spec = importlib.util.spec_from_file_location("promotion_evidence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlalchemy_dialect_and_ssl_param_are_normalised_for_libpq(evidence):
    normalised = evidence.libpq_url(SQLALCHEMY_DSN)
    assert normalised.startswith("postgresql://")
    assert "+asyncpg" not in normalised
    assert "sslmode=require" in normalised
    assert "?ssl=require" not in normalised


@pytest.mark.parametrize(
    "dsn",
    [
        SQLALCHEMY_DSN,
        "postgres://u:p@host/db",
        "postgresql://u:p@host:5432/db?sslmode=require",
    ],
)
def test_redaction_removes_every_connection_uri_shape(evidence, dsn):
    leaked = f'psql: error: invalid connection option "{dsn}"'
    redacted = evidence.redact_dsn(leaked)
    assert dsn not in redacted
    assert "<redacted>" in redacted


def test_redaction_survives_a_dsn_embedded_in_a_longer_message(evidence):
    redacted = evidence.redact_dsn(
        f"connection to {SQLALCHEMY_DSN} failed after 3 attempts; giving up"
    )
    assert PASSWORD not in redacted
    assert "giving up" in redacted


def test_password_goes_to_the_environment_never_to_argv(evidence):
    environment = evidence.libpq_environment(SQLALCHEMY_DSN)
    assert environment["PGPASSWORD"] == PASSWORD
    assert environment["PGUSER"] == "exomem_runtime"
    assert environment["PGHOST"] == "ep-lively-queen.eu-central-1.aws.neon.tech"
    assert environment["PGDATABASE"] == "neondb"
    # asyncpg's `ssl` became libpq's `sslmode` before the split, not after.
    assert environment["PGSSLMODE"] == "require"


def test_query_invokes_psql_without_the_dsn_on_the_command_line(evidence, monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stdout = "row\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return Result()

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    evidence.query(SQLALCHEMY_DSN, "select 1")

    assert not any(PASSWORD in str(argument) for argument in seen["argv"])
    assert not any("neon.tech" in str(argument) for argument in seen["argv"])
    assert seen["env"]["PGPASSWORD"] == PASSWORD


def test_query_failure_does_not_raise_the_dsn(evidence, monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = f'psql: error: invalid connection option "{SQLALCHEMY_DSN}"'

    monkeypatch.setattr(evidence.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(SystemExit) as raised:
        evidence.query(SQLALCHEMY_DSN, "select 1")

    assert PASSWORD not in str(raised.value)
    assert "query failed" in str(raised.value)


def test_unrecognised_query_parameters_survive_rather_than_being_dropped(evidence):
    environment = evidence.libpq_environment(
        "postgresql://u:p@host/db?sslmode=require&channel_binding=require"
    )
    assert environment["PGCHANNEL_BINDING"] == "require"
