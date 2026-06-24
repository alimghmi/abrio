from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from uuid import uuid4

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000/api/v1")
RECIPIENT = "+989121234567"
CONCURRENCY = int(os.environ.get("CONCURRENCY", "32"))
# Balance-limit scenario.
LIMIT_CREDITS = int(os.environ.get("LIMIT_CREDITS", "10"))
LIMIT_ATTEMPTS = int(os.environ.get("LIMIT_ATTEMPTS", "100"))
# Fairness scenario.
HOT_COUNT = int(os.environ.get("HOT_COUNT", "4000"))
SMALL_USERS = int(os.environ.get("SMALL_USERS", "6"))
SMALL_EACH = int(os.environ.get("SMALL_EACH", "20"))
DRAIN_TIMEOUT = float(os.environ.get("DRAIN_TIMEOUT", "120"))

client = httpx.Client(base_url=BASE_URL, timeout=30.0)


@dataclass
class Report:
    checks: list[tuple[bool, str]] = field(default_factory=list)

    def check(self, ok: bool, label: str) -> None:
        self.checks.append((ok, label))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}")

    def ok(self) -> bool:
        return all(ok for ok, _ in self.checks)


def create_user(name: str) -> int:
    resp = client.post("/users/", json={"name": name})
    resp.raise_for_status()
    return int(resp.json()["id"])


def ensure_rate_limiting_disabled() -> bool:
    probe = create_user(f"rl-probe-{uuid4().hex[:8]}")
    topup(probe, 1000)
    statuses = [send(probe, f"rl probe {i}", "express") for i in range(60)]
    if 429 in statuses:
        print(
            "\nERROR: the API is rate limiting (got 429 responses). Load tests must "
            "run with rate limiting OFF so accept/reject counts stay exact.\n"
            "Set RATE_LIMIT_ENABLED=false on the API (the default) and re-run."
        )
        return False
    return True


def topup(user_id: int, amount: int) -> None:
    resp = client.post(f"/users/{user_id}/topup", json={"credit_amount": amount})
    resp.raise_for_status()


def get_balance(user_id: int) -> dict[str, str]:
    resp = client.get(f"/users/{user_id}")
    resp.raise_for_status()
    return resp.json()["balance"]


def summary(user_id: int) -> dict[str, int]:
    resp = client.get("/messages/summary", params={"user_id": user_id})
    resp.raise_for_status()
    resp = resp.json()
    return {"user_id": resp["user_id"], "total": resp["total"], **resp["message_status"]}


def send(user_id: int, body: str, priority: str, idempotency_key: str | None = None) -> int:
    payload = {
        "user_id": user_id,
        "recipient": RECIPIENT,
        "body": body[:70],
        "priority": priority,
        "idempotency_key": idempotency_key or str(uuid4()),
    }
    resp = client.post("/messages/", json=payload)
    return resp.status_code


def send_returning_json(user_id: int, body: str, idem: str) -> dict:
    payload = {
        "user_id": user_id,
        "recipient": RECIPIENT,
        "body": body,
        "priority": "normal",
        "idempotency_key": idem,
    }
    resp = client.post("/messages/", json=payload)
    resp.raise_for_status()
    return resp.json()


def send_batch(user_id: int, count: int, priority: str = "normal") -> int:
    created = 0
    for start in range(0, count, 100):
        chunk = min(100, count - start)
        items = [
            {
                "recipient": RECIPIENT,
                "body": f"batch {start + i}",
                "priority": priority,
                "idempotency_key": str(uuid4()),
            }
            for i in range(chunk)
        ]
        resp = client.post("/messages/batch", json={"user_id": user_id, "messages": items})
        resp.raise_for_status()
        created += resp.json()["created_count"]
    return created


def scenario_balance_limit(report: Report) -> None:
    print("\n== Scenario 1: balance limit under concurrency ==")
    user_id = create_user("limit-user")
    topup(user_id, LIMIT_CREDITS)

    statuses: list[int] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(send, user_id, f"limit {i}", "normal") for i in range(LIMIT_ATTEMPTS)
        ]
        for fut in as_completed(futures):
            statuses.append(fut.result())

    accepted = sum(1 for s in statuses if s == 201)
    rejected_402 = sum(1 for s in statuses if s == 402)
    print(f"  attempts={LIMIT_ATTEMPTS} credits={LIMIT_CREDITS} "
          f"accepted={accepted} rejected_402={rejected_402} "
          f"other={LIMIT_ATTEMPTS - accepted - rejected_402}")

    report.check(accepted == LIMIT_CREDITS, f"exactly {LIMIT_CREDITS} accepted (got {accepted})")
    report.check(
        accepted + rejected_402 == LIMIT_ATTEMPTS,
        "every attempt was either accepted or cleanly rejected with 402",
    )

    bal = get_balance(user_id)
    available = float(bal["available_credits"])
    report.check(available >= 0, f"available_credits never negative (={available})")


