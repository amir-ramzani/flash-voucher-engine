"""Voucher codes: ``<campaignId>.<10 random chars>``.

Embedding the campaign id lets ``/vouchers/{code}/redeem`` find the row by its primary key
(campaignId + caller's userId) with a strongly consistent read — no GSI needed — and the
userId coming from the JWT doubles as the ownership check.
"""

import secrets

from common.keys import is_valid_campaign_id

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
TOKEN_LEN = 10


def new_code(campaign_id: str) -> str:
    token = "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LEN))
    return f"{campaign_id}.{token}"


def parse_code(code: str) -> str:
    """Return the campaign id of a well-formed code, else raise ValueError."""
    campaign_id, sep, token = code.rpartition(".")
    if (
        not sep
        or not is_valid_campaign_id(campaign_id)
        or len(token) != TOKEN_LEN
        or any(c not in ALPHABET for c in token)
    ):
        raise ValueError("malformed voucher code")
    return campaign_id
