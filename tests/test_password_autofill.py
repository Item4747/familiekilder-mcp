import unittest
from unittest.mock import patch

from familiekilder.core import FamiliekilderError
from familiekilder.password_autofill import PasswordAutofill


class Cdp:
    def __init__(self, url="https://csfe.bankid.no/login", field=True):
        self.url, self.field = url, field
        self.calls = []
        self.fail_write = False
        self.target = "login-page"

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": self.target}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame", "url": self.url}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 1}
        if method == "Runtime.evaluate":
            return {
                "result": {"objectId": "password-input"}
                if self.field
                else {"value": None}
            }
        if method == "Runtime.callFunctionOn":
            if self.fail_write:
                raise RuntimeError("must-not-leak-secret")
            return {"result": {"value": True}}
        return {}


class PasswordAutofillTests(unittest.TestCase):
    def setUp(self):
        self.configured = patch(
            "familiekilder.password_autofill.configured", return_value=True
        )
        self.configured.start()
        self.addCleanup(self.configured.stop)
        self.read = patch(
            "familiekilder.password_autofill.get_password", return_value="secret"
        )
        self.get = self.read.start()
        self.addCleanup(self.read.stop)

    def test_exact_bankid_frame_and_one_attempt_only(self):
        helper, cdp = PasswordAutofill("/owner", "bankid"), Cdp()
        self.assertTrue(helper.step(cdp))
        self.assertFalse(helper.step(cdp))
        self.get.assert_called_once_with("/owner", "bankid")
        self.assertEqual(sum(m == "Runtime.callFunctionOn" for m, _ in cdp.calls), 1)

    def test_never_gets_password_for_foreign_or_deceptive_origins(self):
        for url in (
            "http://csfe.bankid.no/login",
            "https://csfe.bankid.no.evil.test",
            "https://csfe.bankid.no@evil.test",
            "https://csfe.bankid.no:8443",
            "https://spond.com",
            "data:text/html,password",
        ):
            with self.subTest(url=url):
                self.assertFalse(PasswordAutofill("/owner", "bankid").step(Cdp(url)))
        self.get.assert_not_called()

    def test_no_password_before_password_field_is_ready(self):
        helper, cdp = PasswordAutofill("/owner", "bankid"), Cdp(field=False)
        self.assertFalse(helper.step(cdp))
        self.get.assert_not_called()
        cdp.field = True
        self.assertTrue(helper.step(cdp))

    def test_uncertain_write_never_retries_or_displays_secret(self):
        helper, cdp = PasswordAutofill("/owner", "bankid"), Cdp()
        cdp.fail_write = True
        with self.assertRaises(FamiliekilderError) as error:
            helper.step(cdp)
        self.assertNotIn("must-not-leak-secret", str(error.exception))
        self.assertFalse(helper.step(cdp))
        self.get.assert_called_once()

    def test_android_cannot_fill_an_existing_tab(self):
        helper = PasswordAutofill("/owner", "bankid", page_ids={"new-private-tab"})
        self.assertFalse(helper.step(Cdp()))
        self.get.assert_not_called()

    def test_service_password_is_never_bankid_password(self):
        self.assertFalse(PasswordAutofill("/owner", "spond").step(Cdp()))
        self.get.assert_not_called()
        self.assertTrue(
            PasswordAutofill("/owner", "spond").step(Cdp("https://spond.com/client"))
        )
        self.get.assert_called_once_with("/owner", "spond")

    def test_no_keychain_read_when_not_configured(self):
        with patch("familiekilder.password_autofill.configured", return_value=False):
            helper = PasswordAutofill("/neighbor", "bankid")
        cdp = Cdp()
        self.assertFalse(helper.step(cdp))
        self.assertEqual(cdp.calls, [])
        self.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
