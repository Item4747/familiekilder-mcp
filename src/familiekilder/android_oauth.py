from __future__ import annotations

import contextlib
import http.server
import math
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable

from .core import FamiliekilderError


RECEIVER_PACKAGE = "no.familiekilder.oauthreceiver"
VIGILO_PACKAGE = "parent.vigilo.no.parentapplication"
VIGILO_REDIRECT_URI = "app://ch-parent-android.vigilo.no"
SKOLEMELDING_PACKAGE = "com.cgi.oslomelding.foresatte"
SKOLEMELDING_REDIRECT_URI = "com.cgi.oslomelding.foresatte://oauth"
DEVICE_BRIDGE_PORT = 38443
MAX_CALLBACK_BYTES = 8192
CALLBACK_TIMEOUT_SECONDS = 30 * 60


class AndroidOAuthError(FamiliekilderError):
    pass


@contextlib.contextmanager
def _android_login_lock():
    # Android's callback receiver and reverse port are shared across providers
    # and profiles. Data reads remain independently locked per provider.
    from .private_lock import private_named_lock

    registry_root = Path.home() / "Library/Application Support/familiekilder-mcp"
    registry_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with private_named_lock(registry_root, "android-devices"):
        yield


def _receiver_sources() -> Path:
    return Path(__file__).resolve().parent / "data" / "android-oauth-receiver"


def _private_build_dir(root: Path) -> Path:
    target = root / ".private" / "android-oauth-receiver"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise AndroidOAuthError("OAuthReceiver-katalogen er utrygg")
    target.chmod(0o700)
    return target


def _first_executable(candidates: list[Path | str], name: str) -> Path:
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered).resolve()
    raise AndroidOAuthError(f"Android-verktøyet {name} er ikke installert")


def _android_sdk_roots() -> list[Path]:
    roots = [
        Path(value).expanduser()
        for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
        if (value := os.environ.get(name))
    ]
    roots.extend(
        [
            Path.home() / "Android" / "Sdk",
            Path.home() / "Library" / "Android" / "sdk",
            Path("/opt/homebrew/share/android-commandlinetools"),
        ]
    )
    return list(dict.fromkeys(roots))


def _android_tools() -> dict[str, Path]:
    sdk_roots = _android_sdk_roots()
    build_roots = [root / "build-tools" / "36.1.0" for root in sdk_roots]
    return {
        "adb": _first_executable(
            [root / "platform-tools" / "adb" for root in sdk_roots], "adb"
        ),
        "aapt2": _first_executable(
            [root / "aapt2" for root in build_roots], "aapt2"
        ),
        "d8": _first_executable([root / "d8" for root in build_roots], "d8"),
        "zipalign": _first_executable(
            [root / "zipalign" for root in build_roots], "zipalign"
        ),
        "apksigner": _first_executable(
            [root / "apksigner" for root in build_roots], "apksigner"
        ),
        "javac": _first_executable([], "javac"),
        "keytool": _first_executable([], "keytool"),
        "zip": _first_executable([Path("/usr/bin/zip")], "zip"),
    }


