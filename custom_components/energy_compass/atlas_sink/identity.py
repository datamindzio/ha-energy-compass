"""Edge identity: Ed25519 site key + site_id persisted in the data dir; registered once."""

import base64
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .signer import sign_headers

KEY_FILE = "identity.key"
SITE_FILE = "site.json"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class SignedClient:
    """POSTs JSON bodies and GETs signed per ADR-0003 as this site."""

    def __init__(
        self,
        base_url: str,
        key: Ed25519PrivateKey,
        site_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.site_id = site_id
        self._key = key
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=30, transport=transport
        )

    async def post(self, path: str, body: dict) -> httpx.Response:
        raw = json.dumps(body, separators=(",", ":")).encode()
        req = self._http.build_request(
            "POST", path, content=raw, headers={"Content-Type": "application/json"}
        )
        # sign what is actually sent: a base_url path prefix is part of @path
        req.headers.update(
            sign_headers(
                self._key,
                self.site_id,
                "POST",
                req.url.path,
                req.url.query.decode(),
                raw,
                created=int(time.time()),
            )
        )
        return await self._http.send(req)

    async def get(
        self, path: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        req = self._http.build_request("GET", path, headers=headers)
        # no body: ADR-0003 covers @method, @path and @query only
        req.headers.update(
            sign_headers(
                self._key,
                self.site_id,
                "GET",
                req.url.path,
                req.url.query.decode(),
                created=int(time.time()),
            )
        )
        return await self._http.send(req)

    async def aclose(self) -> None:
        await self._http.aclose()


class RegistrationFailed(RuntimeError):
    """Non-2xx response to POST /v1/sites; carries the raw response so a caller
    (atlas_sink.sink.register) can classify it into a RegistrationError kind (ADR-0019 §4)."""

    def __init__(self, response: httpx.Response):
        super().__init__(
            f"site registration failed: {response.status_code} {response.text[:200]}"
        )
        self.response = response


class Identity:
    def __init__(self, dir: Path):
        self._dir = Path(dir)
        self._key: Ed25519PrivateKey | None = None
        self.site_id: str | None = None
        self.display_name: str | None = None
        self.tz: str = "UTC"
        key_path = self._dir / KEY_FILE
        if key_path.exists():
            self._key = Ed25519PrivateKey.from_private_bytes(
                base64.urlsafe_b64decode(key_path.read_text().strip() + "==")
            )
        site_path = self._dir / SITE_FILE
        if site_path.exists():
            self.site_id = json.loads(site_path.read_text())["site_id"]

    @property
    def public_key(self) -> str | None:
        """Raw Ed25519 public key, base64url without padding (None until created)."""
        if self._key is None:
            return None
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return _b64u(raw)

    def sign(self, data: bytes) -> bytes:
        if self._key is None:
            raise RuntimeError("identity has no key yet; call ensure_registered")
        return self._key.sign(data)

    def _ensure_key(self) -> Ed25519PrivateKey:
        if self._key is None:
            self._dir.mkdir(parents=True, exist_ok=True)
            key = Ed25519PrivateKey.generate()
            seed = key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            _atomic_write(self._dir / KEY_FILE, _b64u(seed).encode())
            self._key = key
        return self._key

    async def ensure_registered(
        self,
        base_url: str,
        enrollment_secret: str,
        timeout_s: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> str:
        if self.site_id is not None:
            return self.site_id
        self._ensure_key()  # persisted before the call so a retry reuses the key
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout_s
        ) as client:
            resp = await client.post(
                "/v1/sites",
                json={"public_key": self.public_key},
                headers={"Authorization": f"Bearer {enrollment_secret}"},
            )
        if resp.status_code not in (200, 201):
            raise RegistrationFailed(resp)
        site_id = resp.json()["site_id"]
        _atomic_write(self._dir / SITE_FILE, json.dumps({"site_id": site_id}).encode())
        self.site_id = site_id
        return site_id

    def signed_client(
        self, base_url: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> SignedClient:
        if self._key is None or self.site_id is None:
            raise RuntimeError("identity not registered; call ensure_registered")
        return SignedClient(base_url, self._key, self.site_id, transport=transport)
