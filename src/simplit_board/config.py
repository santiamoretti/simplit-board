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
    control_url: str
    enrollment_url: str
    delivery_url: str
    presence_ws_url: str
    state_dir: Path
    trusted_control_pubkey: str
    trusted_delivery_pubkey: str
    org: str

    @property
    def token_url(self) -> str:
        return f"{self.auth_url}/api/v1/auth/token"

    @property
    def login_url(self) -> str:
        return f"{self.auth_url}/api/v1/auth/login"

    @property
    def mfa_verify_url(self) -> str:
        return f"{self.auth_url}/api/v1/auth/mfa/verify"

    @property
    def mfa_setup_url(self) -> str:
        return f"{self.auth_url}/api/v1/auth/mfa/setup"

    @property
    def mfa_enroll_url(self) -> str:
        return f"{self.auth_url}/api/v1/auth/mfa/enroll"

    @property
    def enroll_url(self) -> str:
        return f"{self.enrollment_url}/api/enroll"

    @property
    def targets_url(self) -> str:
        return f"{self.enrollment_url}/api/enroll/targets"

    @property
    def register_url(self) -> str:
        return f"{self.gateway_url}/api/register"

    @property
    def devices_url(self) -> str:
        return f"{self.gateway_url}/api/data/devices"

    @property
    def presence_ws(self) -> str:
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
        control_url=os.environ.get("SIMPLIT_CONTROL_URL", svc("control")),
        enrollment_url=os.environ.get("SIMPLIT_ENROLLMENT_URL", svc("enrollment")),
        delivery_url=os.environ.get("SIMPLIT_DELIVERY_URL", svc("delivery")),
        presence_ws_url=os.environ.get("SIMPLIT_PRESENCE_WS_URL", svc("presence", "wss")),
        state_dir=Path(os.environ.get("SIMPLIT_STATE_DIR", "/var/lib/simplit")),
        trusted_control_pubkey=os.environ.get("SIMPLIT_CONTROL_PUBKEY", ""),
        trusted_delivery_pubkey=os.environ.get("SIMPLIT_DELIVERY_PUBKEY", ""),
        org=os.environ.get("SIMPLIT_ORG", "simplit"),
    )
