from __future__ import annotations
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class DeviceState:
    name: Optional[str] = None
    public_key: Optional[str] = None
    org: Optional[str] = None
    registered: bool = False
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    delivery_pubkey: Optional[str] = None
    current_version: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def device_id(self) -> Optional[str]:
        return self.name

def load_state(path: Path) -> DeviceState:
    if path.exists():
        data = json.loads(path.read_text())
        known = {k: data.get(k) for k in DeviceState().__dict__ if k != 'extra'}
        known['extra'] = data.get('extra', {})
        return DeviceState(**known)
    return DeviceState()

def save_state(path: Path, state: DeviceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, path)
