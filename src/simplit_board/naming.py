"""Human-friendly device names, GCP-project-id style (e.g. ``ancient-binder-4821``).

Uses ``coolname`` for the adjective-noun slug when available, with a small vendored fallback so a fresh
board can always mint a name even before optional deps resolve. The name doubles as the device id, so it must
be a stable, DNS-safe slug: lowercase, hyphen-separated, with a short numeric suffix for collision resistance.
"""
from __future__ import annotations

import secrets

# Vendored fallback wordlists (only used if `coolname` is unavailable). Kept short and evocative on purpose.
_ADJ = [
    "ancient", "brave", "calm", "clever", "cosmic", "crimson", "dusky", "eager", "fabled", "gentle",
    "golden", "hidden", "humble", "iron", "jolly", "lucid", "mellow", "noble", "polar", "quiet",
    "rustic", "silent", "solar", "stark", "swift", "twin", "velvet", "wandering", "wise", "zephyr",
]
_NOUN = [
    "binder", "harbor", "lantern", "meadow", "falcon", "cedar", "ember", "glacier", "willow", "canyon",
    "beacon", "cypress", "otter", "raven", "delta", "summit", "quartz", "thicket", "vortex", "warren",
    "anchor", "cobble", "drifter", "foundry", "grove", "hollow", "isle", "juniper", "kestrel", "loom",
]


def generate_name() -> str:
    """Return a fresh ``adjective-noun-NNNN`` slug suitable as a device id."""
    suffix = secrets.randbelow(9000) + 1000  # 4 digits, no leading zero
    try:
        import coolname  # type: ignore

        slug = coolname.generate_slug(2)
    except Exception:
        slug = f"{secrets.choice(_ADJ)}-{secrets.choice(_NOUN)}"
    slug = "-".join(part.strip().lower() for part in slug.split("-") if part.strip())
    return f"{slug}-{suffix}"


def is_valid_name(name: str) -> bool:
    """A device id must be a lowercase DNS-ish slug (letters, digits, hyphens; 3-63 chars)."""
    if not (3 <= len(name) <= 63):
        return False
    return all(c.isalnum() or c == "-" for c in name) and name[0].isalnum() and name[-1].isalnum()
