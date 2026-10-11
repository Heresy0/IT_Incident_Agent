from functools import lru_cache

from config import AppSettings
from config.incident import load_environment
from workflow.service import WorkflowService


@lru_cache(maxsize=1)
def get_workflow_service() -> WorkflowService:
    load_environment()
    settings = AppSettings()
    return WorkflowService(config_path=settings.config_path)


__all__ = ["WorkflowService", "get_workflow_service"]
