"""Provider bundle wire format, pinned trust and client profile validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

from .core import PROVIDER_SOURCES, FamiliekilderError

_APK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.apk\Z")
BUNDLE_SCHEMA = "family.provider-bundle/v1"
BUNDLE_MEDIA_TYPE = "application/vnd.familiekilder.provider-bundle+json"
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_VALIDITY = timedelta(days=90)
MAX_ISSUED_AT_SKEW = timedelta(minutes=5)


# Trust comes from this compiled pin, never from a downloaded bundle or key file.
PINNED_PROVIDER_KEYS: Mapping[str, str] = {}
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_CODE = re.compile(r"[1-9][0-9]{0,19}\Z")
_B64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_PACKAGE_IDS = {
    "vigilo": "parent.vigilo.no.parentapplication",
    "skolemelding": "com.cgi.oslomelding.foresatte",
    "spond": "com.spond.spond",
    "hoopit": "io.hoopit.app.android.v2",
    "ist-home": "com.ist.home.miniforumschool",
}
# Hoopit's first-party browser client is a separate, explicitly pinned source.
# It is never represented as a Play-installed or APK-signed application.
HOOPIT_WEB_URL = 'https://app.hoopit.io/main.dart.js'
HOOPIT_WEB_EXTRACTOR = {
    'api_root': 'unique https://api.hoopit.io literal',
    'firebase': r'new A\.aAV\("(AIza[A-Za-z0-9_-]{35})","1:921476735762:web:[a-f0-9]+","921476735762","(hoopit-30d5c)",',
}
HOOPIT_WEB_EXTRACTOR_DIGEST = hashlib.sha256(json.dumps(HOOPIT_WEB_EXTRACTOR, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
HOOPIT_WEB_RELEASES = {
    '1104dffce00b48938849ab5f1e64ea03c0dd6894b7575c3790a7e0746a1d2fe0': {
        'url': HOOPIT_WEB_URL,
        'version_name': 'Web 1104dffce00b',
        'extractor_digest': HOOPIT_WEB_EXTRACTOR_DIGEST,
    },
}
_IMMUTABLE_FIELDS = {
    "vigilo": (
        "authority",
        "client_id",
        "client_secret",
        "redirect_uri",
        "scope",
        "api_root",
    ),
    "skolemelding": (
        "oidc_client_id",
        "portal_client_id",
        "issuer",
        "redirect_uri",
        "scopes",
        "authorization_endpoint",
        "oidc_token_endpoint",
    ),
    "spond": ("api_root",),
    "hoopit": ("api_root", "firebase_api_key", "firebase_project_id"),
    "ist-home": ("authority", "client_id", "client_secret", "redirect_uri", "scope"),
}


class ProviderBundleError(FamiliekilderError):
    """A sanitized provider-bundle or provisioning failure."""


@dataclass(frozen=True, repr=False)
class VerifiedProviderBundle:
    """Verified secret-bearing payload with a deliberately redacted repr."""

    key_id: str
    generation: int
    issued_at: datetime
    expires_at: datetime
    sources: Mapping[str, Mapping[str, Any]] = field(repr=False)
    payload_sha256: str
    bundle_digest: str

    def __repr__(self) -> str:
        return (
            "VerifiedProviderBundle("
            f"key_id={self.key_id!r}, generation={self.generation!r}, "
            f"issued_at={self.issued_at.isoformat()!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, "
            f"payload_sha256={self.payload_sha256!r}, "
            f"bundle_digest={self.bundle_digest!r})"
        )


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderBundleError("Providerpakken har dupliserte JSON-felt")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ProviderBundleError("Providerpakken har ugyldige JSON-tall")


def _decode_json(raw: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except ProviderBundleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ProviderBundleError(f"{label} har ugyldig JSON-format") from None
    if not isinstance(value, Mapping):
        raise ProviderBundleError(f"{label} må være ett JSON-objekt")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ProviderBundleError("Providerpakken kunne ikke serialiseres sikkert") from None


def decode_b64url(value: Any, *, label: str, expected_bytes: int | None = None) -> bytes:
    """Decode canonical unpadded base64url; `label` names the field in errors."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > (MAX_BUNDLE_BYTES * 2)
        or not _B64URL.fullmatch(value)
    ):
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}") from None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise ProviderBundleError(f"Providerpakken har ikke-kanonisk {label}")
    if expected_bytes is not None and len(raw) != expected_bytes:
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}")
    return raw


