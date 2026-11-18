"""Store, read and delete one made-up password in the system's own store.

    python scripts/password_trial.py              the store must work
    python scripts/password_trial.py --no-store   the store must be missing

For CI machines, never a computer with real passwords: it writes to the real
store of the user running it. Uses the program's own functions
(`login_passwords.set_password`, `get_password`, `delete_password`) with a new
data root in a temporary folder, so the account name in the store is new and
belongs to no profile. That is Windows Credential Manager, the Secret Service
on Linux (for example GNOME Keyring under dbus-run-session) or the macOS
Keychain.

Afterwards it asks the store directly that nothing is left, and on Windows
also lists the credentials with cmdkey.

With --no-store it checks the other case: the store is missing, saving a
password fails with a message that says so, and a login without a stored
password does not touch the store.

Prints one JSON line and exits 0 when every step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _cmdkey_mentions(text: str) -> bool | None:
    """Whether cmdkey lists a credential that contains text; None off Windows."""

    if os.name != "nt":
        return None
    listed = subprocess.run(["cmdkey", "/list"], capture_output=True, timeout=60, check=False)
    output = listed.stdout.decode("mbcs", errors="replace")
    return text.lower() in output.lower()


def _made_up_password() -> str:
    """A new random value each run, with letters a legacy encoding lacks."""

    return f"{secrets.token_urlsafe(18)}-æøå"


def _with_store(root: Path, report: dict) -> bool:
    from familiekilder import desktop_platform, login_passwords

    password = _made_up_password()

    backend = desktop_platform.password_backend()
    report["backend"] = type(backend).__name__
    identity = login_passwords.account(root, "bankid")
    digest = identity.split(":", 1)[0]
    report["empty_before"] = backend.get_password(login_passwords.SERVICE, identity) is None
    login_passwords.set_password(root, "bankid", password)
    report["configured_after_set"] = login_passwords.configured(root, "bankid")
    report["read_back_equal"] = login_passwords.get_password(root, "bankid") == password
    report["listed_while_stored"] = _cmdkey_mentions(digest)
    login_passwords.delete_password(root, "bankid")
    report["configured_after_delete"] = login_passwords.configured(root, "bankid")
    report["left_in_store"] = backend.get_password(login_passwords.SERVICE, identity) is not None
    report["listed_after_delete"] = _cmdkey_mentions(digest)
    return (
        report["empty_before"]
        and report["configured_after_set"]
        and report["read_back_equal"]
        and report["listed_while_stored"] in (None, True)
        and not report["configured_after_delete"]
        and not report["left_in_store"]
        and report["listed_after_delete"] in (None, False)
    )


def _without_store(root: Path, report: dict) -> bool:
    from familiekilder import desktop_platform, login_passwords
    from familiekilder.core import FamiliekilderError
    from familiekilder.password_autofill import PasswordAutofill

    try:
        desktop_platform.password_backend()
    except FamiliekilderError as error:
        report["missing_message"] = str(error)
    else:
        report["missing_message"] = None
    try:
        login_passwords.set_password(root, "bankid", _made_up_password())
    except FamiliekilderError as error:
        report["set_message"] = str(error)
    else:
        report["set_message"] = None
    report["configured"] = login_passwords.configured(root, "bankid")
    # What a login does first: the helper stays off and reads nothing.
    autofill = PasswordAutofill(root, "bankid")
    report["autofill_enabled"] = autofill.enabled
    report["autofill_step"] = autofill.step(object())
    return (
        bool(report["missing_message"])
        and report["set_message"] == report["missing_message"]
        and not report["configured"]
        and not report["autofill_enabled"]
        and report["autofill_step"] is False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Kontroller at programmet melder klart fra når lageret mangler",
    )
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")

    report: dict[str, object] = {
        "platform": sys.platform,
        "mode": "no-store" if args.no_store else "store",
    }
    root = Path(tempfile.mkdtemp(prefix="familiekilder-passord-")) / "datarot"
    ok = False
    try:
        ok = _without_store(root, report) if args.no_store else _with_store(root, report)
    except Exception as error:  # noqa: BLE001 - the report shows the failure
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if not args.no_store:
            # Never leave the made-up password behind, even after a failure.
            try:
                from familiekilder import login_passwords

                login_passwords.delete_password(root, "bankid")
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(root.parent, ignore_errors=True)
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
