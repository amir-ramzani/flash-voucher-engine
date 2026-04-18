"""Create (or idempotently re-seed) a campaign via the API using a dev admin token.

python -m loadtest.create_campaign my-sale --stock 1000 [--starts-in 0] [--minutes 30]
"""

import argparse
from datetime import UTC, datetime, timedelta

import httpx

from common.devtokens import mint
from common.settings import get_settings


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("campaign_id")
    p.add_argument("--stock", type=int, default=1000)
    p.add_argument("--starts-in", type=float, default=0, help="seconds from now")
    p.add_argument("--minutes", type=float, default=30, help="window length")
    p.add_argument("--base-url", default="http://localhost:8000")
    a = p.parse_args()
    s = get_settings()
    start = datetime.now(UTC) + timedelta(seconds=a.starts_in)
    resp = httpx.post(
        f"{a.base_url}/campaigns",
        headers={"Authorization": f"Bearer {mint('admin', s.dev_jwt_secret, (s.admin_group,))}"},
        json={
            "campaignId": a.campaign_id,
            "totalStock": a.stock,
            "startAt": start.isoformat(),
            "endAt": (start + timedelta(minutes=a.minutes)).isoformat(),
        },
    )
    print(resp.status_code, resp.text)
    resp.raise_for_status()


if __name__ == "__main__":
    main()