def _encode_b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}")
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}") from None
    return result


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ProviderBundleError("Providerpakkens tidspunkt mangler tidssone")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _required_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProviderBundleError(f"Providerpakken har ugyldig {label}")
    return value


def _validate_provenance(source: str, value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get('kind') == 'official-web':
        if source != 'hoopit' or set(value) != {'kind', 'url', 'asset_sha256', 'extractor_digest', 'version_name'}:
            raise ProviderBundleError(f'{source}: web-proveniensen har ukjente eller manglende felt')
        asset_hash = value.get('asset_sha256')
        release = HOOPIT_WEB_RELEASES.get(asset_hash) if isinstance(asset_hash, str) else None
        if release is None or any(value.get(name) != release[name] for name in ('url', 'version_name', 'extractor_digest')):
            raise ProviderBundleError('hoopit: web-proveniensen matcher ikke en pinnet offisiell release')
        return dict(value)
    if not isinstance(value, Mapping) or set(value) != {
        "package_id",
        "version_code",
        "version_name",
        "installer_package",
        "signer_sha256",
        "apk_sha256",
    }:
        raise ProviderBundleError(f"{source}: APK-proveniensen har ukjente eller manglende felt")
    package_id = _required_text(value.get("package_id"), "pakke-ID")
    if package_id != _PACKAGE_IDS[source]:
        raise ProviderBundleError(f"{source}: APK-proveniensen har feil pakke-ID")
    version_code = value.get("version_code")
    if not isinstance(version_code, str) or not _VERSION_CODE.fullmatch(version_code):
        raise ProviderBundleError(f"{source}: APK-proveniensen har ugyldig versjonskode")
    version_name = _required_text(value.get("version_name"), "appversjon", maximum=128)
    if value.get("installer_package") != "com.android.vending":
        raise ProviderBundleError(f"{source}: APK-settet er ikke installert av Play Store")
    signers = value.get("signer_sha256")
    if (
        not isinstance(signers, list)
        or not signers
        or len(signers) > 8
        or not all(isinstance(digest, str) and _SHA256.fullmatch(digest) for digest in signers)
        or signers != sorted(set(signers))
    ):
        raise ProviderBundleError(f"{source}: APK-proveniensen har ugyldige signeringspinner")
    apks = value.get("apk_sha256")
    if (
        not isinstance(apks, Mapping)
        or not apks
        or len(apks) > 256
        or not all(
            isinstance(name, str)
            and _APK_NAME.fullmatch(name)
            and isinstance(digest, str)
            and _SHA256.fullmatch(digest)
            for name, digest in apks.items()
        )
    ):
        raise ProviderBundleError(f"{source}: APK-proveniensen har ugyldige APK-hasher")
    apk_names = set(apks)
    if "base.apk" not in apk_names or any(
        name != "base.apk"
        and not (name.startswith("split_") and name.endswith(".apk"))
        for name in apk_names
    ):
        raise ProviderBundleError(
            f"{source}: APK-proveniensen mangler base eller har ukjente split-navn"
        )
    return {
        "package_id": package_id,
        "version_code": version_code,
        "version_name": version_name,
        "installer_package": "com.android.vending",
        "signer_sha256": list(signers),
        "apk_sha256": dict(sorted(apks.items())),
    }


def _validate_spond_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"api_root", "app_version", "attachment_hosts"}:
        raise ProviderBundleError("spond: providerprofilen har ukjente eller manglende felt")
    if value.get("api_root") != "https://api.spond.com/core/v1":
        raise ProviderBundleError("spond: providerprofilen har feil API-rot")
    app_version = _required_text(value.get("app_version"), "Spond-appversjon", maximum=100)
    hosts = value.get("attachment_hosts")
    if not isinstance(hosts, list) or len(hosts) > 20:
        raise ProviderBundleError("spond: providerprofilen har ugyldige vedleggsverter")
    normalized_hosts: list[str] = []
    for raw_host in hosts:
        host = _required_text(raw_host, "Spond-vedleggsvert", maximum=253).lower().rstrip(".")
        if "/" in host or ":" in host or "*" in host or host.startswith("."):
            raise ProviderBundleError("spond: providerprofilen har ugyldige vedleggsverter")
        try:
            host = host.encode("idna").decode("ascii")
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        except UnicodeError:
            raise ProviderBundleError("spond: providerprofilen har ugyldige vedleggsverter") from None
        if address is not None and not address.is_global:
            raise ProviderBundleError("spond: providerprofilen har ugyldige vedleggsverter")
        if (
            host == "localhost"
            or host.endswith(".localhost")
            or ".." in host
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
            or any(len(label) > 63 for label in host.split("."))
        ):
            raise ProviderBundleError("spond: providerprofilen har ugyldige vedleggsverter")
        normalized_hosts.append(host)
    if len(normalized_hosts) != len(set(normalized_hosts)):
        raise ProviderBundleError("spond: providerprofilen har dupliserte vedleggsverter")
    return {
        "api_root": "https://api.spond.com/core/v1",
        "app_version": app_version,
        "attachment_hosts": sorted(normalized_hosts),
    }


