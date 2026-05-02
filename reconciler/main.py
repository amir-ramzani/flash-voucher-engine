"""Redis vs DynamoDB drift check (and optional repair).

    python -m reconciler.main [--campaign ID ...] [--fix] [--loop SECONDS]

Drift it detects and (with --fix) repairs:

  redis_only    claim in Redis, no DynamoDB row, older than the grace period
                (lost/dead-lettered event)            -> re-enqueue an event (worker is idempotent)
  dynamo_only   row in DynamoDB, missing from Redis   -> restore into the claims hash
                (Redis failover lost writes)             and lower stock accordingly
  code_mismatch both exist with different codes       -> report only (needs a human)
  counter       Campaigns.issued != #ISSUED+REDEEMED  -> report only
  over_issued   DynamoDB issued > totalStock          -> report only; must be impossible

What it CANNOT see: a claim Redis lost *before* the worker read it — no trace remains
anywhere. That user saw WON but holds nothing; the DynamoDB cap guarantees this never
results in more than totalStock vouchers. See docs/PLAN.md §6.
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field

import redis

from common import dynamo as ddb
from common.keys import ACTIVE_CAMPAIGNS, REVOKED, campaign_keys
from common.metrics import Metrics
from common.scripts import register
from common.settings import Settings, get_settings

log = logging.getLogger("reconciler")


@dataclass
class Report:
    campaign_id: str
    total_stock: int | None = None
    redis_claims: int = 0
    redis_stock: int | None = None
    dynamo_rows: int = 0
    dynamo_issued: int = 0
    issued_counter: int | None = None
    redis_only: list[str] = field(default_factory=list)
    dynamo_only: list[str] = field(default_factory=list)
    code_mismatch: list[str] = field(default_factory=list)
    counter_mismatch: bool = False
    over_issued: bool = False
    fixed: dict = field(default_factory=dict)

    @property
    def drift(self) -> int:
        return (
            len(self.redis_only)
            + len(self.dynamo_only)
            + len(self.code_mismatch)
            + int(self.counter_mismatch)
            + int(self.over_issued)
        )


def _stream_id_ms(stream_id: str) -> int:
    return int(stream_id.split("-", 1)[0])


def reconcile(
    s: Settings, r: redis.Redis, dynamo: ddb.Dynamo, campaign_id: str, fix: bool
) -> Report:
    k = campaign_keys(campaign_id)
    rep = Report(campaign_id)

    campaign = dynamo.campaigns.get_item(Key={"campaignId": campaign_id}, ConsistentRead=True).get(
        "Item"
    )
    if campaign:
        rep.total_stock = int(campaign["totalStock"])
        rep.issued_counter = int(campaign.get("issued", 0))

    redis_claims: dict[str, str] = dict(r.hscan_iter(k.claims, count=1000))
    rows = {item["userId"]: item for item in ddb.query_all_claims(dynamo, campaign_id)}
    # Latest event per user (streams are not trimmed; ~totalStock entries).
    events: dict[str, tuple[str, dict]] = {}
    for msg_id, fields in r.xrange(k.events):
        events[fields.get("user", "")] = (msg_id, fields)

    stock = r.get(k.stock)
    rep.redis_stock = int(stock) if stock is not None else None
    rep.redis_claims = len(redis_claims)
    rep.dynamo_rows = len(rows)
    rep.dynamo_issued = sum(1 for i in rows.values() if i["status"] in (ddb.ISSUED, ddb.REDEEMED))

    now_ms = int(time.time() * 1000)
    grace_ms = s.reconciler_grace_s * 1000
    for user, code in redis_claims.items():
        row = rows.get(user)
        if row is None:
            ev = events.get(user)
            if code == REVOKED or (ev and now_ms - _stream_id_ms(ev[0]) < grace_ms):
                continue
            rep.redis_only.append(user)
        elif code != REVOKED and row.get("voucherCode") != code:
            rep.code_mismatch.append(user)
    rep.dynamo_only = [u for u in rows if u not in redis_claims]
    rep.counter_mismatch = (
        rep.issued_counter is not None and rep.issued_counter != rep.dynamo_issued
    )
    rep.over_issued = rep.total_stock is not None and rep.dynamo_issued > rep.total_stock

    if fix:
        for user in rep.redis_only:
            ev = events.get(user)
            ts = ev[1].get("ts") if ev else str(now_ms)
            r.xadd(
                k.events, {"user": user, "code": redis_claims[user], "ts": ts, "src": "reconciler"}
            )
        for user in rep.dynamo_only:
            item = rows[user]
            value = REVOKED if item["status"] == ddb.REVOKED else item["voucherCode"]
            r.hsetnx(k.claims, user, value)
        before, after = register(r).reconcile_stock(keys=[k.meta, k.stock, k.claims])
        rep.fixed = {
            "requeued": len(rep.redis_only),
            "restored": len(rep.dynamo_only),
            "stock_before": before,
            "stock_after": after,
        }
    return rep


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--campaign", action="append", help="campaign id (default: all active)")
    p.add_argument("--fix", action="store_true", help="repair what can be repaired")
    p.add_argument("--loop", type=float, default=0, help="repeat every N seconds")
    args = p.parse_args(argv)

    s = get_settings()
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)
    dynamo = ddb.connect(s)
    metrics = Metrics(s.metrics_namespace, "reconciler", s.metrics_emf)

    while True:
        total_drift = 0
        for cid in args.campaign or sorted(r.smembers(ACTIVE_CAMPAIGNS)):
            rep = reconcile(s, r, dynamo, cid, args.fix)
            total_drift += rep.drift
            print(json.dumps({**asdict(rep), "drift": rep.drift}), flush=True)
            if rep.over_issued:
                metrics.incr("OverIssued")
        metrics.gauge("Drift", total_drift)
        metrics.flush()
        if not args.loop:
            return 1 if total_drift and not args.fix else 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
