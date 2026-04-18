"""Correctness load test: N users race for S vouchers, then everything is cross-checked.

    python -m loadtest.correctness --users 50000 --stock 1000

Fires every user's claim as fast as the connection limit allows (plus duplicate taps for a
fraction of users), then asserts:

  * exactly min(S, users) WON responses, no 5xx
  * every winner's code is unique; each user only ever saw one code
  * after the worker drains: DynamoDB ISSUED rows == Redis claims == Campaigns.issued == S

and prints latency percentiles. Exit code 0 = all assertions passed.

Latency is measured per request from when it is actually sent (client-side queueing for a
free connection is excluded). Uses an asyncio/aiohttp client rather than Locust: what we
need here is the full set of responses for assertions. Latency under sustained load is
locustfile.py's job.
"""

import argparse
import asyncio
import random
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import aiohttp
import redis

from common import dynamo as ddb
from common.devtokens import mint
from common.keys import campaign_keys
from common.settings import get_settings


def pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


async def run(args) -> int:
    s = get_settings()
    campaign = args.campaign or f"lt-{uuid.uuid4().hex[:8]}"
    admin = mint("loadtest-admin", s.dev_jwt_secret, (s.admin_group,))
    now = datetime.now(UTC)

    connector = aiohttp.TCPConnector(limit=args.concurrency)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(args.base_url, connector=connector, timeout=timeout) as c:
        async with c.post(
            "/campaigns",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "campaignId": campaign,
                "totalStock": args.stock,
                "startAt": (now - timedelta(seconds=1)).isoformat(),
                "endAt": (now + timedelta(minutes=30)).isoformat(),
            },
        ) as r:
            r.raise_for_status()
            print(f"campaign {campaign}: {await r.json()}")

        users = [f"u-{campaign}-{i}" for i in range(args.users)]
        print(f"minting {len(users)} tokens ...")
        tokens = {u: mint(u, s.dev_jwt_secret) for u in users}
        taps = users + random.sample(users, int(len(users) * args.dup_rate))
        random.shuffle(taps)

        results: list[tuple[str, int, dict, float, float]] = []
        sem = asyncio.Semaphore(args.concurrency)

        async def tap(user: str):
            async with sem:
                t0 = time.perf_counter()
                try:
                    async with c.post(
                        f"/campaigns/{campaign}/claim",
                        headers={"Authorization": f"Bearer {tokens[user]}"},
                    ) as resp:
                        status = resp.status
                        body = await resp.json() if resp.content_type == "application/json" else {}
                except (aiohttp.ClientError, TimeoutError) as exc:
                    body, status = {"error": repr(exc)}, 0
                done = time.perf_counter()
            results.append((user, status, body, (done - t0) * 1000, done))

        print(f"firing {len(taps)} claims ({len(taps) - len(users)} duplicate taps) ...")
        start = time.perf_counter()
        await asyncio.gather(*(tap(u) for u in taps))
        elapsed = time.perf_counter() - start

    # ------------------------------------------------------------ response assertions
    failures: list[str] = []
    statuses = Counter(st for _, st, _, _, _ in results)
    outcomes = Counter(b.get("result", f"HTTP{st}") for _, st, b, _, _ in results)
    won = [(u, b["voucherCode"], t) for u, st, b, _, t in results if b.get("result") == "WON"]
    expected = min(args.stock, args.users)

    if len(won) != expected:
        failures.append(f"WON responses: {len(won)} != {expected}")
    if any(st >= 500 or st == 0 for st in statuses):
        failures.append(f"server/transport errors: {dict(statuses)}")
    codes_by_user: dict[str, set[str]] = defaultdict(set)
    for u, _st, b, _, _ in results:
        if b.get("voucherCode"):
            codes_by_user[u].add(b["voucherCode"])
    multi = [u for u, cs in codes_by_user.items() if len(cs) > 1]
    if multi:
        failures.append(f"{len(multi)} users saw more than one code, e.g. {multi[:3]}")
    if len({code for _, code, _ in won}) != len(won):
        failures.append("duplicate voucher codes among winners")

    lat = sorted(ms for _, _, _, ms, _ in results)
    sellout = (max(t for *_, t in won) - start) if won else float("nan")
    print(f"\n{len(results)} requests in {elapsed:.2f}s ({len(results) / elapsed:.0f} req/s)")
    print(f"statuses: {dict(statuses)}\noutcomes: {dict(outcomes)}")
    print(
        f"latency ms: p50={pct(lat, 0.50):.1f} p95={pct(lat, 0.95):.1f} "
        f"p99={pct(lat, 0.99):.1f} max={lat[-1]:.1f} mean={statistics.fmean(lat):.1f}"
    )
    print(f"time to sell out: {sellout:.2f}s")

    # ------------------------------------------------------------ durable-path assertions
    if not args.skip_persist_check:
        r = redis.Redis.from_url(s.redis_url, decode_responses=True)
        dynamo = ddb.connect(s)
        k = campaign_keys(campaign)
        deadline = time.monotonic() + args.wait_persist
        issued = 0
        while time.monotonic() < deadline:
            rows = ddb.query_all_claims(dynamo, campaign)
            issued = sum(1 for i in rows if i["status"] == ddb.ISSUED)
            if issued >= expected:
                break
            await asyncio.sleep(1)
        counter = int(dynamo.campaigns.get_item(Key={"campaignId": campaign})["Item"]["issued"])
        hlen = r.hlen(k.claims)
        print(f"\nDynamoDB ISSUED rows={issued} counter={counter} | Redis claims={hlen}")
        if not (issued == counter == hlen == expected):
            failures.append(
                f"persistence mismatch: dynamo={issued} counter={counter} redis={hlen} "
                f"expected={expected}"
            )
        for code in {code for _, code, _ in won} - {i["voucherCode"] for i in rows}:
            failures.append(f"WON code not persisted: {code}")
            break

    print()
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("PASS  all assertions held")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--users", type=int, default=50_000)
    p.add_argument("--stock", type=int, default=1_000)
    p.add_argument("--concurrency", type=int, default=100, help="max in-flight requests")
    p.add_argument("--dup-rate", type=float, default=0.05, help="fraction of users double-tapping")
    p.add_argument("--campaign", help="campaign id (default: random)")
    p.add_argument("--wait-persist", type=float, default=60, help="seconds to wait for the worker")
    p.add_argument("--skip-persist-check", action="store_true")
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