def _android_jar() -> Path:
    candidates = [
        root / "platforms" / "android-35" / "android.jar"
        for root in _android_sdk_roots()
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AndroidOAuthError("Android Platform 35 er ikke installert")


def _run_build(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        raise AndroidOAuthError("OAuthReceiver kunne ikke bygges sikkert") from None


def build_receiver_apk(root: Path | str) -> Path:
    private_root = _private_build_dir(Path(root).resolve())
    apk = private_root / "familiekilder-oauth-receiver.apk"
    keystore = private_root / "receiver.p12"
    sources = _receiver_sources()
    manifest = sources / "AndroidManifest.xml"
    java_source = (
        sources
        / "src"
        / "no"
        / "familiekilder"
        / "oauthreceiver"
        / "MainActivity.java"
    )
    if not manifest.is_file() or not java_source.is_file():
        raise AndroidOAuthError("OAuthReceiver-kildekoden mangler")

    tools = _android_tools()
    android_jar = _android_jar()
    if not keystore.exists():
        _run_build(
            [
                str(tools["keytool"]),
                "-genkeypair",
                "-noprompt",
                "-storetype",
                "PKCS12",
                "-keystore",
                str(keystore),
                "-storepass",
                "android",
                "-keypass",
                "android",
                "-alias",
                "receiver",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-validity",
                "3650",
                "-dname",
                "CN=Familiekilder OAuthReceiver",
            ]
        )
        keystore.chmod(0o600)

    with tempfile.TemporaryDirectory(prefix="build-", dir=private_root) as temporary:
        work = Path(temporary)
        unsigned = work / "receiver-unsigned.apk"
        aligned = work / "receiver-aligned.apk"
        classes = work / "classes"
        dex = work / "dex"
        classes.mkdir(mode=0o700)
        dex.mkdir(mode=0o700)
        _run_build(
            [
                str(tools["aapt2"]),
                "link",
                "-I",
                str(android_jar),
                "--manifest",
                str(manifest),
                "--min-sdk-version",
                "26",
                "--target-sdk-version",
                "35",
                "--version-code",
                "1",
                "--version-name",
                "1.0",
                "-o",
                str(unsigned),
            ]
        )
        _run_build(
            [
                str(tools["javac"]),
                "-source",
                "8",
                "-target",
                "8",
                "-classpath",
                str(android_jar),
                "-d",
                str(classes),
                str(java_source),
            ]
        )
        class_files = sorted(str(path) for path in classes.rglob("*.class"))
        if not class_files:
            raise AndroidOAuthError("OAuthReceiver-kompileringen ga ingen klasser")
        _run_build(
            [
                str(tools["d8"]),
                "--lib",
                str(android_jar),
                "--min-api",
                "26",
                "--output",
                str(dex),
                *class_files,
            ]
        )
        _run_build(
            [
                str(tools["zip"]),
                "-q",
                "-j",
                str(unsigned),
                str(dex / "classes.dex"),
            ]
        )
        _run_build(
            [
                str(tools["zipalign"]),
                "-f",
                "4",
                str(unsigned),
                str(aligned),
            ]
        )
        _run_build(
            [
                str(tools["apksigner"]),
                "sign",
                "--ks",
                str(keystore),
                "--ks-key-alias",
                "receiver",
                "--ks-pass",
                "pass:android",
                "--key-pass",
                "pass:android",
                "--out",
                str(apk),
                str(aligned),
            ]
        )
        _run_build([str(tools["apksigner"]), "verify", "--verbose", str(apk)])
    apk.chmod(0o600)
    return apk


def _adb(
    adb: Path,
    serial: str,
    *arguments: str,
    capture: bool = False,
    timeout: float = 30,
) -> str:
    try:
        result = subprocess.run(
            [str(adb), "-s", serial, *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise AndroidOAuthError("Android-enheten svarte ikke som forventet") from None
    return result.stdout if capture else ""


def _select_device(adb: Path, requested: str | None) -> str:
    try:
        result = subprocess.run(
            [str(adb), "devices"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        raise AndroidOAuthError("ADB kunne ikke finne Android-enheten") from None
    devices = [
        line.split("\t", 1)[0]
        for line in result.stdout.splitlines()[1:]
        if line.endswith("\tdevice")
    ]
    if requested:
        if requested not in devices:
            raise AndroidOAuthError("Den valgte Android-enheten er ikke tilgjengelig")
        return requested
    if len(devices) != 1:
        raise AndroidOAuthError(
            "Koble til nøyaktig én autorisert Android-enhet eller bruk --adb-serial"
        )
    return devices[0]


def _view_command(authorization_url: str) -> str:
    return shlex.join(
        [
            "am",
            "start",
            "-n",
            f"{RECEIVER_PACKAGE}/.MainActivity",
            "-a",
            f"{RECEIVER_PACKAGE}.AUTHORIZE",
            "-d",
            authorization_url,
        ]
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    callback_queue: queue.Queue[str]
    expected_scheme: str
    expected_host: str
    callback_capture: object

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/oauth-callback":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_CALLBACK_BYTES:
            self.send_error(400)
            return
        self.connection.settimeout(5)
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            return
        try:
            callback = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            self.send_error(400)
            return
        if not self.callback_capture.valid(callback):
            self.send_error(400)
            return
        try:
            self.callback_queue.put_nowait(callback)
        except queue.Full:
            self.send_error(409)
            return
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@_android_login_lock()
def capture_callback_android(
    root: Path | str,
    *,
    source: str,
    authorization_url: str,
    adb_serial: str | None = None,
    timeout_seconds: float = CALLBACK_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    source_contracts = {
        "vigilo": {
            "package": VIGILO_PACKAGE,
            "authorization_host": "auth.prod.vigilo-oas.no",
            "authorization_path": "/connect/authorize",
            "redirect_uri": VIGILO_REDIRECT_URI,
        },
        "skolemelding": {
            "package": SKOLEMELDING_PACKAGE,
            "authorization_host": "login.idporten.no",
            "authorization_path": "/authorize",
            "redirect_uri": SKOLEMELDING_REDIRECT_URI,
        },
    }
    contract = source_contracts.get(source)
    if contract is None or not isinstance(authorization_url, str):
        raise AndroidOAuthError("Android-autorisasjonskilden er ugyldig")
    try:
        authorization = urllib.parse.urlsplit(authorization_url)
        redirect = urllib.parse.urlsplit(str(contract["redirect_uri"]))
        authorization_port = authorization.port
    except ValueError:
        raise AndroidOAuthError("Android-autorisasjonslenken er ugyldig") from None
    if (
        authorization.scheme != "https"
        or authorization.hostname != contract["authorization_host"]
        or authorization.path != contract["authorization_path"]
        or not authorization.query
        or authorization.username is not None
        or authorization.password is not None
        or authorization_port not in (None, 443)
        or authorization.fragment
    ):
        raise AndroidOAuthError("Android-autorisasjonslenken er ugyldig")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > CALLBACK_TIMEOUT_SECONDS
    ):
        raise AndroidOAuthError("OAuthReceiver-timeout er ugyldig")

    query = urllib.parse.parse_qs(authorization.query, keep_blank_values=True)
    if (
        len(query.get("state", [])) != 1
        or not query["state"][0]
        or query.get("redirect_uri") != [contract["redirect_uri"]]
    ):
        raise AndroidOAuthError("Android-autorisasjonslenken mangler entydig state/returadresse")

    if cancelled is not None and cancelled():
        raise AndroidOAuthError("Innloggingen ble avbrutt")
    tools = _android_tools()
    adb = tools["adb"]
    serial = _select_device(adb, adb_serial)
    from .android_passwords import claim_device

    claim_device(root, serial)
    apk = build_receiver_apk(root)
    callbacks: queue.Queue[str] = queue.Queue(maxsize=1)
    from .android_callback import CallbackCapture

    capture = CallbackCapture(contract["redirect_uri"], query["state"][0], callbacks)

    class Handler(_CallbackHandler):
        callback_queue = callbacks
        expected_scheme = redirect.scheme
        expected_host = redirect.hostname or ""
        callback_capture = capture

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    host_port = server.server_address[1]
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="android-oauth-loopback",
        daemon=True,
    )
    server_thread.start()
    app_was_enabled = False
    receiver_installed = False
    reverse_installed = False
    from .android_passwords import AndroidPasswordSession

    passwords = AndroidPasswordSession(root, adb, serial)

    try:
        official_package = str(contract["package"])
        installed = _adb(
            adb,
            serial,
            "shell",
            "pm",
            "list",
            "packages",
            official_package,
            capture=True,
        )
        if f"package:{official_package}" in installed:
            enabled = _adb(
                adb,
                serial,
                "shell",
                "pm",
                "list",
                "packages",
                "-e",
                official_package,
                capture=True,
            )
            app_was_enabled = f"package:{official_package}" in enabled
            if app_was_enabled:
                _adb(
                    adb,
                    serial,
                    "shell",
                    "pm",
                    "disable-user",
                    "--user",
                    "0",
                    official_package,
                )

        _adb(adb, serial, "install", "-r", "--no-streaming", str(apk), timeout=120)
        receiver_installed = True
        resolved = _adb(
            adb,
            serial,
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            str(contract["redirect_uri"]),
            capture=True,
        )
        if RECEIVER_PACKAGE not in resolved:
            raise AndroidOAuthError("OAuthReceiver eier ikke Android-callbacken")

        _adb(
            adb,
            serial,
            "reverse",
            f"tcp:{DEVICE_BRIDGE_PORT}",
            f"tcp:{host_port}",
        )
        reverse_installed = True
        # CDP exists only while Chrome is running. Warm it with a non-secret
        # blank page; the actual OAuth URL goes to our new target over CDP.
        with contextlib.suppress(AndroidOAuthError):
            _adb(adb, serial, "shell", "am", "start", "-a",
                 "android.intent.action.VIEW", "-d", "about:blank",
                 "-p", "com.android.chrome")
        if not passwords.start(authorization_url, callback_capture=capture):
            if passwords.autofill.enabled:
                print("Android-passordhjelperen er utilgjengelig; fullfør passordsteget manuelt.")
            _adb(adb, serial, "shell", _view_command(authorization_url))

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise AndroidOAuthError("Innloggingen ble avbrutt")
            passwords.step()
            try:
                return callbacks.get(timeout=min(1.0, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
        raise AndroidOAuthError("Android-innloggingen rakk ikke å levere callbacken")
    finally:
        with contextlib.suppress(FamiliekilderError):
            passwords.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        if reverse_installed:
            with contextlib.suppress(AndroidOAuthError):
                _adb(
                    adb,
                    serial,
                    "reverse",
                    "--remove",
                    f"tcp:{DEVICE_BRIDGE_PORT}",
                )
        if receiver_installed:
            with contextlib.suppress(AndroidOAuthError):
                _adb(adb, serial, "uninstall", RECEIVER_PACKAGE, timeout=60)
        if app_was_enabled:
            with contextlib.suppress(AndroidOAuthError):
                _adb(adb, serial, "shell", "pm", "enable", official_package)


def capture_vigilo_callback_android(
    root: Path | str,
    *,
    authorization_url: str,
    adb_serial: str | None = None,
    timeout_seconds: float = CALLBACK_TIMEOUT_SECONDS,
) -> str:
    return capture_callback_android(
        root,
        source="vigilo",
        authorization_url=authorization_url,
        adb_serial=adb_serial,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "AndroidOAuthError",
    "build_receiver_apk",
    "capture_callback_android",
    "capture_vigilo_callback_android",
]
