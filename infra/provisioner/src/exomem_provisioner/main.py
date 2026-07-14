"""Deployable API factory with strict environment startup."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import ProvisionerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .logging import configure_content_free_logging
from .repository import OperationRepository


def _create_app(settings: ProvisionerSettings):
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
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


def create_app_from_env():
    configure_content_free_logging()
    settings = ProvisionerSettings()  # type: ignore[call-arg]  # required values come from env
    return _create_app(settings)


def run_api() -> None:
    configure_content_free_logging()
    settings = ProvisionerSettings()  # type: ignore[call-arg]  # required values come from env
    uvicorn.run(
        _create_app(settings),
        host="0.0.0.0",
        port=8080,
        access_log=False,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
    )
