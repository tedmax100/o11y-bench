# Telemetry Schema Catalog — semantics & conventions

This file describes what the signals **mean** and the **conventions** they
follow. It deliberately does **not** list the exhaustive inventory of metrics,
span names or log fields — those drift, and they are supplied live instead:

- A **capability snapshot** is injected per question for the service(s) it's
  about (real metric names + labels, span names, log fields, read just now).
- The `discover_metrics` / `discover_span_names` / `discover_log_fields` tools
  fetch the same on demand for any service.

**Trust the live snapshot over this file for "what exists"** (exact metric /
span / field names). Use this file for "what it means" and how the stack is
wired.

## Services

| service | role | code path (in repo) | git_version |
|---------|------|---------------------|-------------|
| webapp | public edge — receives external HTTP, forwards to api-gateway | `demo-services/services/webapp/` | v5.2.0 |
| api-gateway | thin proxy router to backend services | `demo-services/services/api-gateway/` | v4.0.0 |
| user-service | user lookup + auth check | `demo-services/services/user/` | v1.3.0 |
| order-service | products / cart / orders. Calls user + payment | `demo-services/services/order/` | v3.1.2 |
| payment-service | charges (authorise / decline / refund) | `demo-services/services/payment/` | read it live |

**All services live in one monorepo: `tedmax100/o11y-bench`** — that is the
`repo` for `github_compare` / `github_get_file` and matches the `git_repo`
label on every signal. **Only `payment-service` currently has real git tags**; `github_compare` on
the other services 404s, so only run deploy correlation for payment-service.
Which tags exist, and which one an incident sits on, come from a tool result —
the `git_version` label on the signal, never from this file.

Dependency edges (caller → callee). The **authoritative, queryable** version of
this graph — plus criticality tier and journey membership — is the Signal Plane
topology (`app/signals/topology.yaml`), injected per-RCA as a "Signal context"
block; **trust that block over this prose** when present. This stays as the
fallback for when signal context isn't injected:

```
webapp → api-gateway
api-gateway → {user-service, order-service, payment-service}
order-service → {user-service, payment-service}
user-service (leaf)        payment-service (leaf)
```

HTTP endpoints (owning service):

| method | path | owner |
|--------|------|-------|
| GET | /api/users, /api/users/{id} | user-service |
| GET | /api/products, /api/cart | order-service |
| POST | /api/orders | order-service |
| POST | /api/payments | payment-service (proxied via api-gateway → `/charge`) |

## Cross-signal conventions

- Every signal carries `service_name`, `git_version` (deployed revision),
  `git_repo` (always `tedmax100/o11y-bench`), and `deployment_environment=demo`.
  `service_version` mirrors `git_version` (OTel semconv); prefer `git_version`
  for cross-signal joins and the GitHub tools.
- **There is no `up{}` for application services.** The OTel Collector pushes via
  remote_write (not scraped), so `up{service_name="..."}` is always empty
  regardless of health. Check liveness with a fresh sample on a counter the
  service emits, e.g. `rate(<some_total>[5m]) > 0`.
- **Loki** — the *only* indexable stream-selector labels are `service_name`,
  `git_repo`, `git_version`, `deployment_environment`. The selector key is
  `service_name` (**NOT `service`** — `{service="..."}` matches nothing).
  Everything else (`event`, `trace_id`, `detected_level`, business fields) is
  **structured metadata**: filter it *after* the selector (e.g.
  `| event="payment.declined"`), never as a `{...}` selector. Do not use
  `service` / `app` / `container` / `pod` / `job` as selectors — not indexed.
- **Log severity / finding "errors"** — these services log business events at
  **INFO**; there is **no `level` field and no ERROR-level lines** for the demo
  incidents. The severity field that exists is `detected_level` / `severity_text`
  (all `info` here). So **find incidents via the `event` field, not severity** —
  e.g. payment declines are `| event="payment.declined"`, gateway failures are
  `| event="payment.gateway_error"`. `| level="ERROR"` matches nothing.
- **Tempo** — resource/span attributes use **dotted** names
  (`resource.service.name`, `span.http.route`, `status`). Trace structure,
  root→leaf: `webapp → api-gateway → <target service> → <dep service>`.
- **Prometheus** — OTel→remote_write with `resource_to_telemetry_conversion`,
  so resource attrs become labels. Histograms are `*_bucket/_sum/_count`
  (use `histogram_quantile` over `_bucket`); counters end `_total`. Always
  aggregate (`sum by (...)`, `topk`) — don't fetch raw per-series.

## BizEvent enum & per-event fields (Loki structured metadata)

`event` is low-cardinality (safe to `sum by (event)`). The values and the extra
fields each carries are domain knowledge, not discoverable from labels alone:

