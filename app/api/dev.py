"""Development-only operational endpoints."""

from fastapi import APIRouter, Request

from app.api.dependencies import get_api_context
from app.core.exceptions import ConfigurationError

router = APIRouter(prefix="/dev", tags=["development"])


@router.post("/trigger-due-jobs")
async def trigger_due_jobs(request: Request) -> dict[str, int]:
    """Force one scheduler pass for local demos and acceptance tests."""

    scheduler = get_api_context(request).scheduler
    if scheduler is None:
        raise ConfigurationError("Scheduler is not configured")
    return {"processed": await scheduler.run_once()}

