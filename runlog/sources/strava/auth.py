"""Strava OAuth2: authorize URL, code exchange, and token refresh.

The token-exchange functions are pure request/response wrappers so they can be
driven from either the interactive ``runlog strava auth`` flow or a stored
refresh token. See https://developers.strava.com/docs/authentication/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

if TYPE_CHECKING:
    from runlog.config import StravaCredentials

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEFAULT_SCOPE = "activity:read_all"


@dataclass(frozen=True)
class TokenResponse:
    """Access/refresh token pair returned by the token endpoint."""

    access_token: str
    refresh_token: str
    expires_at: int


def authorize_url(client_id: str, redirect_uri: str, scope: str = DEFAULT_SCOPE) -> str:
    """Build the URL the user visits to grant access."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": scope,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _post_token(payload: dict[str, str]) -> TokenResponse:
    response = requests.post(TOKEN_URL, data=payload, timeout=30)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return TokenResponse(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=int(body["expires_at"]),
    )


def exchange_code(creds: StravaCredentials, code: str) -> TokenResponse:
    """Exchange an authorization code for tokens (initial one-time auth)."""
    return _post_token(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    )


def refresh_access_token(creds: StravaCredentials) -> TokenResponse:
    """Exchange the stored refresh token for a fresh access token."""
    if not creds.refresh_token:
        raise RuntimeError(
            "No STRAVA_REFRESH_TOKEN set. Run `runlog strava auth` first."
        )
    return _post_token(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        }
    )
