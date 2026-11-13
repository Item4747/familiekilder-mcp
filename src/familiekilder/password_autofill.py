"""One attempt per login, scoped to exact credential origins and visible fields.

This helper never handles phone approval, OTPs, new passwords or signatures.
Unknown pages remain manual. Passwords travel in CDP memory, never argv or files.
"""

import json

from .core import FamiliekilderError
from .login_passwords import configured, get_password
from .sources import REGISTRY

ORIGINS = {
    "bankid": (
        "https://csfe.bankid.no",
        "https://auth.bankid.no",
        "https://login.bankid.no",
    ),
    **{name: s.password_origins for name, s in REGISTRY.items() if s.password_origins},
}

# Select a single visible, empty password field. Verify origin again in the
# document itself, including when a frame navigates between CDP calls.
_SELECTOR = r"""(() => {
  const allowed = ORIGINS;
  if (!allowed.includes(location.origin)) return null;
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const fields = [...document.querySelectorAll('input[type="password"]')].filter(visible);
  if (fields.length !== 1) return null;
  const e = fields[0];
  if (e.disabled || e.readOnly || e.value || e.autocomplete === 'new-password') return null;
  const text = document.body.innerText.toLowerCase();
  if (/signer|sign document|signering|endre passord|change password|nytt passord|new password|reset password/.test(text)) return null;
  if (BANKID && (!text.includes('bankid') || !/passord|password/.test(text))) return null;
  return e;
})()"""

_FILL = r"""function(password, allowed) {
  if (!allowed.includes(location.origin) || !this.isConnected || this.type !== 'password' ||
      this.disabled || this.readOnly || this.value || this.autocomplete === 'new-password' ||
      !this.getClientRects().length) return false;
  const e = this;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, password);
  e.dispatchEvent(new Event('input', {bubbles: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  const buttons = [...document.querySelectorAll('button, input[type="submit"]')].filter(b =>
    !b.disabled && b.getClientRects().length &&
    /^(neste|fortsett|logg inn|continue|next|sign in|log in)$/i.test((b.innerText || b.value || '').trim()) &&
    (!e.form || b.form === e.form));
  if (buttons.length === 1) buttons[0].click();
  return true;
}"""


class PasswordAutofill:
    def __init__(self, root, kind):
        self.root = root
        self.kind = kind
        self.enabled = configured(root, kind)
        self.attempted = False
        self._sessions = {}
        # Set on the first step and kept, so a later tab is never filled.
        self._page_ids = None

    def step(self, cdp):
        if not self.enabled or self.attempted:
            return False
        try:
            return self._step(cdp)
        except FamiliekilderError:
            raise
        except Exception:  # noqa: BLE001 - never expose secret-bearing backend errors
            # Do not expose CDP exception text, which can contain request values.
            self.attempted = True
            raise FamiliekilderError(
                "Automatisk passordutfylling stoppet; fullfør innloggingen manuelt"
            ) from None

    def _step(self, cdp):
        targets = cdp.call("Target.getTargets").get("targetInfos", [])
        if self._page_ids is None:
            pages = [t["targetId"] for t in targets if t.get("type") == "page"]
            if len(pages) != 1:
                return False
            self._page_ids = set(pages)
        frames = set()
        # Page trees establish ownership before inspecting out-of-process
        # iframes. An unrelated BankID tab/frame must never be selected.
        ordered = sorted(targets, key=lambda t: t.get("type") != "page")
        for target in ordered:
            if not (
                target.get("type") == "page" and target.get("targetId") in self._page_ids
            ) and not (
                target.get("type") == "iframe" and target.get("targetId") in frames
            ):
                continue
            target_id = target.get("targetId")
            if target_id not in self._sessions:
                self._sessions[target_id] = cdp.call(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )["sessionId"]
            session = self._sessions[target_id]
            tree = cdp.call("Page.getFrameTree", session_id=session).get(
                "frameTree", {}
            )
            pending = [tree]
            while pending:
                node = pending.pop()
                pending.extend(node.get("childFrames", []))
                frame = node.get("frame", {})
                frames.add(frame.get("id"))
                from .browser_bootstrap import _exact_origin

                if not any(
                    _exact_origin(frame.get("url"), origin)
                    for origin in ORIGINS[self.kind]
                ):
                    continue
                try:
                    context = cdp.call(
                        "Page.createIsolatedWorld",
                        {
                            "frameId": frame["id"],
                            "worldName": "familiekilder-password",
                        },
                        session_id=session,
                    )["executionContextId"]
                except FamiliekilderError:
                    # Remote frames need their own target attachment; frames
                    # navigating during discovery are checked on the next tick.
                    continue
                expression = _SELECTOR.replace(
                    "ORIGINS", json.dumps(ORIGINS[self.kind])
                ).replace("BANKID", str(self.kind == "bankid").lower())
                response = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "contextId": context,
                        "silent": True,
                    },
                    session_id=session,
                )
                obj = response.get("result", {}).get("objectId")
                if not obj or "exceptionDetails" in response:
                    continue
                try:
                    password = get_password(self.root, self.kind)
                    if password is None:
                        return False
                    # Mark BEFORE sending: uncertain outcomes must not replay.
                    self.attempted = True
                    result = cdp.call(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": obj,
                            "functionDeclaration": _FILL,
                            "arguments": [
                                {"value": password},
                                {"value": list(ORIGINS[self.kind])},
                            ],
                            "returnByValue": True,
                            "silent": True,
                            "userGesture": True,
                        },
                        session_id=session,
                    )
                    if "exceptionDetails" in result:
                        raise FamiliekilderError(
                            "Passordutfyllingen må fullføres manuelt"
                        )
                    return result.get("result", {}).get("value") is True
                finally:
                    cdp.call(
                        "Runtime.releaseObject", {"objectId": obj}, session_id=session
                    )
        return False