```
payment.requested  payment.authorized  payment.declined  payment.refunded
payment.gateway_error   order.created  order.updated  order.cancelled
user.logged_in  user.registered  user.auth_failed
http.request_received  http.request_failed   cache.miss  deployment.started
```

| event | extra fields |
|-------|--------------|
| `payment.requested` / `payment.authorized` | `order_id`, `user_id`, `amount_cents`, `payment_id` |
| `payment.declined` | `order_id`, `reason` (read the values off a result) |
| `payment.gateway_error` | `order_id` |
| `order.created` | `order_id`, `user_id`, `amount_cents` |
| `order.cancelled` | `user_id`, `reason` (`auth_failed` / `payment_declined` / `unknown_product`), `upstream_status` |
| `user.logged_in` / `user.auth_failed` | `user_id`, `reason` (read the values off a result) |
| `http.request_received` | `method`, `path` (template, e.g. `/api/users/{id}`) |
| `http.request_failed` | `upstream`, `status`, `reason` (`network`) |

## Query style (use live names from the snapshot)

```promql
# p95 latency per service (histogram → histogram_quantile over _bucket)
histogram_quantile(0.95, sum by (service_name, le) (rate(<duration>_bucket[5m])))
```
```logql
# incident events per service — services log at INFO; filter by `event`, NOT by
# level (no ERROR level exists). E.g. payment declines:
sum by (service_name) (count_over_time({deployment_environment="demo"} | event="payment.declined" [1h]))
```
```traceql
# errors originating in a service
{ resource.service.name = "<service>" && status = error }
```

### Where the identifying fields live

This section says where things ARE, not how to call a tool. Rules about which
arguments to pass live in the tools themselves, which check the call and answer a
wrong one with the rewrite — a rule repeated here would be a second opinion the
model follows over the tool's, and measurably was.

- **Stream labels vs structured metadata (Loki).** Indexable stream labels here
  are `service_name`, `git_repo`, `git_version`, `deployment_environment`.
  `trace_id`, `level`, `event` and the business fields are structured metadata,
  not stream labels.
- **git_version is everywhere** — a label on every metric and every log line, and
  on a trace's resource as `service.version` (trace searches return it).

## Feature flags

Services read feature flags from a `flags.json` ConfigMap (`payment-flags` for
payment-service), so **behaviour can change without a new image**. Two
consequences worth carrying into an investigation: a flag flip is a legitimate
"what changed" hypothesis even when no deploy is visible, and a flag flip that
ships together with a version bump looks exactly like a code regression from the
telemetry alone. Which flags exist and what each one does is in the service's
repo, not in this file — read the code diff (`github_compare` / `github_get_file`)
or the ConfigMap rather than assuming.

## Deploy correlation

When a spike correlates with a `git_version` boundary:

1. Repo is always `tedmax100/o11y-bench` (also the `git_repo` label).
2. Previous version = the `git_version` value just before the spike — read
   both values off the breakdown you already ran, don't assume a pair.
3. `github_compare("tedmax100/o11y-bench", base=<old>, head=<new>)` to see the
   diff (naturally scoped to that service's path).
4. If a suspicious file shows up, `github_get_file(...)` to read the new code.
5. Cite the commit SHA(s) + a one-line summary alongside the telemetry queries.
   Only payment-service has real tags today (see Services).

## Kubernetes (infra signal — the other half of deploy correlation)

The services run as Deployments in namespace **`demo`**, labelled
`app=<service_name>` (e.g. `app=payment-service`); pods also carry `git_version`
as a label. The k8s tools resolve a service to its objects through that label.

Use k8s to separate a **platform** failure from a **code** regression — the
distinction the telemetry alone can't always make:

| Symptom from k8s | Likely cause |
|---|---|
| `OOMKilled` (last_terminated) / rising `restarts` | memory limit too low / leak — infra/config, not logic |
| `CrashLoopBackOff` (waiting_reason) | container can't start (bad config, missing env, panic on boot) |
| `ImagePullBackOff` / `ErrImagePull` | bad image tag / registry — the deploy never ran |
| `ProgressDeadlineExceeded`, `available_replicas < desired` | rollout never went healthy — new version is NOT actually serving |
| `FailedScheduling` / `Evicted` | node resource pressure, not the service's code |

Rule of thumb: if a `git_version` boundary lines up with the incident AND
`k8s_deployment_status` shows the new revision became Available with healthy
replicas, the regression is in the **code** (→ `github_compare`). If the rollout
is stuck or pods are crashing, it's **infra/config** — say so and skip the code
diff. These tools are **read-only**; they never restart, scale, or delete.
