"""Persist one claim event to DynamoDB — the durable path's correctness core.

Each claim is written in a transaction with a guarded increment of the campaign's
``issued`` counter:

    Update Campaigns   SET issued = issued + 1   IF issued < totalStock
    Put    Claims      {status: ISSUED}          IF attribute_not_exists(userId)

So DynamoDB can never hold more than ``totalStock`` issued vouchers, even if a Redis
failover lost writes and let Redis hand out extra wins. Such extra winners get a
``REVOKED`` row (and Redis is told), which makes the failure visible and explainable.
"""

import hashlib
import logging
from enum import StrEnum

from botocore.exceptions import ClientError

from common import dynamo as ddb
from common.keys import REVOKED, campaign_keys

log = logging.getLogger("worker.persister")


class Outcome(StrEnum):
    ISSUED = "ISSUED"
    DUPLICATE = "DUPLICATE"  # already persisted earlier (redelivery / retry) — no-op
    REVOKED = "REVOKED"  # durable cap reached: Redis over-issued


class PoisonEvent(ValueError):
    """Event can never succeed (malformed). Dead-letter immediately."""


def _token(campaign_id: str, user_id: str) -> str:
    # Idempotency token: identical retries within 10 min return the original result.
    return hashlib.sha256(f"{campaign_id}:{user_id}".encode()).hexdigest()[:36]


class Persister:
    def __init__(self, dynamo: ddb.Dynamo, redis_client, campaigns_table: str, claims_table: str):
        self.dynamo = dynamo
        self.redis = redis_client
        self.campaigns_table = campaigns_table
        self.claims_table = claims_table

    def persist(self, campaign_id: str, fields: dict[str, str]) -> Outcome:
        try:
            user, code, ts = fields["user"], fields["code"], int(fields["ts"])
        except (KeyError, ValueError) as exc:
            raise PoisonEvent(f"malformed event {fields!r}") from exc

        claim_item = {
            "campaignId": {"S": campaign_id},
            "userId": {"S": user},
            "voucherCode": {"S": code},
            "status": {"S": ddb.ISSUED},
            "claimedAt": {"N": str(ts)},
        }
        try:
            self.dynamo.client.transact_write_items(
                ClientRequestToken=_token(campaign_id, user),
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.campaigns_table,
                            "Key": {"campaignId": {"S": campaign_id}},
                            "UpdateExpression": "SET issued = issued + :one",
                            "ConditionExpression": "issued < totalStock",
                            "ExpressionAttributeValues": {":one": {"N": "1"}},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.claims_table,
                            "Item": claim_item,
                            "ConditionExpression": "attribute_not_exists(userId)",
                        }
                    },
                ],
            )
            return Outcome.ISSUED
        except ClientError as exc:
            if ddb.error_code(exc) != "TransactionCanceledException":
                raise
            reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]

        if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
            return Outcome.DUPLICATE
        if reasons and reasons[0] == "ConditionalCheckFailed":
            return self._revoke(campaign_id, user, code, ts)
        # e.g. TransactionConflict / throttling: raise so the event stays pending and retries.
        raise RuntimeError(f"transaction cancelled: {reasons}")

    def _revoke(self, campaign_id: str, user: str, code: str, ts: int) -> Outcome:
        log.error("durable cap reached for %s; revoking claim of %s", campaign_id, user)
        try:
            self.dynamo.claims.put_item(
                Item={
                    "campaignId": campaign_id,
                    "userId": user,
                    "voucherCode": code,
                    "status": ddb.REVOKED,
                    "claimedAt": ts,
                },
                ConditionExpression="attribute_not_exists(userId)",
            )
        except Exception as exc:
            if not ddb.is_conditional_failure(exc):
                raise
            return Outcome.DUPLICATE  # someone already wrote the row (issued or revoked)
        self.redis.hset(campaign_keys(campaign_id).claims, user, REVOKED)
        return Outcome.REVOKED
