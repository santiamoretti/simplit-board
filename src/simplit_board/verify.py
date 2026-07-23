"""Verify control's signed job envelope.

Control signs the job with Ed25519. The presence frame carries ``payload`` and ``signature`` as base64 strings;
the signature is over the RAW decoded payload bytes (Jackson's compact JSON in declaration order, nulls
included) — so we must verify over ``b64decode(payload)`` and then ``json.loads`` those same bytes. NEVER
re-serialize the job to verify: any reordering/nul-dropping breaks the signature. This is the only trust gate —
presence itself does no crypto, so a blank/missing control key means we reject every job.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _b64(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad)
    except Exception:
        return base64.urlsafe_b64decode(s + pad)


def load_control_key(pub_b64: str) -> Ed25519PublicKey:
    raw = _b64(pub_b64)
    if len(raw) != 32:
        raise ValueError(f"control public key must be 32 raw bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_job(control_key: Ed25519PublicKey, payload_b64: str, signature_b64: str) -> dict[str, Any]:
    """Verify the envelope and return the decoded job dict. Raises on a bad signature."""
    payload_bytes = _b64(payload_b64)
    control_key.verify(_b64(signature_b64), payload_bytes)
    return json.loads(payload_bytes)


def verify_signature(pub_key: Ed25519PublicKey, message: bytes, signature_b64: str) -> bool:
    """True iff ``signature_b64`` is a valid Ed25519 signature by ``pub_key`` over ``message``. Used to
    authenticate a streamed artifact's manifest (the signature is over the artifact's raw SHA-256 bytes)."""
    try:
        pub_key.verify(_b64(signature_b64), message)
        return True
    except Exception:
        return False
