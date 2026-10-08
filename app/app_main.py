import logging
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import AppSettings
from api.health import router as health_router
from workflow.dependencies import get_workflow_service
from api.auth import router as auth_router, get_current_principal, Principal
from api.incidents import router as incident_router
from api.runs import router as run_router, get_run, run_events
from observability.telemetry import configure_logging, METRICS


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
configure_logging()
logging.getLogger("incident").setLevel(logging.INFO)


def create_app(settings=None) -> FastAPI:
    settings = settings or AppSettings()
    @asynccontextmanager
    async def lifespan(app):
        service = get_workflow_service()
        await asyncio.to_thread(service.start)
        try:
            yield
        finally:
            await asyncio.to_thread(service.close)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Run-ID", "X-Research-Run-ID"],
    )
    app.include_router(health_router)
    app.include_router(run_router)
    app.include_router(auth_router)
    # Temporary read-only URL aliases for the current front end and old bookmarks.
    app.add_api_route('/api/v1/research/runs/{run_id}', get_run, methods=['GET'], include_in_schema=False)
    app.add_api_route('/api/v1/research/runs/{run_id}/events', run_events, methods=['GET'], include_in_schema=False)
    app.include_router(incident_router)
    @app.get("/metrics", include_in_schema=False)
    def metrics(principal: Principal = Depends(get_current_principal)):
        if principal.role != "operator":
            raise HTTPException(403, "该接口仅供演示运维身份访问。")
        return Response(METRICS.render(), media_type="text/plain; version=0.0.4")
    return app


app = create_app()


if __name__ == "__main__":
    runtime_settings = AppSettings()
    uvicorn.run(
        "app_main:app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=runtime_settings.app_env == "development",
    )
