from __future__ import annotations

import threading
import time
from typing import Any

import jwt
from jwt import PyJWKClient

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = f"{GITHUB_OIDC_ISSUER}/.well-known/jwks"
GITHUB_OIDC_AUDIENCE = "rngn-reels-wc-worker"
GITHUB_REPOSITORY = "znamteam-max/rngn-reels-wc-bot"
GITHUB_REPOSITORY_OWNER = "znamteam-max"
GITHUB_REF = "refs/heads/main"
GITHUB_EVENTS = {"schedule", "workflow_dispatch"}
JWKS_TTL_SECONDS = 300


class GitHubOIDCError(ValueError):
    pass


_JWKS_CLIENT: PyJWKClient | None = None
_JWKS_CLIENT_CREATED_AT = 0.0
_JWKS_LOCK = threading.Lock()


def _get_jwks_client() -> PyJWKClient:
    global _JWKS_CLIENT, _JWKS_CLIENT_CREATED_AT
    now = time.monotonic()
    if _JWKS_CLIENT is not None and now - _JWKS_CLIENT_CREATED_AT < JWKS_TTL_SECONDS:
        return _JWKS_CLIENT
    with _JWKS_LOCK:
        now = time.monotonic()
        if _JWKS_CLIENT is None or now - _JWKS_CLIENT_CREATED_AT >= JWKS_TTL_SECONDS:
            _JWKS_CLIENT = PyJWKClient(
                GITHUB_OIDC_JWKS_URL,
                cache_keys=True,
                cache_jwk_set=True,
                lifespan=JWKS_TTL_SECONDS,
                timeout=3,
            )
            _JWKS_CLIENT_CREATED_AT = now
    return _JWKS_CLIENT


def validate_github_oidc_token(token: str) -> dict[str, Any]:
    if not token or token.count(".") != 2:
        raise GitHubOIDCError("invalid GitHub OIDC token")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise GitHubOIDCError("unsupported GitHub OIDC token")
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=GITHUB_OIDC_AUDIENCE,
            issuer=GITHUB_OIDC_ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "repository",
                    "repository_owner",
                    "ref",
                    "event_name",
                ]
            },
        )
    except GitHubOIDCError:
        raise
    except jwt.PyJWTError as exc:
        raise GitHubOIDCError("GitHub OIDC validation failed") from exc
    except Exception as exc:
        raise GitHubOIDCError("GitHub OIDC key validation failed") from exc

    expected = {
        "aud": GITHUB_OIDC_AUDIENCE,
        "repository": GITHUB_REPOSITORY,
        "repository_owner": GITHUB_REPOSITORY_OWNER,
        "ref": GITHUB_REF,
    }
    if any(claims.get(name) != value for name, value in expected.items()):
        raise GitHubOIDCError("GitHub OIDC claims are not allowed")
    if claims.get("event_name") not in GITHUB_EVENTS:
        raise GitHubOIDCError("GitHub OIDC event is not allowed")
    return claims
