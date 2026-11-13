"""Small allowlisted HTTP transport for read-only source adapters."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import stat
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .core import FamiliekilderError
from .live_traffic import paced_request
from .network_usage import http_attempt
from .private_state import PrivateStateError, ensure_private_directory, fsync_directory


DEFAULT_MAX_JSON_BYTES = 1024 * 1024
DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 366 * 24 * 60 * 60
_COMPLETED_CALL = ContextVar('familiekilder_completed_api_call', default=None)


@contextmanager
def completed_calls(observer):
    """Observe fully read, validated successes, never request starts or errors."""
    token = _COMPLETED_CALL.set(observer)
    try:
        yield
    finally:
        _COMPLETED_CALL.reset(token)


def api_call_completed():
    observer = _COMPLETED_CALL.get()
    if observer is not None:
        # Progress reporting must not turn a valid source read into a failure.
        with suppress(Exception):
            observer()


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_BLOCKED_EXTRA_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "transfer-encoding",
}


class ApiTransportError(FamiliekilderError):
    """A network failure whose message never contains response or request data."""


class ApiContentRejected(ApiTransportError):
    """A received attachment has unsupported content; not a source outage."""


class ApiHTTPError(ApiTransportError):
    """Sanitized HTTP rejection with only status and an allowlisted error code."""

    def __init__(
        self,
        status_code: int,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        suffix = f", {error_code}" if error_code else ""
        super().__init__(f"API-endepunktet avviste forespørselen (HTTP {status_code}{suffix})")


class ApiRequestNotSent(ApiTransportError):
    """Only raised with evidence that no HTTP request bytes were sent."""
    request_sent = False


def definitely_not_sent(exc):
    reason = getattr(exc, 'reason', exc)
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError,
                               ssl.SSLCertVerificationError))


@dataclass(frozen=True)
class DownloadResult:
    byte_size: int
    sha256: str
    content_type: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is a pre-validated public IP."""

    def __init__(self, hostname: str, address: str, *, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        try:
            sock = socket.create_connection(
                (self._pinned_address, self.port),
                self.timeout,
                self.source_address,
            )
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            try:
                sock.close()
            except (NameError, OSError):
                pass
            raise


class _PinnedResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: _PinnedHTTPSConnection,
    ) -> None:
        self._response = response
        self._connection = connection
        self.headers = response.headers

    def read(self, amount: int | None = None) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _retry_after_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    """Parse Retry-After without retaining or exposing a response header."""

    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdecimal():
        delay = int(stripped)
    else:
        try:
            target = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        delay = max(0, int((target.astimezone(UTC) - current.astimezone(UTC)).total_seconds()))
    if delay > MAX_RETRY_AFTER_SECONDS:
        return MAX_RETRY_AFTER_SECONDS
    return delay


