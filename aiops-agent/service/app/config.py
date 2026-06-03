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

    # Hard ceiling on tool calls per turn, enforced by the RCA graph's budget
    # guard (not just the system prompt). Matches the prompt's stated ceiling.
    # The push/webhook entrypoint can override this per-run later.
    tool_call_budget: int = 4

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    # --- Alert webhook (PUSH-mode RCA, doc v3 §4) --------------------------
    # Shared secret for POST /webhook/alert, passed as X-Webhook-Secret header
    # or ?token=. fail-closed: empty → endpoint disabled (503); set → request
    # must match or it's rejected (401). doc v3 §4.5 / §6.1.
    webhook_secret: str = ""
    # Same fingerprint inside this window folds into the running investigation
    # instead of spawning a new one — alert storms must not fan out. doc v3 §4.2.
    alert_cooldown_seconds: int = 600
    # Headless runs have no human to interrupt them, so their own hard ceiling.
    webhook_tool_call_budget: int = 6
    # Optional findings sink: if both set, the headless conclusion is posted as a
    # Grafana annotation. Absent → the sink just logs.
    grafana_url: str = ""
    grafana_token: str = ""

    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
