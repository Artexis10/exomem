"""Deployable API factory with strict environment startup."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import ProvisionerSettings
from .crypto import AesGcmEnvelopeCodec
from .database import ProvisionerDatabase
from .entrypoint import help_requested
from .logging import configure_content_free_logging
from .provider_identity import ProviderRecoveryIdentityCodec
from .repository import OperationRepository


def _require_production_database(settings: ProvisionerSettings) -> None:
    if not settings.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
        raise RuntimeError("PostgreSQL is required for production provisioner startup")


def _require_provider_identity_signer(settings: ProvisionerSettings) -> None:
    if settings.provider_recovery_signing_key is None:
        raise RuntimeError("provider recovery signing key is required for production startup")


def _create_app(settings: ProvisionerSettings):
    database = ProvisionerDatabase(settings)
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
        claim_seconds=settings.claim_seconds,
        max_failure_attempts=settings.max_failure_attempts,
    )
    provider_identity_codec = (
        ProviderRecoveryIdentityCodec.from_encoded_seed(
            settings.provider_recovery_signing_key.get_secret_value()
        )
        if settings.provider_recovery_signing_key is not None
        else None
    )
    app = create_app(
        settings=settings,
        readiness_probe=database.ready,
        repository=repository,
        provider_identity_codec=provider_identity_codec,
    )
    app.state.database = database
    app.state.repository = repository
    app.state.provider_identity_public_key = (
        provider_identity_codec.public_key() if provider_identity_codec is not None else None
    )
    app.router.add_event_handler("shutdown", database.dispose)
    return app


def create_app_from_env():
    configure_content_free_logging()
    settings = ProvisionerSettings()  # type: ignore[call-arg]  # required values come from env
    _require_production_database(settings)
    _require_provider_identity_signer(settings)
    return _create_app(settings)


def run_api() -> None:
    if help_requested("exomem-provisioner-api", "hosted provisioner HTTPS API"):
        return
    configure_content_free_logging()
    settings = ProvisionerSettings()  # type: ignore[call-arg]  # required values come from env
    _require_production_database(settings)
    _require_provider_identity_signer(settings)
    uvicorn.run(
        _create_app(settings),
        host="0.0.0.0",
        port=8080,
        access_log=False,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
    )