def _normalized_host(value: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or ":" in value:
        raise ApiTransportError("API-vertslisten har ugyldig format")
    try:
        normalized = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise ApiTransportError("API-vertslisten har ugyldig format") from None
    if not normalized:
        raise ApiTransportError("API-vertslisten har ugyldig format")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise ApiTransportError("API-vertslisten kan ikke bruke en lokal vert")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ApiTransportError("API-vertslisten kan ikke bruke en privat adresse")
    return normalized


def _safe_headers(value: Mapping[str, str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, header_value in (value or {}).items():
        if (
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or name.lower() in _BLOCKED_EXTRA_HEADERS
            or not isinstance(header_value, str)
            or not header_value
            or any(ord(character) < 32 or ord(character) == 127 for character in header_value)
        ):
            raise ApiTransportError("Et ekstra API-headerfelt har ugyldig format")
        headers[name] = header_value
    return headers


class ApiTransport:
    """HTTP client constrained to exact URLs and/or HTTPS hostnames.

    Exact URL entries include their scheme and port and are useful for fixed API
    endpoints (and local test servers). Host entries always require HTTPS on the
    default port and are intended for provider-controlled attachment paths.
    """

    def __init__(
        self,
        *,
        allowed_urls: set[str] | frozenset[str] | tuple[str, ...] = (),
        allowed_hosts: set[str] | frozenset[str] | tuple[str, ...] = (),
        safe_error_codes: set[str] | frozenset[str] | tuple[str, ...] = (),
        timeout: float = 30.0,
        max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        user_agent: str = "familiekilder-source/0.2",
    ) -> None:
        if not allowed_urls and not allowed_hosts:
            raise ApiTransportError("API-transporten krever en eksplisitt tillatelsesliste")
        if timeout <= 0 or max_json_bytes <= 0 or max_download_bytes <= 0:
            raise ApiTransportError("API-transportens grenser er ugyldige")
        if (
            not isinstance(user_agent, str)
            or not user_agent
            or any(ord(character) < 32 or ord(character) == 127 for character in user_agent)
        ):
            raise ApiTransportError("API-transportens user-agent er ugyldig")
        self.allowed_urls = frozenset(allowed_urls)
        self.allowed_hosts = frozenset(_normalized_host(host) for host in allowed_hosts)
        self.safe_error_codes = frozenset(safe_error_codes)
        self.timeout = float(timeout)
        self.max_json_bytes = int(max_json_bytes)
        self.max_download_bytes = int(max_download_bytes)
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def _validate_url(self, url: str) -> str:
        if (
            not isinstance(url, str)
            or not url
            or len(url) > 8192
            or any(ord(character) < 33 or ord(character) == 127 for character in url)
        ):
            raise ApiTransportError("API-URL-en har ugyldig format")
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError:
            raise ApiTransportError("API-URL-en har ugyldig format") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ApiTransportError("API-URL-en har ugyldig format")
        try:
            normalized_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            raise ApiTransportError("API-URL-en har ugyldig format") from None
        if url in self.allowed_urls:
            return url
        if (
            parsed.scheme == "https"
            and port in (None, 443)
            and normalized_host in self.allowed_hosts
        ):
            return url
        raise ApiTransportError("API-klienten nektet et ukjent endepunkt")

    def _error_code(self, raw: bytes) -> str | None:
        if len(raw) > self.max_json_bytes:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping):
            return None
        for field in ("error", "code", "errorCode"):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate in self.safe_error_codes:
                return candidate
        return None

    @paced_request
    def _open(
        self,
        url: str,
        *,
        data: bytes | None,
        method: str,
        headers: Mapping[str, str] | None,
    ) -> Any:
        target = self._validate_url(url)
        if target not in self.allowed_urls:
            try:
                parsed_target = urllib.parse.urlsplit(target)
                addresses = socket.getaddrinfo(
                    parsed_target.hostname,
                    parsed_target.port or 443,
                    type=socket.SOCK_STREAM,
                )
            except (OSError, ValueError):
                raise ApiRequestNotSent(
                    "API-vertens offentlige adresse kunne ikke bekreftes"
                ) from None
            if not addresses:
                raise ApiTransportError(
                    "API-vertens offentlige adresse kunne ikke bekreftes"
                )
            public_addresses: list[str] = []
            for _family, _kind, _proto, _canonical, sockaddr in addresses:
                try:
                    address = ipaddress.ip_address(sockaddr[0])
                except ValueError:
                    raise ApiTransportError(
                        "API-vertens adresse har ugyldig format"
                    ) from None
                if not address.is_global:
                    raise ApiTransportError(
                        "API-vertens adresse er ikke offentlig"
                    )
                public_addresses.append(str(address))
            request_headers = {
                "Accept": "application/json",
                "User-Agent": self.user_agent,
                **_safe_headers(headers),
            }
            path = parsed_target.path or "/"
            if parsed_target.query:
                path += "?" + parsed_target.query
            connection = _PinnedHTTPSConnection(
                str(parsed_target.hostname),
                public_addresses[0],
                timeout=self.timeout,
            )
            try:
                # Establish TCP/TLS before http.client buffers any HTTP bytes.
                try:
                    connection.connect()
                except (OSError, TimeoutError, ssl.SSLError):
                    raise ApiRequestNotSent('Tilkobling feilet før forespørselen ble sendt') from None
                http_attempt()
                connection.request(method, path, body=data, headers=request_headers)
                response = connection.getresponse()
            except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException):
                connection.close()
                raise ApiTransportError(
                    "Det tillatte API-endepunktet kunne ikke nås"
                ) from None
            if not 200 <= int(response.status) < 300:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                try:
                    raw = response.read(self.max_json_bytes + 1)
                except (OSError, http.client.HTTPException):
                    raw = b""
                finally:
                    response.close()
                    connection.close()
                raise ApiHTTPError(
                    int(response.status),
                    self._error_code(raw),
                    retry_after,
                )
            return _PinnedResponse(response, connection)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **_safe_headers(headers),
        }
        request = urllib.request.Request(
            target,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            http_attempt()
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
            try:
                raw = exc.read(self.max_json_bytes + 1)
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                exc.close()
            raise ApiHTTPError(
                int(exc.code),
                self._error_code(raw),
                retry_after,
            ) from None
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            if definitely_not_sent(exc):
                raise ApiRequestNotSent('Tilkobling feilet før forespørselen ble sendt') from None
            raise ApiTransportError("Det tillatte API-endepunktet kunne ikke nås") from None

    def _request_json(
        self,
        url: str,
        *,
        data: bytes | None,
        method: str,
        headers: Mapping[str, str] | None,
        allow_empty: bool = False,
    ) -> Any:
        response = self._open(url, data=data, method=method, headers=headers)
        try:
            raw = response.read(self.max_json_bytes + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ApiTransportError("API-responsen kunne ikke leses") from None
        finally:
            response.close()
        if len(raw) > self.max_json_bytes:
            raise ApiTransportError("API-endepunktet returnerte for mye data")
        if allow_empty and not raw:
            api_call_completed()
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiTransportError("API-endepunktet returnerte ugyldig JSON") from None
        api_call_completed()
        return value

    @staticmethod
    def _auth_headers(
        bearer_token: str | None,
        headers: Mapping[str, str] | None,
    ) -> dict[str, str]:
        result = _safe_headers(headers)
        if bearer_token is not None:
            if (
                not isinstance(bearer_token, str)
                or not bearer_token
                or any(ord(character) < 33 or ord(character) == 127 for character in bearer_token)
            ):
                raise ApiTransportError("Bearer-tokenet har ugyldig format")
            if any(name.lower() == "authorization" for name in result):
                raise ApiTransportError("Authorization-headeret ble oppgitt flere ganger")
            result["Authorization"] = f"Bearer {bearer_token}"
        return result

    def get_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self._request_json(
            url,
            data=None,
            method="GET",
            headers=self._auth_headers(bearer_token, headers),
        )

    def delete_json(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        """One bodyless DELETE, with the same URL/auth/pacing guards as GET."""
        return self._request_json(
            url,
            data=None,
            method="DELETE",
            headers=self._auth_headers(bearer_token, headers),
            allow_empty=allow_empty,
        )

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        if not isinstance(payload, Mapping):
            raise ApiTransportError("JSON-forespørselen må være et objekt")
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            raise ApiTransportError("JSON-forespørselen kunne ikke serialiseres") from None
        request_headers = self._auth_headers(bearer_token, headers)
        request_headers["Content-Type"] = "application/json"
        return self._request_json(
            url,
            data=data,
            method="POST",
            headers=request_headers,
            allow_empty=allow_empty,
        )

    def post_json_array(
        self,
        url: str,
        payload: list,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Explicit array contract; post_json retains its object-only guard."""
        if not isinstance(payload, list):
            raise ApiTransportError("JSON-forespørselen må være en liste")
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise ApiTransportError("JSON-forespørselen kunne ikke serialiseres") from None
        request_headers = self._auth_headers(bearer_token, headers)
        request_headers["Content-Type"] = "application/json"
        return self._request_json(url, data=data, method="POST", headers=request_headers)

    def put_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if not isinstance(payload, Mapping):
            raise ApiTransportError("JSON-forespørselen må være et objekt")
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            raise ApiTransportError("JSON-forespørselen kunne ikke serialiseres") from None
        request_headers = self._auth_headers(bearer_token, headers)
        request_headers["Content-Type"] = "application/json"
        return self._request_json(
            url,
            data=data,
            method="PUT",
            headers=request_headers,
        )

    def patch_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if not isinstance(payload, Mapping):
            raise ApiTransportError("JSON-forespørselen må være et objekt")
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise ApiTransportError("JSON-forespørselen kunne ikke serialiseres") from None
        request_headers = self._auth_headers(bearer_token, headers)
        request_headers["Content-Type"] = "application/json"
        return self._request_json(url, data=data, method="PATCH", headers=request_headers)

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if not isinstance(fields, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in fields.items()
        ):
            raise ApiTransportError("Skjemadataene har ugyldig format")
        data = urllib.parse.urlencode(fields).encode("ascii")
        request_headers = self._auth_headers(bearer_token, headers)
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self._request_json(
            url,
            data=data,
            method="POST",
            headers=request_headers,
        )

    def download(
        self,
        url: str,
        target: Path | str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        allowed_content_types: set[str] | frozenset[str] | None = None,
    ) -> DownloadResult:
        """Stream one allowlisted response atomically into ``.private``."""

        limit = self.max_download_bytes if max_bytes is None else max_bytes
        _download_destination(target, limit)
        response = self._open(
            url, data=None, method="GET",
            headers=self._auth_headers(bearer_token, headers),
        )
        result = save_download_response(
            response, target, max_bytes=limit,
            allowed_content_types=allowed_content_types,
        )
        api_call_completed()
        return result


def _download_destination(target, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ApiTransportError("Nedlastingsgrensen er ugyldig")
    destination = Path(target)
    ensure_private_directory(destination.parent)
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise ApiTransportError("Nedlastingsmålet kunne ikke kontrolleres") from None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise ApiTransportError("Nedlastingsmålet må være en vanlig privat fil")

    return destination


def save_download_response(response, target, *, max_bytes, allowed_content_types=None, consume=None) -> DownloadResult:
    """Commit a complete, bounded response atomically; always close the response.

    URL, redirect and authentication policy remain the caller's responsibility.
    ``consume`` can enforce an aggregate run budget before bytes reach disk.
    """
    limit = max_bytes
    try:
        destination = _download_destination(target, limit)
        content_type_header = response.headers.get("Content-Type")
        content_type = (
            content_type_header.split(";", 1)[0].strip().lower()
            if isinstance(content_type_header, str)
            else None
        )
        if allowed_content_types is not None and content_type not in allowed_content_types:
            raise ApiContentRejected("Vedlegget har en ikke-tillatt MIME-type")
        declared = None
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                raise ApiTransportError("Vedlegget oppga ugyldig størrelse") from None
            if declared < 0 or declared > limit:
                raise ApiTransportError("Vedlegget overskred nedlastingsgrensen")

        temporary_path: Path | None = None
        digest = hashlib.sha256()
        total = 0
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary_path.chmod(0o600)
                while chunk := response.read(min(1024 * 1024, limit + 1 - total)):
                    total += len(chunk)
                    if total > limit:
                        raise ApiTransportError("Vedlegget overskred nedlastingsgrensen")
                    if consume is not None:
                        consume(len(chunk))
                    digest.update(chunk)
                    temporary.write(chunk)
                if declared is not None and total != declared:
                    raise ApiTransportError("Vedlegget ble ikke lastet ned fullstendig")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            destination.chmod(0o600)
            fsync_directory(destination.parent)
        except (PrivateStateError, ApiTransportError):
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ApiTransportError("Vedlegget kunne ikke lagres trygt") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return DownloadResult(total, digest.hexdigest(), content_type)

    finally:
        response.close()
