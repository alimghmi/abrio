# Dispatch Flow

End-to-end lifecycle of a single message, from API submission to terminal delivery outcome.

---

## Phase 1 — Submission (API)

**Code**: `app/usecases/messages.py: MessageUseCase.create_message`

```
Client POST /messages
      │
      ▼
  Pydantic validation (recipient regex, body length, priority enum)
      │
      ▼
  MessageUseCase.create_message()
      │
      ├── BEGIN TRANSACTION
      │     ├── MessageRepository.create_message()        → INSERT messages
      │     ├── session.flush()                            → get message.id
      │     ├── DispatchJobRepository.create()             → INSERT dispatch_jobs (pending)
      │     └── BalanceRepository.reserve_credits()        → UPDATE balances SET reserved_credits += cost
      │           ↑ SELECT … FOR UPDATE on balance row
      │           ↑ CHECK (credits >= reserved_credits) fires here on overflow
      ├── COMMIT
      │
      ├── on UniqueViolation → fetch + return existing message (idempotent replay)
      ├── on CheckViolation (balance) → 402 InsufficientBalanceError
      └── on ForeignKeyViolation (user) → 404 UserNotFoundError
```

**Atomicity guarantee**: all three writes (message, dispatch_job, balance reservation) commit together or not at all. There is no window where a message exists without a dispatch_job or without a reserved credit.

**Idempotency**: the `UNIQUE (user_id, idempotency_key)` constraint is the authoritative guard. Two concurrent requests for the same key may both pass the application-level check and race to the DB; the one that arrives second gets a `UniqueViolation`, the use case then fetches the committed row and returns it. The client always gets a consistent response.

---

## Phase 2 — Relay (DB → RabbitMQ)

**Code**: `infra/workers/dispatch_relay.py`

Two relay processes run in parallel, one per priority. Each loop iteration:

```
WHILE True:
  ├── [every 30 s] reclaim_stale_inflight()
  │     UPDATE dispatch_jobs SET status=retry, delivery_attempts+=1
  │     WHERE status IN (published, dispatching) AND lease expired
  │
  ├── claim_jobs()
  │     [FAIRNESS PASS]
  │     SELECT id, row_number() OVER (PARTITION BY user_id ORDER BY available_at, created_at)
  │     FROM dispatch_jobs WHERE eligible
  │     → keep rows where rn <= per_user_limit (5 express / 20 normal)
  │     → order by rn, id → interleaved round-robin
  │     → SELECT dispatch_job FOR UPDATE SKIP LOCKED
  │     [TOP-UP PASS]
  │     Fill remaining batch capacity FIFO (excludes already-claimed ids)
  │     → SET locked_at = now(), locked_by = relay_id
  │
  └── publish_claimed_jobs()
        FOR each job:
          process_dispatch_job.apply_async(args=[job_id], task_id=job_id, queue=sms.<priority>)
          → on success: add to published_ids
          → on failure: add to failed_jobs; break (stop publishing this batch)
        │
        ├── BEGIN TRANSACTION
        │     ├── mark_published(published_ids)
        │     │     UPDATE dispatch_jobs SET status=published, published_at=now(), locked_at=NULL
        │     └── mark_publish_retry(failed_jobs)
        │           UPDATE dispatch_jobs SET status=retry, retry_count+=1, available_at=now()+backoff
        └── COMMIT
```

**Publish backoff**: `min(60, 2^min(retry_count, 6)) + rand(0,1)` seconds. Grows from ~1 s to 60 s, capped, with jitter.

**Stale lease reaper**: runs every `REAP_INTERVAL_SECONDS` (30 s). Returns jobs stuck in `published` or `dispatching` past `INFLIGHT_LEASE_SECONDS` (120 s) back to `retry`, incrementing `delivery_attempts`. Closes the stranded-job hole when a worker dies or a delivery message is lost.

**Horizontal scaling**: multiple relay replicas for the same priority are safe. `SKIP LOCKED` ensures each row is claimed by exactly one replica. Stale leases are reclaimable after `LEASE_SECONDS` (60 s).

---

## Phase 3 — Delivery (Celery Worker)

**Code**: `infra/workers/dispatch_tasks.py` → `app/usecases/dispatch.py: DispatchUseCase.process`

