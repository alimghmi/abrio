# Architecture Overview

## System Summary

Abrio Gateway is a multi-tenant SMS dispatch platform that accepts message submissions via REST, enforces per-customer credit limits atomically, and delivers messages asynchronously through a durable transactional outbox backed by PostgreSQL, RabbitMQ, and Celery workers.

The system is designed to sustain ~100 million messages per day and tens of thousands of tenants. All correctness invariants (no negative balance, no duplicate charges, no double delivery) are enforced in PostgreSQL — not in application code — so they hold even under maximum concurrency.

---

## Process Topology

```
Clients
  │
  ▼ HTTP
┌────────────┐
│  FastAPI   │   (uvicorn, N replicas)
│   (api)    │
└─────┬──────┘
      │ writes to DB within a single transaction
      ▼
┌────────────────────────────────────────┐
│            PostgreSQL                  │
│  ┌────────┐ ┌────────┐ ┌────────────┐ │
│  │ users  │ │ msgs   │ │dispatch_   │ │
│  │balance │ │        │ │jobs        │ │
│  └────────┘ └────────┘ └────────────┘ │
└───────────────────┬────────────────────┘
                    │ relay reads pending jobs
          ┌─────────┴─────────┐
          │                   │
  ┌───────▼───────┐   ┌───────▼───────┐
  │ relay-normal  │   │ relay-express │
  │ (-Q normal)   │   │ (-Q express)  │
  └───────┬───────┘   └───────┬───────┘
          │ publishes to RabbitMQ
          ▼
  ┌──────────────────────────────────────┐
  │           RabbitMQ                   │
  │  sms.normal queue  sms.express queue │
  └───────┬──────────────────┬───────────┘
          │                  │
  ┌───────▼───────┐  ┌───────▼───────────┐
  │ worker-normal │  │  worker-express   │
  │ (Celery)      │  │  (Celery)         │
  └───────┬───────┘  └───────┬───────────┘
          │ writes outcome to DB
          ▼
     PostgreSQL (outcome committed)
```

**Redis** is used only for rate limiting (token bucket state). It is not in the critical path for message delivery.

---

## Service Roles

| Service | Binary / Command | Role |
|---|---|---|
| `api` | `uvicorn main:app` | Accept HTTP requests, validate, reserve credits, write message + dispatch_job atomically |
| `relay-normal` | `python -m infra.workers.dispatch_relay -Q normal` | Poll DB for pending normal jobs, publish to `sms.normal`, run stale-lease reaper |
| `relay-express` | `python -m infra.workers.dispatch_relay -Q express` | Same for express jobs → `sms.express` |
| `worker-normal` | `celery worker -Q sms.normal` | Consume from `sms.normal`, call provider, commit outcome |
| `worker-express` | `celery worker -Q sms.express` | Same for express; dedicated capacity so express is never blocked by normal |

---

## Layer Map

```
src/
├── api/              ← HTTP layer (FastAPI routes, schemas, middleware)
│   ├── routes/       ← Endpoint handlers (call usecases only)
│   ├── schemas/      ← Pydantic request/response models
│   ├── deps.py       ← Dependency injection wiring
│   ├── rate_limit.py ← Redis token-bucket middleware
│   └── v1/router.py  ← Mounts all routes under /api/v1
│
├── app/usecases/     ← Business logic + transaction boundaries
│   ├── messages.py   ← Submit, batch submit, query
│   ├── balance.py    ← Top-up
│   ├── users.py      ← User CRUD
│   └── dispatch.py   ← Worker-side delivery, outcome commit
│
├── infra/
│   ├── db/
│   │   ├── models/          ← SQLAlchemy ORM + DB constraints/indexes
│   │   ├── repositories/    ← Query/mutation logic (never commit)
│   │   └── session.py       ← SessionLocal factory
│   ├── providers/           ← SmsProvider protocol + Dummy/Mock implementations
│   └── workers/
│       ├── celery_app.py    ← Celery configuration, queues, RabbitMQ topology
│       ├── dispatch_relay.py ← Relay loop, claim_batch, publish, reaper
│       └── dispatch_tasks.py ← Celery task entry-point
│
├── domain/
│   ├── enums.py      ← State machine enums (MessageStatus, PaymentStatus, …)
│   └── errors.py     ← AppError hierarchy (HTTP status + error code)
│
└── core/
    ├── config.py     ← Pydantic-settings Settings (all env vars)
    ├── logging.py    ← structlog JSON/console configuration
    ├── metrics.py    ← Prometheus counters/histograms/gauges + DB scrapers
    └── observability.py ← Request-ID middleware + metrics endpoint
```

**Dependency rule**: dependencies point inward. Routes → usecases → repositories → models. Routes never call repositories directly.

---

## Infrastructure Stack

| Component | Version | Purpose |
|---|---|---|
| PostgreSQL | 16 | Primary datastore, enforces all correctness invariants |
| RabbitMQ | 3.13 | Durable message broker; `sms` direct exchange, two queues |
| Redis | 7 | Rate-limit token-bucket state |
| Python | 3.12 | Runtime |
| FastAPI + uvicorn | latest | ASGI web framework |
| SQLAlchemy | latest | ORM + connection pooling |
| psycopg (v3) | latest | PostgreSQL driver (synchronous, used throughout) |
| Celery | latest | Distributed task queue |
| structlog | latest | Structured JSON logging |
| prometheus-client | latest | Metrics exposition |

---

## Key Design Decisions

### Correctness via DB constraints, not application locks

The balance `CHECK (credits >= reserved_credits)` is the real guard against overspending. Application code does a `SELECT … FOR UPDATE` on the balance row and increments `reserved_credits`, relying on the constraint as the final arbiter under concurrent requests. On `CheckViolation`, the use case returns `InsufficientBalanceError`. This prevents negative balances even when 1000 concurrent requests race for a 10-credit balance.

Idempotency is enforced by a `UNIQUE (user_id, idempotency_key)` constraint. On `UniqueViolation`, the use case fetches and returns the existing message — ensuring two racing requests for the same key return the same response.

### Transactional outbox

On submission, one transaction atomically creates the `messages` row, the `dispatch_jobs` row, and the credit reservation. The relay reads only from `dispatch_jobs` — the outbox — so messages are never lost even if RabbitMQ is temporarily down. The relay retries publication with exponential backoff indefinitely (separate budget from delivery retries).

### Two separate retry budgets

`dispatch_jobs.retry_count` counts relay→broker publish retries (unbounded, rides out broker outages). `delivery_attempts` counts worker→provider delivery retries (bounded by `MAX_DELIVERY_ATTEMPTS`, defaults 5). They must never share a counter — sharing would allow transient broker outages to consume the delivery budget.

### Express deadline (TTL)

Express messages with `express_ttl_seconds` (default 120 s) are abandoned rather than delivered late. The deadline is checked both at claim time and before scheduling a retry whose backoff would land after the TTL expires — ensuring a stale OTP is never delivered.

### Per-customer fairness

The relay's `claim_batch` uses a two-pass window function query:
1. **Fairness pass**: cap each customer at `per_user_limit` jobs per cycle, interleaved round-robin.
2. **Top-up pass**: fill remaining batch capacity FIFO, so throughput is not wasted when only one customer is active.

This ensures a flooding tenant cannot monopolise relay capacity while other tenants have pending jobs. Verified by the load-test fairness scenario.
