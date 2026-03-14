"""Test fixtures: in-process fakeredis (with real Lua via lupa) + moto DynamoDB.

No Docker needed. The compose stack + loadtest/ cover the real Redis / DynamoDB Local.
"""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from common import dynamo as ddb
from common.devtokens import mint
from common.scripts import register
from common.settings import Settings

os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        redis_url="redis://unused",
        dynamodb_endpoint=None,
        aws_region="ap-southeast-1",
        auth_mode="dev",
        rate_limit_per_sec=1000,
        worker_min_idle_ms=0,
        worker_block_ms=10,
        reconciler_grace_s=0,
    )


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


@pytest.fixture
def r(redis_server):
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def ar(redis_server):
    return fakeredis.aioredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def scripts(r):
    return register(r)


@pytest.fixture
def dynamo(settings):
    with mock_aws():
        ddb.ensure_tables(settings)
        yield ddb.connect(settings)


@pytest.fixture
def campaign_id() -> str:
    return f"c-{uuid.uuid4().hex[:8]}"


def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def make_campaign(dynamo, scripts, r):
    """Create a campaign directly (DynamoDB row + Redis seed), bypassing the API."""
    from common.keys import ACTIVE_CAMPAIGNS, campaign_keys

    def _make(cid: str, stock: int, start_ms: int | None = None, end_ms: int | None = None):
        start_ms = start_ms if start_ms is not None else now_ms() - 1000
        end_ms = end_ms if end_ms is not None else now_ms() + 600_000
        dynamo.campaigns.put_item(
            Item={
                "campaignId": cid,
                "totalStock": stock,
                "startAtMs": start_ms,
                "endAtMs": end_ms,
                "issued": 0,
                "status": "ACTIVE",
            }
        )
        k = campaign_keys(cid)
        scripts.seed(keys=[k.meta, k.stock], args=[stock, start_ms, end_ms])
        r.sadd(ACTIVE_CAMPAIGNS, cid)
        return k

    return _make


@pytest.fixture
def client(settings, ar, dynamo):
    from api.app import create_app

    with TestClient(create_app(settings, redis_client=ar, dynamo=dynamo)) as c:
        yield c


@pytest.fixture
def auth(settings):
    def _auth(user: str, admin: bool = False) -> dict:
        groups = (settings.admin_group,) if admin else ()
        return {"Authorization": f"Bearer {mint(user, settings.dev_jwt_secret, groups)}"}

    return _auth


def window(start_offset_s: float = -1, minutes: float = 10) -> dict:
    start = datetime.now(UTC) + timedelta(seconds=start_offset_s)
    return {"startAt": start.isoformat(), "endAt": (start + timedelta(minutes=minutes)).isoformat()}
