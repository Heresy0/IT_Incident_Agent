"""Minimal IT runtime configuration; no research, vector store or search dependency."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv


@dataclass(frozen=True)
class IncidentConfig:
    api_key: str = field(default='', repr=False)
    model: str = 'qwen-turbo'
    postgres_dsn: str = field(default='postgresql://127.0.0.1:5432/postgres', repr=False)

    @classmethod
    def from_file(cls, path):
        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / '.env.local', override=False)
        load_dotenv(root / '.env', override=False)
        config_path = Path(path)
        data = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError('配置文件必须是对象')
        def value(name, env, default):
            return os.getenv(env) or data.get(name) or default
        return cls(api_key=value('api_key', 'DASHSCOPE_API_KEY', ''),
                   model=value('model', 'MODEL', 'qwen-turbo'),
                   postgres_dsn=value('postgres_dsn', 'POSTGRES_DSN', cls.postgres_dsn))
