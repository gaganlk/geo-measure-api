"""Application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import files as files_router
from app.api.schemas import HealthResponse
from app.config import get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.db.database import create_tables

# ---------------------------------------------------------------------------
# Version (single source of truth)
# ---------------------------------------------------------------------------
__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Create upload directory and DB tables on startup."""
    get_settings().UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await create_tables()
    yield


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="Geo Measure API",
        description=(
            "Upload a zipped Shapefile or KML and receive area/length measurements "
            "for every feature, reprojected to the best-fit UTM zone."
        ),
        version=__version__,
        lifespan=lifespan,
        # Constrain request body size at the framework level
        # (actual byte checking is done in the upload endpoint)
    )

    # ------------------------------------------------------------------
    # Exception handlers
    # ------------------------------------------------------------------
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    app.include_router(files_router.router, prefix="/api")

    # ------------------------------------------------------------------
    # Health endpoint
    # ------------------------------------------------------------------
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["meta"],
        summary="Health check",
    )
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    return app


app = create_app()
