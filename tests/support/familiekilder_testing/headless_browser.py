"""A headless Chromium-family browser for tests that need real browser rules.

Some rules exist only in a browser, such as a page's Content-Security-Policy
applied to the redirect after a form submission. An HTTP client in a test
does not enforce them. This module starts Brave or Chrome headless with a new
temporary profile and drives it over the DevTools pipe, never a TCP port. It
never uses the user's own profile and opens no window.

Only the macOS app locations that the browser bootstrap knows are used. On
other systems, or when neither browser is installed, `find_browser` returns
None and the test should skip.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from familiekilder import browser_bootstrap
from familiekilder.browser_bootstrap import BrowserBootstrapError


def find_browser() -> Path | None:
    """The first installed Brave or Chrome on macOS, or None."""

    if sys.platform != "darwin":
        return None
    for candidate in (*browser_bootstrap._BRAVE_PATHS, *browser_bootstrap._CHROME_PATHS):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _arguments(executable: Path, profile: Path) -> list[str]:
    return [
        str(executable),
        f"--user-data-dir={profile}",
        "--headless",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-background-mode",
        "--disable-crash-reporter",
        "--disable-breakpad",
        # Never ask the macOS keychain for the browser's storage key.
        "--use-mock-keychain",
        "--password-store=basic",
        "about:blank",
    ]


class HeadlessBrowser:
    """One headless browser with one page, for the length of a `with` block."""

    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._cdp: Any = None
        self._session: str | None = None

    def __enter__(self) -> HeadlessBrowser:
        self._profile = tempfile.TemporaryDirectory(prefix="familiekilder-headless-")
        try:
            self._launch(Path(self._profile.name))
            target = self._cdp.call("Target.createTarget", {"url": "about:blank"})
            attached = self._cdp.call(
                "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
            )
            self._session = attached["sessionId"]
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _launch(self, profile: Path) -> None:
        # The same pipe setup as the browser bootstrap: the browser reads
        # DevTools requests on descriptor 3 and writes replies on 4. The
        # child ends are moved above 10 so the wrapper's dup2 cannot clash.
        raw_read, parent_write = os.pipe()
        parent_read, raw_write = os.pipe()
        child_read = browser_bootstrap._duplicate_child_descriptor(raw_read)
        child_write = browser_bootstrap._duplicate_child_descriptor(raw_write)
        os.close(raw_read)
        os.close(raw_write)
        try:
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    browser_bootstrap._PIPE_EXEC_WRAPPER,
                    str(child_read),
                    str(child_write),
                    *_arguments(self.executable, profile),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child_read, child_write),
                start_new_session=True,
            )
        except BaseException:
            os.close(parent_write)
            os.close(parent_read)
            raise
        finally:
            os.close(child_read)
            os.close(child_write)
        self._cdp = browser_bootstrap._PipeCdpClient(parent_write, parent_read)

    def navigate(self, url: str) -> None:
        self._cdp.call("Page.navigate", {"url": url}, session_id=self._session)

    def evaluate(self, expression: str) -> Any:
        result = self._cdp.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session_id=self._session,
        )
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, *, timeout: float = 15.0) -> Any:
        """Evaluate until the value is truthy. Navigation may interrupt a call."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.evaluate(expression)
            except BrowserBootstrapError:
                value = None
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"The page never satisfied: {expression}")
            time.sleep(0.05)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                try:
                    self._cdp.call("Browser.close", timeout=3.0)
                except Exception:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        if self._profile is not None:
            self._profile.cleanup()
            self._profile = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        self.server.received.put(self.path)  # type: ignore[attr-defined]
        body = b"Oppdiktet assistent fikk svaret."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@contextlib.contextmanager
def callback_server() -> Iterator[tuple[str, queue.Queue[str]]]:
    """A local stand-in for an assistant's OAuth callback.

    Yields its base URL (its own port on 127.0.0.1, so its own origin) and a
    queue with the path and query of every GET it receives.
    """

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.received = queue.Queue()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.received  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
