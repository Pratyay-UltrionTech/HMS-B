"""
Azure Application Insights integration via Azure Monitor OpenTelemetry.

Reads APPLICATIONINSIGHTS_CONNECTION_STRING from the environment.
If unset or initialization fails, the application continues without telemetry.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("hms.telemetry")

CONNECTION_STRING_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
SERVICE_NAME = "ultrion-hms-api"
# Collect logs from hms.* (hms.api, hms.telemetry, …) without ingesting SDK noise.
LOGGER_NAMESPACE = "hms"

_TELEMETRY_ENABLED = False


class TraceContextFilter(logging.Filter):
    """Attach OpenTelemetry trace/span IDs to log records for correlation."""

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id = ""
        span_id = ""
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context() if span is not None else None
            if ctx is not None and ctx.is_valid:
                trace_id = format(ctx.trace_id, "032x")
                span_id = format(ctx.span_id, "016x")
        except Exception:
            pass
        record.otelTraceID = trace_id  # type: ignore[attr-defined]
        record.otelSpanID = span_id  # type: ignore[attr-defined]
        return True


def is_telemetry_enabled() -> bool:
    return _TELEMETRY_ENABLED


def setup_telemetry() -> bool:
    """
    Configure Azure Monitor OpenTelemetry exporters and default instrumentations.

    Automatically enables (via the distro, when libraries are present):
    - FastAPI HTTP request duration / status codes / failures (app instrumented separately)
    - requests / urllib / urllib3 outbound HTTP
    - psycopg2 PostgreSQL dependency spans
    - Exceptions and logging correlated to traces (logger_name)
    - Performance counters (CPU / memory) when supported

    SQLAlchemy and httpx are instrumented via dedicated helpers after engines/clients exist.
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

        # FastAPI is instrumented on the app instance so import order in main.py
        # cannot prevent request tracing. Other bundled instrumentations stay on.
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

        _install_trace_context_logging()
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
    """Instrument SQLAlchemy so query timings appear under Dependencies."""
    if not _TELEMETRY_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
        logger.info("SQLAlchemy engine instrumented for Azure Monitor")
    except Exception as exc:
        logger.warning("SQLAlchemy instrumentation failed: %s", exc, exc_info=True)


def instrument_http_clients() -> None:
    """
    Ensure outbound HTTP clients are instrumented.

    requests / urllib / urllib3 are covered by configure_azure_monitor.
    httpx is instrumented here when the package and instrumentation are available.
    """
    if not _TELEMETRY_ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.info("httpx client instrumented for Azure Monitor")
    except ImportError:
        logger.debug("httpx instrumentation skipped (package not installed)")
    except Exception as exc:
        logger.warning("httpx instrumentation failed: %s", exc, exc_info=True)


class _CorrelatedLogFormatter(logging.Formatter):
    """Formatter that never fails when trace fields are absent."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "otelTraceID"):
            record.otelTraceID = ""  # type: ignore[attr-defined]
        if not hasattr(record, "otelSpanID"):
            record.otelSpanID = ""  # type: ignore[attr-defined]
        return super().format(record)


def _install_trace_context_logging() -> None:
    """Add trace/span IDs to the root logger for local + App Insights correlation."""
    root = logging.getLogger()
    trace_filter = TraceContextFilter()
    root.addFilter(trace_filter)
    for handler in root.handlers:
        handler.addFilter(trace_filter)
        fmt = getattr(handler.formatter, "_fmt", None) if handler.formatter else None
        if fmt and "otelTraceID" not in fmt:
            handler.setFormatter(
                _CorrelatedLogFormatter(
                    "%(asctime)s [%(levelname)s] [trace=%(otelTraceID)s span=%(otelSpanID)s] %(message)s"
                )
            )
