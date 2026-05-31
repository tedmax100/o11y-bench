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
| payment-service | charges. Has the `payment_use_new_validator` flag | `demo-services/services/payment/` | v2.4.1 |

**All services live in one monorepo: `tedmax100/o11y-bench`** — that is the
`repo` for `github_compare` / `github_get_file` and matches the `git_repo`
label on every signal. **Only `payment-service` currently has real git tags**
(`v2.4.1` → `v2.5.0`); `github_compare` on the other services 404s, so only run
deploy correlation for payment-service.

Dependency edges (caller → callee):

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
  `git_repo`, `git_version`, `deployment_environment`. Everything else (`level`,
  `event`, `trace_id`, business fields) is **structured metadata**: filter it
  *after* the selector (`| level="ERROR"`), never as a `{...}` selector. Do not
  use `app` / `container` / `pod` / `job` — they aren't indexed.
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
| `payment.declined` | `order_id`, `reason` (`new_validator_odd_cents` …) |
| `payment.gateway_error` | `order_id` |
| `order.created` | `order_id`, `user_id`, `amount_cents` |
| `order.cancelled` | `user_id`, `reason` (`auth_failed` / `payment_declined` / `unknown_product`), `upstream_status` |
| `user.logged_in` / `user.auth_failed` | `user_id`, `reason` (`not_found` / `transient`) |
| `http.request_received` | `method`, `path` (template, e.g. `/api/users/{id}`) |
| `http.request_failed` | `upstream`, `status`, `reason` (`network`) |

## Query style (use live names from the snapshot)

```promql
# p95 latency per service (histogram → histogram_quantile over _bucket)
histogram_quantile(0.95, sum by (service_name, le) (rate(<duration>_bucket[5m])))
```
```logql
# errors per service — aggregate at the source, don't pull raw lines
sum by (service_name) (count_over_time({deployment_environment="demo"} | level="ERROR" [1h]))
```
```traceql
# errors originating in a service
{ resource.service.name = "<service>" && status = error }
```

## Feature flags & incident scenarios

**payment-service** has a `payment_use_new_validator` flag (from `flags.json`,
a ConfigMap). Flipping it `true` and bumping `git_version` `v2.4.1` → `v2.5.0`
simulates a bad deploy where odd-cents amounts get declined — `payment.declined`
spikes under `git_version="v2.5.0"` in both Loki (`sum by (git_version, event)`)
and Prometheus (declined charges by `git_version`).

Trigger:

```bash
kubectl -n demo create configmap payment-flags \
  --from-literal=flags.json='{"payment_use_new_validator": true}' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n demo patch deployment payment-service --type=merge \
  -p '{"spec":{"template":{"metadata":{"labels":{"git_version":"v2.5.0"}}}}}'
```

Other incident scenarios (order latency, user-service cache lag) are **not yet
implemented** — don't claim to find them.

## Deploy correlation

When a spike correlates with a `git_version` boundary:

1. Repo is always `tedmax100/o11y-bench` (also the `git_repo` label).
2. Previous version = the `git_version` value just before the spike (e.g.
   `v2.4.1` if the spike is on `v2.5.0`).
3. `github_compare("tedmax100/o11y-bench", base=<old>, head=<new>)` to see the
   diff (naturally scoped to that service's path).
4. If a suspicious file shows up, `github_get_file(...)` to read the new code.
5. Cite the commit SHA(s) + a one-line summary alongside the telemetry queries.
   Only payment-service has real tags today (see Services).
