"""A fake Spond and a network block inside the install trial's own environment.

scripts/stdio_trial.py writes a startup file (.pth) and a small module into
site-packages of the trial's throwaway virtual environment, the
`.private/venv` of the copied project. Every Python started from that
environment then calls `activate()` here. The installed package has no switch
for this: without the startup file nothing in this module runs, and the
startup file exists only in the trial's own copy.

`activate()` does three things:

- blocks the network: name lookups and connections to anything other than
  this machine raise OSError and print NETWORK_BLOCKED to stderr;
- replaces `_default_transport` in `familiekilder.spond` and
  `familiekilder.spond_auth`, so the real Spond readers, login checks and
  MCP tools talk to the shared fake transports in this package, which answer
  with the fictional content in the fixture file, and print each requested
  path after REQUEST on stderr;
- prints a line with Norwegian characters to stderr when the MCP server module
  loads, the way a warning from the server would.

`seed()` stores the built-in Spond setup the wizard would store and imports a
fictional session through `spond_auth.import_session`, which checks it
against the fake profile endpoint.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import ipaddress
import json
import socket
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

# ASCII markers on stderr, so the trial finds them in any encoding.
ACTIVE = "@@trial-active"
NETWORK_BLOCKED = "@@trial-network-blocked"
UNKNOWN_ADDRESS = "@@trial-unknown-address"
NORWEGIAN_STDERR = "@@trial-norwegian-stderr"
REQUEST = "@@trial-request"

_fixture: dict[str, Any] = {}
_collector = None
_auth = None


def _say(marker: str, text: str = "") -> None:
    line = f"{marker} {text}".rstrip()
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - stderr must never stop the server
        pass


# --- Network block ---------------------------------------------------------


def _loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _refuse(host: Any) -> None:
    _say(NETWORK_BLOCKED, repr(host))
    raise OSError("install trial: the network is blocked")


def _block_network() -> None:
    if getattr(socket, "_familiekilder_trial_blocked", False):
        return
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _loopback(host):
            _refuse(host)
        return original_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host):
        if not _loopback(host):
            _refuse(host)
        return original_gethostbyname(host)

    def gethostbyname_ex(host):
        if not _loopback(host):
            _refuse(host)
        return original_gethostbyname_ex(host)

    def checked(sock, address) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
            _refuse(address[0])

    def connect(self, address):
        checked(self, address)
        return original_connect(self, address)

    def connect_ex(self, address):
        checked(self, address)
        return original_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket._familiekilder_trial_blocked = True


# --- Fake Spond ------------------------------------------------------------


def _not_found(url: str):
    from familiekilder.api_transport import ApiHTTPError

    _say(UNKNOWN_ADDRESS, urllib.parse.urlsplit(url).path)
    return ApiHTTPError(404)


def _answer(url: str) -> Any:
    """The fake Spond API: the fixture's fictional profile, group and post."""

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    _say(REQUEST, path)
    post = _fixture["post"]
    if path == "/core/v1/profile":
        return _fixture["profile"]
    if path == "/core/v1/groups":
        return _fixture["groups"]
    if path == "/core/v1/posts":
        return [post] if query.get("type") == ["PLAIN"] else []
    if path == f"/core/v1/posts/{post['id']}":
        return post
    if path in {"/core/v1/sponds", "/core/v1/activities/summary"}:
        return []
    if path == f"/core/v1/group/{post['groupId']}/filesToken":
        return {"value": _fixture["files_token"]}
    if path == f"/storage/upload/group/{_fixture['file_id']}":
        if query.get("auth") != [_fixture["files_token"]]:
            raise _not_found(url)
        return {"url": _fixture["storage_url"]}
    raise _not_found(url)


def _collector_transport():
    from familiekilder.api_transport import DownloadResult
    from familiekilder_testing.spond import FakeCollectorTransport

    class TrialCollectorTransport(FakeCollectorTransport):
        """The shared fake, with the fixture's attachment instead of b"fixture"."""

        def download(self, url, target, **options):
            self.downloads.append(
                (
                    url,
                    Path(target),
                    options.get("bearer_token"),
                    options.get("headers"),
                    options.get("max_bytes"),
                )
            )
            _say(REQUEST, "download " + urllib.parse.urlsplit(url).path)
            if url != _fixture["storage_url"]:
                raise _not_found(url)
            payload = _fixture["attachment_text"].encode("utf-8")
            path = Path(target)
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o600)
            return DownloadResult(
                len(payload), hashlib.sha256(payload).hexdigest(), "text/plain; charset=utf-8"
            )

    return TrialCollectorTransport(_answer)


def _auth_transport():
    from familiekilder_testing.spond_auth import FakeAuthTransport

    # A refresh is never expected: the fictional access token is valid.
    return FakeAuthTransport(profile=dict(_fixture["profile"]), refresh=OSError("no refresh"))


def _patch_spond(module) -> None:
    def default_transport(_config):
        global _collector
        if _collector is None:
            _collector = _collector_transport()
        return _collector

    module._default_transport = default_transport


def _patch_spond_auth(module) -> None:
    def default_transport(_config):
        global _auth
        if _auth is None:
            _auth = _auth_transport()
        return _auth

    module._default_transport = default_transport


def _announce_server(_module) -> None:
    _say(NORWEGIAN_STDERR, _fixture["stderr_text"])


_PATCHES: dict[str, Callable[[Any], None]] = {
    "familiekilder.spond": _patch_spond,
    "familiekilder.spond_auth": _patch_spond_auth,
    "familiekilder.source_server": _announce_server,
}


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader, patch: Callable[[Any], None]) -> None:
        self.loader = loader
        self.patch = patch

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module) -> None:
        self.loader.exec_module(module)
        self.patch(module)


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Patch the named modules right after they load, wherever they are found."""

    def find_spec(self, name, path=None, target=None):
        patch = _PATCHES.get(name)
        if patch is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, patch)
        return spec


def activate(fixture_path: str) -> None:
    """Block the network and route Spond to the fake; see the module docstring."""

    global _fixture
    if any(isinstance(finder, _PatchingFinder) for finder in sys.meta_path):
        return
    _fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    _block_network()
    sys.meta_path.insert(0, _PatchingFinder())
    for name, patch in _PATCHES.items():
        if name in sys.modules:
            patch(sys.modules[name])
    _say(ACTIVE)


def seed(root: str) -> Mapping[str, Any]:
    """Store the built-in Spond setup and import the fictional session."""

    from familiekilder import spond_auth
    from familiekilder.client_defaults import default_client_config, store_default
    from familiekilder.private_state import atomic_write_private_json

    data_root = Path(root)
    store_default(data_root, "spond", default_client_config("spond"), module=spond_auth)
    export = data_root / ".private" / "prøveimport" / "spond-økt.json"
    atomic_write_private_json(export, _fixture["session_export"])
    try:
        return spond_auth.import_session(data_root, export)
    finally:
        export.unlink(missing_ok=True)
