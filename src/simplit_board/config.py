"""Runtime configuration — cloud endpoints, state dir, and the trusted control signing key.

Everything is environment-overridable so the same tool image runs against a dev cloud, a test container, or a
real appliance without a rebuild. Defaults point at the live SimplitSecurity cloud.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOMAIN = "orangemushroom-5c8222a5.brazilsouth.azurecontainerapps.io"


@dataclass
class Config:
    domain: str
    auth_url: str
    gateway_url: str
    # Where the board fetches its own automation from, and what it delivers finished reports through. The board
    # defaults both to localhost, which on an appliance means "nothing scheduled ever runs", so the agent has to
    # name them explicitly at launch.
    flows_url: str
    integrations_url: str
    control_url: str
    enrollment_url: str
    delivery_url: str
    presence_ws_url: str
    state_dir: Path
    trusted_control_pubkey: str  # base64url Ed25519 — verifies control's signed pushes
    trusted_delivery_pubkey: str  # base64 Ed25519 — verifies the delivery service's streamed artifacts
    org: str

    # ── derived endpoints (match the live cloud contract) ──
    @property
    def token_url(self) -> str:
        # NOT OAuth2: POST application/json {clientId, clientSecret} -> {"accessToken": "<RS256 JWT>"}
        return f"{self.auth_url}/api/v1/auth/token"

    @property
    def login_url(self) -> str:
        # operator sign-in — proves who is enrolling; the returned token authorizes the enrollment
        return f"{self.auth_url}/api/v1/auth/login"

    @property
    def mfa_verify_url(self) -> str:
        # redeem a login 2FA challenge with the 6-digit code -> full session token
        return f"{self.auth_url}/api/v1/auth/mfa/verify"

    @property
    def mfa_setup_url(self) -> str:
        # begin first-time TOTP enrollment (forced-login flow) -> {secret, qrDataUri}
        return f"{self.auth_url}/api/v1/auth/mfa/setup"

    @property
    def mfa_enroll_url(self) -> str:
        # confirm the first code and activate 2FA -> full session token
        return f"{self.auth_url}/api/v1/auth/mfa/enroll"

    @property
    def enroll_url(self) -> str:
        # the dedicated enrollment microservice — engine-gated (operator must hold enrollDevice); mints the
        # device credential + creates its resource under the org. Returns the device's client-credentials.
        return f"{self.enrollment_url}/api/enroll"

    @property
    def targets_url(self) -> str:
        # placement options for this board — the org's subdivisions. Enrollment pulls them from tenancy (S2S)
        # and returns them here, so the operator can choose WHERE the board's resource is created.
        return f"{self.enrollment_url}/api/enroll/targets"

    @property
    def register_url(self) -> str:
        # POST {} with Bearer <device JWT>; deviceId=token.sub, org=token.agencyId. Idempotent.
        return f"{self.gateway_url}/api/register"

    @property
    def devices_url(self) -> str:
        return f"{self.gateway_url}/api/data/devices"

    @property
    def presence_ws(self) -> str:
        # token goes in the QUERY string, not a header
        return f"{self.presence_ws_url}/ws"

    @property
    def key_path(self) -> Path:
        return self.state_dir / "device.key"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "device.json"

    @property
    def jar_dir(self) -> Path:
        return self.state_dir / "service"


def load_config() -> Config:
    dom = os.environ.get("SIMPLIT_DOMAIN", DEFAULT_DOMAIN)
    def svc(name: str, scheme: str = "https") -> str:
        return f"{scheme}://simplit-{name}.{dom}"
    return Config(
        domain=dom,
        auth_url=os.environ.get("SIMPLIT_AUTH_URL", svc("auth")),
        gateway_url=os.environ.get("SIMPLIT_GATEWAY_URL", svc("cloud-gateway")),
        flows_url=os.environ.get("SIMPLIT_FLOWS_URL", svc("flows")),
        integrations_url=os.environ.get("SIMPLIT_INTEGRATIONS_URL", svc("integrations")),
        control_url=os.environ.get("SIMPLIT_CONTROL_URL", svc("control")),
        enrollment_url=os.environ.get("SIMPLIT_ENROLLMENT_URL", svc("enrollment")),
        delivery_url=os.environ.get("SIMPLIT_DELIVERY_URL", svc("delivery")),
        presence_ws_url=os.environ.get("SIMPLIT_PRESENCE_WS_URL", svc("presence", "wss")),
        state_dir=Path(os.environ.get("SIMPLIT_STATE_DIR", "/var/lib/simplit")),
        trusted_control_pubkey=os.environ.get("SIMPLIT_CONTROL_PUBKEY", ""),
        trusted_delivery_pubkey=os.environ.get("SIMPLIT_DELIVERY_PUBKEY", ""),
        org=os.environ.get("SIMPLIT_ORG", "simplit"),
    )
