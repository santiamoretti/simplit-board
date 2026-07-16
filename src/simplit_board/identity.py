from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Identity:
    private_key: Ed25519PrivateKey

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def public_b64u(self) -> str:
        raw = self.public_key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return _b64u(raw)

    def sign_b64u(self, message: bytes) -> str:
        return _b64u(self.private_key.sign(message))


def load_or_create(path: Path) -> Identity:
    """Load the device private key from ``path`` (raw Ed25519, base64url) or create + persist it atomically."""
    if path.exists():
        priv = Ed25519PrivateKey.from_private_bytes(_unb64u(path.read_text().strip()))
        return Identity(priv)
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_b64u(raw))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return Identity(priv)


def verify(public_b64u: str, message: bytes, signature_b64u: str) -> bool:
    """Verify an Ed25519 signature (both key and signature base64url-encoded). Used to check control's pushes."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(_unb64u(public_b64u))
        pub.verify(_unb64u(signature_b64u), message)
        return True
    except Exception:
        return False
