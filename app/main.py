"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.dependencies import ApiContext
from app.api.dev import router as dev_router
from app.api.health import router as health_router
from app.api.telegram import router as telegram_router
from app.api.whatsapp import router as whatsapp_router
from app.core.config import get_settings
from app.core.exceptions import BookablError
from app.core.logging import configure_logging


def create_app(api_context: ApiContext | None = None) -> FastAPI:
    """Build the API, optionally with an explicitly supplied test context."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = api_context.settings if api_context else get_settings()
        configure_logging(settings.log_level)
        if api_context is None:
            from app.bootstrap import build_runtime

            runtime = await build_runtime(settings)
            application.state.runtime = runtime
            application.state.api_context = runtime.api_context
            if settings.run_workers_in_api:
                from app.workers.supervisor import supervise_workers

                async with supervise_workers(runtime):
                    yield
                return
        yield

    application = FastAPI(title="BOOKABL", version="0.1.0", lifespan=lifespan)
    if api_context is not None:
        application.state.api_context = api_context
    application.include_router(health_router)
    application.include_router(whatsapp_router)
    application.include_router(telegram_router)
    route_settings = api_context.settings if api_context else get_settings()
    if route_settings.app_env == "dev":
        application.include_router(dev_router)

    @application.exception_handler(BookablError)
    async def handle_bookabl_error(_request: Request, exc: BookablError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    return application


app = create_app()
