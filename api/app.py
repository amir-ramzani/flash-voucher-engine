"""FastAPI service: the fast path.

Hot path (claim) touches only Redis: one rate-limit script + one claim script.
DynamoDB is only read/written by the admin, my-voucher and redeem endpoints.
"""

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Literal

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from api.auth import Authenticator, Principal, current_user, require_admin
from common import dynamo as ddb
from common.codes import new_code, parse_code
from common.keys import (
    ACTIVE_CAMPAIGNS,
    CAMPAIGN_ID_RE,
    REVOKED,
    campaign_keys,
    is_valid_campaign_id,
    ratelimit_key,
)
from common.metrics import Metrics
from common.scripts import register
from common.settings import Settings, get_settings

log = logging.getLogger("api")


API_VERSION = "1.0.0"

API_DESCRIPTION = """Flash-sale voucher engine: many users race to claim a fixed number of vouchers.

**Guarantees**
* At most one voucher per user per campaign; duplicate taps return the same code.
* Exactly `totalStock` `WON` responses while Redis is healthy; the durable store
  (DynamoDB) never holds more than `totalStock` issued vouchers.

**Authentication** — every endpoint except `/health` needs `Authorization: Bearer <JWT>`.
The user id is the token's `sub`. Admin endpoints need `admin` in `cognito:groups`.
Locally (`AUTH_MODE=dev`) mint tokens with `python -m common.devtokens <user> [--admin]`.

**Voucher lifecycle**: `PENDING` (claimed, persisting) → `ISSUED` → `REDEEMED`.
`REVOKED` = the claim was refused by the durable cap (only after a Redis failover).
"""

TAGS = [
    {"name": "campaigns", "description": "Admin: create and seed campaigns."},
    {"name": "claims", "description": "The hot path: claim a voucher, look up your voucher."},
    {"name": "vouchers", "description": "Redeem a voucher at checkout."},
    {"name": "ops", "description": "Health checks."},
]


# --------------------------------------------------------------------------- models
class ErrorOut(BaseModel):
    detail: str = Field(examples=["unknown campaign"])


class HealthOut(BaseModel):
    ok: bool


class CampaignIn(BaseModel):
    campaignId: str = Field(
        pattern=CAMPAIGN_ID_RE.pattern,
        description="Lowercase letters, digits and dashes; 1–40 chars; no dots.",
        examples=["sale-1111"],
    )
    totalStock: int = Field(gt=0, le=1_000_000, description="Number of vouchers.")
    startAt: AwareDatetime = Field(
        description="Claim window start (timezone required).", examples=["2026-06-06T12:00:00Z"]
    )
    endAt: AwareDatetime = Field(
        description="Claim window end, exclusive.", examples=["2026-06-06T12:30:00Z"]
    )

    @model_validator(mode="after")
    def _window(self):
        if self.endAt <= self.startAt:
            raise ValueError("endAt must be after startAt")
        return self


class CampaignOut(BaseModel):
    campaignId: str
    totalStock: int
    startAt: datetime
    endAt: datetime
    remainingStock: int = Field(description="Live stock in Redis at the time of the call.")


ClaimResult = Literal["WON", "ALREADY", "SOLD_OUT", "REVOKED"]
VoucherStatus = Literal["PENDING", "ISSUED", "REDEEMED", "REVOKED"]


class ClaimOut(BaseModel):
    result: ClaimResult = Field(
        description=(
            "`WON`: you got a voucher. `ALREADY`: you claimed before — same code returned. "
            "`SOLD_OUT`: no stock left. `REVOKED`: your earlier win was refused by the "
            "durable cap."
        )
    )
    voucherCode: str | None = Field(
        default=None,
        description="Present for WON and ALREADY. Format `<campaignId>.<10 chars>`.",
        examples=["sale-1111.K7QZ3MWD9P"],
    )


class VoucherOut(BaseModel):
    campaignId: str
    voucherCode: str | None
    status: VoucherStatus = Field(
        description="`PENDING` = claimed in Redis, not yet persisted (usually a few ms)."
    )
    claimedAt: int | None = Field(default=None, description="Epoch milliseconds.")
    redeemedAt: int | None = Field(default=None, description="Epoch milliseconds.")


def _err(description: str) -> dict:
    return {"model": ErrorOut, "description": description}


AUTH_ERRORS = {401: _err("Missing or invalid bearer token")}


# --------------------------------------------------------------------------- helpers
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _int(v) -> int | None:
    return int(v) if isinstance(v, (int, Decimal)) else None


def _voucher_from_item(item: dict) -> VoucherOut:
    return VoucherOut(
        campaignId=item["campaignId"],
        voucherCode=item.get("voucherCode"),
        status=item["status"],
        claimedAt=_int(item.get("claimedAt")),
        redeemedAt=_int(item.get("redeemedAt")),
    )


