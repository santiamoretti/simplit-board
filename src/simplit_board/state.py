"""Durable device state (device.json). Written atomically so a power cut can never corrupt the identity."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DeviceState:
    name: Optional[str] = None            # friendly id, e.g. ancient-binder-4821 — also the presence device id
    public_key: Optional[str] = None      # base64url Ed25519 public key
    org: Optional[str] = None
    registered: bool = False
    client_id: Optional[str] = None       # service-account client id issued at registration (if any)
    client_secret: Optional[str] = None   # service-account secret issued at registration (if any)
    delivery_pubkey: Optional[str] = None # base64 Ed25519 code-signing anchor (the BUILD verify key), pinned
                                          # in-band from the authenticated enrollment response at register time
    control_pubkey: Optional[str] = None  # base64 Ed25519 key a control JOB is verified against, pinned the same
                                          # way. Without it the board rejects every job ("no control public key
                                          # configured"), which is invisible until a push silently fails.
    current_version: Optional[str] = None # version of the deployed java service
    extra: dict = field(default_factory=dict)

    @property
    def device_id(self) -> Optional[str]:
        return self.name


def load_state(path: Path) -> DeviceState:
    if path.exists():
        data = json.loads(path.read_text())
        known = {k: data.get(k) for k in DeviceState().__dict__ if k != "extra"}
        known["extra"] = data.get("extra", {})
        return DeviceState(**known)
    return DeviceState()


def save_state(path: Path, state: DeviceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)  # atomic
