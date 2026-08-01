"""Device authentication — mint and cache the device's cloud JWT.

The Simplit token endpoint is NOT OAuth2: it takes a JSON ``{clientId, clientSecret}`` and returns
``{"accessToken": "<RS256 JWT>"}`` (camelCase). The JWT's ``sub`` is the deviceId and ``agencyId`` is the org —
that identity is what the gateway reads at registration and what the presence relay routes pushes to. We cache
the token until ~60s before its ``exp`` claim. Mirrors the board's ServiceTokenProvider.
"""
from __future__ import annotations

import base64
import json
import threading
import time

import requests


class TokenError(RuntimeError):
    pass


class TokenProvider:
    def __init__(self, token_url: str, client_id: str, client_secret: str, timeout: float = 30.0):
        self._url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: str | None = None
        self._exp_ms: int = 0

    def current(self) -> str:
        with self._lock:
            now = int(time.time() * 1000)
            if self._token and now < self._exp_ms - 60_000:
                return self._token
            resp = requests.post(
                self._url,
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code // 100 != 2:
                raise TokenError(f"token endpoint {self._url} -> HTTP {resp.status_code}: {resp.text[:200]}")
            tok = resp.json().get("accessToken")
            if not tok:
                raise TokenError(f"token response had no accessToken: {resp.text[:200]}")
            self._token = tok
            self._exp_ms = _jwt_exp_ms(tok)
            return tok


def _jwt_exp_ms(jwt: str) -> int:
    """Read the ``exp`` claim (seconds) from an unverified JWT and return it in ms; default +50min."""
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(claims["exp"]) * 1000
    except Exception:
        return int(time.time() * 1000) + 50 * 60 * 1000
