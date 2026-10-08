from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    app_name: str = "IT Incident Agent"
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    config_path: str = str(Path(__file__).resolve().parents[2] / "config.json")

    model_config = SettingsConfigDict(
        env_file=(str(Path(__file__).resolve().parents[2] / ".env"),
                  str(Path(__file__).resolve().parents[2] / ".env.local")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def cors_origins(self) -> list[str]:
        values = [item.strip() for item in self.cors_allow_origins.split(",")]
        return [item for item in values if item]
