"""Mint HS256 dev tokens (AUTH_MODE=dev). Used by tests and load tests — never in AWS.

python -m common.devtokens user-123 [--admin]
"""

import argparse
import time

import jwt

from common.settings import get_settings


def mint(user_id: str, secret: str, groups: tuple[str, ...] = (), ttl_s: int = 3600) -> str:
    now = int(time.time())
    claims = {"sub": user_id, "iat": now, "exp": now + ttl_s, "cognito:groups": list(groups)}
    return jwt.encode(claims, secret, algorithm="HS256")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    s = get_settings()
    print(mint(args.user_id, s.dev_jwt_secret, (s.admin_group,) if args.admin else ()))
