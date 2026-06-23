![Banner](docs/banner.svg)

# abrio

Multi-tenant SMS gateway for idempotent message submission, credit reservation, transactional outbox dispatch, priority queues, fair task distributation, and safe retries.

## Run

```bash
docker compose up --build
```

- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/api/v1/health/ready

## Test

```bash
uv sync --dev
uv run pytest src/tests/unit
docker compose up -d postgres redis rabbitmq
uv run pytest
```

## Docs

See [docs/README.md](docs/README.md) for architecture, invariants, dispatch flow, and scaling notes.
