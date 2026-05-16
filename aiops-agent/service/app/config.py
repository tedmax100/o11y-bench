from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"

    mcp_grafana_url: str = "http://localhost:8080/mcp"

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