def _check_campaign_id(campaign_id: str) -> None:
    if not is_valid_campaign_id(campaign_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown campaign")


# --------------------------------------------------------------------------- app
def create_app(
    settings: Settings | None = None,
    redis_client: aioredis.Redis | None = None,
    dynamo: ddb.Dynamo | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = redis_client or aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.settings = settings
        app.state.redis = r
        app.state.scripts = register(r)
        app.state.dynamo = dynamo or ddb.connect(settings)
        app.state.auth = Authenticator(settings)
        app.state.metrics = Metrics(settings.metrics_namespace, "api", settings.metrics_emf)

        async def flusher():
            while True:
                await asyncio.sleep(settings.metrics_flush_s)
                app.state.metrics.flush()

        task = asyncio.create_task(flusher())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            app.state.metrics.flush()
            if redis_client is None:
                await r.aclose()

    app = FastAPI(
        title="Flash-Sale Voucher Engine",
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=TAGS,
        # operationId = handler name (health, claim, redeem, ...) for clean client codegen.
        generate_unique_id_function=lambda route: route.name,
        lifespan=lifespan,
    )

    @app.get(
        "/health",
        tags=["ops"],
        summary="Health check (API up + Redis reachable)",
        description=(
            "Unauthenticated. Returns 200 when the API process is up **and** can reach Redis "
            "(the claim path depends on it), 503 otherwise. Used by the ALB target group and "
            "the docker-compose healthcheck. DynamoDB is deliberately not checked: claims "
            "keep working while it is degraded (the worker retries)."
        ),
        response_model=HealthOut,
        responses={
            200: {
                "description": "Healthy",
                "content": {"application/json": {"example": {"ok": True}}},
            },
            503: {
                "model": ErrorOut,
                "description": "Redis unreachable",
                "content": {"application/json": {"example": {"detail": "redis unreachable"}}},
            },
        },
    )
    async def health(request: Request):
        try:
            await request.app.state.redis.ping()
        except Exception as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "redis unreachable") from exc
        return HealthOut(ok=True)

    # ---------------------------------------------------------------- admin
    @app.post(
        "/campaigns",
        tags=["campaigns"],
        summary="Create a campaign (admin)",
        description=(
            "Writes the campaign to DynamoDB, then seeds Redis. Re-posting **identical** "
            "parameters is idempotent (returns 200 and re-seeds Redis without refilling "
            "stock); different parameters for an existing id return 409."
        ),
        response_model=CampaignOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": CampaignOut, "description": "Already existed with identical params"},
            **AUTH_ERRORS,
            403: _err("Caller is not in the admin group"),
            409: _err("Campaign exists with different parameters"),
        },
    )
    async def create_campaign(
        body: CampaignIn,
        request: Request,
        response: Response,
        _admin: Principal = Depends(require_admin),
    ):
        st = request.app.state
        start_ms, end_ms = _ms(body.startAt), _ms(body.endAt)
        item = {
            "campaignId": body.campaignId,
            "totalStock": body.totalStock,
            "startAt": body.startAt.isoformat(),
            "endAt": body.endAt.isoformat(),
            "startAtMs": start_ms,
            "endAtMs": end_ms,
            "issued": 0,  # durable issuance counter, guarded by the worker's transaction
            "status": "ACTIVE",
            "createdAt": _now_ms(),
        }
        # 1. Source of truth first. Re-POSTing identical params is idempotent (repairs Redis).
        try:
            await asyncio.to_thread(
                st.dynamo.campaigns.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(campaignId)",
            )
        except Exception as exc:
            if not ddb.is_conditional_failure(exc):
                raise
            existing = (
                await asyncio.to_thread(
                    st.dynamo.campaigns.get_item,
                    Key={"campaignId": body.campaignId},
                    ConsistentRead=True,
                )
            ).get("Item", {})
            same = (
                int(existing.get("totalStock", -1)) == body.totalStock
                and int(existing.get("startAtMs", -1)) == start_ms
                and int(existing.get("endAtMs", -1)) == end_ms
            )
            if not same:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "campaign exists with different parameters"
                ) from exc
            response.status_code = status.HTTP_200_OK

        # 2. Seed Redis (stock set with NX — never refilled by a retry).
        k = campaign_keys(body.campaignId)
        remaining = await st.scripts.seed(
            keys=[k.meta, k.stock], args=[body.totalStock, start_ms, end_ms]
        )
        await st.redis.sadd(ACTIVE_CAMPAIGNS, body.campaignId)
        return CampaignOut(
            campaignId=body.campaignId,
            totalStock=body.totalStock,
            startAt=body.startAt,
            endAt=body.endAt,
            remainingStock=int(remaining),
        )

    # ---------------------------------------------------------------- claim (hot path)
    @app.post(
        "/campaigns/{campaign_id}/claim",
        tags=["claims"],
        summary="Claim a voucher",
        description=(
            "Atomic check-then-claim in Redis. Safe to retry: a returning winner always gets "
            "`ALREADY` with their original code, even after the window closes. "
            "Sold-out is a normal **200** outcome, not an error."
        ),
        response_model=ClaimOut,
        responses={
            **AUTH_ERRORS,
            404: _err("Unknown campaign"),
            409: _err("`NOT_STARTED` or `ENDED` — outside the claim window"),
            429: _err("Per-user rate limit exceeded"),
        },
    )
    async def claim(campaign_id: str, request: Request, user: Principal = Depends(current_user)):
        st = request.app.state
        t0 = time.perf_counter()
        _check_campaign_id(campaign_id)

        hits = await st.scripts.ratelimit(keys=[ratelimit_key(user.user_id)], args=[1000])
        if int(hits) > st.settings.rate_limit_per_sec:
            st.metrics.incr("ClaimRateLimited")
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down")

        k = campaign_keys(campaign_id)
        result, value = await st.scripts.claim(
            keys=[k.meta, k.stock, k.claims, k.events],
            args=[user.user_id, new_code(campaign_id)],
        )

        if result == "WON" and st.settings.redis_wait_replicas > 0:
            acked = await st.redis.execute_command(
                "WAIT", st.settings.redis_wait_replicas, st.settings.redis_wait_timeout_ms
            )
            if int(acked) < st.settings.redis_wait_replicas:
                st.metrics.incr("ClaimReplicationLagging")
                log.warning("WON for %s not yet replicated (acked=%s)", user.user_id, acked)

        st.metrics.timing_ms("ClaimLatency", (time.perf_counter() - t0) * 1000)
        st.metrics.incr(f"Claim{result.title().replace('_', '')}")

        match result:
            case "WON":
                return ClaimOut(result="WON", voucherCode=value)
            case "ALREADY" if value == REVOKED:
                return ClaimOut(result="REVOKED")
            case "ALREADY":
                return ClaimOut(result="ALREADY", voucherCode=value)
            case "SOLD_OUT":
                return ClaimOut(result="SOLD_OUT")
            case "NOT_STARTED" | "ENDED":
                raise HTTPException(status.HTTP_409_CONFLICT, result)
            case _:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown campaign")

    # ---------------------------------------------------------------- my voucher
    @app.get(
        "/campaigns/{campaign_id}/my-voucher",
        tags=["claims"],
        summary="Get my voucher for a campaign",
        description=(
            "Reads DynamoDB (strongly consistent); falls back to Redis and reports `PENDING` "
            "if the worker hasn't persisted the claim yet."
        ),
        response_model=VoucherOut,
        responses={**AUTH_ERRORS, 404: _err("You have no voucher in this campaign")},
    )
    async def my_voucher(
        campaign_id: str, request: Request, user: Principal = Depends(current_user)
    ):
        st = request.app.state
        _check_campaign_id(campaign_id)
        resp = await asyncio.to_thread(
            st.dynamo.claims.get_item,
            Key={"campaignId": campaign_id, "userId": user.user_id},
            ConsistentRead=True,
        )
        if item := resp.get("Item"):
            return _voucher_from_item(item)
        # Not persisted yet — the worker may be a few ms behind. Redis knows.
        code = await st.redis.hget(campaign_keys(campaign_id).claims, user.user_id)
        if code is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no voucher")
        if code == REVOKED:
            return VoucherOut(campaignId=campaign_id, voucherCode=None, status="REVOKED")
        return VoucherOut(campaignId=campaign_id, voucherCode=code, status="PENDING")

    # ---------------------------------------------------------------- redeem
    @app.post(
        "/vouchers/{code}/redeem",
        tags=["vouchers"],
        summary="Redeem my voucher",
        description=(
            "Conditional update `ISSUED → REDEEMED`. Only the owner (token `sub`) can redeem. "
            "While the voucher is still `PENDING` this returns 409 with `Retry-After: 1`."
        ),
        response_model=VoucherOut,
        responses={
            **AUTH_ERRORS,
            404: _err("Malformed code, unknown voucher, or not yours"),
            409: _err("Already redeemed, revoked, or still being issued (retry)"),
        },
    )
    async def redeem(code: str, request: Request, user: Principal = Depends(current_user)):
        st = request.app.state
        try:
            campaign_id = parse_code(code)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown voucher") from exc
        key = {"campaignId": campaign_id, "userId": user.user_id}
        try:
            resp = await asyncio.to_thread(
                st.dynamo.claims.update_item,
                Key=key,
                UpdateExpression="SET #s = :redeemed, redeemedAt = :now",
                ConditionExpression=(
                    "attribute_exists(userId) AND voucherCode = :code AND #s = :issued"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":redeemed": ddb.REDEEMED,
                    ":issued": ddb.ISSUED,
                    ":code": code,
                    ":now": _now_ms(),
                },
                ReturnValues="ALL_NEW",
            )
            return _voucher_from_item(resp["Attributes"])
        except Exception as exc:
            if not ddb.is_conditional_failure(exc):
                raise
            failure = exc

        # Explain the failure. Ownership is implicit: the key uses the caller's userId.
        item = (
            await asyncio.to_thread(st.dynamo.claims.get_item, Key=key, ConsistentRead=True)
        ).get("Item")
        if item is None:
            pending = await st.redis.hget(campaign_keys(campaign_id).claims, user.user_id)
            if pending == code:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "voucher is still being issued, retry shortly",
                    headers={"Retry-After": "1"},
                ) from failure
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown voucher") from failure
        if item.get("voucherCode") != code:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown voucher") from failure
        raise HTTPException(status.HTTP_409_CONFLICT, f"voucher is {item['status']}") from failure

    return app


app = create_app()
