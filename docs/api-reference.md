# API Reference

Base path: `{API_PREFIX}/v1` (default `/api/v1`)

All error responses share the envelope:
```json
{ "error": { "code": "<string>", "message": "<string>" } }
```

All list endpoints are paginated (`page`, `size` query params; response includes `total`, `pages`, `items`).

---

## Health

### `GET /health/live`
Liveness probe. Returns `200` immediately; no dependency checks.

### `GET /health/ready`
Readiness probe. Checks PostgreSQL, Redis, and RabbitMQ.

**Response `200`**:
```json
{
  "status": "ok",
  "service": "abrio-gateway",
  "redis":    { "status": "ok", "duration_ms": 4.2, "error": null },
  "database": { "status": "ok", "duration_ms": 8.1, "error": null },
  "rabbitmq": { "status": "ok", "duration_ms": 12.3, "error": null },
  "timestamp": 1782240425.1
}
```

**Response `503`**: one or more dependencies unhealthy.

---

## Pricing

### `GET /pricing`
Return the current per-message credit cost.

**Response `200`**:
```json
{
  "normal_message":  "1.00",
  "express_message": "1.00"
}
```

---

## Users

### `POST /users`
Create a user. A `Balance` row (with 0 credits) is created atomically.

**Request**:
```json
{ "name": "Acme Corp" }
```

**Response `201`**:
```json
{
  "id": 42,
  "name": "Acme Corp",
  "balance": {
    "credits":           "0.00",
    "reserved_credits":  "0.00",
    "available_credits": "0.00",
    "updated_at": "2026-06-23T10:00:00Z"
  },
  "created_at": "2026-06-23T10:00:00Z"
}
```

---

### `GET /users`
Paginated list of users.

**Query params**: `page` (default 1), `size` (default 20, max 100).

**Response `200`**: `PaginatedResponse[UserResponse]`

---

### `GET /users/{user_id}`
Retrieve a single user with their current balance.

**Response `200`**: `UserResponse` (see above).
**Response `404`** `user_not_found`: user does not exist.

---

### `POST /users/{user_id}/topup`
Add credits to the user's balance. Idempotency is the caller's responsibility (no server-side topup idempotency key).

**Request**:
```json
{ "credit_amount": "100.00" }
```

**Response `200`**:
```json
{
  "user_id":           42,
  "credits":           "100.00",
  "reserved_credits":  "0.00",
  "available_credits": "100.00",
  "updated_at": "2026-06-23T10:01:00Z"
}
```

**Response `404`** `user_not_found`.

---

## Messages

### `POST /messages`
Submit a single SMS message. Exactly one credit is reserved atomically; `status=queued`, `payment_status=reserved`.

**Request**:
```json
{
  "user_id":         42,
  "recipient":       "+989121234567",
  "body":            "Your OTP is 4421",
  "priority":        "express",
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000"
}
```

Field constraints:
- `recipient`: matches `^(\+98|0)?9\d{9}$` (Iranian mobile numbers, normalised or raw)
- `body`: 1–70 characters (single SMS page; Persian and English treated identically)
- `priority`: `"normal"` | `"express"`
- `idempotency_key`: UUID; unique per `(user_id, idempotency_key)`. Repeat calls with the same key return the original message without charging again.

**Response `201`**: `MessageResponse`
```json
{
  "id":              "a1b2c3d4-...",
  "user_id":         42,
  "recipient":       "+989121234567",
  "body":            "Your OTP is 4421",
  "cost":            "1.00",
  "priority":        "express",
  "idempotency_key": "550e8400-...",
  "status":          "queued",
  "payment_status":  "reserved",
  "created_at":      "2026-06-23T10:00:00Z",
  "updated_at":      "2026-06-23T10:00:00Z"
}
```

**Response `402`** `insufficient_balance`: user's available credits < message cost.
**Response `404`** `user_not_found`.
**Response `400`** `idempotency_conflict`: concurrent racing submissions with the same key that cannot be resolved to an existing message.

---

### `POST /messages/batch`
Submit up to 100 messages for a single user in one atomic transaction. Credits for all messages are reserved together; if any check fails, the entire batch is rejected.

