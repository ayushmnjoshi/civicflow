"""FastAPI application — main entry point.

Usage:
    uvicorn app.main:app --reload          (dev, from backend/)
    uvicorn app.main:create_app() --reload (also works)
"""

import logging
import time

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.routers import applications, auth, departments, documents, health, internal, notifications, officer

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if settings.app_env != "production" else logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── factory ───────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    """Application factory — creates and configures the FastAPI instance."""

    app = FastAPI(
        title="CivicFlow API",
        description="AI-powered civic services platform.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── exception handlers ────────────────────────────────────────────────────

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        """Convert all HTTPException instances to the standard error shape."""
        msg = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            headers=dict(exc.headers) if exc.headers else None,
            content={
                "error": {
                    "code": exc.status_code,
                    "message": msg,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # exc.errors() may contain non-JSON-serializable objects in ctx["error"]
        # (e.g. ValueError instances from pydantic v2 field_validators).  Sanitise
        # them to their string representation so JSONResponse never crashes.
        def _safe_errors(errs: list) -> list:
            safe = []
            for e in errs:
                e2 = dict(e)
                if "ctx" in e2 and isinstance(e2["ctx"], dict):
                    e2["ctx"] = {
                        k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                        for k, v in e2["ctx"].items()
                    }
                safe.append(e2)
            return safe

        safe = _safe_errors(exc.errors())
        logger.warning("Validation error on %s: %s", request.url, safe)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "message": "Request validation failed.",
                    "detail": safe,
                }
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_exception_handler(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        logger.error("Database error on %s: %s", request.url, exc, exc_info=False)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": status.HTTP_503_SERVICE_UNAVAILABLE,
                    "message": "Database unavailable.",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "message": "An unexpected error occurred.",
                }
            },
        )

    # ── request logging middleware ────────────────────────────────────────────

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s → %d  (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
        return response

    # ── routers ───────────────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(departments.router)
    app.include_router(applications.router)
    app.include_router(documents.router)
    app.include_router(internal.router)
    app.include_router(officer.router)
    app.include_router(notifications.router)

    return app


# Module-level app instance used by uvicorn directly:
#   uvicorn app.main:app --reload
app = create_app()
