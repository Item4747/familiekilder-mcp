"""Settings for running the MCP server on an always-on host.

A host is a small Linux machine or container that serves the MCP server over
HTTP. Its secrets come from environment variables, never from the data disk:

- ``FAMILIEKILDER_PUBLIC_URL``: the public HTTPS address, e.g.
  ``https://example.fly.dev``. TLS ends in front of the server, but OAuth
  metadata must name the address clients use.
- ``FAMILIEKILDER_OWNER_CODE_HASH``: a slow scrypt hash of the owner code that
  the approval page asks for. The code itself is never stored.
- ``FAMILIEKILDER_INSTALLATION_ID``: a random ID that binds token ownership to
  this installation. A copy of the data disk without it cannot renew tokens.

Nothing here reads the network or the data root.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .core import FamiliekilderError

PUBLIC_URL_ENV = "FAMILIEKILDER_PUBLIC_URL"
OWNER_CODE_HASH_ENV = "FAMILIEKILDER_OWNER_CODE_HASH"
INSTALLATION_ID_ENV = "FAMILIEKILDER_INSTALLATION_ID"
ROOT_ENV = "FAMILIEKILDER_ROOT"

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

# Six groups of four characters from 31 unambiguous symbols: about 119 bits.
OWNER_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
OWNER_CODE_GROUPS = 6
OWNER_CODE_GROUP_LENGTH = 4

# scrypt with 32 MiB of memory per check, about 0.1 s on a small host.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
_HASH_PREFIX = "scrypt"

_INSTALLATION_ID = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


class HostingError(FamiliekilderError):
    """The host is not set up for HTTP mode."""


def default_data_root() -> Path:
    """The data root when neither ``--root`` nor ``FAMILIEKILDER_ROOT`` is set."""

    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/familiekilder-mcp"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "familiekilder-mcp"


def is_remote_host() -> bool:
    """True on a host set up for HTTP mode (outside macOS, with an installation ID)."""

    return sys.platform != "darwin" and bool(os.environ.get(INSTALLATION_ID_ENV))


def refuse_on_remote_host(what: str) -> None:
    """Stop login steps that only work on the user's own Mac."""

    if is_remote_host():
        raise HostingError(
            f"{what} gjøres på Macen, ikke på verten. Logg inn i veiviseren på Macen "
            "og flytt innloggingen med «familiekilder-mcp remote move ADRESSE»."
        )


def new_installation_id() -> str:
    return secrets.token_urlsafe(32)


def installation_id() -> str | None:
    """The installation ID from the environment, or None when unset."""

    value = os.environ.get(INSTALLATION_ID_ENV)
    if not value:
        return None
    if not _INSTALLATION_ID.fullmatch(value):
        raise HostingError(
            f"{INSTALLATION_ID_ENV} må være 32–128 tegn med bokstaver, tall, - eller _"
        )
    return value


def new_owner_code() -> str:
    groups = [
        "".join(secrets.choice(OWNER_CODE_ALPHABET) for _ in range(OWNER_CODE_GROUP_LENGTH))
        for _ in range(OWNER_CODE_GROUPS)
    ]
    return "-".join(groups)


def normalize_owner_code(value: str) -> str:
    """Accept the code as shown, in any case, with or without spaces and dashes."""

    return "".join(character for character in value.lower() if character not in " -\t\r\n")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _scrypt(code: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        normalize_owner_code(code).encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=SCRYPT_MAXMEM,
        dklen=32,
    )


def hash_owner_code(code: str) -> str:
    """Return ``scrypt:N:r:p:salt:hash``; safe to store as a host secret."""

    salt = secrets.token_bytes(16)
    digest = _scrypt(code, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return ":".join(
        (_HASH_PREFIX, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(digest))
    )


@dataclass(frozen=True)
class OwnerCodeHash:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes

    @classmethod
    def parse(cls, value: str) -> "OwnerCodeHash":
        parts = value.strip().split(":")
        try:
            if len(parts) != 6 or parts[0] != _HASH_PREFIX:
                raise ValueError
            n, r, p = (int(part) for part in parts[1:4])
            salt, digest = _unb64(parts[4]), _unb64(parts[5])
        except (ValueError, TypeError):
            raise HostingError(f"{OWNER_CODE_HASH_ENV} har ukjent format") from None
        # Refuse weak or absurd parameters instead of silently accepting them.
        if not (2**14 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 4):
            raise HostingError(f"{OWNER_CODE_HASH_ENV} har ugyldige scrypt-parametere")
        if len(salt) < 16 or len(digest) != 32:
            raise HostingError(f"{OWNER_CODE_HASH_ENV} har ugyldig salt eller lengde")
        return cls(n, r, p, salt, digest)

    def matches(self, code: str) -> bool:
        if not isinstance(code, str) or not code or len(code) > 200:
            return False
        candidate = _scrypt(code, self.salt, self.n, self.r, self.p)
        return hmac.compare_digest(candidate, self.digest)


@dataclass(frozen=True)
class HostSettings:
    public_url: str
    owner_code: OwnerCodeHash
    installation_id: str | None

    @property
    def issuer_url(self) -> str:
        return self.public_url

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).netloc


def normalize_public_url(value: str) -> str:
    """Validate an origin-only URL; HTTPS except on loopback for local tests."""

    value = (value or "").strip().rstrip("/")
    parts = urlsplit(value)
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parts.scheme not in {"https", "http"}
        or (parts.scheme == "http" and not loopback)
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise HostingError(
            f"{PUBLIC_URL_ENV} må være en HTTPS-adresse uten sti, for eksempel "
            "https://familiekilder-eksempel.fly.dev"
        )
    return value


def host_settings(environ: dict[str, str] | None = None) -> HostSettings:
    """Read and check the HTTP-mode settings; raise if anything is missing."""

    environ = os.environ if environ is None else environ
    missing = [
        name
        for name in (PUBLIC_URL_ENV, OWNER_CODE_HASH_ENV)
        if not environ.get(name)
    ]
    if sys.platform != "darwin" and not environ.get(INSTALLATION_ID_ENV):
        missing.append(INSTALLATION_ID_ENV)
    if missing:
        raise HostingError(
            "HTTP-modus krever innlogging foran serveren. Mangler: "
            + ", ".join(missing)
            + ". Lag verdiene med «familiekilder-mcp remote init» eller "
            "«familiekilder-mcp deploy fly»."
        )
    owner_code = OwnerCodeHash.parse(environ[OWNER_CODE_HASH_ENV])
    identity = environ.get(INSTALLATION_ID_ENV) or None
    if identity is not None and not _INSTALLATION_ID.fullmatch(identity):
        raise HostingError(
            f"{INSTALLATION_ID_ENV} må være 32–128 tegn med bokstaver, tall, - eller _"
        )
    return HostSettings(normalize_public_url(environ[PUBLIC_URL_ENV]), owner_code, identity)
