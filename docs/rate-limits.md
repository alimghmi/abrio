# Rate Limits and Relay Dispatch Tuning

## Overview

The gateway enforces limits at two distinct control points:

1. **API ingress** — Redis token buckets applied per-user, globally per endpoint, and system-wide across all traffic.
2. **Relay egress** — The transactional outbox relay caps how many jobs it claims per iteration and how many jobs any one user may occupy in a single batch.

These two systems are deliberately separate. The API rate limiter protects ingress and prevents a single tenant from flooding the submission path. The relay fairness parameters protect egress and prevent a large job backlog from starving other users' messages during dispatch.

---

## Capacity Model

The target system serves approximately **100 million SMS submissions per day**.

```
Daily average:  100,000,000 / 86,400 s ≈ 1,157 msg/s
```

Traffic is not uniform. Design for a **5× peak factor** over the daily average:

```
Peak throughput:  1,157 × 5 ≈ 5,787 msg/s
                            ≈ 347,000 msg/min
```

The system applies an **80/20 split** between normal and express traffic, which matches the relay batch sizing and the queue architecture:

```
Normal (80%):   347,000 × 0.80 = 277,600 → rounded to 300,000/min
Express (20%):  347,000 × 0.20 =  69,400 → rounded to  75,000/min
Total messages: 300,000 + 75,000 = 375,000/min
```

API requests are not 1:1 with messages. The batch endpoint allows up to `MAX_MESSAGES_PER_BATCH=100` messages per request. Counting all endpoint categories (submissions, status reads, reports, user reads, user writes, pricing), total API request volume is estimated at:

```
System requests ceiling:  375,000 msg/min + ~75,000 other req/min ≈ 450,000 req/min
```

---

## Per-User Submission Limits

Per-user limits are intentionally aligned with the relay `per_user_limit` values so that a user who saturates their ingress rate limit cannot send jobs faster than the relay will dispatch them.

| Priority | Rate (msg/min) | Rate (msg/s) | Burst | Alignment |
|----------|---------------|--------------|-------|-----------|
| Normal   | 1,200         | 20           | 200   | `RELAY_NORMAL_PER_USER_LIMIT=20` |
| Express  | 300           | 5            | 100   | `RELAY_EXPRESS_PER_USER_LIMIT=5`; burst allows short OTP spikes |

**Why burst differs from per-minute rate / 60:**
- Normal burst = 200 = one full `RELAY_NORMAL_BATCH_SIZE` worth of jobs, allowing a cold-start user to immediately fill a relay batch before the token refill rate takes over.
- Express burst = 100 = five full relay batches, accommodating a spike of OTP requests (a user logging in all their customers simultaneously).

The batch endpoint counts one message token per item, split by priority. A batch of 80 normal + 20 express messages consumes 80 normal tokens, 20 express tokens, and 1 batch-request token.

---

## Global Endpoint Limits

Global limits cap aggregate throughput across all tenants for each endpoint category. They prevent the cluster from being overwhelmed even if per-user limits have not been reached.

| Scope | Limit (req or msg/min) | Burst | Derivation |
|-------|----------------------|-------|------------|
| Normal messages | 300,000/min | 30,000 | 5× daily avg × 80% split |
| Express messages | 75,000/min | 15,000 | 5× daily avg × 20% split |
| Batch requests | 3,000/min | 300 | 300,000 msg ÷ 100 msg/batch |
| Message status reads | 120,000/min | 12,000 | ~40% of message volume as reads |
| Message reports | 12,000/min | 1,200 | Low-frequency summary endpoint |
| User reads | 30,000/min | 3,000 | ~10% of message volume |
| User writes (top-up, reset) | 6,000/min | 600 | Infrequent credit operations |
| User creates | 1,200/min | 120 | Rare provisioning operation |
| Pricing reads | 12,000/min | 1,200 | Infrequent, cacheable endpoint |

**Burst sizing:** All global bursts are set to 10% of the per-minute rate (one 6-second spike). This allows short coordinated bursts without letting sustained overload through.

---

## System-Wide Circuit Breakers

System limits apply across all tenants, all endpoints combined. They act as a last-resort circuit breaker when global-per-endpoint limits would otherwise be bypassed by a large number of small tenants each staying within their own global bucket.

| Metric | Limit (per min) | Burst |
|--------|----------------|-------|
| Total API requests | 450,000 | 45,000 |
| Total submitted messages | 375,000 | 37,500 |

Every non-health API request consumes one system request token. Every accepted message (single or batch item) also consumes one system message token.

---

## Relay Dispatch Fairness

