from __future__ import annotations

import os
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from uuid import uuid4

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000/api/v1")
WORKERS = int(os.environ.get("WORKERS", "100"))
DURATION = int(os.environ.get("DURATION", "20"))
WARMUP = int(os.environ.get("WARMUP", "5"))
USERS = int(os.environ.get("USERS", "100"))
CREDITS = int(os.environ.get("CREDITS", "200000"))
EXPRESS_RATIO = float(os.environ.get("EXPRESS_RATIO", "0.4"))

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
DRAIN_TIMEOUT = float(os.environ.get("DRAIN_TIMEOUT", "300"))
DRAIN_POLL_INTERVAL = float(os.environ.get("DRAIN_POLL_INTERVAL", "2"))
RECIPIENT = "+989121234567"

EXPRESS_TTL_SECONDS = int(os.environ.get("EXPRESS_TTL_SECONDS", "120"))

TUNING_ENV_KEYS = (
    "WORKER_NORMAL_CONCURRENCY",
    "WORKER_EXPRESS_CONCURRENCY",
    "RELAY_NORMAL_REPLICAS",
    "RELAY_EXPRESS_REPLICAS",
    "RELAY_NORMAL_BATCH_SIZE",
    "RELAY_NORMAL_PER_USER_LIMIT",
    "RELAY_EXPRESS_BATCH_SIZE",
    "RELAY_EXPRESS_PER_USER_LIMIT",
)

TERMINAL_STATUSES = {"sent", "permanent_failed"}
PAGE_SIZE = 100

client = httpx.Client(
    base_url=BASE_URL,
    timeout=30.0,
    limits=httpx.Limits(
        max_connections=max(100, WORKERS + 20),
        max_keepalive_connections=max(20, WORKERS),
    ),
)


@dataclass
class MessageResult:
    priority: str
    message_id: str | None


@dataclass
class Sample:
    status: int
    latency_ms: float
    warmup: bool
    user_id: int
    batch_size: int
    messages: list[MessageResult]


@dataclass
class ProcessingSample:
    priority: str
    status: str
    latency_ms: float
    created_at: datetime
    completed_at: datetime


@dataclass
class DrainResult:
    drained: bool
    elapsed_s: float
    expected: int
    terminal: int
    peak_backlog: int


