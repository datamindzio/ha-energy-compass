"""Ownership proof (ADR-0004): compact JWS (EdDSA) signed offline with the site key."""

import base64
import json
import secrets
from datetime import datetime

ISSUER = "energy-compass"
AUDIENCE = "energyatlas-bot"
MAX_TTL = 900
DEFAULT_NAME = "My home"
MAX_NAME = 64


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _segment(obj: dict) -> str:
    return _b64u(
        json.dumps(
            obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        ).encode()
    )


def generate(identity, now: datetime, ttl: int = MAX_TTL) -> str:
    """Return a bare compact JWT proving control of `identity.site_id`. No I/O."""
    if not 0 < ttl <= MAX_TTL:
        raise ValueError(f"ttl must be 1..{MAX_TTL} seconds")
    if not identity.site_id:
        raise ValueError("identity has no site_id; register the site first")
    name = (getattr(identity, "display_name", None) or "").strip()[:MAX_NAME].strip()
    iat = int(now.timestamp())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": identity.site_id}
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": identity.site_id,
        "iat": iat,
        "exp": iat + ttl,
        "jti": _b64u(secrets.token_bytes(16)),
        "name": name or DEFAULT_NAME,
        "tz": getattr(identity, "tz", None) or "UTC",
    }
    signing_input = f"{_segment(header)}.{_segment(claims)}"
    return f"{signing_input}.{_b64u(identity.sign(signing_input.encode()))}"
