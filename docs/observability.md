# Observability

## Overview

The system exposes structured logs and Prometheus metrics from every process.
Metrics cover the full message lifecycle: HTTP submission → transactional outbox
→ RabbitMQ relay → Celery worker → provider call → balance settlement.

## Metrics

### Scrape endpoints

| Process | Endpoint | Notes |
|---------|----------|-------|
| API | `api:8000/metrics` | Application metrics + DB-backed gauges |
| Relay (normal) | `relay-normal:9101/metrics` | Publication counters for normal queue |
| Relay (express) | `relay-express:9101/metrics` | Publication counters for express queue |
| Worker (normal) | `worker-normal:9102/metrics` | Delivery counters via multiprocess mode |
| Worker (express) | `worker-express:9102/metrics` | Delivery counters via multiprocess mode |
| RabbitMQ | `rabbitmq:15692/metrics` | Native plugin, enabled in docker-compose |

Worker metrics use Prometheus multiprocess mode because Celery runs prefork
workers. Each worker container clears `PROMETHEUS_MULTIPROC_DIR` on startup and
calls `mark_process_dead` on shutdown. Without `PROMETHEUS_MULTIPROC_DIR` set,
worker delivery metrics are unavailable.

### Application metrics

**HTTP layer** — recorded in middleware for every non-health, non-`/metrics` request:

| Metric | Type | Labels | What it measures |
|--------|------|--------|-----------------|
| `abrio_http_requests_total` | Counter | `method`, `route`, `status_code` | Requests handled |
| `abrio_http_request_duration_seconds` | Histogram | `method`, `route` | Request latency |

**Message submission** — recorded after the transaction commits (or on rejection):

| Metric | Type | Labels | What it measures |
|--------|------|--------|-----------------|
| `abrio_messages_submitted_total` | Counter | `priority`, `submission_type` | Accepted messages |
| `abrio_message_submission_rejected_total` | Counter | `reason`, `submission_type` | Rejected submissions |
| `abrio_idempotent_replays_total` | Counter | `submission_type` | Deduplicated resubmissions |

**Dispatch relay** — recorded per relay cycle, outside the claim transaction:

| Metric | Type | Labels | What it measures |
|--------|------|--------|-----------------|
| `abrio_relay_jobs_published_total` | Counter | `priority` | Jobs published to RabbitMQ |
| `abrio_relay_publish_failures_total` | Counter | `priority` | Jobs the relay failed to publish |

**Delivery** — recorded by workers after the outcome transaction commits:

| Metric | Type | Labels | What it measures |
|--------|------|--------|-----------------|
| `abrio_delivery_attempts_total` | Counter | `priority`, `outcome` | Terminal delivery outcomes |
| `abrio_message_end_to_end_duration_seconds` | Histogram | `priority`, `outcome` | Submission-to-outcome latency |
| `abrio_dispatch_retries_total` | Counter | `priority`, `retry_type` | Scheduled retries |

**DB-backed gauges** — collected on `/metrics` requests, with separate cache TTLs:

| Metric | Type | Labels | Cache TTL | What it measures |
|--------|------|--------|-----------|-----------------|
| `abrio_dispatch_ready_jobs` | Gauge | `priority` | 5 s | Dispatch jobs due for publication |
| `abrio_dispatch_oldest_ready_age_seconds` | Gauge | `priority` | 5 s | Age of the oldest ready job |
| `abrio_payment_consistency_violations` | Gauge | `type` | 30 s | DB-level invariant violations |

If the DB is unavailable when gauges are refreshed, the endpoint still responds,
the error is logged as `dependency_unavailable`, and stale values are served
until the next successful query.

### Bounded label sets

All label values are validated against an allowlist before being recorded. This
prevents unbounded cardinality if unexpected values reach a metric call:

- `priority`: `normal`, `express`
- `submission_type`: `single`, `batch`
- `outcome`: `success`, `temporary_failure`, `permanent_failure`, `unexpected_failure`
- `retry_type`: `publication`, `delivery`
- `reason`: `rate_limited`, `global_rate_limited`, `system_rate_limited`, `insufficient_balance`, `invalid_request`, `idempotency_conflict`, `database_error`
- `type` (payment violations): `negative_credits`, `negative_reserved_credits`, `reserved_exceeds_credits`, `reserved_without_message`, `message_balance_mismatch`

