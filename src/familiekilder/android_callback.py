"""Capture an OAuth app redirect even when Chrome blocks the final navigation.

Only the new login target is observed. No request bodies, cookies or network log
are retained. Normal provider callback validation (including PKCE) still applies.
"""

import hmac
import queue
from urllib.parse import parse_qs, urlsplit


class CallbackCapture:
    def __init__(self, redirect_uri, state, callbacks):
        self.redirect = urlsplit(redirect_uri)
        self.state = state
        self.callbacks = callbacks
        self.session_id = None

    def valid(self, candidate):
        if not isinstance(candidate, str) or not 0 < len(candidate) <= 8192:
            return False
        if any(ord(char) <= 32 or ord(char) == 127 for char in candidate):
            return False
        try:
            value = urlsplit(candidate)
            params = parse_qs(value.query, keep_blank_values=True, max_num_fields=20)
            states = params.get("state", [])
            return (
                value.scheme == self.redirect.scheme
                and value.netloc == self.redirect.netloc
                # Match the provider validator: Android can add a root slash
                # to an authority-only app URI. Other paths are never accepted.
                and ("" if value.path == "/" else value.path)
                == ("" if self.redirect.path == "/" else self.redirect.path)
                and value.username is None
                and value.password is None
                and value.port is None
                and not value.fragment
                and len(states) == 1
                and hmac.compare_digest(states[0].encode(), self.state.encode())
                and (
                    (len(params.get("code", [])) == 1 and bool(params["code"][0]) and "error" not in params)
                    or (len(params.get("error", [])) == 1 and bool(params["error"][0]) and "code" not in params)
                )
            )
        except (ValueError, UnicodeError):
            return False

    def accept(self, candidate):
        if not self.valid(candidate):
            return False
        try:
            self.callbacks.put_nowait(candidate)
        except queue.Full:
            pass  # The receiver and CDP can observe the same redirect.
        return True

    def event(self, event):
        if self.session_id is None or event.get("sessionId") != self.session_id:
            return
        params = event.get("params", {})
        method = event.get("method")
        if method == "Network.responseReceivedExtraInfo":
            headers = params.get("headers", {})
        elif method == "Network.requestWillBeSent":
            headers = params.get("redirectResponse", {}).get("headers", {})
            self.accept(params.get("request", {}).get("url"))
        elif method == "Page.frameRequestedNavigation":
            self.accept(params.get("url"))
            return
        else:
            return
        for key, value in headers.items():
            if key.lower() == "location":
                self.accept(value)
