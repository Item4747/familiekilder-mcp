"""A `.private` folder that a test makes by hand, as the product makes it."""

from __future__ import annotations

import os
from pathlib import Path


def protect(private: Path) -> None:
    """Give a hand-made `.private` the protection the product gives it.

    Tests make `.private` with mode 0700, which is all the product does on
    macOS and Linux. Windows ignores the mode; there the product gives
    `.private` a user-only access list, and readers refuse the folder without
    it. Elsewhere this does nothing.
    """

    if os.name != "nt":
        return
    from familiekilder.windows_acl import restrict

    restrict(private)


def readable_by_others(path: Path) -> None:
    """Let other local users read `path`: mode 0644, or on Windows an entry for Users."""

    path.chmod(0o644)
    if os.name != "nt":
        return
    from familiekilder.windows_acl import current_user_sid, set_dacl

    # BU is the local Users group, so every signed-in account may read the file.
    set_dacl(path, f"D:P(A;;FA;;;{current_user_sid()})(A;;FR;;;BU)")


def private_again(path: Path) -> None:
    """Undo `readable_by_others`."""

    path.chmod(0o600)
    if os.name != "nt":
        return
    from familiekilder.windows_acl import current_user_sid, set_dacl

    set_dacl(path, f"D:P(A;;FA;;;{current_user_sid()})(A;;FA;;;SY)")