def _validate_source_config(source: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderBundleError(f"{source}: providerprofilen må være et objekt")
    try:
        if source == "vigilo":
            if set(value) != {
                "authority",
                "client_id",
                "client_secret",
                "redirect_uri",
                "scope",
                "api_root",
                "app_version",
                "attachment_hosts",
            }:
                raise ValueError
            from .vigilo_auth import _parse_client_config

            normalized = _parse_client_config(value).as_private_json()
            normalized["scope"] = " ".join(sorted(normalized["scope"].split()))
            return normalized
        if source == "skolemelding":
            allowed = {
                "oidc_client_id",
                "portal_client_id",
                "app_version",
                "issuer",
                "redirect_uri",
                "scopes",
                "authorization_endpoint",
                "oidc_token_endpoint",
                "attachment_hosts",
                "attachment_path_prefixes",
            }
            required = allowed - {"authorization_endpoint", "oidc_token_endpoint"}
            if not required.issubset(value) or not set(value).issubset(allowed):
                raise ValueError
            from .skolemelding_auth import _parse_client_config

            return _parse_client_config(value).as_private_json()
        if source == "hoopit":
            from .hoopit_auth import _parse_client_config

            return _parse_client_config(value).as_private_json()
        if source == "ist-home":
            if set(value) != {"authority", "client_id", "client_secret", "redirect_uri", "scope"}:
                raise ValueError
            from .ist_oauth import _parse_client_config

            normalized = _parse_client_config(value).as_private_json()
            normalized["scope"] = " ".join(sorted(normalized["scope"].split()))
            return normalized
        if source == "spond":
            return _validate_spond_config(value)
    except ProviderBundleError:
        raise
    except Exception:
        raise ProviderBundleError(f"{source}: providerprofilen er ugyldig") from None
    raise ProviderBundleError("Providerpakken inneholder en ukjent kilde")


def _validate_payload(
    payload: Mapping[str, Any], *, now: datetime
) -> tuple[int, datetime, datetime, dict[str, Mapping[str, Any]]]:
    if set(payload) != {"generation", "issued_at", "expires_at", "sources"}:
        raise ProviderBundleError("Providerpayloaden har ukjente eller manglende felt")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 1 <= generation < 2**63:
        raise ProviderBundleError("Providerpakken har ugyldig generasjon")
    issued_at = _parse_timestamp(payload.get("issued_at"), "issued_at")
    expires_at = _parse_timestamp(payload.get("expires_at"), "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > MAX_VALIDITY:
        raise ProviderBundleError("Providerpakken har ugyldig gyldighetsperiode")
    normalized_now = now.astimezone(UTC)
    if issued_at > normalized_now + MAX_ISSUED_AT_SKEW:
        raise ProviderBundleError("Providerpakken er utstedt i fremtiden")
    if expires_at <= normalized_now:
        raise ProviderBundleError("Providerpakken er utløpt")
    sources = payload.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != set(PROVIDER_SOURCES):
        raise ProviderBundleError("Providerpakken må inneholde nøyaktig alle kildene")
    normalized_sources: dict[str, Mapping[str, Any]] = {}
    for source in PROVIDER_SOURCES:
        profile = sources[source]
        if not isinstance(profile, Mapping) or set(profile) != {"config", "provenance"}:
            raise ProviderBundleError(f"{source}: providerprofilen har ugyldig struktur")
        normalized_sources[source] = {
            "config": _validate_source_config(source, profile.get("config")),
            "provenance": _validate_provenance(source, profile.get("provenance")),
        }
    return generation, issued_at, expires_at, normalized_sources


def _public_key(value: Any) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = decode_b64url(value, label="offentlig nøkkel", expected_bytes=32)
    else:
        raise ProviderBundleError("Den pinnede providerverifikasjonsnøkkelen er ugyldig")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise ProviderBundleError("Den pinnede providerverifikasjonsnøkkelen er ugyldig") from None


def verify_provider_bundle(
    raw_bundle: bytes,
    *,
    trusted_keys: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> VerifiedProviderBundle:
    """Verify exact envelope/payload bytes and normalize all five profiles."""

    if not isinstance(raw_bundle, bytes) or not raw_bundle or len(raw_bundle) > MAX_BUNDLE_BYTES:
        raise ProviderBundleError("Providerpakken mangler eller er større enn tillatt")
    envelope = _decode_json(raw_bundle, label="Providerpakken")
    if set(envelope) != {"schema", "key_id", "payload_b64", "signature"}:
        raise ProviderBundleError("Providerpakken har ukjente eller manglende envelope-felt")
    if envelope.get("schema") != BUNDLE_SCHEMA:
        raise ProviderBundleError("Providerpakken har en ukjent kontrakt")
    key_id = envelope.get("key_id")
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise ProviderBundleError("Providerpakken har ugyldig key_id")
    keys = PINNED_PROVIDER_KEYS if trusted_keys is None else trusted_keys
    if key_id not in keys:
        raise ProviderBundleError("Providerpakkens signatur kunne ikke verifiseres")
    payload_raw = decode_b64url(envelope.get("payload_b64"), label="payload")
    if not payload_raw or len(payload_raw) > MAX_BUNDLE_BYTES:
        raise ProviderBundleError("Providerpakkens payload er større enn tillatt")
    signature = decode_b64url(envelope.get("signature"), label="signatur", expected_bytes=64)
    try:
        _public_key(keys[key_id]).verify(signature, payload_raw)
    except InvalidSignature:
        raise ProviderBundleError("Providerpakkens signatur kunne ikke verifiseres") from None
    payload = _decode_json(payload_raw, label="Providerpayloaden")
    check_time = now or datetime.now(UTC)
    if check_time.tzinfo is None:
        raise ProviderBundleError("Verifikasjonstidspunktet mangler tidssone")
    generation, issued_at, expires_at, sources = _validate_payload(payload, now=check_time)
    return VerifiedProviderBundle(
        key_id=key_id,
        generation=generation,
        issued_at=issued_at,
        expires_at=expires_at,
        sources=sources,
        payload_sha256=hashlib.sha256(payload_raw).hexdigest(),
        bundle_digest=hashlib.sha256(raw_bundle).hexdigest(),
    )
