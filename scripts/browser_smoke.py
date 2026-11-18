"""Start one real login browser against a local test page, then clean up.

    python scripts/browser_smoke.py --browser chrome

The same path as a login, without a login: a new data root in a temporary
folder, a disposable browser profile below its .private folder, the catalog's
choice and check of the browser, the DevTools pipe and the cleanup. The page
is served from 127.0.0.1 and contacts nothing else. The user's own data root
and browser profiles are not used.

Prints one JSON line and exits 0 when the browser started, DevTools answered
with the page title and the profile folder was gone afterwards.
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

TITLE = "Familiekilder røyktest"
PAGE = f"<!doctype html><meta charset=utf-8><title>{TITLE}</title><p>Oppdiktet side.".encode()


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        self.server.requests += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_args: object) -> None:
        pass


def _page_title(cdp, url: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    session = None
    while time.monotonic() < deadline:
        targets = cdp.call("Target.getTargets").get("targetInfos", [])
        pages = [
            target
            for target in targets
            if target.get("type") == "page" and str(target.get("url", "")).startswith(url)
        ]
        if pages:
            if session is None:
                attached = cdp.call(
                    "Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}
                )
                session = attached["sessionId"]
            result = cdp.call(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
                session_id=session,
            )
            title = result.get("result", {}).get("value")
            if title == TITLE:
                return title
        time.sleep(0.2)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", choices=("brave", "chrome", "edge"))
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    from familiekilder import browser_bootstrap
    from familiekilder.browsers import select_browser

    report: dict[str, object] = {"requested": args.browser}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    server.requests = 0  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    root = Path(tempfile.mkdtemp(prefix="familiekilder-smoke-"))
    browser = None
    run_path = None
    ok = False
    try:
        chosen = select_browser(args.browser)
        report.update(browser=chosen.app.id, executable=chosen.executable)
        run_path = browser_bootstrap._create_run_directory(root)
        started = time.monotonic()
        browser = browser_bootstrap._launch_brave(run_path, url, None, browser=args.browser)
        version = browser.cdp.call("Browser.getVersion", timeout=args.timeout)
        report["product"] = version.get("product")
        report["title"] = _page_title(browser.cdp, url, args.timeout)
        report["page_requests"] = server.requests  # type: ignore[attr-defined]
        report["seconds"] = round(time.monotonic() - started, 1)
        browser.close()
        browser = None
        browser_bootstrap._cleanup_run_directory(root, run_path)
        report["profile_removed"] = not run_path.exists()
        left = list((root / ".private" / "browser-bootstrap").iterdir())
        report["left_in_bootstrap_folder"] = len(left)
        ok = report["title"] == TITLE and report["profile_removed"] is True and not left
    except Exception as error:  # noqa: BLE001 - the report shows the failure
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        server.shutdown()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
