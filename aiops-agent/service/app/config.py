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
    max_blast_pods: int = 5  # affected-pod ceiling; over → abort
    deny_singletons: bool = True  # single-replica targets are riskier → refuse
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
    breaker_fail_threshold: int = 2  # consecutive failures on a target → trip open

    # --- Verify + rollback (step 7 後半 7b-4) ---------------------------------
    verify_delay_seconds: int = 60  # settle window between execute and verify query
    require_rollback_contract: bool = True  # no rollback contract → executor refuses

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
    # Of those, at least this many must be human/grader labels (source not
    # "remediation-verified/-failed"). Self-produced labels alone cannot unlock AUTO.
    governance_min_human_labeled_runs: int = 20
    # Which `grading_mode`s may enter the calibration curve the gate reads. The
    # ECE/overconfidence math assumes `correct=1` means "the claim stated at this
    # confidence was right", and only `culprit` rows mean that: an `inconclusive`
    # row's `correct` says "it hedged appropriately", which is a different
    # question on the same column. Mixing them lets one mode's error cancel the
    # other's. Rows with no recorded mode are excluded (fail-closed).
    governance_calibration_modes: list[str] = ["culprit"]

    # --- Draft runbook synthesis (knowledge-loop §1 閉環二) ----------------
    # When an investigation is labeled correct=True and no active runbook matched
    # the alert, synthesize a draft runbook YAML and write it to
    # `runbook_dir/drafts/`. If `draft_runbook_pr_enabled` is True (requires
    # `github_token` + `draft_runbook_repo`), also open a GitHub PR for review.
    draft_runbook_enabled: bool = True
    draft_runbook_pr_enabled: bool = False
    # owner/repo of the Git repo where runbooks live (e.g. "acme/o11y-runbooks").
    # Required only when `draft_runbook_pr_enabled` is True.
    draft_runbook_repo: str = ""

    # --- Loop engineering (knowledge-loop §4.4) ----------------------------
    # If the extracted Findings.confidence is below this after a headless run,
    # re-invoke the agent on the same thread asking it to pivot to a different
    # hypothesis. Gated by max_hypothesis_loops so it can't loop indefinitely.
    confidence_loop_threshold: float = 0.6
    max_hypothesis_loops: int = 3

    # --- Learn 閉環效度約束 (7b-5 §6.2) ------------------------------------
    # Whether remediation verify outcomes are written back as CE correctness labels.
    # Default False: remediation outcomes only feed fix-efficacy + breaker, not CE.
    learn_remediation_into_ce: bool = False

    alert_provisioning_enabled: bool = True

    # --- Persistence layer (step 7 後半 7b-0) ------------------------------
    # Durable, atomic store (SQLite) replacing the ephemeral JSONL files for the
    # CE harness + investigation log (and later action_requests/audit). On a PVC
    # in-cluster (STORE_PATH=/data/aiops.db) so it survives the rollout restarts
    # the execution plane itself triggers. See app/store.py.
    store_path: str = "aiops.db"

    # --- Signal Plane (decision-grade telemetry; signal-plane-design s1) ----
    # A first-class topology/criticality/journey artifact injected into the RCA
    # as decision-grade context, replacing the prose dependency graph in the
    # catalog. Read-only enrichment upstream of the reasoning core; fail-open —
    # an unreadable artifact is skipped, never blocking a run. `topology_path`
    # empty → the topology.yaml shipped beside app/signals/.
    signal_plane_enabled: bool = True
    topology_path: str = ""
    # Per-service signal contracts (s3): authoritative SLI queries + freshness +
    # exclusions. Empty path → contracts.yaml shipped beside app/signals/.
    signal_contracts_path: str = ""
    # Weaver semconv registry path (schema single source of truth). Empty →
    # repo-root demo-services/weaver/registry (dev/CI only; not shipped in the
    # agent image). Used by the dev-time contract↔registry alignment check.
    weaver_registry_path: str = ""
    # Per-service signal fragments (ownership: each service owns its declaration).
    # Empty → repo-root demo-services/services (dev/CI only). The compiler
    # (app/signals/compile.py) aggregates *.signal.yaml into the agent's
    # topology.yaml/contracts.yaml; the agent ships those compiled artifacts.
    signal_fragments_dir: str = ""
    # --- Data-Quality SLO (s5; ARE flagship #2) ----------------------------
    # The topology reconcile (s2) drift + its freshness become a DQ verdict that
    # governance reads: when the signal model isn't decision-grade (drift, stale,
    # or never reconciled) autonomy is narrowed (AUTO → PROPOSE), mirroring how
    # calibration error gates autonomy. "Confidently acting on a wrong map" is as
    # dangerous as "confidently wrong" — both must earn AUTO.
    dq_min_score: float = 0.9  # declared/observed agreement floor for proven-good
    dq_max_reconcile_age_seconds: int = 3600  # a reconcile older than this → DQ stale
    # Dependency-health blame propagation (s4): before the agent loop, run each
    # neighbour's error SLI live (read-only, off the agent budget) so the agent
    # knows whether the symptom is inherited from a failing downstream dep. A
    # neighbour's error ratio over this threshold is "unhealthy".
    signal_dependency_health_enabled: bool = True
    signal_health_error_threshold: float = 0.05
    signal_health_max_neighbors: int = 6
    # s4.2 edge-attributed impact: when a downstream is unhealthy, measure the
    # caller's own failures attributed to it (the edge's `attribution` query) now
    # vs a baseline `offset` ago. A per-second delta over the floor means the
    # caller is *materially* impacted (a real symptom), not just topologically
    # adjacent — so a baseline-level rate isn't mistaken for incident impact.
    signal_health_baseline_offset: str = "1h"
    signal_health_impact_min_delta: float = 0.05

    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
