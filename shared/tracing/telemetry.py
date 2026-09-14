"""
Azure Application Insights integration via Azure Monitor OpenTelemetry.

Conforms to UltrionTech-Backend-Template shared/tracing/ specification.
Reads APPLICATIONINSIGHTS_CONNECTION_STRING from the environment.
If unset or initialization fails, the application continues safely without telemetry.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("hms.telemetry")

CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
SERVICE_NAME = "ultrion-hms-api"
LOGGER_NAMESPACE = "hms"

_TELEMETRY_ENABLED = False


def is_telemetry_enabled() -> bool:
    """Return whether OpenTelemetry Azure Monitor exporter is active."""
    return _TELEMETRY_ENABLED


def setup_telemetry() -> bool:
    """
    Configure Azure Monitor OpenTelemetry exporters and default instrumentations.
    Returns True if successfully enabled, False otherwise (no-op).
    """
    global _TELEMETRY_ENABLED

    if _TELEMETRY_ENABLED:
        return True

    connection_string = os.getenv(CONNECTION_STRING_ENV, "").strip()
    if not connection_string:
        logger.info(
            "%s is not set; Azure Monitor telemetry is disabled",
            CONNECTION_STRING_ENV,
        )
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.resource import ResourceAttributes

        resource = Resource.create(
            {
                ResourceAttributes.SERVICE_NAME: SERVICE_NAME,
                ResourceAttributes.SERVICE_NAMESPACE: "ultrion-hms",
            }
        )

        configure_azure_monitor(
            connection_string=connection_string,
            logger_name=LOGGER_NAMESPACE,
            enable_live_metrics=True,
            enable_performance_counters=True,
            resource=resource,
            instrumentation_options={
                "fastapi": {"enabled": False},
            },
        )

        _TELEMETRY_ENABLED = True
        logger.info("Azure Monitor OpenTelemetry configured (service=%s)", SERVICE_NAME)
        return True
    except Exception as exc:
        logger.warning(
            "Azure Monitor telemetry initialization failed; continuing without telemetry: %s",
            exc,
            exc_info=True,
        )
        _TELEMETRY_ENABLED = False
        return False


def instrument_fastapi_app(app: Any) -> None:
    """Instrument one FastAPI app so every included router is traced."""
    if not _TELEMETRY_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            return
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI application instrumented for Azure Monitor")
    except Exception as exc:
        logger.warning("FastAPI instrumentation failed: %s", exc, exc_info=True)


def instrument_sqlalchemy_engine(engine: Any) -> None:
    """Instrument SQLAlchemy engine so query timings appear under Dependencies."""
    if not _TELEMETRY_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
        logger.info("SQLAlchemy engine instrumented for Azure Monitor")
    except Exception as exc:
        logger.warning("SQLAlchemy engine instrumentation failed: %s", exc, exc_info=True)


def instrument_http_clients() -> None:
    """Instrument httpx/requests for outbound HTTP dependency tracking."""
    if not _TELEMETRY_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.info("HTTPX client instrumented for Azure Monitor")
    except Exception as exc:
        logger.warning("HTTP client instrumentation skipped: %s", exc)
