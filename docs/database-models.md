# Database Models

Schema is created at boot by SQLAlchemy `create_all` when `DB_CREATE_ALL=true`. There are no Alembic migrations — adding or altering a column requires `docker compose down -v` to drop the volume, then `up` again.

All timestamps are `TIMESTAMP WITH TIME ZONE`. All monetary amounts are `NUMERIC(10,2)`.

---

## Entity Relationship

```
users (1)
  │
  ├─── balances (1:1)   user_id UNIQUE
  │
  └─── messages (1:N)   user_id, idempotency_key UNIQUE
         │
         └─── dispatch_jobs (1:1)   message_id UNIQUE
```

---

## `users`

| Column | Type | Constraints |
|---|---|---|
| `id` | SERIAL (PK) | auto-increment |
| `name` | VARCHAR(255) | NOT NULL |
| `created_at` | TIMESTAMPTZ | server default `now()` |
| `updated_at` | TIMESTAMPTZ | server default `now()`, updated on write |

**CHECK constraints**:
- `updated_at >= created_at`

---

## `balances`

| Column | Type | Constraints |
|---|---|---|
| `id` | SERIAL (PK) | |
| `user_id` | INT (FK→users.id) | UNIQUE |
| `credits` | NUMERIC(10,2) | default 0.00 |
| `reserved_credits` | NUMERIC(10,2) | default 0.00 |
| `created_at` | TIMESTAMPTZ | server default |
| `updated_at` | TIMESTAMPTZ | server default, auto-update |

**CHECK constraints**:
- `credits >= 0` — no negative balance
- `reserved_credits >= 0` — no negative reservation
- `credits >= reserved_credits` ← **the correctness guard** — triggers `InsufficientBalanceError` under concurrent submissions

**Hybrid property**:
- `available_credits = credits - reserved_credits` (read in Python and exposed in API)

**Credit lifecycle**:
```
topup:    credits += amount
submit:   reserved_credits += cost    (reserve)
send:     credits -= cost             (settle: deduct from both)
          reserved_credits -= cost
fail:     reserved_credits -= cost    (release: refund reservation only)
```

---

## `messages`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID (PK) | default `uuid4()` |
| `user_id` | INT (FK→users.id) | indexed |
| `recipient` | VARCHAR(13) | NOT NULL |
| `body` | VARCHAR(70) | NOT NULL |
| `cost` | NUMERIC(10,2) | NOT NULL |
| `priority` | ENUM | `normal` / `express` |
| `idempotency_key` | UUID | NOT NULL |
| `status` | ENUM | indexed; default `queued` |
| `payment_status` | ENUM | default `reserved` |
| `created_at` | TIMESTAMPTZ | server default |
| `updated_at` | TIMESTAMPTZ | auto-update |

**CHECK constraints**:
- `cost > 0`
- `updated_at >= created_at`

**UNIQUE constraint**:
- `(user_id, idempotency_key)` — enforces idempotency at the DB level

**Indexes**:
- `ix_messages_user_id` (on `user_id`)
- `ix_messages_status` (on `status`)
- `ix_messages_priority` (on `priority`)
- `ix_messages_user_status_created_at` composite (user_id, status, created_at) — supports the `get_messages_slice` filters efficiently

**MessageStatus state machine**:
```
queued → dispatching → sent
                    → failed           (transient failure, may be retried via dispatch_job)
                    → permanent_failed  (terminal: max attempts or express TTL exceeded)
```

**PaymentStatus state machine**:
```
reserved → deducted   (message sent, credits settled)
         → refunded   (permanent failure, reservation released)
```

---

## `dispatch_jobs`

The transactional outbox. One row per message, immutable `payload` JSONB snapshot.

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID (PK) | default `uuid4()` |
| `message_id` | UUID (FK→messages.id CASCADE) | UNIQUE |
| `user_id` | INT (FK→users.id) | indexed (denormalised for fairness query) |
| `payload` | JSONB | immutable snapshot of message fields |
| `priority` | ENUM | `normal` / `express` |
| `status` | ENUM | default `pending` |
| `retry_count` | INT | default 0; publish retries (relay→broker) |
| `delivery_attempts` | INT | default 0; delivery retries (worker→provider) |
| `available_at` | TIMESTAMPTZ | when job becomes claimable; server default `now()` |
| `locked_at` | TIMESTAMPTZ | NULL when not claimed |
| `locked_by` | VARCHAR(100) | relay or worker identity string |
| `published_at` | TIMESTAMPTZ | when relay published to broker |
| `completed_at` | TIMESTAMPTZ | terminal timestamp |
| `provider_message_id` | VARCHAR(255) | provider-assigned ID on success |
| `last_error` | TEXT | most recent error message (max 2000 chars) |
| `created_at` | TIMESTAMPTZ | server default |
| `updated_at` | TIMESTAMPTZ | auto-update |

**CHECK constraints**:
- `retry_count >= 0`
- `delivery_attempts >= 0`
- `updated_at >= created_at`

**Indexes**:
- `ix_dispatch_jobs_ready` on `(priority, status, available_at, created_at)` — relay claim scan
- `ix_dispatch_jobs_fairness` on `(priority, status, user_id, available_at)` — per-user fairness window function

**Payload fields** (stored at creation, never mutated):
```json
{
  "message_id": "uuid",
  "user_id":    42,
  "recipient":  "+989121234567",
  "body":       "Your OTP is 4421",
  "priority":   "express",
  "cost":       "1.00"
}
```

**DispatchJobStatus state machine**:
```
pending  ─── relay claims + publishes ───► published ──► worker picks up ──► dispatching
   ▲                                          │                                    │
   │          relay publish retry             │ stale (reaper)                     │
   │◄─── retry ◄──── mark_publish_retry       └───────────────────────────────────►retry
   │
   └── retry ──► claim again ──► published ──► ... (up to max_delivery_attempts)
                                                       │
                                              terminal outcome
                                              ┌─────────────────┐
                                              ▼                 ▼
                                          completed           failed
```

**Two retry budgets**:
- `retry_count`: publish retries (relay→broker). Unbounded. Durable outbox survives arbitrarily long broker outages.
- `delivery_attempts`: delivery retries (worker→provider). Bounded by `MAX_DELIVERY_ATTEMPTS` (default 5). Exhaustion → permanent failure + credit refund.

These counters must never share a budget; sharing would allow broker outages to consume the delivery budget.

**Lease mechanism**:
- `locked_at` + `locked_by` form a lease. A relay or worker sets these when it claims a job.
- If `locked_at` is older than `LEASE_SECONDS` (60 s for relay), another replica may reclaim.
- If `locked_at` for a DISPATCHING job is older than `INFLIGHT_LEASE_SECONDS` (120 s), the reaper returns it to RETRY and increments `delivery_attempts`.

---

## Concurrency Model

All critical writes use `SELECT … FOR UPDATE` (or `FOR UPDATE SKIP LOCKED` for batch claiming) to prevent lost updates. No application-level distributed locking is used.

| Operation | Lock pattern |
|---|---|
| Credit reservation | `SELECT balance FOR UPDATE` → increment reserved_credits → rely on CHECK constraint |
| Relay claim batch | `SELECT dispatch_job FOR UPDATE SKIP LOCKED` — multi-replica safe |
| Worker delivery claim | `SELECT dispatch_job FOR UPDATE` — single-row, re-checked for ownership |
| Balance settlement / release | `SELECT balance FOR UPDATE` → modify |
