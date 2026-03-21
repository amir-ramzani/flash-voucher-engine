"""JWT verification.

- dev:     HS256 with a shared secret, so load tests can mint 50k tokens locally.
- cognito: RS256 Cognito *access* tokens; JWKS is fetched once and cached by PyJWKClient.
"""

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common.settings import Settings


@dataclass(frozen=True)
class Principal:
    user_id: str
    groups: frozenset[str]


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._jwks: jwt.PyJWKClient | None = None
        if settings.auth_mode == "cognito":
            if not (
                settings.cognito_region
                and settings.cognito_user_pool_id
                and settings.cognito_client_id
            ):
                raise RuntimeError(
                    "AUTH_MODE=cognito requires COGNITO_REGION/USER_POOL_ID/CLIENT_ID"
                )
            self._issuer = (
                f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
                f"{settings.cognito_user_pool_id}"
            )
            self._jwks = jwt.PyJWKClient(f"{self._issuer}/.well-known/jwks.json", cache_keys=True)

    def verify(self, token: str) -> Principal:
        if self._jwks is None:
            claims = jwt.decode(
                token,
                self.settings.dev_jwt_secret,
                algorithms=["HS256"],
                options={"require": ["sub", "exp"]},
            )
        else:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"require": ["sub", "exp", "iss"]},
            )
            if claims.get("token_use") != "access":
                raise jwt.InvalidTokenError("not an access token")
            if claims.get("client_id") != self.settings.cognito_client_id:
                raise jwt.InvalidTokenError("wrong client")
        return Principal(user_id=claims["sub"], groups=frozenset(claims.get("cognito:groups", [])))


_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return request.app.state.auth.verify(creds.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc


def require_admin(request: Request, user: Principal = Depends(current_user)) -> Principal:
    if request.app.state.settings.admin_group not in user.groups:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user
