"""Chaos (local): simulate a Redis failover that lost the last K acknowledged claims.

Real failover can't be reproduced on a single local Redis, so we fake its *effect* on an
already-persisted campaign: delete K entries from the claims hash and give their stock
back — exactly the state a promoted replica that missed those writes would be in.

Then:
  1. the reconciler must detect K dynamo_only users,
  2. --fix must restore them and lower stock back to 0 (assumes a sold-out campaign).

The other half of failover — Redis re-selling lost stock to *new* users — is covered by
tests/test_worker.py::test_cap_revokes_over_issued_claims (the durable cap).

    python -m loadtest.chaos.simulate_redis_loss --campaign <id> --lose 25
"""

import argparse
import random
import sys

import redis

from common import dynamo as ddb
from common.keys import campaign_keys
from common.settings import get_settings
from reconciler.main import reconcile


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--campaign", required=True)
    p.add_argument("--lose", type=int, default=25)
    a = p.parse_args()
    s = get_settings()
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)
    dynamo = ddb.connect(s)
    k = campaign_keys(a.campaign)

    users = random.sample(sorted(r.hkeys(k.claims)), a.lose)
    pipe = r.pipeline(transaction=True)
    pipe.hdel(k.claims, *users)
    pipe.incrby(k.stock, len(users))
    pipe.execute()
    print(f"simulated loss of {len(users)} claims; stock now {r.get(k.stock)}")

    before = reconcile(s, r, dynamo, a.campaign, fix=False)
    print(f"detected dynamo_only={len(before.dynamo_only)}")
    after = reconcile(s, r, dynamo, a.campaign, fix=True)
    print(f"fix: {after.fixed}")
    check = reconcile(s, r, dynamo, a.campaign, fix=False)

    ok = len(before.dynamo_only) == len(users) and check.drift == 0 and int(r.get(k.stock)) == 0
    print("PASS" if ok else f"FAIL drift={check.drift} stock={r.get(k.stock)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
