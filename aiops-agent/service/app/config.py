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

    # --- Kubernetes read-only signal source (v3 §2) ------------------------
    # The demo workloads live in ns `demo` and label pods/deployments with
    # `app=<service_name>`. Both are config so this isn't pinned to the demo.
    # The agent uses the pod's read-only ServiceAccount in-cluster, or the local
    # kubeconfig host-side; if neither is available the k8s tools degrade to a
    # clean "unavailable" result rather than crashing the turn.
    k8s_namespace: str = "demo"
    k8s_label_key: str = "app"

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

    # --- Runbook / SOP layer (v3 §5, ARE gap-analysis §4.2 step 5) ---------
    # Tier 0 (link) + Tier 1 (read-only diagnostics). When a firing alert matches
    # a runbook, its rendered steps are injected into the headless RCA and (if
    # enabled) its read-only diagnostics are auto-run to confirm preconditions.
    # Remediation steps are rendered for the on-call but never executed here.
    runbook_dir: str = "runbooks"
    runbook_enabled: bool = True
    runbook_run_diagnostics: bool = True

    # --- Calibration-error (CE) harness (ARE gap-analysis §4.2 step 2) -----
    # Each headless run logs its Findings.confidence here; correctness is filled
    # in offline (o11y-bench score or ground-truth match) and CE computed from
    # the pairs. Prerequisite for any Tier 2 confidence threshold. Best-effort:
    # a logging failure never breaks an investigation.
    calibration_enabled: bool = True
    # Legacy JSONL path — kept only as the source for the one-time migration into
    # the SQLite store (store_path). Live reads/writes go through app.store.
    calibration_log_path: str = "calibration.jsonl"
    # A graded run counts as "correct" when its o11y-bench score clears this.
    calibration_correct_threshold: float = 0.7

    # --- Headless investigation store (plugin visibility; gap-analysis step 6) -
    # Each alert-driven RCA is recorded so the plugin can list conclusions +
    # governance decisions. Read-only display; best-effort recording.
    investigations_enabled: bool = True
    investigations_log_path: str = "investigations.jsonl"

    # --- Governance gate + action registry (ARE Governance plane; v3 §5.2) --
    # Decides per proposed remediation: AUTO / PROPOSE / ESCALATE, from the run's
    # confidence AND measured calibration. `actions_enabled` is the master kill
    # switch for ACTUALLY executing a registered action — it stays False until a
    # human-reviewed Tier 2 enablement (step 7). The gate produces proposals
    # regardless; nothing mutates state while this is False.
    actions_enabled: bool = False
    governance_conf_high: float = 0.8

    # --- Action-request lifecycle (step 7 後半 7b-1) -----------------------
    # Each AUTO/PROPOSE governance decision becomes a tracked ActionRequest the
    # plugin can approve/reject. Creation is best-effort (gated here); execution
    # is still kill-switched by actions_enabled above. ESCALATE makes no request.
    action_requests_enabled: bool = True
    # Approvals go stale: a request not acted on within this window is expired so
    # its preconditions can't be acted on after the world has moved (TOCTOU).
    approval_ttl_seconds: int = 900

    # --- Dry-run + blast-radius policy (step 7 後半 7b-2) -------------------
    # Read-only gates that run before any (kill-switched) execution: re-verify the
    # runbook's preconditions still hold, and refuse actions whose computed blast
    # radius exceeds policy. All fail-closed — an unreadable dry-run aborts.
    execution_namespace_allowlist: list[str] = ["demo"]
    max_blast_pods: int = 5          # affected-pod ceiling; over → abort
    deny_singletons: bool = True     # single-replica targets are riskier → refuse
    # Namespaces no action may ever touch, regardless of allowlist.
    protected_namespaces: list[str] = ["kube-system", "kube-public", "kube-node-lease"]

    # --- Circuit breaker + idempotency (step 7 後半 7b-3) ------------------
    # Stops automation runaway + rollback flapping. Global sliding-window rate
    # limit; per-(action,target) trips open after N consecutive failures and stays
    # open until a human resets it (POST /actions/breaker/reset). Idempotency keys
    # an execution by (action, target, incident fp) so an alert storm can't act on
    # the same target twice. Breaker state is durable (survives restart) — a
    # breaker that forgets it tripped isn't a safety mechanism.
    breaker_enabled: bool = True
    breaker_max_actions_per_window: int = 3
    breaker_window_seconds: int = 3600
    breaker_fail_threshold: int = 2   # consecutive failures on a target → trip open

    # --- Design-alert capability (ARE gap-analysis §4.2 step 6 / v3 §6) -----
    # First side-effecting + human-in-the-loop capability: the agent proposes an
    # alert rule (```alert``` block); a human button click POSTs it to
    # /alerts/provision, which writes it to Grafana. Reversible (rules can be
    # deleted) and human-confirmed, so it defaults ON — unlike `actions_enabled`
    # (autonomous mutation, default off). Still fail-closed: provisioning also
    # needs grafana_url + grafana_token, else the endpoint refuses.
    governance_conf_low: float = 0.5
    # If measured overconfidence exceeds this, AUTO is downgraded to PROPOSE.
    governance_max_overconfidence: float = 0.1
    # AUTO requires at least this many labeled runs — autonomy must be earned.
    governance_min_labeled_runs: int = 20

    alert_provisioning_enabled: bool = True

    # --- Persistence layer (step 7 後半 7b-0) ------------------------------
    # Durable, atomic store (SQLite) replacing the ephemeral JSONL files for the
    # CE harness + investigation log (and later action_requests/audit). On a PVC
    # in-cluster (STORE_PATH=/data/aiops.db) so it survives the rollout restarts
    # the execution plane itself triggers. See app/store.py.
    store_path: str = "aiops.db"

    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
