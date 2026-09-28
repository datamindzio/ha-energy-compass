"""RFC 9421 / RFC 9530 request signer (ADR-0003 profile, Ed25519 only)."""

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

LABEL = "sig1"


@dataclass(frozen=True)
class SignedHeaders:
    signature_input: str
    signature: str
    content_digest: str | None
    signature_base: str

    def as_headers(self) -> dict[str, str]:
        headers = {"Signature-Input": self.signature_input, "Signature": self.signature}
        if self.content_digest is not None:
            headers["Content-Digest"] = self.content_digest
        return headers


def content_digest(body: bytes) -> str:
    """RFC 9530 Content-Digest over the body bytes as sent (compressed if gzip)."""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def sign_request(
    private_key: Ed25519PrivateKey,
    site_id: str,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    *,
    created: int,
) -> SignedHeaders:
    """Sign a site request. `query` has no leading '?'; `body` is the bytes as sent."""
    query = query.removeprefix("?")
    digest = content_digest(body) if body else None
    components = ["@method", "@path", "@query"] + (["content-digest"] if digest else [])
    params = (
        "("
        + " ".join(f'"{c}"' for c in components)
        + f');created={created};keyid="{site_id}";alg="ed25519"'
    )
    lines = [
        f'"@method": {method.upper()}',
        f'"@path": {path}',
        f'"@query": ?{query}',
    ]
    if digest:
        lines.append(f'"content-digest": {digest}')
    lines.append(f'"@signature-params": {params}')
    base = "\n".join(lines)
    sig = base64.b64encode(private_key.sign(base.encode())).decode()
    return SignedHeaders(
        signature_input=f"{LABEL}={params}",
        signature=f"{LABEL}=:{sig}:",
        content_digest=digest,
        signature_base=base,
    )


def sign_headers(
    private_key: Ed25519PrivateKey,
    site_id: str,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    *,
    created: int,
) -> Mapping[str, str]:
    return sign_request(
        private_key, site_id, method, path, query, body, created=created
    ).as_headers()
