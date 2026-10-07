import logging
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from backend.config import AppSettings
from backend.router import health_router, research_router
from backend.service import get_workflow_service
from backend.auth import router as auth_router, get_current_principal, Principal
from backend.router.memory_router import router as memory_router
from mult_agents.harness.telemetry import configure_logging, METRICS


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
configure_logging()
logging.getLogger("mult_agents").setLevel(logging.INFO)
logging.getLogger("backend").setLevel(logging.INFO)


def create_app() -> FastAPI:
    settings = AppSettings()
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
        expose_headers=["X-Research-Run-ID"],
    )
    app.include_router(health_router)
    app.include_router(research_router)
    app.include_router(auth_router)
    app.include_router(memory_router)
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