**Request**:
```json
{
  "user_id": 42,
  "messages": [
    {
      "recipient":       "+989121234567",
      "body":            "Batch msg 1",
      "priority":        "normal",
      "idempotency_key": "uuid-1"
    },
    {
      "recipient":       "+989129876543",
      "body":            "Batch msg 2",
      "priority":        "express",
      "idempotency_key": "uuid-2"
    }
  ]
}
```

Duplicate `idempotency_key` values within a single batch request are rejected with `400 idempotency_duplicate` before the transaction begins.

**Response `201`**:
```json
{
  "created_count": 2,
  "messages": [ /* MessageResponse × 2 */ ]
}
```

**Response `402`** `insufficient_balance`: total batch cost exceeds available credits.
**Response `400`** `idempotency_duplicate` | `idempotency_conflict`.

---

### `GET /messages`
Paginated message list with optional filters.

**Query params**:
| Param | Type | Description |
|---|---|---|
| `user_id` | int | Filter by user |
| `status` | string | `queued` \| `dispatching` \| `sent` \| `failed` \| `permanent_failed` |
| `priority` | string | `normal` \| `express` |
| `payment_status` | string | `reserved` \| `deducted` \| `refunded` |
| `created_after` | datetime | ISO 8601 |
| `created_before` | datetime | ISO 8601 |
| `updated_after` | datetime | ISO 8601 |
| `updated_before` | datetime | ISO 8601 |
| `page` | int | Default 1 |
| `size` | int | Default 20, max 100 |

**Response `200`**: `PaginatedResponse[MessageResponse]`
```json
{
  "total": 500,
  "page":  1,
  "size":  20,
  "pages": 25,
  "items": [ /* MessageResponse[] */ ]
}
```

---

### `GET /messages/summary`
Aggregated status and payment counts for a user.

**Query params**: `user_id` (required).

**Response `200`**:
```json
{
  "user_id": 42,
  "total":   1000,
  "message_status": {
    "queued":          10,
    "dispatching":      5,
    "sent":           970,
    "failed":           2,
    "permanent_failed": 13
  },
  "payment_status": {
    "reserved":  15,
    "deducted": 970,
    "refunded":  15
  }
}
```

---

### `GET /messages/{message_id}`
Retrieve a single message by UUID.

**Query params**: `user_id` (required for scoping; prevents cross-tenant access).

**Response `200`**: `MessageResponse`.
**Response `404`** `message_not_found`.

---

## Status + Payment State Machines

```
MessageStatus:  queued → dispatching → sent
                                    ↘ failed           (transient, may retry)
                                    ↘ permanent_failed  (terminal)

PaymentStatus:  reserved → deducted   (on sent)
                         → refunded   (on permanent_failed)

DispatchJobStatus: pending → published → dispatching → completed
                          → retry                   → retry → ...
                                                    → failed
```

---

## Rate Limiting

Rate limiting uses a Redis token-bucket per `(user_id, endpoint)` and shared global/system buckets. All limits are configurable via environment variables.

Default per-user limits (per minute / burst):
- Normal messages: 12,000 / 2,000
- Express messages: 1,200 / 200
- Batch requests: 120 / 20

On rejection: `HTTP 429` with headers `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`.

Rate limiting is **fail-open** by default (`RATE_LIMIT_FAIL_OPEN=true`): if Redis is unavailable, requests proceed normally.

---

## Metrics

`GET /metrics` — Prometheus text format. Key metrics:

| Metric | Type | Description |
|---|---|---|
| `abrio_http_requests_total` | Counter | Requests by method, route, status |
| `abrio_http_request_duration_seconds` | Histogram | Latency by route |
| `abrio_messages_submitted_total` | Counter | Accepted submissions by priority |
| `abrio_message_submission_rejected_total` | Counter | Rejections by reason |
| `abrio_idempotent_replays_total` | Counter | Idempotent key replays |
| `abrio_delivery_attempts_total` | Counter | Outcomes by priority and result |
| `abrio_message_end_to_end_duration_seconds` | Histogram | Submission → terminal latency |
| `abrio_dispatch_ready_jobs` | Gauge | Jobs queued for relay |
| `abrio_dispatch_oldest_ready_age_seconds` | Gauge | Oldest pending job age |
| `abrio_dispatch_retries_total` | Counter | Retries by type (publication/delivery) |
| `abrio_payment_consistency_violations` | Gauge | Balance accounting anomalies (should be 0) |