### Payment consistency gauge

The payment consistency gauge runs two queries to detect invariant breaks:

1. Negative-credit and reserved-exceeds-total checks directly on `balances`.
2. A join between `balances` and `messages WHERE payment_status = RESERVED` to
   detect mismatches between `reserved_credits` and the sum of reserved message
   costs per user.

These queries are cached for 30 seconds (compared to 5 s for the ready-jobs
gauges) because they are heavier and violations warrant investigation rather than
real-time alerting. Any non-zero value is also logged as
`payment_consistency_violation`.

## Logging

| Setting | Default (Docker) | Notes |
|---------|-----------------|-------|
| `LOG_FORMAT` | `json` | `console` for local dev |
| `LOG_LEVEL` | `INFO` | |
| `LOG_REQUEST_ID_HEADER` | `X-Request-ID` | |
| `SERVICE_NAME` | per container | `abrio-api`, `abrio-relay`, `abrio-worker-normal`, `abrio-worker-express` |

Every log line is a JSON object with:
`timestamp`, `level`, `service`, `logger`, `event`, `request_id`, plus
context fields like `message_id`, `dispatch_job_id`, `priority`, `outcome`,
`error_type`, `duration_ms`.

Message bodies, recipients, and idempotency keys are never logged.

### Stable log events

| Event | Level | Emitter |
|-------|-------|---------|
| `http_request_completed` | INFO | Middleware |
| `message_submission_accepted` | INFO | MessageUseCase |
| `message_submission_rejected` | INFO | MessageUseCase |
| `message_idempotent_replay` | INFO | MessageUseCase |
| `rate_limit_rejected` | INFO | Rate limiter |
| `dispatch_jobs_claimed` | INFO | Relay |
| `dispatch_jobs_published` | INFO | Relay |
| `dispatch_publish_failed` | WARNING | Relay |
| `dispatch_relay_reclaimed_stale_inflight` | WARNING | Relay reaper |
| `dispatch_retry_scheduled` | WARNING | Relay / DispatchUseCase |
| `delivery_attempt_started` | INFO | DispatchUseCase |
| `delivery_attempt_succeeded` | INFO | DispatchUseCase |
| `delivery_attempt_temporarily_failed` | WARNING | DispatchUseCase |
| `delivery_attempt_permanently_failed` | WARNING | DispatchUseCase |
| `payment_consistency_violation` | WARNING | Metrics collector |
| `dependency_unavailable` | ERROR | Metrics collector, health checks |

## Request IDs

The API reads `X-Request-ID` from the request header. If the value is a valid
UUID it is reused; otherwise a new UUID is generated. The ID is:

- stored in a `contextvars` context so it is available to all log calls in the
  request lifecycle
- echoed back in the response header
- reset after every request so it cannot bleed into the next request

## Health checks

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/health/live` | Liveness — always returns `ok` if the process is up |
| `GET /api/v1/health/ready` | Readiness — probes Redis, Postgres, and RabbitMQ |

Health endpoints are excluded from HTTP metrics and access logs to avoid noise.

## Grafana dashboard

The provisioned dashboard `Abrio Overview` is organised in four rows:

**Submission** — accepted submissions/sec by priority, rejection rate by reason,
idempotent replay rate.

**Dispatch pipeline** — ready-job backlog by priority, oldest ready job age,
relay publish rate, relay publish failure rate.

**Delivery** — terminal delivery outcomes/sec, p95 end-to-end latency for
successful deliveries, retry rate by type (publication vs delivery).

**Infrastructure** — RabbitMQ ready-message depth per queue, consumer count per
queue, payment consistency violation count.

## Startup

```bash
# Start the application stack
docker compose up --build -d

# Start monitoring (Prometheus + Grafana)
docker compose -f monitoring/docker-compose.yml up -d
```

- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (login: `admin` / `admin`)

The monitoring stack joins the same `abrio-network` as the application but is
independent — stopping Prometheus or Grafana does not affect message processing.
