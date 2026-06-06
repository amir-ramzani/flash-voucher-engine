"""End-to-end smoke test against a running deployment (local compose or AWS).

    python -m scripts.smoke                                   # local, AUTH_MODE=dev
    python -m scripts.smoke --base-url http://my-alb ...      # AWS (see --help for tokens)

Exercises every endpoint once on a fresh 2-voucher campaign — including the worker
(waits for the claim to become ISSUED in DynamoDB) — and exits non-zero on any failure.
Takes a couple of seconds; meant for "did the deploy work?", not for load.

Auth: in dev mode tokens are minted locally from DEV_JWT_SECRET. Against Cognito pass
real access tokens: --admin-token, and --user-tokens with three different users.
"""

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from common.devtokens import mint
from common.settings import get_settings


class Smoke:
    def __init__(self, base_url: str, timeout: float):
        self.c = httpx.Client(base_url=base_url, timeout=timeout)
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        print(f"  {mark} {name}" + (f"  — {detail}" if detail and not ok else ""))
        self.failures += not ok
        return ok

    def call(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.c.request(method, path, headers=headers, **kw)


def run(args) -> int:
    s = get_settings()
    if args.admin_token:
        admin = args.admin_token
        users = args.user_tokens.split(",")
        if len(users) < 3:
            sys.exit("--user-tokens needs 3 comma-separated tokens for 3 different users")
    else:
        run_id = uuid.uuid4().hex[:6]
        admin = mint(f"smoke-admin-{run_id}", s.dev_jwt_secret, (s.admin_group,))
        users = [mint(f"smoke-{n}-{run_id}", s.dev_jwt_secret) for n in ("alice", "bob", "carol")]
    alice, bob, carol = users[:3]
    campaign = f"smoke-{uuid.uuid4().hex[:8]}"
    t = Smoke(args.base_url, args.timeout)
    print(f"smoke test → {args.base_url}  (campaign {campaign})\n")

    print("health")
    # Wait for readiness: right after `make up` the port accepts connections before
    # uvicorn is listening (connection reset) or Redis is reachable (503).
    deadline, r, last_err = time.monotonic() + args.wait_ready, None, ""
    while True:
        try:
            r = t.call("GET", "/health")
            if r.status_code == 200:
                break
            last_err = f"HTTP {r.status_code} {r.text}"
        except httpx.HTTPError as exc:
            last_err = repr(exc)
        if time.monotonic() > deadline:
            t.check(f"API ready within {args.wait_ready:.0f}s", False, last_err)
            return 1
        time.sleep(0.5)
    t.check("GET /health → 200", True)

    print("auth")
    r = t.call("POST", f"/campaigns/{campaign}/claim")
    t.check("no token → 401", r.status_code == 401, r.text)
    r = t.call("POST", f"/campaigns/{campaign}/claim", token="not-a-jwt")
    t.check("garbage token → 401", r.status_code == 401, r.text)

    print("campaign")
    now = datetime.now(UTC)
    body = {
        "campaignId": campaign,
        "totalStock": 2,
        "startAt": (now - timedelta(seconds=5)).isoformat(),
        "endAt": (now + timedelta(minutes=10)).isoformat(),
    }
    r = t.call("POST", "/campaigns", token=alice, json=body)
    t.check("non-admin create → 403", r.status_code == 403, r.text)
    r = t.call("POST", "/campaigns", token=admin, json=body)
    if not t.check("admin create → 201", r.status_code == 201, r.text):
        return 1
    r = t.call("POST", "/campaigns", token=admin, json=body)
    t.check("identical re-POST → 200 (idempotent)", r.status_code == 200, r.text)
    r = t.call("POST", "/campaigns", token=admin, json={**body, "totalStock": 3})
    t.check("conflicting re-POST → 409", r.status_code == 409, r.text)

    print("claim")
    r = t.call("POST", f"/campaigns/{campaign}/claim", token=alice)
    won = r.json() if r.status_code == 200 else {}
    t.check("alice → WON", won.get("result") == "WON", r.text)
    code = won.get("voucherCode", "")
    r = t.call("POST", f"/campaigns/{campaign}/claim", token=alice)
    t.check(
        "alice again → ALREADY, same code",
        r.status_code == 200 and r.json() == {"result": "ALREADY", "voucherCode": code},
        r.text,
    )
    r = t.call("POST", f"/campaigns/{campaign}/claim", token=bob)
    t.check("bob → WON", r.status_code == 200 and r.json().get("result") == "WON", r.text)
    r = t.call("POST", f"/campaigns/{campaign}/claim", token=carol)
    t.check(
        "carol → SOLD_OUT", r.status_code == 200 and r.json().get("result") == "SOLD_OUT", r.text
    )
    r = t.call("POST", "/campaigns/smoke-missing-campaign/claim", token=carol)
    t.check("unknown campaign → 404", r.status_code == 404, r.text)

    print("persistence (worker)")
    status, deadline = None, time.monotonic() + args.persist_wait
    while time.monotonic() < deadline:
        r = t.call("GET", f"/campaigns/{campaign}/my-voucher", token=alice)
        status = r.json().get("status") if r.status_code == 200 else f"HTTP {r.status_code}"
        if status == "ISSUED":
            break
        time.sleep(0.2)
    t.check(
        f"alice's voucher ISSUED within {args.persist_wait:.0f}s",
        status == "ISSUED",
        f"last status {status} — is the worker running?",
    )
    r = t.call("GET", f"/campaigns/{campaign}/my-voucher", token=carol)
    t.check("carol my-voucher → 404", r.status_code == 404, r.text)

    print("redeem")
    r = t.call("POST", f"/vouchers/{code}/redeem", token=bob)
    t.check("bob redeems alice's code → 404", r.status_code == 404, r.text)
    r = t.call("POST", f"/vouchers/{code}/redeem", token=alice)
    t.check(
        "alice redeems → REDEEMED",
        r.status_code == 200 and r.json().get("status") == "REDEEMED",
        r.text,
    )
    r = t.call("POST", f"/vouchers/{code}/redeem", token=alice)
    t.check("alice redeems again → 409", r.status_code == 409, r.text)
    r = t.call("POST", "/vouchers/not-a-code/redeem", token=alice)
    t.check("malformed code → 404", r.status_code == 404, r.text)

    print()
    if t.failures:
        print(f"\033[31mFAILED\033[0m  {t.failures} check(s)")
        return 1
    print("\033[32mPASSED\033[0m  all smoke checks")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--persist-wait", type=float, default=10, help="seconds to wait for worker")
    p.add_argument("--wait-ready", type=float, default=30, help="seconds to wait for /health")
    p.add_argument("--admin-token", help="real admin access token (Cognito)")
    p.add_argument("--user-tokens", default="", help="3 comma-separated user tokens (Cognito)")
    sys.exit(run(p.parse_args()))


if __name__ == "__main__":
    main()