@dataclass
class Stats:
    _samples: list[Sample] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, sample: Sample) -> None:
        with self._lock:
            self._samples.append(sample)

    def snapshot(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def benchmark_message_count(self) -> int:
        with self._lock:
            return sum(sample.batch_size for sample in self._samples if not sample.warmup)


def create_user(name: str) -> int:
    resp = client.post("/users/", json={"name": name})
    resp.raise_for_status()
    return int(resp.json()["id"])


def topup(user_id: int, amount: int) -> None:
    resp = client.post(f"/users/{user_id}/topup", json={"credit_amount": amount})
    resp.raise_for_status()


def build_batch(rng: random.Random, size: int) -> list[str]:
    return ["express" if rng.random() < EXPRESS_RATIO else "normal" for _ in range(size)]


def submit_batch(user_id: int, priorities: list[str]) -> tuple[int, float, list[MessageResult]]:
    payload = {
        "user_id": user_id,
        "messages": [
            {
                "recipient": RECIPIENT,
                "body": "bench",
                "priority": priority,
                "idempotency_key": str(uuid4()),
            }
            for priority in priorities
        ],
    }
    t0 = time.perf_counter()
    try:
        resp = client.post("/messages/batch", json=payload)
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code == 201:
            try:
                items = resp.json()["messages"]
                results = [
                    MessageResult(
                        priority=str(item.get("priority", priority)),
                        message_id=str(item["id"]),
                    )
                    for item, priority in zip(items, priorities, strict=False)
                ]
                if results:
                    return resp.status_code, latency_ms, results
            except (KeyError, TypeError, ValueError):
                pass
        # Non-201 (or unparsable 201): track the intended priorities without ids.
        return (
            resp.status_code,
            latency_ms,
            [MessageResult(priority=priority, message_id=None) for priority in priorities],
        )
    except httpx.HTTPError:
        return (
            0,
            (time.perf_counter() - t0) * 1000,
            [MessageResult(priority=priority, message_id=None) for priority in priorities],
        )


def worker_loop(
    user_ids: list[int],
    stats: Stats,
    stop_at: float,
    warmup_until: float,
) -> None:
    rng = random.Random()
    while time.monotonic() < stop_at:
        user_id = rng.choice(user_ids)
        priorities = build_batch(rng, BATCH_SIZE)
        status, latency_ms, messages = submit_batch(user_id, priorities)
        stats.add(
            Sample(
                status=status,
                latency_ms=latency_ms,
                warmup=time.monotonic() < warmup_until,
                user_id=user_id,
                batch_size=len(priorities),
                messages=messages,
            )
        )


def wait_for_queue_drain(stats: Stats) -> DrainResult:
    accepted = [sample for sample in stats.snapshot() if sample.status == 201]
    expected_by_user: Counter[int] = Counter()
    for sample in accepted:
        expected_by_user[sample.user_id] += sample.batch_size
    expected_total = sum(expected_by_user.values())

    if expected_total == 0:
        return DrainResult(True, 0.0, 0, 0, 0)

    completed_by_user = {user_id: 0 for user_id in expected_by_user}
    started = time.monotonic()
    peak_backlog = 0

    while True:
        for user_id, expected in expected_by_user.items():
            try:
                resp = client.get("/messages/summary", params={"user_id": user_id})
                resp.raise_for_status()
                summary = resp.json()["message_status"]
                terminal = int(summary.get("sent", 0)) + int(summary.get("permanent_failed", 0))
                completed_by_user[user_id] = min(expected, terminal)
            except (httpx.HTTPError, TypeError, ValueError) as e:
                print(f"Error waiting for queue to drain: {e}")
                # A temporary polling failure should not abort the run.
                continue

        terminal_total = sum(completed_by_user.values())
        peak_backlog = max(peak_backlog, expected_total - terminal_total)
        elapsed = time.monotonic() - started
        print(
            f"  [drain] {elapsed:6.1f}s  terminal={terminal_total}/{expected_total}"
            f"  backlog={expected_total - terminal_total}",
            end="\r",
            flush=True,
        )

        if terminal_total >= expected_total:
            print()
            return DrainResult(True, elapsed, expected_total, terminal_total, peak_backlog)

        if elapsed >= DRAIN_TIMEOUT:
            print()
            return DrainResult(False, elapsed, expected_total, terminal_total, peak_backlog)

        time.sleep(DRAIN_POLL_INTERVAL)


def fetch_user_messages(user_id: int) -> list[dict]:
    messages: list[dict] = []
    page = 1

    while True:
        resp = client.get(
            "/messages/",
            params={"user_id": user_id, "page": page, "size": PAGE_SIZE},
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        messages.extend(items)

        pages = int(payload.get("pages", 0))
        if not items or page >= pages:
            break
        page += 1

    return messages


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def collect_processing_samples(
    stats: Stats,
) -> tuple[list[ProcessingSample], int, int]:
    """Measure creation-to-terminal latency from persisted API timestamps."""

    benchmark_messages: list[tuple[MessageResult, int]] = [
        (message, sample.user_id)
        for sample in stats.snapshot()
        if not sample.warmup and sample.status == 201
        for message in sample.messages
    ]
    tracked = [
        (message, user_id)
        for message, user_id in benchmark_messages
        if message.message_id is not None
    ]
    untracked = len(benchmark_messages) - len(tracked)

    expected_ids = {message.message_id for message, _ in tracked if message.message_id}
    user_ids = {user_id for _, user_id in tracked}
    records_by_id: dict[str, dict] = {}

    for user_id in user_ids:
        try:
            for item in fetch_user_messages(user_id):
                message_id = str(item.get("id"))
                if message_id in expected_ids:
                    records_by_id[message_id] = item
        except (httpx.HTTPError, TypeError, ValueError):
            continue

    processing: list[ProcessingSample] = []
    for message, _ in tracked:
        assert message.message_id is not None
        item = records_by_id.get(message.message_id)
        if item is None or item.get("status") not in TERMINAL_STATUSES:
            continue

        try:
            created_at = parse_datetime(str(item["created_at"]))
            completed_at = parse_datetime(str(item["updated_at"]))
        except (KeyError, TypeError, ValueError):
            continue

        processing.append(
            ProcessingSample(
                priority=message.priority,
                status=str(item["status"]),
                latency_ms=max(0.0, (completed_at - created_at).total_seconds() * 1000),
                created_at=created_at,
                completed_at=completed_at,
            )
        )

    missing_or_nonterminal = len(tracked) - len(processing)
    return processing, missing_or_nonterminal, untracked


def _pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * p / 100 + 0.5) - 1))
    return sorted_values[idx]


def latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {key: 0.0 for key in ("p50", "p75", "p90", "p95", "p99", "max", "mean")}
    values = sorted(latencies)
    return {
        "p50": _pct(values, 50),
        "p75": _pct(values, 75),
        "p90": _pct(values, 90),
        "p95": _pct(values, 95),
        "p99": _pct(values, 99),
        "max": values[-1],
        "mean": mean(values),
    }


_W = 64


def _hr(char: str = "─") -> str:
    return char * _W


def _row(label: str, value: str) -> str:
    return f"  {label:<28} {value}"


def print_priority_latency(
    title: str,
    express_latencies: list[float],
    normal_latencies: list[float],
) -> None:
    if not express_latencies and not normal_latencies:
        return

    print()
    print(f"  {_hr()}")
    print(f"  {title}")
    print(f"  {_hr()}")
    print(f"  {'priority':<12}  {'count':>7}  {'p50':>10}  {'p95':>10}  {'p99':>10}  {'max':>10}")
    print(f"  {_hr('·')}")

    for priority, latencies in (
        ("express", express_latencies),
        ("normal", normal_latencies),
    ):
        if not latencies:
            continue
        values = latency_stats(latencies)
        print(
            f"  {priority:<12}  {len(latencies):>7}"
            f"  {values['p50']:>9.1f}ms"
            f"  {values['p95']:>9.1f}ms"
            f"  {values['p99']:>9.1f}ms"
            f"  {values['max']:>9.1f}ms"
        )


def print_tuning_config() -> None:
    provided = {key: os.environ.get(key) for key in TUNING_ENV_KEYS}
    if not any(provided.values()):
        return

    print()
    print(f"  {_hr()}")
    print("  Dispatch tuning under test (from benchmark env)")
    print(f"  {_hr()}")
    for key in TUNING_ENV_KEYS:
        print(_row(f"{key}:", provided[key] or "?"))


