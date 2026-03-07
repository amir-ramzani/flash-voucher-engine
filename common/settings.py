from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Redis -------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    # After a WON, block until this many replicas acknowledged the write (0 = off).
    # Shrinks — but does not close — the window where a failover loses a claim.
    redis_wait_replicas: int = 0
    redis_wait_timeout_ms: int = 20

    # --- DynamoDB ----------------------------------------------------------
    aws_region: str = "ap-southeast-1"
    dynamodb_endpoint: str | None = None  # set for DynamoDB Local
    # Leave unset in AWS (task role). Set via .env for DynamoDB Local on the host.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    campaigns_table: str = "Campaigns"
    claims_table: str = "VoucherClaims"

    # --- Auth --------------------------------------------------------------
    # "dev": HS256 tokens signed with dev_jwt_secret (local + load tests only).
    # "cognito": RS256 access tokens verified against the user pool JWKS.
    auth_mode: Literal["dev", "cognito"] = "dev"
    dev_jwt_secret: str = "dev-secret-change-me-at-least-32-bytes!"
    cognito_region: str | None = None
    cognito_user_pool_id: str | None = None
    cognito_client_id: str | None = None
    admin_group: str = "admin"

    # --- API ---------------------------------------------------------------
    rate_limit_per_sec: int = 5

    # --- Worker ------------------------------------------------------------
    worker_batch_size: int = 100
    worker_block_ms: int = 1000
    worker_min_idle_ms: int = 30_000  # pending this long => assume consumer died, reclaim
    worker_max_deliveries: int = 5  # then dead-letter
    worker_refresh_s: float = 5.0

    # --- Reconciler --------------------------------------------------------
    reconciler_grace_s: int = 60  # ignore claims younger than this (worker may be catching up)

    # --- Metrics -----------------------------------------------------------
    metrics_emf: bool = False  # emit CloudWatch Embedded Metric Format to stdout
    metrics_namespace: str = "FlashVoucher"
    metrics_flush_s: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
