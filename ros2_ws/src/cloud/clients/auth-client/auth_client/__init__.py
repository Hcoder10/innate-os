# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Innate auth client: OIDC JWT acquisition and timed renewal for robots.

Usage::

    from auth_client import AuthProvider, AuthError

    auth = AuthProvider(
        issuer_url="https://auth-v1.svc.innate.bot",
        service_key="sk_...",
    )
    token = auth.token          # lazily discovers OIDC + fetches JWT
    print(auth.expires_at)      # when the JWT expires
"""

from auth_client.httpx_auth import InnateBearerAuth
from auth_client.provider import AuthError, AuthProvider

__all__: list[str] = ["AuthProvider", "AuthError", "InnateBearerAuth"]
