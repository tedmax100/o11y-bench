# Telemetry Schema Catalog

Authoritative description of what's actually in the demo-services k3d cluster.
Use this to write correct queries instead of guessing label or field names.

## Services

Five services run in the `demo` namespace. Every service emits via OTel
(traces / metrics / logs) through `otel-collector` → Tempo / Prometheus
(remote-write) / Loki (native OTLP).

| service | role | github_repo | git_version |
|---------|------|-------------|-------------|
| webapp | public edge — receives external HTTP, forwards to api-gateway | tedmax100/o11y-bench-webapp | v5.2.0 |
| api-gateway | thin proxy router to backend services | tedmax100/o11y-bench-api-gateway | v4.0.0 |
| user-service | user lookup + auth check | tedmax100/o11y-bench-user-service | v1.3.0 |
| order-service | products / cart / orders. Calls user + payment | tedmax100/o11y-bench-order-service | v3.1.2 |
| payment-service | charges. Has the `payment_use_new_validator` flag | tedmax100/o11y-bench-payment-service | v2.4.1 |

The `github_repo` column maps each service to the `owner/repo` you pass to
`github_compare` / `github_get_file`. The `git_version` field on logs and the
`git_version` Prometheus label hold the deployed revision (a valid ref for the
github tools).

Dependency edges (caller → callee):

```
webapp → api-gateway
api-gateway → {user-service, order-service, payment-service}
order-service → {user-service, payment-service}
user-service (leaf)
payment-service (leaf)
```

HTTP endpoints (owning service):

| method | path | owner |
|--------|------|-------|
| GET | /api/users | user-service |
| GET | /api/users/{id} | user-service |
| GET | /api/products | order-service |
| GET | /api/cart | order-service |
| POST | /api/orders | order-service |
| POST | /api/payments | payment-service (proxied via api-gateway → `/charge`) |

## Loki

**Stream labels** (the *only* indexable selectors):

| label | value |
|-------|-------|
| `service_name` | one of: `webapp`, `api-gateway`, `user-service`, `order-service`, `payment-service` |
| `git_repo` | e.g. `tedmax100/o11y-bench-payment-service` |
| `git_version` | e.g. `v2.4.1` |
| `deployment_environment` | always `demo` |

These come from OTel resource attributes promoted by Loki's OTLP ingestion
(see `limits_config.otlp_config.resource_attributes` in `11-loki.yaml`).
Do **not** use `app`, `service`, `container`, `pod`, `job`, `level`, `event`
as stream labels — they are not indexed.

**Structured metadata** (label-filter only, after the selector):

| field | values |
|-------|--------|
| `level` | `INFO`, `WARN`, `ERROR` |
| `event` | `BizEvent` enum, see below |
| `trace_id` | 32-char hex — joins to Tempo |
| `span_id` | 16-char hex |

**Per-event extra fields** (also structured metadata, vary by event):

| event | extra fields |
|-------|--------------|
| `payment.requested` / `payment.authorized` | `order_id`, `user_id`, `amount_cents`, `payment_id` |
| `payment.declined` | `order_id`, `reason` (`new_validator_odd_cents` etc.) |
| `payment.gateway_error` | `order_id` |
| `order.created` | `order_id`, `user_id`, `amount_cents` |
| `order.cancelled` | `user_id`, `reason` (`auth_failed` / `payment_declined` / `unknown_product`), `upstream_status` |
| `user.logged_in` / `user.auth_failed` | `user_id`, `reason` (`not_found` / `transient`) |
| `http.request_received` | `method`, `path` (template form, e.g. `/api/users/{id}`) |
| `http.request_failed` | `upstream` (target URL), `status`, `reason` (`network`) |

**`BizEvent` enum** (low-cardinality — safe to `sum by (event)`):

```
payment.requested  payment.authorized  payment.declined
payment.refunded   payment.gateway_error
order.created      order.updated       order.cancelled
user.logged_in     user.registered     user.auth_failed
http.request_received  http.request_failed
cache.miss         deployment.started
```

### LogQL examples for this data

```logql
# Errors per service in the last hour — aggregate at the datasource
sum by (service_name) (
  count_over_time({deployment_environment="demo"} | level="ERROR" [1h])
)

# Decline rate on payment-service, grouped by reason
sum by (reason) (
  count_over_time({service_name="payment-service"} | event="payment.declined" [10m])
)

# THE v2 fallback aggregation — what wrap.py auto-rewrites large outputs into
topk(20,
  sum by (service_name, level, event, git_version) (
    count_over_time({service_name="payment-service"}[5m])
  )
)

# Find which git_version a spike happened on
sum by (git_version, event) (
  count_over_time({service_name="payment-service"} | event=~"payment\\..*" [10m])
)

# Pivot from an error log to its trace_id
{service_name="order-service"} | level="ERROR" | line_format "{{.message}} trace={{.trace_id}}"
```