def scenario_idempotency(report: Report) -> None:
    print("\n== Scenario 2: idempotency ==")
    user_id = create_user("idem-user")
    topup(user_id, 5)
    idem = str(uuid4())

    first = send_returning_json(user_id, "idem hello", idem)
    second = send_returning_json(user_id, "idem hello", idem)

    report.check(first["id"] == second["id"], "duplicate idempotency key returns the same message")
    bal = get_balance(user_id)
    # One message charged: reserved or already deducted, but credits - available == 1.
    charged = float(bal["credits"]) - float(bal["available_credits"])
    report.check(charged == 1.0, f"charged exactly once for a repeated key (charged={charged})")


def scenario_fairness(report: Report) -> None:
    print("\n== Scenario 3: fairness (hot user must not starve small users) ==")
    hot = create_user("hot-user")
    topup(hot, HOT_COUNT)
    smalls = [create_user(f"small-{i}") for i in range(SMALL_USERS)]
    for uid in smalls:
        topup(uid, SMALL_EACH)

    # Flood the hot user first (via the batch endpoint, so the backlog lands
    # fast and actually stands in the queue) — worst case for FIFO fairness.
    print(f"  flooding hot user with {HOT_COUNT} messages (batched)...")
    t_flood = time.time()
    hot_accepted = send_batch(hot, HOT_COUNT)
    print(f"  hot accepted={hot_accepted} in {time.time() - t_flood:.1f}s")

    # Now the small users submit, AFTER the hot backlog already exists.
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = []
        for uid in smalls:
            for i in range(SMALL_EACH):
                futs.append(pool.submit(send, uid, f"small {i}", "normal"))
        small_accepted = sum(1 for f in as_completed(futs) if f.result() == 201)
    print(f"  small users submitted {small_accepted} messages at t0")

    # Poll until all small users are fully delivered (or timeout).
    small_done_at: dict[int, float] = {}
    hot_pending_when_all_small_done: int | None = None
    deadline = t0 + DRAIN_TIMEOUT
    while time.time() < deadline:
        all_small_done = True
        for uid in smalls:
            if uid in small_done_at:
                continue
            s = summary(uid)
            if s["sent"] >= SMALL_EACH:
                small_done_at[uid] = time.time() - t0
            else:
                all_small_done = False
        if all_small_done:
            hs = summary(hot)
            hot_pending_when_all_small_done = hs["total"] - hs["sent"] - hs["permanent_failed"]
            break
        time.sleep(0.25)

    if small_done_at:
        slowest = max(small_done_at.values())
        print(f"  all small users delivered within {slowest:.2f}s of submitting")
    print(f"  hot still pending when small users finished: {hot_pending_when_all_small_done}")

    report.check(
        len(small_done_at) == SMALL_USERS,
        f"all {SMALL_USERS} small users fully delivered (done={len(small_done_at)})",
    )
    # Fairness signal: small users finished while a meaningful hot backlog remained,
    # i.e. they did NOT have to wait behind the entire hot flood (pure FIFO).
    if hot_pending_when_all_small_done is not None:
        report.check(
            hot_pending_when_all_small_done > 0,
            "small users drained while hot backlog still pending (not starved behind FIFO)",
        )

    # Drain everything and check settlement correctness for the hot user.
    print("  draining hot backlog...")
    while time.time() < t0 + DRAIN_TIMEOUT:
        hs = summary(hot)
        if hs["sent"] + hs["permanent_failed"] >= hs["total"]:
            break
        time.sleep(0.5)

    hs = summary(hot)
    report.check(
        hs["sent"] + hs["permanent_failed"] == hs["total"],
        f"hot user fully terminal (sent={hs['sent']} perm_failed={hs['permanent_failed']} "
        f"total={hs['total']})",
    )
    bal = get_balance(hot)
    credits = float(bal["credits"])
    reserved = float(bal["reserved_credits"])
    # dummy provider always succeeds -> every sent message deducted 1 credit.
    report.check(
        credits == HOT_COUNT - hs["sent"],
        f"hot credits deducted exactly per sent message (credits={credits}, "
        f"sent={hs['sent']}, topup={HOT_COUNT})",
    )
    report.check(reserved == 0.0, f"hot reserved credits returned to zero (reserved={reserved})")


def main() -> int:
    """
    1. Balance limit under concurrency: a user with C credits hit with many more
    than C concurrent submissions accepts exactly C and never goes negative.
    2. Idempotency: resubmitting the same key returns the same message, charged once.
    3. Fairness: a hot user flooding the queue must not starve small users — their
    messages drain quickly even while the hot backlog is still being worked off.
    4. Settlement correctness: once drained, credits deducted == messages sent,
    reserved credits return to zero, nothing is left stuck in a non-terminal state.
    """
    print(f"Target: {BASE_URL}")
    try:
        client.get("/health/ready").raise_for_status()
    except Exception as exc:
        print(f"API not reachable: {exc}")
        return 2

    if not ensure_rate_limiting_disabled():
        return 2

    report = Report()
    scenario_balance_limit(report)
    scenario_idempotency(report)
    scenario_fairness(report)

    print("\n== Result ==")
    passed = sum(1 for ok, _ in report.checks if ok)
    print(f"  {passed}/{len(report.checks)} checks passed")
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
