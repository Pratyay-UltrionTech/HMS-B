import logging
import os

import uvicorn

from app.config import get_settings

logger = logging.getLogger("hms.api")

if __name__ == "__main__":
    settings = get_settings()
    logger.info("Starting Ultrion HMS API (pid=%s) on http://%s:%s", os.getpid(), settings.backend_host, settings.backend_port)
    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=True,
        log_level="info",
    )
