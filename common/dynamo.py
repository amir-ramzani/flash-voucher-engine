"""DynamoDB access + table definitions (local bootstrap; AWS uses infra/stack.yaml)."""

import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

from common.settings import Settings

# Claim statuses in VoucherClaims
ISSUED = "ISSUED"
REDEEMED = "REDEEMED"
REVOKED = "REVOKED"  # the user was told WON but the durable cap was already reached


@dataclass(frozen=True)
class Dynamo:
    client: Any  # low-level client (transactions)
    campaigns: Any  # Table resource
    claims: Any  # Table resource


def _boto_kwargs(settings: Settings) -> dict:
    kwargs: dict = {"region_name": settings.aws_region, "endpoint_url": settings.dynamodb_endpoint}
    # Explicit keys only when configured (e.g. from .env for DynamoDB Local); otherwise
    # boto3's default chain (env, ECS task role, ...) applies.
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


def connect(settings: Settings) -> Dynamo:
    kwargs = _boto_kwargs(settings)
    resource = boto3.resource("dynamodb", **kwargs)
    return Dynamo(
        # NOT resource.meta.client: that one auto-serializes Python values, which would
        # double-wrap the explicit {"S": ...} attribute values used in transactions.
        client=boto3.client("dynamodb", **kwargs),
        campaigns=resource.Table(settings.campaigns_table),
        claims=resource.Table(settings.claims_table),
    )


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def is_conditional_failure(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and error_code(exc) == "ConditionalCheckFailedException"


def table_definitions(settings: Settings) -> list[dict]:
    return [
        {
            "TableName": settings.campaigns_table,
            "KeySchema": [{"AttributeName": "campaignId", "KeyType": "HASH"}],
            "AttributeDefinitions": [{"AttributeName": "campaignId", "AttributeType": "S"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
        {
            "TableName": settings.claims_table,
            "KeySchema": [
                {"AttributeName": "campaignId", "KeyType": "HASH"},
                {"AttributeName": "userId", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "campaignId", "AttributeType": "S"},
                {"AttributeName": "userId", "AttributeType": "S"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
        },
    ]


def ensure_tables(settings: Settings, wait_s: float = 30.0) -> None:
    """Create tables if missing. Retries while DynamoDB Local is still booting."""
    client = boto3.client("dynamodb", **_boto_kwargs(settings))
    deadline = time.monotonic() + wait_s
    while True:
        try:
            existing = set(client.list_tables()["TableNames"])
            break
        except EndpointConnectionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    for definition in table_definitions(settings):
        if definition["TableName"] in existing:
            continue
        client.create_table(**definition)
        client.get_waiter("table_exists").wait(TableName=definition["TableName"])
        print(f"created table {definition['TableName']}")


def query_all_claims(dynamo: Dynamo, campaign_id: str) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {
        "KeyConditionExpression": "campaignId = :c",
        "ExpressionAttributeValues": {":c": campaign_id},
        "ConsistentRead": True,
    }
    while True:
        page = dynamo.claims.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
