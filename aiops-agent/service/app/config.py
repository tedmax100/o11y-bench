from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"

    # Direct native-API endpoints. Defaults target localhost for host-side dev
    # (kubectl port-forward); the in-cluster Deployment overrides these with
    # internal DNS (prometheus.demo.svc:9090 ...) via env.
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"

    github_token: str = ""

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
