"""配置模块：统一加载 .env 与 config.json，并构建全局 AppConfig。"""

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


@dataclass(frozen=True)
class AppConfig:
    api_key: str
    model: str
    thread_id: str
    user_id: str
    tenant_id: str
    max_iterations: int
    enable_memory: bool
    short_term_ttl_seconds: int
    short_term_max_messages: int
    short_term_summary_threshold: int
    short_term_backend: str
    long_term_backend: str
    long_term_scope: str
    save_conversation_task: bool
    checkpointer_backend: str
    enable_milvus: bool
    memory_top_k: int
    redis_url: str
    postgres_dsn: str
    milvus_host: str
    milvus_port: int
    milvus_collection: str

    def with_overrides(self, **kwargs) -> "AppConfig":
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **cleaned)

    @staticmethod
    def _default_config_path() -> Path:
        return Path(__file__).resolve().parents[2] / "config.json"

    @staticmethod
    def _resolve_str(data: dict, field: str, env_key: str, default: str = "") -> str:
        env_value = os.getenv(env_key)
        if env_value is not None and str(env_value).strip() != "":
            return str(env_value).strip()
        file_value = data.get(field)
        if file_value is not None and str(file_value).strip() != "":
            return str(file_value).strip()
        return default

    @staticmethod
    def _resolve_int(data: dict, field: str, env_key: str, default: int) -> int:
        value = AppConfig._resolve_str(data, field, env_key, str(default))
        return int(value)

    @staticmethod
    def _resolve_bool(data: dict, field: str, env_key: str, default: bool) -> bool:
        value = AppConfig._resolve_str(data, field, env_key, "true" if default else "false")
        return value.lower() == "true"

    @staticmethod
    def from_file(path: str | Path | None = None) -> "AppConfig":
        config_path = Path(path) if path else AppConfig._default_config_path()
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件格式错误")
        api_key = AppConfig._resolve_str(data, "api_key", "DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ValueError(
                f"缺少 DASHSCOPE_API_KEY 配置，请在 {config_path} 中填写 api_key，或设置环境变量 DASHSCOPE_API_KEY"
            )
        model = AppConfig._resolve_str(data, "model", "MODEL", "qwen-plus")
        thread_id = AppConfig._resolve_str(data, "thread_id", "THREAD_ID", "default")
        user_id = AppConfig._resolve_str(data, "user_id", "USER_ID", "default_user")
        tenant_id = AppConfig._resolve_str(data, "tenant_id", "TENANT_ID", "default_tenant")
        max_iterations = min(1, max(0, AppConfig._resolve_int(data, "max_iterations", "MAX_ITERATIONS", 1)))
        enable_memory = AppConfig._resolve_bool(data, "enable_memory", "ENABLE_MEMORY", True)
        short_term_ttl_seconds = AppConfig._resolve_int(data, "short_term_ttl_seconds", "SHORT_TERM_TTL_SECONDS", 604800)
        short_term_max_messages = AppConfig._resolve_int(data, "short_term_max_messages", "SHORT_TERM_MAX_MESSAGES", 30)
        short_term_summary_threshold = AppConfig._resolve_int(
            data, "short_term_summary_threshold", "SHORT_TERM_SUMMARY_THRESHOLD", 20
        )
        short_term_backend = AppConfig._resolve_str(data, "short_term_backend", "SHORT_TERM_BACKEND", "postgres").lower()
        long_term_backend = AppConfig._resolve_str(data, "long_term_backend", "LONG_TERM_BACKEND", "postgres").lower()
        long_term_scope = AppConfig._resolve_str(data, "long_term_scope", "LONG_TERM_SCOPE", "user").lower()
        save_conversation_task = AppConfig._resolve_bool(data, "save_conversation_task", "SAVE_CONVERSATION_TASK", False)
        checkpointer_backend = AppConfig._resolve_str(data, "checkpointer_backend", "CHECKPOINTER_BACKEND", "auto").lower()
        enable_milvus = AppConfig._resolve_bool(data, "enable_milvus", "ENABLE_MILVUS", True)
        memory_top_k = AppConfig._resolve_int(data, "memory_top_k", "MEMORY_TOP_K", 6)
        redis_url = AppConfig._resolve_str(data, "redis_url", "REDIS_URL", "redis://127.0.0.1:6379")
        postgres_dsn = AppConfig._resolve_str(
            data, "postgres_dsn", "POSTGRES_DSN", "postgresql://127.0.0.1:5432/postgres"
        )
        milvus_host = AppConfig._resolve_str(data, "milvus_host", "MILVUS_HOST", "127.0.0.1")
        milvus_port = AppConfig._resolve_int(data, "milvus_port", "MILVUS_PORT", 19530)
        milvus_collection = AppConfig._resolve_str(data, "milvus_collection", "MILVUS_COLLECTION", "mult_agent_memory")
        return AppConfig(
            api_key=api_key,
            model=model,
            thread_id=thread_id,
            user_id=user_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
            short_term_ttl_seconds=short_term_ttl_seconds,
            short_term_max_messages=short_term_max_messages,
            short_term_summary_threshold=short_term_summary_threshold,
            short_term_backend=short_term_backend,
            long_term_backend=long_term_backend,
            long_term_scope=long_term_scope,
            save_conversation_task=save_conversation_task,
            checkpointer_backend=checkpointer_backend,
            enable_milvus=enable_milvus,
            memory_top_k=memory_top_k,
            redis_url=redis_url,
            postgres_dsn=postgres_dsn,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            milvus_collection=milvus_collection,
        )

    @staticmethod
    def from_env() -> "AppConfig":
        data: dict = {}
        api_key = AppConfig._resolve_str(data, "api_key", "DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ValueError("缺少 DASHSCOPE_API_KEY 环境变量")
        model = AppConfig._resolve_str(data, "model", "MODEL", "qwen-plus")
        thread_id = AppConfig._resolve_str(data, "thread_id", "THREAD_ID", "default")
        user_id = AppConfig._resolve_str(data, "user_id", "USER_ID", "default_user")
        tenant_id = AppConfig._resolve_str(data, "tenant_id", "TENANT_ID", "default_tenant")
        max_iterations = min(1, max(0, AppConfig._resolve_int(data, "max_iterations", "MAX_ITERATIONS", 1)))
        enable_memory = AppConfig._resolve_bool(data, "enable_memory", "ENABLE_MEMORY", True)
        short_term_ttl_seconds = AppConfig._resolve_int(data, "short_term_ttl_seconds", "SHORT_TERM_TTL_SECONDS", 604800)
        short_term_max_messages = AppConfig._resolve_int(data, "short_term_max_messages", "SHORT_TERM_MAX_MESSAGES", 30)
        short_term_summary_threshold = AppConfig._resolve_int(
            data, "short_term_summary_threshold", "SHORT_TERM_SUMMARY_THRESHOLD", 20
        )
        short_term_backend = AppConfig._resolve_str(data, "short_term_backend", "SHORT_TERM_BACKEND", "postgres").lower()
        long_term_backend = AppConfig._resolve_str(data, "long_term_backend", "LONG_TERM_BACKEND", "postgres").lower()
        long_term_scope = AppConfig._resolve_str(data, "long_term_scope", "LONG_TERM_SCOPE", "user").lower()
        save_conversation_task = AppConfig._resolve_bool(data, "save_conversation_task", "SAVE_CONVERSATION_TASK", False)
        checkpointer_backend = AppConfig._resolve_str(data, "checkpointer_backend", "CHECKPOINTER_BACKEND", "auto").lower()
        enable_milvus = AppConfig._resolve_bool(data, "enable_milvus", "ENABLE_MILVUS", True)
        memory_top_k = AppConfig._resolve_int(data, "memory_top_k", "MEMORY_TOP_K", 6)
        redis_url = AppConfig._resolve_str(data, "redis_url", "REDIS_URL", "redis://127.0.0.1:6379")
        postgres_dsn = AppConfig._resolve_str(
            data, "postgres_dsn", "POSTGRES_DSN", "postgresql://127.0.0.1:5432/postgres"
        )
        milvus_host = AppConfig._resolve_str(data, "milvus_host", "MILVUS_HOST", "127.0.0.1")
        milvus_port = AppConfig._resolve_int(data, "milvus_port", "MILVUS_PORT", 19530)
        milvus_collection = AppConfig._resolve_str(data, "milvus_collection", "MILVUS_COLLECTION", "mult_agent_memory")
        return AppConfig(
            api_key=api_key,
            model=model,
            thread_id=thread_id,
            user_id=user_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
            short_term_ttl_seconds=short_term_ttl_seconds,
            short_term_max_messages=short_term_max_messages,
            short_term_summary_threshold=short_term_summary_threshold,
            short_term_backend=short_term_backend,
            long_term_backend=long_term_backend,
            long_term_scope=long_term_scope,
            save_conversation_task=save_conversation_task,
            checkpointer_backend=checkpointer_backend,
            enable_milvus=enable_milvus,
            memory_top_k=memory_top_k,
            redis_url=redis_url,
            postgres_dsn=postgres_dsn,
            milvus_host=milvus_host,
            milvus_port=milvus_port,
            milvus_collection=milvus_collection,
        )