def print_report(
    stats: Stats,
    load_elapsed: float,
    drain: DrainResult,
    processing: list[ProcessingSample],
    missing_or_nonterminal: int,
    untracked_accepted: int,
) -> None:
    samples = [sample for sample in stats.snapshot() if not sample.warmup]
    if not samples:
        print("No samples collected. Is the API running?")
        return

    total_batches = len(samples)
    total_messages = sum(sample.batch_size for sample in samples)
    counts = Counter(sample.status for sample in samples)
    success = counts[201]
    balance_err = counts[402]
    rate_limited = counts[429]
    server_err = sum(value for code, value in counts.items() if code >= 500)
    network_err = counts[0]
    other = total_batches - success - balance_err - rate_limited - server_err - network_err

    accepted_messages = sum(sample.batch_size for sample in samples if sample.status == 201)

    api_latencies = [sample.latency_ms for sample in samples]
    api_values = latency_stats(api_latencies)

    def pct(count: int) -> str:
        return f"({100 * count / total_batches:5.1f}%)" if total_batches else ""

    print()
    print("=" * _W)
    print("  SMS Gateway Benchmark (batch endpoint)")
    print(f"  {BASE_URL}")
    print(
        f"  workers={WORKERS}  batch_size={BATCH_SIZE}  duration={load_elapsed:.1f}s  "
        f"warmup={WARMUP}s  users={USERS}"
    )
    print("=" * _W)

    print_tuning_config()

    print()
    print(f"  {_hr()}")
    print("  API throughput")
    print(f"  {_hr()}")
    print(_row("Batch requests:", str(total_batches)))
    print(_row("Messages submitted:", str(total_messages)))
    print(_row("Batch throughput:", f"{total_batches / load_elapsed:.1f} req/s"))
    print(_row("Message throughput:", f"{total_messages / load_elapsed:.1f} msg/s"))
    print(_row("Accepted messages:", str(accepted_messages)))
    print(_row("Successful batches (201):", f"{success:>7}  {pct(success)}"))
    print(_row("Balance errors (402):", f"{balance_err:>7}  {pct(balance_err)}"))
    print(_row("Rate limited (429):", f"{rate_limited:>7}  {pct(rate_limited)}"))
    print(_row("Server errors (5xx):", f"{server_err:>7}  {pct(server_err)}"))
    print(_row("Network errors:", f"{network_err:>7}  {pct(network_err)}"))
    if other:
        print(_row("Other:", f"{other:>7}  {pct(other)}"))

    if total_batches and rate_limited / total_batches > 0.05:
        print()
        print(
            f"  WARNING: {pct(rate_limited).strip()} of batches were rate limited (429). "
            "Throughput below reflects ingress throttling, not system capacity. "
            "Run with RATE_LIMIT_ENABLED=false (the default) and re-run."
        )

    print()
    print(f"  {_hr()}")
    print(f"  API latency - all {total_batches} batch requests (ms)")
    print(f"  {_hr()}")
    for label in ("p50", "p75", "p90", "p95", "p99", "max", "mean"):
        print(_row(f"{label}:", f"{api_values[label]:.1f}"))

    terminal_counts = Counter(sample.status for sample in processing)
    processing_latencies = [sample.latency_ms for sample in processing]

    print()
    print(f"  {_hr()}")
    print("  Message processing and queue drain")
    print(f"  {_hr()}")
    drain_rate = drain.terminal / drain.elapsed_s if drain.elapsed_s > 0 else 0.0
    ingest_rate = total_messages / load_elapsed if load_elapsed > 0 else 0.0
    print(_row("Accepted incl. warmup:", str(drain.expected)))
    print(_row("Terminal incl. warmup:", str(drain.terminal)))
    print(_row("Queue drain completed:", "yes" if drain.drained else "NO - timeout"))
    print(_row("Drain time after load:", f"{drain.elapsed_s:.1f}s"))
    print(_row("Peak backlog after load:", str(drain.peak_backlog)))
    print(_row("Drain rate:", f"{drain_rate:.1f} msg/s"))
    print(_row("Ingest rate:", f"{ingest_rate:.1f} msg/s"))
    print(_row("Measured benchmark messages:", str(len(processing))))
    print(_row("Sent:", str(terminal_counts["sent"])))
    print(_row("Permanently failed:", str(terminal_counts["permanent_failed"])))
    print(_row("Missing/non-terminal:", str(missing_or_nonterminal)))
    if untracked_accepted:
        print(_row("Accepted without parsed ID:", str(untracked_accepted)))

    if processing:
        span = (
            max(sample.completed_at for sample in processing)
            - min(sample.created_at for sample in processing)
        ).total_seconds()
        if span > 0:
            print(_row("Observed processing rate:", f"{len(processing) / span:.1f} msg/s"))

        processing_values = latency_stats(processing_latencies)
        print()
        print(f"  {_hr()}")
        print("  End-to-end processing latency (ms)")
        print("  message creation -> terminal database state")
        print(f"  {_hr()}")
        for label in ("p50", "p75", "p90", "p95", "p99", "max", "mean"):
            print(_row(f"{label}:", f"{processing_values[label]:.1f}"))

        print_priority_latency(
            "End-to-end processing latency by priority (ms)",
            [sample.latency_ms for sample in processing if sample.priority == "express"],
            [sample.latency_ms for sample in processing if sample.priority == "normal"],
        )

    print_delivery_health(processing, drain, ingest_rate, drain_rate)

    print()
    print("=" * _W)
    print()


