"""
FastAPI application factory and ASGI entry point.

Conforms to UltrionTech-Backend-Template app/main.py specification.
Wires configuration, lifespan, middleware, exception handlers, and root router.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hms_migration.app.lifespan import lifespan
from hms_migration.app.router import root_router
from hms_migration.config.settings import Settings, get_settings
from hms_migration.shared.exceptions.handlers import register_exception_handlers
from hms_migration.shared.middleware.request_logging import RequestLogMiddleware
from hms_migration.shared.tracing.telemetry import instrument_fastapi_app, setup_telemetry

# Azure Monitor / Application Insights (no-op when connection string is unset)
setup_telemetry()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure a FastAPI application instance."""
    cfg = settings or get_settings()

    app = FastAPI(
        title="Ultrion HMS API",
        description="Backend API for Ultrion Hospital Management System (Target Modular Architecture)",
        version="2.0.0",
        lifespan=lifespan,
    )

    # Register shared exception handlers
    register_exception_handlers(app)

    # Instrument FastAPI application for OpenTelemetry / Azure Monitor
    instrument_fastapi_app(app)

    # Register request logging and CORS middleware
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount root router at /api
    app.include_router(root_router, prefix="/api")

    @app.get("/")
    def root():
        return {
            "service": "Ultrion HMS API (Modular)",
            "docs": "/docs",
            "health": "/api/health",
        }

    @app.get("/api/health")
    @app.get("/health")
    def health_check():
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "hms_migration.app.main:app",
        host=cfg.backend_host,
        port=cfg.backend_port,
        reload=True,
    )