```
Celery receives task: process_dispatch_job(job_id)
      │
      ▼
  DispatchUseCase.process(job_id)
      │
      ├── _claim_for_delivery()
      │     BEGIN TRANSACTION
      │       job = SELECT dispatch_job FOR UPDATE
      │       if job is None or job.status terminal → return None (duplicate delivery ignored)
      │       if job not claimable (another worker has fresh lease) → return None
      │       message = SELECT message FOR UPDATE
      │       if message.payment_status != RESERVED → raise RuntimeError (invariant violation)
      │       if express TTL exceeded → _permanently_fail() → COMMIT → return None
      │       else → mark_dispatching(job) + mark_dispatching(message)
      │     COMMIT
      │
      ├── provider.send(message_id, recipient, body)
      │     DummySmsProvider → always succeeds
      │     MockSmsProvider  → [FAIL_TEMP] / [FAIL_PERMANENT] body markers, or random fail_rate
      │
      └── _record_outcome(job_id, worker_id, result)
            BEGIN TRANSACTION
              job = SELECT dispatch_job FOR UPDATE
              if job.locked_by != worker_id → bail (lease lost to reaper)
              message = SELECT message FOR UPDATE
              │
              ├── SUCCESS
              │     balance.settle(amount)          credits -= cost, reserved_credits -= cost
              │     mark_sent(message)              status=sent, payment_status=deducted
              │     mark_completed(job)             status=completed
              │
              ├── PERMANENT_FAILURE
              │     _permanently_fail()
              │       balance.release_credits()     reserved_credits -= cost
              │       mark_permanent_failure(msg)   status=permanent_failed, payment_status=refunded
              │       mark_failed(job)              status=failed
              │
              └── TEMPORARY_FAILURE
                    next_attempt = delivery_attempts + 1
                    if next_attempt >= max_delivery_attempts → _permanently_fail() (see above)
                    elif next_retry would exceed express TTL → _permanently_fail()
                    else
                      mark_retryable_failure(msg)   status=failed (transient)
                      mark_delivery_retry(job)      status=retry, delivery_attempts+=1
                                                    available_at = now() + backoff
            COMMIT
```

**At-least-once delivery**: `acks_late=True` + `reject_on_worker_lost=True`. The task acknowledgment is sent only after the handler returns. If the worker crashes mid-delivery, RabbitMQ redelivers the task; the duplicate-delivery guard in `_claim_for_delivery` silently ignores it if the job already reached a terminal state or if another worker holds a fresh lease.

**Delivery backoff**: same formula as publish backoff (`min(60, 2^min(attempt, 6)) + jitter`).

**Express TTL check**: performed twice — once before claiming for delivery (abandons before provider call) and once when scheduling the next retry (abandons if the retry would land after the TTL). This ensures a late OTP is never sent.

---

## Credit Flow Timeline

```
User topup:    credits=10, reserved_credits=0,  available=10

Submit msg 1:  credits=10, reserved_credits=1,  available=9
Submit msg 2:  credits=10, reserved_credits=2,  available=8

Worker sends msg 1 (success):
               credits=9,  reserved_credits=1,  available=8

Worker sends msg 2 (permanent fail):
               credits=9,  reserved_credits=0,  available=9  ← refunded
```

The invariant `credits >= reserved_credits >= 0` is enforced by DB CHECK constraints at all times.

---

## Fault Tolerance Matrix

| Failure | Recovery mechanism |
|---|---|
| RabbitMQ down at publish time | Relay backs off and retries. Outbox survives in DB. |
| Worker crashes before ack | RabbitMQ redelivers; duplicate-delivery guard ignores if already terminal |
| Worker crashes after ack, before outcome commit | Reaper reclaims `DISPATCHING` job after `INFLIGHT_LEASE_SECONDS`; increments `delivery_attempts` |
| Relay crashes with claimed jobs | Stale lease expires (`LEASE_SECONDS`); another relay claims the rows |
| API crash after DB commit | Dispatch job exists; relay picks it up |
| API crash before DB commit | Nothing committed; client retries with same idempotency key |
| Postgres transient error in worker | Task raises; Celery redelivers (acks_late) |
| Express deadline exceeded | Job abandoned (`permanent_failed`, credit refunded) |
| Max delivery attempts reached | Job abandoned (`permanent_failed`, credit refunded) |

---

## Scaling Guidance

To increase throughput, scale these components independently:

| Component | Scale by | Effect |
|---|---|---|
| `api` | More uvicorn replicas or workers | More concurrent submissions |
| `worker-normal` | More Celery workers or concurrency | Higher normal delivery throughput |
| `worker-express` | More Celery workers | Lower express latency |
| `relay-normal` | More replicas (safe via SKIP LOCKED) | Faster drain of large normal backlogs |
| `relay-express` | More replicas | Faster express pickup |
| PostgreSQL | PgBouncer + read replicas for queries | Scale read workload |
| RabbitMQ | Cluster | Broker HA and throughput |

Benchmark result on a single Hetzner CCX43 (16 vCPU, 61 GiB, Docker), 50,000 messages:

- End-to-end throughput: **838 msg/s** (submit → all terminal)
- Drain rate (delivery, after submission stops): **1,028 msg/s**
- API submission: **1,981 msg/s** (batch endpoint, 4 uvicorn workers)

See [performance.md](performance.md) for the full configuration and run details. Throughput
scales with `WORKER_*_CONCURRENCY` and `RELAY_*_REPLICAS` (SKIP LOCKED keeps replicas safe)
until DB row-contention or broker becomes the bottleneck.
