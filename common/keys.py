"""Redis key layout.

Every per-campaign key carries the hash tag ``{campaign:<id>}`` so all of them land in the
same cluster slot — a requirement for touching them from one Lua script.
"""

import re
from dataclasses import dataclass

CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ACTIVE_CAMPAIGNS = "campaigns:active"  # set of campaign ids the worker consumes
CONSUMER_GROUP = "persisters"
REVOKED = "REVOKED"  # claims-hash value for a claim the durable path refused (cap reached)


@dataclass(frozen=True)
class CampaignKeys:
    meta: str  # hash: totalStock, startAt, endAt (epoch ms)
    stock: str  # string int: remaining vouchers
    claims: str  # hash: userId -> voucherCode | REVOKED
    events: str  # stream: claim events for the worker
    dlq: str  # stream: dead-lettered events


def is_valid_campaign_id(campaign_id: str) -> bool:
    return bool(CAMPAIGN_ID_RE.match(campaign_id))


def campaign_keys(campaign_id: str) -> CampaignKeys:
    tag = f"{{campaign:{campaign_id}}}"
    return CampaignKeys(
        meta=f"{tag}:meta",
        stock=f"{tag}:stock",
        claims=f"{tag}:claims",
        events=f"{tag}:events",
        dlq=f"{tag}:dlq",
    )


def campaign_id_from_stream(stream_key: str) -> str:
    # "{campaign:abc}:events" -> "abc"
    return stream_key[len("{campaign:") : stream_key.index("}")]


def ratelimit_key(user_id: str) -> str:
    return f"ratelimit:{user_id}"
