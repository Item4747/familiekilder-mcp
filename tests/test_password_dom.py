"""Opt-in, offline DOM checks against a disposable tab in Android Chrome."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder.android_passwords import AndroidPasswordSession
from familiekilder.password_autofill import _FILL, _SELECTOR


@unittest.skipUnless(
    os.environ.get("FAMILIEKILDER_ANDROID_TEST_SERIAL"), "Android DOM test is opt-in"
)
class PasswordDomTests(unittest.TestCase):
    def test_actual_chrome_password_field_guards_and_submission(self):
        adb = Path.home() / "Library/Android/sdk/platform-tools/adb"
        with patch("familiekilder.password_autofill.configured", return_value=True):
            browser = AndroidPasswordSession(
                "/unused-test-profile",
                adb,
                os.environ["FAMILIEKILDER_ANDROID_TEST_SERIAL"],
            )
        self.addCleanup(browser.close)
        self.assertTrue(
            browser.start("about:blank"), "Android Chrome CDP could not create test tab"
        )
        cdp = browser.cdp
        session = cdp.call(
            "Target.attachToTarget", {"targetId": browser.target_id, "flatten": True}
        )["sessionId"]
        frame = cdp.call("Page.getFrameTree", session_id=session)["frameTree"]["frame"][
            "id"
        ]

        def evaluate(expression):
            result = cdp.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                session_id=session,
            )
            self.assertNotIn("exceptionDetails", result)
            return result.get("result", {}).get("value")

        fixtures = [
            (
                '<p>BankID: Ditt personlige passord</p><input type="password"><button onclick="document.body.dataset.submitted=1">Fortsett</button>',
                True,
            ),
            ('<p>BankID: godkjenn på telefon</p><input type="text">', False),
            (
                '<p>BankID: nytt passord</p><input type="password" autocomplete="new-password">',
                False,
            ),
            (
                '<p>BankID: passord</p><input type="password"><input type="password">',
                False,
            ),
            ('<p>BankID: passord</p><input type="password" hidden>', False),
            ('<p>BankID: passord</p><input type="password" disabled>', False),
            ('<p>BankID: passord</p><input type="password" value="existing">', False),
            (
                '<p>BankID: Signer dokument med passord</p><input type="password">',
                False,
            ),
        ]
        for html, expected in fixtures:
            with self.subTest(html=html):
                cdp.call(
                    "Page.setDocumentContent",
                    {"frameId": frame, "html": html},
                    session_id=session,
                )
                selector = _SELECTOR.replace("ORIGINS", '["null"]').replace(
                    "BANKID", "true"
                )
                self.assertEqual(evaluate(f"Boolean({selector})"), expected)
                if expected:
                    # Recheck origin at the point of writing, including navigation races.
                    field = cdp.call(
                        "Runtime.evaluate", {"expression": selector}, session_id=session
                    )["result"]["objectId"]

                    def fill(origins, field=field):
                        return cdp.call(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": field,
                                "functionDeclaration": _FILL,
                                "arguments": [
                                    {"value": "disposable-fixture-password"},
                                    {"value": origins},
                                ],
                                "returnByValue": True,
                            },
                            session_id=session,
                        )["result"]["value"]

                    self.assertFalse(fill(["https://csfe.bankid.no"]))
                    self.assertEqual(
                        evaluate("document.querySelector('input').value"), ""
                    )
                    self.assertTrue(fill(["null"]))
                    self.assertEqual(evaluate("document.body.dataset.submitted"), "1")
                    self.assertFalse(fill(["null"]))
                    cdp.call(
                        "Runtime.releaseObject", {"objectId": field}, session_id=session
                    )


if __name__ == "__main__":
    unittest.main()