## Prometheus

OTel → Prometheus via `prometheusremotewrite` with `resource_to_telemetry_conversion: true`,
so resource attributes become metric labels.

**There is no `up{}` metric for any application service.** Prometheus only
generates `up` for targets it scrapes directly; here the OTel Collector pushes
via remote_write, so `up{service_name="..."}` is **always empty** regardless of
whether the service is healthy. To check liveness use a fresh sample on a
counter the service actually emits, e.g.
`rate(http_server_duration_milliseconds_count{service_name="<svc>"}[5m]) > 0`
or, for payment specifically, `rate(payment_charges_total[5m]) > 0`.

**Labels on every metric**:

- `service_name` — same value as the Loki stream label
- `git_repo`, `git_version` — promoted from OTel resource attrs
- `deployment_environment` — `demo`

**Application metrics** (created by `o11y_shared` and the service code):

| metric | type | extra labels | owner |
|--------|------|--------------|-------|
| `payment_charges_total` | counter | `status` (`authorized` / `declined` / `error`), optional `reason` | payment-service |
| `payment_charge_duration_seconds_{bucket,sum,count}` | histogram | `status`, `le` | payment-service |
| `orders_total` | counter | `status` (`created` / `cancelled` / `error`), `reason` when not created | order-service |
| `order_create_duration_seconds_{bucket,sum,count}` | histogram | `status`, `le` | order-service |
| `user_lookups_total` | counter | `op` (`list` / `get`) | user-service |
| `user_auth_checks_total` | counter | — | user-service |

**Auto-instrumentation metrics** (from `opentelemetry-instrumentation-fastapi` / `httpx`):

| metric | extra labels | notes |
|--------|--------------|-------|
| `http_server_duration_milliseconds_{bucket,sum,count}` | `http_method`, `http_status_code`, `http_route` | server-side |
| `http_client_duration_milliseconds_{bucket,sum,count}` | `http_method`, `http_status_code`, `net_peer_name` | outgoing httpx calls |

### PromQL examples

```promql
# p95 latency per service for the order create handler
histogram_quantile(0.95,
  sum by (service_name, le) (rate(order_create_duration_seconds_bucket[5m]))
)

# Payment error rate per git_version (v2 cross-version comparison)
sum by (git_version) (rate(payment_charges_total{status="error"}[5m]))
/
sum by (git_version) (rate(payment_charges_total[5m]))

# HTTP 5xx rate per service (auto-instrumentation)
sum by (service_name) (
  rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[5m])
)
```

## Tempo

Trace structure per request (root → leaf):

```
webapp        "GET /api/<path>"      kind=server
  ↳ api-gateway  "GET /api/<path>"   kind=server
    ↳ <target service> "GET /api/..." kind=server
      ↳ <dep service>  httpx          kind=client → server (if any)
```

**Resource attributes** (queryable): `service.name`, `git_repo`, `git_version`,
`deployment.environment`.

**Span attributes** (auto-instrumentation): `http.method`, `http.route`,
`http.url`, `http.status_code`, `net.peer.name`.

### TraceQL examples

```traceql
# All errors originating in payment-service in the window
{ resource.service.name = "payment-service" && status = error }

# Slow order creation traces
{ resource.service.name = "order-service" && span.http.route = "/api/orders" && duration > 500ms }

# Cross-version: traces hitting the newly deployed payment version
{ resource.service.name = "payment-service" && resource.git_version = "v2.5.0" }
```

## Feature flags & incident scenarios

Currently planted: **payment-service** has a `payment_use_new_validator` flag
(read from `flags.json` mounted as a ConfigMap). Flipping it to `true` and
bumping `GIT_VERSION` from `v2.4.1` to `v2.5.0` simulates a bad deploy where
odd-cents amounts get declined.

To trigger:

```bash
kubectl -n demo create configmap payment-flags \
  --from-literal=flags.json='{"payment_use_new_validator": true}' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n demo set env deploy/payment-service GIT_VERSION=v2.5.0
```

The agent should see `payment.declined` spike under `git_version="v2.5.0"`
in both Loki (`sum by (git_version, event)`) and Prometheus
(`payment_charges_total{status="declined"}`).

Other incident scenarios (order latency, user-service cache lag) are **not
yet implemented** — don't claim to find them.

## Deploy correlation

Whenever you find a spike correlated with a `git_version` boundary:

1. Read the `git_repo` from the same logs / from this catalog.
2. The previous version is the value of `git_version` immediately before the
   spike (e.g. `v2.4.1` if the spike is on `v2.5.0`).
3. Call `github_compare(repo, base=<old>, head=<new>)` to see what changed.
4. If a suspicious file shows up in the diff,
   `github_get_file(repo, path, ref=<new>, start, end)` to read the new code.
5. Cite the commit SHA(s) and a one-line summary of what changed in your
   final answer, alongside the telemetry queries.
