# Performance

## Environment

| Resource | Spec |
|---|---|
| Host | Hetzner CCX43 (dedicated vCPU) |
| CPU | 16 vCPU, AMD EPYC-Milan (8 cores × 2 threads) |
| RAM | 61 GiB, 0 swap |
| Disk | 343 GB NVMe SSD |
| OS | Ubuntu 26.04 LTS, kernel 7.0 |
| Runtime | Docker Compose, single host |
| Postgres | `fsync=on`, `synchronous_commit=on` (full durability) |

## Configuration

| Component | Setting |
|---|---|
| API | uvicorn, 4 workers |
| worker-normal | Celery `--concurrency 160` |
| worker-express | Celery `--concurrency 80` |
| relay-normal | 10 replicas |
| relay-express | 6 replicas |
| Relay batch (normal / express) | 160 / 80 |
| Relay per-user limit (normal / express) | 20 / 10 |
| DB pool per process | `pool_size=2`, `max_overflow=3` |
| Postgres `max_connections` | 300 |
| Postgres `shared_buffers` / `effective_cache_size` | 1 GB / 4 GB |

## Benchmark

| Metric | Value |
|---|---|
| Messages delivered | 50,000 / 50,000 |
| End-to-end throughput | 838 msg/s |
| End-to-end time | 59.6 s |
| Submission throughput | 1,981 msg/s |
| **Drain rate (tap-off after submit)** | **1,028 msg/s** |
| Permanent failures | 0 |
| Express / normal TTL-expired | 0 % / 0 % |
| Peak backlog | 38,435 |

### End-to-end latency (submit → terminal DB state)

| Percentile | Express | Normal |
|---|---|---|
| p50 | 23,676 ms | 27,757 ms |
| p95 | 29,814 ms | 34,978 ms |
| p99 | 30,688 ms | 35,368 ms |

## Correctness / fairness (real-Postgres concurrency tests)

| Property | Result |
|---|---|
| 10 credits, 100 concurrent requests | Exactly 10 accepted, 0 negative balance |
| Same idempotency key × 2 | 1 message, 1 charge |
| Fairness: hot user 4,000 + 6 small × 20 | Small users delivered in 1.37 s |
