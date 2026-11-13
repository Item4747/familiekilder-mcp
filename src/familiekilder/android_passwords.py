"""Private ADB/CDP bridge for one newly created Android Chrome login tab."""

import hashlib
import http.client
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from .core import FamiliekilderError
from .password_autofill import PasswordAutofill


def claim_device(root, serial, *, bind=False):
    """Never reuse an Android browser's cookies between personal profiles.

    The registry is per OS user, deliberately independent of --root/--profile.
    A different profile needs its own device/emulator, or a deliberately reset
    device and registry entry. There is no automatic reassignment.
    """
    from .login_passwords import account
    from .private_lock import private_named_lock
    from .private_state import atomic_write_private_json, read_private_json

    registry_root = Path.home() / "Library/Application Support/familiekilder-mcp"
    registry_root.mkdir(parents=True, exist_ok=True)
    path = registry_root / ".private/android-device-profiles.json"
    key = hashlib.sha256(serial.encode()).hexdigest()
    identity = account(root, "bankid")
    with private_named_lock(registry_root, "android-devices", wait=True):
        bindings = dict(read_private_json(path)) if path.exists() else {}
        if key in bindings and bindings[key] != identity:
            raise FamiliekilderError(
                "Android-enheten tilhører en annen profil. Bruk en egen enhet/emulator for denne profilen."
            )
        if key not in bindings:
            if not bind:
                raise FamiliekilderError(
                    "Bind din egen Android-enhet til profilen først med android-device bind --adb-serial ENHET"
                )
            bindings[key] = identity
            atomic_write_private_json(path, bindings)


class _SocketCdp:
    def __init__(self, socket, event_handler=None):
        self.socket = socket
        self.sequence = 0
        self.event_handler = event_handler

    def _receive(self, timeout):
        self.socket.settimeout(timeout)
        raw = self.socket.recv()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError
        response = json.loads(raw)
        if self.event_handler and "method" in response:
            self.event_handler(response)
        return response

    def poll(self, timeout=0.05):
        from websocket import WebSocketTimeoutException

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                self._receive(max(0.001, deadline - time.monotonic()))
        except (WebSocketTimeoutException, TimeoutError):
            return
        except Exception:
            raise FamiliekilderError("Android-innloggingshjelperen mistet forbindelsen") from None

    def call(self, method, params=None, *, session_id=None, timeout=10):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        try:
            self.socket.send(json.dumps(message))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = self._receive(max(0.01, deadline - time.monotonic()))
                if response.get("id") != self.sequence:
                    continue
                if "error" in response:
                    break
                return response.get("result", {})
        except Exception:  # noqa: BLE001, S110 - never expose secret-bearing backend errors
            pass
        raise FamiliekilderError("Android-passordhjelperen mistet forbindelsen")


class AndroidPasswordSession:
    def __init__(self, root, adb, serial):
        self.autofill = PasswordAutofill(root, "bankid")
        self.adb, self.serial = adb, serial
        self.port = None
        self.socket = None
        self.cdp = None
        self.target_id = None

    def start(self, authorization_url, *, callback_capture=None):
        if not self.autofill.enabled and callback_capture is None:
            return False
        from websocket import create_connection

        from .android_oauth import _adb

        navigating = False
        try:
            # Existing Chrome tabs are never used for password entry. The CDP
            # channel must already be available; otherwise use the manual flow.
            port = _adb(
                self.adb,
                self.serial,
                "forward",
                "tcp:0",
                "localabstract:chrome_devtools_remote",
                capture=True,
            ).strip()
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError
            self.port = int(port)
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.request("GET", "/json/version")
                response = connection.getresponse()
                raw = response.read(65537)
                if response.status != 200 or len(raw) > 65536:
                    raise ValueError
                address = json.loads(raw)["webSocketDebuggerUrl"]
            finally:
                connection.close()
            parts = urlsplit(address)
            if (
                parts.scheme != "ws"
                or parts.hostname not in {"127.0.0.1", "localhost"}
                or parts.port != self.port
                or parts.username
                or parts.password
                or not (
                    parts.path == "/devtools/browser"
                    or parts.path.startswith("/devtools/browser/")
                )
                or parts.query
                or parts.fragment
            ):
                raise ValueError
            self.socket = create_connection(
                address,
                timeout=3,
                suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost"],
            )
            self.cdp = _SocketCdp(
                self.socket, callback_capture.event if callback_capture else None
            )
            # Android Chrome does not support createBrowserContext. The device
            # is exclusively bound to this profile before login starts.
            self.target_id = self.cdp.call(
                "Target.createTarget", {"url": "about:blank"}
            )["targetId"]
            self.autofill.page_ids = {self.target_id}
            session = self.cdp.call(
                "Target.attachToTarget", {"targetId": self.target_id, "flatten": True}
            )["sessionId"]
            self.autofill._sessions[self.target_id] = session
            if callback_capture:
                callback_capture.session_id = session
                self.cdp.call("Network.enable", session_id=session)
                self.cdp.call("Page.enable", session_id=session)
            # Install the listener BEFORE navigating: an existing ID-porten
            # session may immediately redirect, without any user interaction.
            navigating = True
            self.cdp.call("Page.navigate", {"url": authorization_url}, session_id=session)
            return True
        except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
            self.close()
            if navigating:
                raise FamiliekilderError(
                    "Android-forbindelsen brøt under oppstart; start en ny innlogging"
                ) from None
            return False

    def step(self):
        if self.cdp is not None:
            from .browser_bootstrap import _password_step

            self.cdp.poll()
            _password_step(self.autofill, self.cdp)

    def close(self):
        from .android_oauth import _adb

        if self.socket is not None:
            try:
                if self.cdp is not None and self.target_id:
                    self.cdp.call("Target.closeTarget", {"targetId": self.target_id})
            except Exception:  # noqa: BLE001, S110 - never expose secret-bearing backend errors
                pass
            try:
                self.socket.close()
            except Exception:  # noqa: BLE001, S110 - never expose secret-bearing backend errors
                pass
            self.socket = None
        self.cdp = None
        if self.port is not None:
            try:
                _adb(self.adb, self.serial, "forward", "--remove", f"tcp:{self.port}")
            finally:
                self.port = None
