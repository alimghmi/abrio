# Running and Testing

## Prerequisites

- Docker + Docker Compose
- Python 3.12
- [uv](https://github.com/astral-sh/uv) package manager

---

## Quick Start

```bash
# Create a shared Docker network (one-time)
docker network create abrio-network

# Start all services
docker compose up -d

# Wait for readiness
curl http://localhost:8000/api/v1/health/ready
```

Services started:
| Service | Port |
|---|---|
| API (FastAPI) | 8000 |
| PostgreSQL | 5432 |
| Redis | 6379 |
| RabbitMQ AMQP | 5672 |
| RabbitMQ Management UI | 15672 (guest/guest) |
| RabbitMQ Prometheus | 15692 |

The `api` container runs `backend_pre_start.py` on boot, which waits for PostgreSQL and calls `init_db()` to create all tables.

---

## Development Setup

```bash
uv sync --dev            # install all dependencies including dev group

# Run the API locally (requires Postgres, Redis, RabbitMQ running)
uv run uvicorn main:app --reload --app-dir src

# Run the relay (normal priority)
uv run python -m infra.workers.dispatch_relay -Q normal

# Run a Celery worker
uv run celery -A infra.workers.celery_app.celery_app worker -Q sms.normal --loglevel=INFO
```

### Environment Variables

Copy `.env.example` to `.env` and adjust as needed. Key variables:

```env
DATABASE_URL=postgresql+psycopg://sms_gateway:sms_gateway@localhost:5432/sms_gateway
CELERY_BROKER_URL=amqp://guest:guest@localhost:5672//
CELERY_RESULT_BACKEND=redis://localhost:6379/1
REDIS_URL=redis://localhost:6379/0

COST_PER_MESSAGE=1.00
COST_PER_EXPRESS_MESSAGE=1.00
MAX_DELIVERY_ATTEMPTS=5
EXPRESS_TTL_SECONDS=120
SMS_PROVIDER=dummy          # dummy | mock
SMS_MOCK_FAIL_RATE=0.0      # 0.0–1.0 (used with mock provider)

RATE_LIMIT_ENABLED=false    # enable for production; requires Redis
APP_DEBUG=false             # enables /users/{id}/zero endpoint (dev only)
DB_CREATE_ALL=true          # auto-create tables on boot
```

---

## Running Tests

```bash
# Unit tests only (no external services needed)
uv run pytest src/tests/unit -q

# Integration tests (requires Postgres, Redis, RabbitMQ)
uv run pytest src/tests/integration -q

# Full suite
uv run pytest -q

# Specific test
uv run pytest src/tests/unit/test_messages_unit.py::test_create_message_reserves_credits -v

# By marker
uv run pytest -m unit
uv run pytest -m integration
uv run pytest -m concurrency
```

Test coverage threshold: 60%. Current coverage: ~78%.

> **Note**: A few tests are skipped unless `APP_DEBUG=true` (the `/zero` endpoint tests). The integration tests require all three infrastructure services to be running.

---

## Code Quality

```bash
uv run ruff format src           # auto-format
uv run ruff format --check src   # check only (CI mode)
uv run ruff check src            # lint
uv run mypy src --exclude '^src/tests/'  # strict type checking
```

---

## Schema Changes

There are **no Alembic migrations**. SQLAlchemy's `create_all` only creates missing tables — it never `ALTER`s existing ones.

To apply a column addition or change:
```bash
docker compose down -v       # drop postgres_data volume
docker compose up -d         # recreate; init_db() runs on api boot
```

---

## Load Testing and Benchmarking

Requires the full stack to be running (`docker compose up -d`).

### Correctness / Fairness Load Test

Verifies balance limits under concurrency, idempotency, and per-customer fairness:

```bash
BASE_URL=http://localhost:8000/api/v1 \
CONCURRENCY=50 \
DRAIN_TIMEOUT=120 \
uv run python scripts/loadtest.py
```

Scenarios tested:
1. **Balance limit under concurrency**: 100 concurrent requests against a 10-credit user → exactly 10 accepted.
2. **Idempotency**: same key submitted twice → same message, charged once.
3. **Fairness**: hot user with 4,000-message backlog must not starve 6 small users.

### Throughput Benchmark

Measures API submission throughput and end-to-end processing latency:

```bash
BASE_URL=http://localhost:8000/api/v1 \
WORKERS=100 \
DURATION=20 \
WARMUP=5 \
USERS=10 \
CREDITS=500000 \
EXPRESS_RATIO=0.3 \
DRAIN_TIMEOUT=300 \
uv run python scripts/benchmark.py
```

Key environment variables for the benchmark:

| Variable | Default | Description |
|---|---|---|
| `WORKERS` | 200 | Concurrent HTTP workers |
| `DURATION` | 30 | Benchmark window in seconds (after warmup) |
| `WARMUP` | 5 | Warmup duration in seconds |
| `USERS` | 10 | Number of test users to create |
| `CREDITS` | 200,000 | Credits per user |
| `EXPRESS_RATIO` | 0.4 | Fraction of express messages (0.0–1.0) |
| `DRAIN_TIMEOUT` | 300 | Max seconds to wait for queue drain |

---

## Observability

### Prometheus Metrics

```bash
curl http://localhost:8000/metrics
```

See [observability.md](observability.md) for the full list of metrics and a Grafana dashboard description.

### Grafana Dashboard

A pre-configured Grafana dashboard is available in the `monitoring/` directory:

```bash
cd monitoring && docker compose up -d
```

Access Grafana at `http://localhost:3000` (admin/admin).

### Structured Logs

All services emit JSON-structured logs (set `LOG_FORMAT=json`). Request IDs are propagated via `X-Request-ID` header and included in every log entry.

---

## Resetting State

```bash
# Wipe all data and restart clean
docker compose down -v && docker compose up -d
```
