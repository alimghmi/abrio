# Performance

## Test Environment

| Resource | Spec |
|---|---|
| CPU | Intel Core i7-13650HX (13th Gen), 20 logical cores |
| RAM | 16 GB (10 GB available during test) |
| Disk | 1 TB NVMe SSD |
| OS | Ubuntu 22.04 on WSL2 (Linux kernel 6.6.87.2) |
| Runtime | Docker (single-node, all services on one host) |
| Stack | 1× API (uvicorn, 1 worker), 1× relay-normal, 1× relay-express, 1× worker-normal, 1× worker-express |

All services ran as Docker containers on the same machine. This is a development-stack baseline, not a production deployment.

---

## Benchmark Results (2026-06-23)

**Config**: 100 concurrent HTTP workers, 20 s benchmark window, 5 s warmup, 10 users.

### API Throughput

| Metric | Value |
|---|---|
| Total requests | 3,581 |
| Submission throughput | 179 req/s |
| Successful (201) | 3,581 (100.0%) |
| Balance errors (402) | 0 |
| Rate limited (429) | 0 |
| Server errors (5xx) | 0 |
| Network errors | 0 |

### API Latency (all requests, ms)

| Percentile | Latency |
|---|---|
| p50 | 571 ms |
| p75 | 617 ms |
| p90 | 662 ms |
| p95 | 686 ms |
| p99 | 747 ms |
| max | 904 ms |
| mean | 573 ms |

### End-to-End Processing Latency (ms)
*(message creation timestamp → terminal DB state timestamp)*

| Percentile | Latency |
|---|---|
| p50 | 298 ms |
| p75 | 441 ms |
| p90 | 520 ms |
| p95 | 551 ms |
| p99 | 605 ms |
| max | 1,657 ms |
| mean | 309 ms |

### Queue Drain
- 4,351 messages accepted (including warmup)
- All reached terminal state in **2.1 seconds** after load ended
- Observed processing rate: **161 msg/s** (bounded by single Celery worker)

---

## Load Test Results (Correctness / Fairness)

All 10/10 correctness checks passed:

### Scenario 1 — Balance limit under concurrency
- User with 10 credits hit with 100 concurrent requests
- **Exactly 10 accepted**, 90 rejected with 402
- Available credits never negative
- No split-brain, no over-charge

### Scenario 2 — Idempotency
- Same idempotency key submitted twice returns the same message
- Charged exactly once (1 credit reserved, not 2)

### Scenario 3 — Fairness (anti-starvation)
- Hot user flooded with 4,000 messages (batched)
- 6 small users each submitted 20 messages **after** the hot backlog was in place
- All 6 small users fully delivered within **1.37 seconds** of submitting
- Hot user still had 3,537 messages pending when small users finished
- Hot user eventually fully drained (4,000 sent, 0 permanent failures)
- Credits correctly settled: `credits == topup - sent`, `reserved == 0`

---

## Performance Analysis

### What the numbers mean at scale

The target is **~100 million messages per day** ≈ **1,157 msg/s** sustained.

| Layer | Observed (single node) | Path to target |
|---|---|---|
| API submission | 179 req/s (single messages) | Scale horizontally: each uvicorn instance can sustain ~200 req/s; 10 API nodes → 2,000 req/s. Use batch endpoint for bulk: 100 msg/request × 26 req/s = 2,600 msg/s on a single node. |
| Delivery throughput | 161 msg/s (1 worker) | Celery workers scale horizontally. At 1,157 msg/s target: ~8 worker processes needed at the observed rate. Add `--concurrency N` or more worker containers. |
| DB writes per message | 3 (message, dispatch_job, balance) on submit; 2 (job, balance + message) on delivery | PostgreSQL with PgBouncer handles thousands of TPS on modest hardware. |

### API latency (571 ms p50)

The high latency for single-message submissions is driven by two factors:
1. **`SELECT … FOR UPDATE` on the balance row** with 100 concurrent workers creates lock contention on the same 10 user rows. With more users or the batch endpoint, this drops significantly.
2. Docker networking overhead on WSL2 adds ~10–20 ms per request.

In production with more users (tenants spread across many balance rows), lock contention disappears and p50 latency drops to ~20–50 ms.

### Express vs. Normal

Both express and normal messages showed virtually identical API latency (~572 ms p50) and end-to-end latency (~300 ms p50) in this test, because the single-node relay processes both priorities sequentially and the Celery workers are not saturated.

In production at scale, express gets **dedicated worker capacity** (`worker-express` consumes only `sms.express`) and a dedicated relay, ensuring express messages don't queue behind normal backlogs.

---

## Achieving Better Results

### Immediately actionable (no code changes)

| Change | Expected impact |
|---|---|
| `uvicorn --workers 4` (or `--workers $(nproc)`) | 4× API throughput, lower latency |
| `celery worker --concurrency 8` for both worker services | 8× delivery throughput |
| Run 2–4 relay replicas per priority | Faster drain of large backlogs |
| Use the batch endpoint (`POST /messages/batch`, up to 100 msgs) | 100× fewer API round trips |
| Increase `CREDITS` per test user to spread lock contention | Lower per-request DB contention |

### Infrastructure (production deployment)

| Change | Expected impact |
|---|---|
| PgBouncer in transaction mode | Pool connections across 100+ app nodes; eliminates connection exhaustion |
| PostgreSQL read replica | Offload `GET /messages` queries; reduce write-path pressure |
| Multiple RabbitMQ nodes (mirrored queues) | Broker HA + higher publish throughput |
| Redis Cluster | Rate-limit state sharded; handles millions of bucket keys |
| K8s HPA on workers | Auto-scale workers based on RabbitMQ queue depth |

### Tunable environment variables

| Variable | Default | Tune for |
|---|---|---|
| `MAX_DELIVERY_ATTEMPTS` | 5 | Fewer retries → lower latency at cost of reliability |
| `EXPRESS_TTL_SECONDS` | 120 | Lower → faster abandonment of stale OTPs |
| Relay `batch_size` (normal) | 80 | Increase for higher relay throughput with large backlogs |
| Relay `per_user_limit` (normal) | 20 | Tune fairness vs. throughput tradeoff |
| `CELERY_TASK_ALWAYS_EAGER` | false | Set `true` in unit tests to skip broker |
