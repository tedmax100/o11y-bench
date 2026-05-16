# Telemetry Schema Catalog

Authoritative description of what's actually in the sidecar's Prometheus / Loki / Tempo.
Use this to write correct queries instead of guessing label or field names.

## Services

Five services run in the sidecar. `job` and `service` labels both carry the
service name; `instance` is `<service>:<port>`.

| service | instance | role |
|---------|----------|------|
| webapp | webapp:8080 | edge — receives all user HTTP requests |
| api-gateway | api-gateway:8081 | routing layer between webapp and backends |
| user-service | user-service:8082 | user / auth |
| order-service | order-service:8083 | orders, cart, products |
| payment-service | payment-service:8084 | payments |

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
| GET | /api/products | order-service |
| POST | /api/orders | order-service |
| POST | /api/payments | payment-service |
| GET | /api/cart | order-service |

## Loki

**Labels** (the *only* indexable selectors):

- `job` — service name (same value as `service`)
- `service` — service name
- `level` — `info` | `warn` | `error`

Use `service` or `job` to pick a service; use `level` to filter severity.
Do **not** use `app`, `container`, `pod`, etc — they don't exist here.

**Log format**: every line is JSON. Parse with `| json`.

Common fields across all logs:

| field | type | notes |
|-------|------|-------|
| timestamp | string | ISO 8601 with millisecond |
| level | string | info / warn / error |
| service | string | redundant with the label |
| message | string | human-readable summary |

Request logs (info / error level) additionally carry:

| field | type | notes |
|-------|------|-------|
| method | string | GET / POST |
| path | string | `/api/users`, `/api/orders`, ... |
| status | int | 200 on success; 500 / 502 / 503 on error |
| duration_ms | float | request latency |
| trace_id | string | 32-char hex, matches Tempo trace |

Warning logs come in three flavors (mutually exclusive fields):

- Slow query: `table` (users / orders / products / sessions), `query_ms`
- Retry backlog: `queue="payment-retries"`, `queue_depth`
- Cache refresh lag: `job_name="auth-cache-refresh"`, `lag_seconds`, `stale_keys`

Deployment logs:

- `event="deployment"`, `version`, `message="deployment started: <svc> v2.4.1 -> v2.5.0"`

Typical error `message` values:

- `"request failed"`, `"internal server error"`, `"upstream service error"`, `"database connection timeout"`

### LogQL examples for this data

```logql
# Count errors per service in the last hour — aggregate at datasource
sum by (service) (
  count_over_time({level="error"} [1h])
)

# 5xx rate on payment-service
sum(
  rate({service="payment-service", level="error"} | json | status >= 500 [5m])
)

# Slow queries warning — only project the fields we need
{service="order-service", level="warn"}
  | json
  | query_ms > 1000
  | line_format "{{.timestamp}} table={{.table}} ms={{.query_ms}}"

# Retry backlog depth from warning logs
{level="warn"} | json | queue="payment-retries"
  | line_format "{{.timestamp}} depth={{.queue_depth}}"

# Deployment events
{level="info"} | json | event="deployment"
```

## Prometheus

All metrics carry `job` and `instance` labels (service name and `<svc>:<port>`).

| metric | type | extra labels | notes |
|--------|------|--------------|-------|
| up | gauge | — | always 1 in this dataset |
| process_cpu_seconds_total | counter | — | use `rate(...[5m])` |
| process_resident_memory_bytes | gauge | — | bytes |
| go_goroutines | gauge | — | proxy for concurrency |
| http_requests_total | counter | status | 200 / 500 / 502 / 503 |
| http_request_duration_seconds_{sum,count,bucket} | histogram | `le` for bucket | standard Prom buckets |
| service_retry_queue_depth | gauge | — | non-zero only during the payment incident |
| service_cache_refresh_lag_seconds | gauge | — | non-zero only during the user-service cache incident |

### PromQL examples

```promql
# p95 latency per service
histogram_quantile(0.95,
  sum by (job, le) (rate(http_request_duration_seconds_bucket[5m]))
)

# Error rate (5xx / total) per service
sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))
/
sum by (job) (rate(http_requests_total[5m]))

# Retry queue depth spike (payment / order incident signal)
max by (job) (service_retry_queue_depth) > 0

# Cache refresh lag (user-service incident signal)
max by (job) (service_cache_refresh_lag_seconds) > 0
```

## Tempo

Trace structure per request (root → leaf):

```
webapp     "GET /api/orders"        kind=server
  ↳ api-gateway "route /api/orders" kind=server
    ↳ <target service> "handle GET /api/orders" kind=server  ← origin of error
      ↳ <dep service>  "call <dep>"             kind=client  ← also error=true
      ↳ user-service   "refresh auth cache"     kind=internal (cache incident only)
```

**Resource attribute**: `service.name`.

**Span attributes**: `http.method`, `http.route`, `http.url`, `http.status_code`.

**Span status convention** (important — this is how to find the origin):

- Upstream spans (webapp, api-gateway) carry `http.status_code=500` but their
  span status is **NOT** error.
- The target service span and its dependency-call span are the only ones with
  span `status=error`. So `{ span:status = error }` filters to the originating
  service, not the whole trace.

### TraceQL examples

```traceql
# Failures originating in payment-service in the last hour
{ resource.service.name = "payment-service" && span:status = error }

# Slow order requests
{ resource.service.name = "order-service" && span.http.route = "/api/orders" && duration > 500ms }

# Traces whose root saw a 5xx (top-level only)
{ span.http.status_code >= 500 && span:kind = server }
```

## Known incident windows (relative to data end time)

The data generator plants three deterministic incidents. These are the patterns
to recognize:

| ~time before data end | duration | signal |
|----------------------|----------|--------|
| 3h | 30 min | **payment-service error spike** — 70% errors at payment-service, cascading 15% to order-service and 8% to api-gateway. `service_retry_queue_depth` spikes on payment & order. Deployment log ~2 min before the spike. |
| 6h | 45 min | **order-service latency** — p95 5x on order-service, 2x on api-gateway / webapp. `/api/orders` dominates the slow paths. |
| 9h | 40 min | **user-service cache refresh** — 4x latency on `/api/users`. `service_cache_refresh_lag_seconds` spikes on user-service. Deployment log ~3 min before. Traces show `refresh auth cache` internal spans. |

When asked an RCA question, time-box your queries around these windows rather
than scanning the whole 24h.
