# Abrio Monitoring Stack

This stack is intentionally separate from the application stack. It starts only
Prometheus and Grafana and joins the shared `abrio-network`.

## Start

```bash
docker network create abrio-network
docker compose up --build -d
docker compose -f monitoring/docker-compose.yml up -d
```

- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000
- Grafana login: `admin` / `admin` unless `GRAFANA_ADMIN_USER` and
  `GRAFANA_ADMIN_PASSWORD` are set.

## Scrape Targets

Prometheus scrapes:

- `api:8000/metrics`
- `relay-normal:9101/metrics`
- `relay-express:9101/metrics`
- `worker-normal:9102/metrics`
- `worker-express:9102/metrics`
- `rabbitmq:15692/metrics`
- `prometheus:9090/metrics`

RabbitMQ metrics come from the `rabbitmq_prometheus` plugin enabled in the main
Compose stack. Worker metrics use Prometheus multiprocess mode because Celery
uses prefork workers.

## Grafana

Grafana provisions Prometheus as the default data source and loads one dashboard:

- `Abrio Overview`

The dashboard includes accepted messages, submission rejections, API rate and
latency, dispatch backlog age, relay publication health, delivery outcomes,
retry rate, payment consistency, and RabbitMQ queue depth/consumers.

## Stop

```bash
docker compose -f monitoring/docker-compose.yml down
```

Stopping Prometheus or Grafana does not stop Abrio and does not affect message
processing.
