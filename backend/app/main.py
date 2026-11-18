"""ASGI entrypoint: uvicorn app.main:app."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.v1 import (
    auth,
    categories,
    garages,
    maintenance_records,
    pm_plans,
    reports,
    users,
    vehicle_imports,
    vehicles,
)
from app.api.v1 import settings as settings_api
from app.auth.dependencies import enforce_api_session
from app.config import Settings
from app.db import Database, get_session
from app.errors import register_error_handlers
from app.seed import seed_admin


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = Settings.from_env() if settings is None else settings
    database = Database(configuration.database_url)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            if configuration.sandbox_seed:

                def bootstrap() -> None:
                    with database.sessions() as session:
                        seed_admin(session, configuration)

                await run_in_threadpool(bootstrap)
            yield
        finally:
            database.dispose()

    application = FastAPI(title="MTN API", version="0.1.0", lifespan=lifespan)
    application.state.settings = configuration
    application.state.database = database
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(configuration.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    register_error_handlers(application)
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(enforce_api_session)])
    api.include_router(auth.router)
    api.include_router(users.router)
    api.include_router(garages.router)
    api.include_router(categories.router)
    api.include_router(settings_api.router)
    api.include_router(vehicles.router)
    api.include_router(vehicle_imports.router)
    api.include_router(maintenance_records.router)
    api.include_router(pm_plans.router)
    api.include_router(reports.router)
    application.include_router(api)

    @application.get(
        "/health/ready",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
    )
    def readiness(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
        try:
            ready = session.execute(select(1)).scalar_one() == 1
        except SQLAlchemyError:
            ready = False
        # DB failure is expected here; neither its URL nor driver error is exposed.
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
            headers={"Cache-Control": "no-store"},
        )

    return application


app = create_app()
