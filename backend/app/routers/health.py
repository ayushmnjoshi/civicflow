"""Health-check router.

GET /health
  200  {"status": "ok", "database": "connected"}   — DB reachable
  503  {"error": {"code": 503, "message": "..."}}  — DB not reachable
"""

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dependencies import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Health check",
    response_description="Service and database status",
)
def health_check(db: Session = Depends(get_db)) -> JSONResponse:
    """
    Runs **SELECT 1** through the active DB session.

    - Returns **200** when the database is reachable.
    - Returns **503** when the database is not reachable.
    """
    try:
        db.execute(text("SELECT 1"))
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "database": "connected"},
        )
    except SQLAlchemyError as exc:
        logger.error("Health check — DB unreachable: %s", exc, exc_info=False)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": status.HTTP_503_SERVICE_UNAVAILABLE,
                    "message": "Database unavailable.",
                }
            },
        )
