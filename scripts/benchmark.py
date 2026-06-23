from __future__ import annotations

import os
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean
from uuid import uuid4

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000/api/v1")
WORKERS = int(os.environ.get("WORKERS", "50"))
DURATION = int(os.environ.get("DURATION", "20"))
WARMUP = int(os.environ.get("WARMUP", "5"))
USERS = int(os.environ.get("USERS", "10"))
CREDITS = int(os.environ.get("CREDITS", "200000"))
EXPRESS_RATIO = float(os.environ.get("EXPRESS_RATIO", "0.2"))
RECIPIENT = "+989121234567"

client = httpx.Client(base_url=BASE_URL, timeout=10.0)


@dataclass
class Sample:
    status: int
    latency_ms: float
    priority: str
    warmup: bool


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


def create_user(name: str) -> int:
    resp = client.post("/users/", json={"name": name})
    resp.raise_for_status()
    return int(resp.json()["id"])


def topup(user_id: int, amount: int) -> None:
    resp = client.post(f"/users/{user_id}/topup", json={"credit_amount": amount})
    resp.raise_for_status()


def submit(user_id: int, priority: str) -> tuple[int, float]:
    payload = {
        "user_id": user_id,
        "recipient": RECIPIENT,
        "body": "bench",
        "priority": priority,
        "idempotency_key": str(uuid4()),
    }
    t0 = time.perf_counter()
    try:
        resp = client.post("/messages/", json=payload)
        return resp.status_code, (time.perf_counter() - t0) * 1000
    except Exception:
        return 0, (time.perf_counter() - t0) * 1000


def worker_loop(
    user_ids: list[int],
    stats: Stats,
    stop_at: float,
    warmup_until: float,
) -> None:
    rng = random.Random()
    my_users = user_ids
    while time.time() < stop_at:
        user_id = rng.choice(my_users)
        priority = "express" if rng.random() < EXPRESS_RATIO else "normal"
        status, latency_ms = submit(user_id, priority)
        is_warmup = time.time() < warmup_until
        stats.add(Sample(status=status, latency_ms=latency_ms, priority=priority, warmup=is_warmup))


def _pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * p / 100 + 0.5) - 1))
    return sorted_values[idx]


def latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {k: 0.0 for k in ("p50", "p75", "p90", "p95", "p99", "max", "mean")}
    sv = sorted(latencies)
    return {
        "p50": _pct(sv, 50),
        "p75": _pct(sv, 75),
        "p90": _pct(sv, 90),
        "p95": _pct(sv, 95),
        "p99": _pct(sv, 99),
        "max": sv[-1],
        "mean": mean(sv),
    }


_W = 58


def _hr(char: str = "─") -> str:
    return char * _W


def _row(label: str, value: str) -> str:
    return f"  {label:<22} {value}"


def print_report(stats: Stats, elapsed: float) -> None:
    samples = [s for s in stats.snapshot() if not s.warmup]
    if not samples:
        print("No samples collected. is the API running?")
        return

    total = len(samples)
    counts = Counter(s.status for s in samples)
    success = counts[201]
    balance_err = counts[402]
    server_err = sum(v for k, v in counts.items() if k >= 500)
    network_err = counts[0]
    other = total - success - balance_err - server_err - network_err

    all_lat = [s.latency_ms for s in samples]
    p = latency_stats(all_lat)
    express_lat = [s.latency_ms for s in samples if s.priority == "express"]
    normal_lat = [s.latency_ms for s in samples if s.priority == "normal"]

    def pct(n: int) -> str:
        return f"({100 * n / total:5.1f}%)" if total else ""

    print()
    print("=" * _W)
    print("  SMS Gateway Benchmark")
    print(f"  {BASE_URL}")
    print(f"  workers={WORKERS}  duration={elapsed:.1f}s  warmup={WARMUP}s  users={USERS}")
    print("=" * _W)

    print()
    print(f"  {_hr()}")
    print("  Throughput")
    print(f"  {_hr()}")
    print(_row("Requests:", str(total)))
    print(_row("Throughput:", f"{total / elapsed:.1f} req/s"))
    print(_row("Successful (201):", f"{success:>6}  {pct(success)}"))
    print(_row("Balance err (402):", f"{balance_err:>6}  {pct(balance_err)}"))
    print(_row("Server errors (5xx):", f"{server_err:>6}  {pct(server_err)}"))
    print(_row("Network errors:", f"{network_err:>6}  {pct(network_err)}"))
    if other:
        print(_row("Other:", f"{other:>6}  {pct(other)}"))

    print()
    print(f"  {_hr()}")
    print(f"  Latency — all {total} requests (ms)")
    print(f"  {_hr()}")
    for label, key in [
        ("p50", "p50"),
        ("p75", "p75"),
        ("p90", "p90"),
        ("p95", "p95"),
        ("p99", "p99"),
        ("max", "max"),
        ("mean", "mean"),
    ]:
        print(_row(f"{label}:", f"{p[key]:.1f}"))

    if express_lat and normal_lat:
        ep = latency_stats(express_lat)
        np_ = latency_stats(normal_lat)
        print()
        print(f"  {_hr()}")
        print("  Latency by priority (ms)")
        print(f"  {_hr()}")
        header = f"  {'priority':<12}  {'count':>6}  {'p50':>8}  {'p95':>8}  {'p99':>8}  {'max':>8}"
        print(header)
        print(f"  {_hr('·')}")

        def prow(name: str, lat_stats: dict[str, float], n: int) -> str:
            return (
                f"  {name:<12}  {n:>6}"
                f"  {lat_stats['p50']:>7.1f}ms"
                f"  {lat_stats['p95']:>7.1f}ms"
                f"  {lat_stats['p99']:>7.1f}ms"
                f"  {lat_stats['max']:>7.1f}ms"
            )

        print(prow("express", ep, len(express_lat)))
        print(prow("normal", np_, len(normal_lat)))

    print()
    print("=" * _W)
    print()


def main() -> int:
    print(f"Connecting to {BASE_URL}...")
    try:
        client.get("/health/ready").raise_for_status()
    except Exception as exc:
        print(f"API not reachable: {exc}")
        return 2

    print(f"Creating {USERS} test users with {CREDITS} credits each...")
    user_ids: list[int] = []
    for i in range(USERS):
        uid = create_user(f"bench-{uuid4().hex[:8]}")
        topup(uid, CREDITS)
        user_ids.append(uid)
        print(f"  user {i + 1}/{USERS} (id={uid})", end="\r")
    print(f"  {USERS} users ready.{' ' * 20}")

    stats = Stats()
    t_start = time.time()
    warmup_until = t_start + WARMUP
    stop_at = t_start + WARMUP + DURATION

    print(f"Running {WARMUP}s warmup then {DURATION}s benchmark ({WORKERS} workers)...")
    threads = [
        threading.Thread(
            target=worker_loop,
            args=(user_ids, stats, stop_at, warmup_until),
            daemon=True,
        )
        for _ in range(WORKERS)
    ]
    for t in threads:
        t.start()

    bench_start = t_start + WARMUP
    while time.time() < stop_at:
        elapsed = max(0.0, time.time() - bench_start)
        phase = "warmup" if time.time() < warmup_until else "bench "
        live = sum(1 for s in stats.snapshot() if not s.warmup)
        print(f"  [{phase}] {elapsed:5.1f}s  bench_requests={live}", end="\r", flush=True)
        time.sleep(0.5)
    print()

    for t in threads:
        t.join(timeout=5)

    elapsed = time.time() - bench_start
    print_report(stats, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
