"""Deployable API factory with strict environment startup."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import ProvisionerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .repository import OperationRepository


def create_app_from_env():
    settings = ProvisionerSettings()  # type: ignore[call-arg]  # required values come from env
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
    )
    app = create_app(
        settings=settings,
        readiness_probe=database.ready,
        repository=repository,
    )
    app.state.database = database
    app.state.repository = repository
    app.router.add_event_handler("shutdown", database.dispose)
    return app


def run_api() -> None:
    uvicorn.run(
        "exomem_provisioner.main:create_app_from_env",
        factory=True,
        host="0.0.0.0",
        port=8080,
        access_log=False,
    )
