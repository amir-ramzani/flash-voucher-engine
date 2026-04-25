"""Latency/throughput load test. Each simulated user taps "claim" once.

Create the campaign first (`python -m loadtest.create_campaign my-sale --stock 1000`), then:

    CAMPAIGN_ID=my-sale locust -f loadtest/locustfile.py --host http://localhost:8000 \
        --users 50000 --spawn-rate 5000 --headless -t 60s

50k users from one machine needs distributed mode (one process per core):

    locust -f loadtest/locustfile.py --master ...
    locust -f loadtest/locustfile.py --worker   # x N

Requires AUTH_MODE=dev on the target (tokens are minted locally with DEV_JWT_SECRET).
"""

import itertools
import os
import uuid

from locust import FastHttpUser, constant, task

from common.devtokens import mint

CAMPAIGN_ID = os.environ.get("CAMPAIGN_ID", "demo")
SECRET = os.environ.get("DEV_JWT_SECRET", "dev-secret-change-me-at-least-32-bytes!")
_RUN = uuid.uuid4().hex[:6]  # unique per locust process so workers don't share user ids
_ids = itertools.count()

OK = {"WON", "ALREADY", "SOLD_OUT"}


class Claimer(FastHttpUser):
    wait_time = constant(3600)  # one tap per user, then idle

    def on_start(self):
        self.user_id = f"lc-{_RUN}-{next(_ids)}"
        self.headers = {"Authorization": f"Bearer {mint(self.user_id, SECRET)}"}

    @task
    def claim(self):
        with self.client.post(
            f"/campaigns/{CAMPAIGN_ID}/claim",
            headers=self.headers,
            name="POST /campaigns/:id/claim",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200 and resp.json().get("result") in OK:
                resp.success()
            else:
                resp.failure(f"{resp.status_code} {resp.text[:100]}")