The relay controls egress from the transactional outbox to RabbitMQ. Each relay container runs a tight loop:

1. **Claim** up to `batch_size` jobs with `SELECT … FOR UPDATE SKIP LOCKED`, applying a window-function fairness pass that caps any single user at `per_user_limit` jobs per batch.
2. **Publish** claimed jobs to RabbitMQ outside the claim transaction.
3. **Sleep** `IDLE_SLEEP_SECONDS=0.5` when the queue is empty.

| Parameter | Normal | Express | Rationale |
|-----------|--------|---------|-----------|
| `RELAY_NORMAL_BATCH_SIZE` | 80 | — | 80 normal jobs/iteration. At `IDLE_SLEEP_SECONDS=0.5` with no idle: up to 160 publishes/s per relay replica. |
| `RELAY_NORMAL_PER_USER_LIMIT` | 20 | — | Fairness cap: a hot user gets at most 20 of 80 slots **when other users have enough work to fill the rest**. With ≥4 active users, each gets an equal interleaved share. |
| `RELAY_EXPRESS_BATCH_SIZE` | — | 20 | Smaller batch keeps express relay latency low; express backlog should be small by design. |
| `RELAY_EXPRESS_PER_USER_LIMIT` | — | 5 | Fairness cap: a hot user gets at most 5 of 20 slots when others have work. Matches the 5 msg/s per-user API cap. |

### Normal/express split (80/20)

The 80/20 relay batch split matches the expected traffic mix (80% normal bulk, 20% express OTP). This is a tuning default, not a hard constraint. In practice:

- If express traffic is heavier than 20%, increase `RELAY_EXPRESS_BATCH_SIZE` or add express relay replicas.
- If a relay replica's claim loop runs too slowly under load, increase `RELAY_NORMAL_BATCH_SIZE`.
- Scaling horizontally (more relay replicas per priority) is the preferred lever for throughput — each replica uses `SKIP LOCKED` so they parallelise without contention.

### Fairness invariant

The `per_user_limit` parameters enforce an anti-starvation guarantee: a single hot user with a large backlog can claim at most `per_user_limit` slots per batch, leaving the remaining `batch_size − per_user_limit` slots available to other users. With `RELAY_NORMAL_PER_USER_LIMIT=20` and `RELAY_NORMAL_BATCH_SIZE=80`, a hot user can occupy at most 25% of any batch cycle. The window-function pass interleaves users round-robin, so the remaining 60 slots are distributed evenly across other active users before a FIFO top-up fills any spare capacity.

---

## Environment Variables

All parameters are configurable via environment variable. The defaults shipped in `.env.example` and `docker-compose.yml` match the capacity model above.

### Relay

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_NORMAL_BATCH_SIZE` | `80` | Max jobs claimed per normal relay iteration |
| `RELAY_NORMAL_PER_USER_LIMIT` | `20` | Max jobs per user per normal batch |
| `RELAY_EXPRESS_BATCH_SIZE` | `20` | Max jobs claimed per express relay iteration |
| `RELAY_EXPRESS_PER_USER_LIMIT` | `5` | Max jobs per user per express batch |

### Per-user API rate limits

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_NORMAL_MESSAGES_PER_MINUTE` | `1200` | Normal messages per user per minute |
| `RATE_LIMIT_NORMAL_MESSAGES_BURST` | `200` | Burst allowance |
| `RATE_LIMIT_EXPRESS_MESSAGES_PER_MINUTE` | `300` | Express messages per user per minute |
| `RATE_LIMIT_EXPRESS_MESSAGES_BURST` | `100` | Burst allowance |
| `RATE_LIMIT_BATCH_REQUESTS_PER_MINUTE` | `120` | Batch POST requests per user per minute |
| `RATE_LIMIT_BATCH_REQUESTS_BURST` | `20` | Burst allowance |
| `RATE_LIMIT_MESSAGE_STATUS_PER_MINUTE` | `2400` | Status reads per user per minute |
| `RATE_LIMIT_MESSAGE_STATUS_BURST` | `400` | Burst allowance |
| `RATE_LIMIT_MESSAGE_REPORTS_PER_MINUTE` | `240` | Report requests per user per minute |
| `RATE_LIMIT_MESSAGE_REPORTS_BURST` | `40` | Burst allowance |
| `RATE_LIMIT_USER_READS_PER_MINUTE` | `600` | User GET requests per user per minute |
| `RATE_LIMIT_USER_READS_BURST` | `100` | Burst allowance |
| `RATE_LIMIT_USER_WRITES_PER_MINUTE` | `60` | Top-up/reset requests per user per minute |
| `RATE_LIMIT_USER_WRITES_BURST` | `15` | Burst allowance |
| `RATE_LIMIT_USER_CREATES_PER_MINUTE` | `20` | User creation requests (by IP) per minute |
| `RATE_LIMIT_USER_CREATES_BURST` | `5` | Burst allowance |
| `RATE_LIMIT_PRICING_PER_MINUTE` | `240` | Pricing reads (by IP) per minute |
| `RATE_LIMIT_PRICING_BURST` | `40` | Burst allowance |

