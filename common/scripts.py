"""Loads the Lua scripts and binds them to a redis client (sync or asyncio).

redis-py's ``register_script`` calls EVALSHA and transparently falls back to EVAL (which
caches the script) on NOSCRIPT, e.g. after a failover to a fresh replica.
"""

from dataclasses import dataclass
from importlib.resources import files
from typing import Any

_LUA = files("common") / "lua"


def _load(name: str) -> str:
    return (_LUA / f"{name}.lua").read_text()


CLAIM_LUA = _load("claim")
SEED_LUA = _load("seed")
RATELIMIT_LUA = _load("ratelimit")
RECONCILE_STOCK_LUA = _load("reconcile_stock")


@dataclass(frozen=True)
class Scripts:
    claim: Any
    seed: Any
    ratelimit: Any
    reconcile_stock: Any


def register(client) -> Scripts:
    return Scripts(
        claim=client.register_script(CLAIM_LUA),
        seed=client.register_script(SEED_LUA),
        ratelimit=client.register_script(RATELIMIT_LUA),
        reconcile_stock=client.register_script(RECONCILE_STOCK_LUA),
    )