def print_delivery_health(
    processing: list[ProcessingSample],
    drain: DrainResult,
    ingest_rate: float,
    drain_rate: float,
) -> None:
    if not processing:
        return

    def split(priority: str) -> tuple[int, int]:
        sent = sum(1 for s in processing if s.priority == priority and s.status == "sent")
        failed = sum(
            1 for s in processing if s.priority == priority and s.status == "permanent_failed"
        )
        return sent, failed

    express_sent, express_failed = split("express")
    normal_sent, normal_failed = split("normal")
    express_total = express_sent + express_failed
    normal_total = normal_sent + normal_failed
    express_fail_rate = express_failed / express_total if express_total else 0.0

    print()
    print(f"  {_hr()}")
    print("  Delivery health by priority")
    print(f"  express permanent_failures = TTL deaths (age > {EXPRESS_TTL_SECONDS}s in backlog)")
    print(f"  {_hr()}")
    print(f"  {'priority':<12}  {'sent':>9}  {'failed':>9}  {'fail %':>9}")
    print(f"  {_hr('·')}")
    for priority, sent, failed, total in (
        ("express", express_sent, express_failed, express_total),
        ("normal", normal_sent, normal_failed, normal_total),
    ):
        rate = failed / total if total else 0.0
        print(f"  {priority:<12}  {sent:>9}  {failed:>9}  {100 * rate:>8.2f}%")

    # Verdict: the tuning goal is to keep express fresh AND keep up with ingest.
    keeps_up = drain_rate >= ingest_rate * 0.7
    express_ok = express_fail_rate < 0.01
    backlog_ok = drain.peak_backlog <= max(drain.expected * 0.5, 1)
    passed = drain.drained and keeps_up and express_ok and backlog_ok

    print()
    print(f"  {_hr()}")
    print("  Verdict")
    print(f"  {_hr()}")
    print(_row("Drain completed:", "PASS" if drain.drained else "FAIL"))
    print(
        _row(
            "Drain keeps up with ingest:",
            f"{'PASS' if keeps_up else 'FAIL'}  " f"({drain_rate:.0f} vs {ingest_rate:.0f} msg/s)",
        )
    )
    print(
        _row(
            "Express TTL deaths < 1%:",
            f"{'PASS' if express_ok else 'FAIL'}  ({100 * express_fail_rate:.2f}%)",
        )
    )
    print(
        _row(
            "Backlog stayed bounded:",
            f"{'PASS' if backlog_ok else 'FAIL'}  (peak {drain.peak_backlog})",
        )
    )
    print()
    print(_row("OVERALL:", "PASS — tuned well" if passed else "FAIL — needs more capacity"))


def ensure_rate_limiting_disabled() -> bool:
    probe = create_user(f"rl-probe-{uuid4().hex[:8]}")
    topup(probe, 1000)
    rng = random.Random()
    statuses = [submit_batch(probe, build_batch(rng, BATCH_SIZE))[0] for _ in range(10)]
    if 429 in statuses:
        print(
            "\nERROR: the API is rate limiting (got 429 responses). The benchmark "
            "measures system capacity, not the rate limiter — run with rate limiting "
            "OFF (RATE_LIMIT_ENABLED=false, the default) and re-run."
        )
        return False
    return True


def main() -> int:
    print(f"Connecting to {BASE_URL}...")
    try:
        client.get("/health/ready").raise_for_status()
    except Exception as exc:
        print(f"API not reachable: {exc}")
        return 2

    if not ensure_rate_limiting_disabled():
        return 2

    print(f"Creating {USERS} test users with {CREDITS} credits each...")
    user_ids: list[int] = []
    for i in range(USERS):
        user_id = create_user(f"bench-{uuid4().hex[:8]}")
        topup(user_id, CREDITS)
        user_ids.append(user_id)
        print(f"  user {i + 1}/{USERS} (id={user_id})", end="\r")
    print(f"  {USERS} users ready.{' ' * 20}")

    stats = Stats()
    started = time.monotonic()
    warmup_until = started + WARMUP
    stop_at = warmup_until + DURATION

    print(
        f"Running {WARMUP}s warmup then {DURATION}s benchmark "
        f"({WORKERS} workers, {BATCH_SIZE} msgs/batch)..."
    )
    threads = [
        threading.Thread(
            target=worker_loop,
            args=(user_ids, stats, stop_at, warmup_until),
            daemon=True,
        )
        for _ in range(WORKERS)
    ]
    for thread in threads:
        thread.start()

    while time.monotonic() < stop_at:
        now = time.monotonic()
        elapsed = max(0.0, now - warmup_until)
        phase = "warmup" if now < warmup_until else "bench "
        print(
            f"  [{phase}] {elapsed:5.1f}s  bench_messages={stats.benchmark_message_count()}",
            end="\r",
            flush=True,
        )
        time.sleep(0.5)
    print()

    for thread in threads:
        thread.join(timeout=10)

    print(
        "Waiting for accepted messages, including warmup traffic, "
        "to reach sent or permanent_failed..."
    )
    drain = wait_for_queue_drain(stats)
    processing, missing_or_nonterminal, untracked_accepted = collect_processing_samples(stats)

    print_report(
        stats=stats,
        load_elapsed=max(0.001, float(DURATION)),
        drain=drain,
        processing=processing,
        missing_or_nonterminal=missing_or_nonterminal,
        untracked_accepted=untracked_accepted,
    )

    return 0 if drain.drained else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        client.close()