### Global endpoint limits

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_GLOBAL_NORMAL_MESSAGES_PER_MINUTE` | `300000` | Normal messages across all users |
| `RATE_LIMIT_GLOBAL_NORMAL_MESSAGES_BURST` | `30000` | Burst allowance |
| `RATE_LIMIT_GLOBAL_EXPRESS_MESSAGES_PER_MINUTE` | `75000` | Express messages across all users |
| `RATE_LIMIT_GLOBAL_EXPRESS_MESSAGES_BURST` | `15000` | Burst allowance |
| `RATE_LIMIT_GLOBAL_BATCH_REQUESTS_PER_MINUTE` | `3000` | Batch POST requests across all users |
| `RATE_LIMIT_GLOBAL_BATCH_REQUESTS_BURST` | `300` | Burst allowance |
| `RATE_LIMIT_GLOBAL_MESSAGE_STATUS_PER_MINUTE` | `120000` | Status reads across all users |
| `RATE_LIMIT_GLOBAL_MESSAGE_STATUS_BURST` | `12000` | Burst allowance |
| `RATE_LIMIT_GLOBAL_MESSAGE_REPORTS_PER_MINUTE` | `12000` | Report requests across all users |
| `RATE_LIMIT_GLOBAL_MESSAGE_REPORTS_BURST` | `1200` | Burst allowance |
| `RATE_LIMIT_GLOBAL_USER_READS_PER_MINUTE` | `30000` | User GET requests across all users |
| `RATE_LIMIT_GLOBAL_USER_READS_BURST` | `3000` | Burst allowance |
| `RATE_LIMIT_GLOBAL_USER_WRITES_PER_MINUTE` | `6000` | Top-up/reset requests across all users |
| `RATE_LIMIT_GLOBAL_USER_WRITES_BURST` | `600` | Burst allowance |
| `RATE_LIMIT_GLOBAL_USER_CREATES_PER_MINUTE` | `1200` | User creation across all IPs |
| `RATE_LIMIT_GLOBAL_USER_CREATES_BURST` | `120` | Burst allowance |
| `RATE_LIMIT_GLOBAL_PRICING_PER_MINUTE` | `12000` | Pricing reads across all IPs |
| `RATE_LIMIT_GLOBAL_PRICING_BURST` | `1200` | Burst allowance |

### System-wide circuit breakers

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_SYSTEM_REQUESTS_PER_MINUTE` | `450000` | All API requests combined |
| `RATE_LIMIT_SYSTEM_REQUESTS_BURST` | `45000` | Burst allowance |
| `RATE_LIMIT_SYSTEM_MESSAGES_PER_MINUTE` | `375000` | All submitted messages combined |
| `RATE_LIMIT_SYSTEM_MESSAGES_BURST` | `37500` | Burst allowance |

---

## Tuning Guide

### Adjusting for a smaller or larger deployment

Scale all limits linearly with the target message volume. If the target is 10M msgs/day instead of 100M:

```
Daily average:     10,000,000 / 86,400 ≈ 116 msg/s
Peak (5×):         116 × 5 ≈ 580 msg/s ≈ 34,800 msg/min
Global normal:     34,800 × 0.80 ≈ 27,840 → 28,000/min
Global express:    34,800 × 0.20 ≈ 6,960  →  7,000/min
System messages:   34,800 total
```

### Raising per-user limits for large tenants

If a specific tenant legitimately needs more than 1,200 normal messages per minute, raise `RATE_LIMIT_NORMAL_MESSAGES_PER_MINUTE` globally or implement per-tenant overrides (not currently supported; rate limits use a single shared Redis bucket keyed by `user_id`). Also raise `RELAY_NORMAL_PER_USER_LIMIT` to give that tenant more relay throughput — otherwise the API will accept submissions faster than the relay can dispatch them.

### `RATE_LIMIT_ENABLED=false` (default)

Rate limiting is disabled by default so that load tests and benchmarks are not throttled. Enable it in production by setting `RATE_LIMIT_ENABLED=true` and providing `RATE_LIMIT_REDIS_URL`.
