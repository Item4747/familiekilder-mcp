"""Entry point of the container image.

Started as root only so it can prepare the data disk that the host mounts
(Fly.io mounts volumes owned by root). It creates the data root with mode
0700, hands it to the unprivileged user, drops to that user and starts the
server. Arguments, if given, replace the default command, so admin commands
on the host run as the same user, for example::

    python -m familiekilder.container remote clients
"""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

CONTAINER_USER = "familiekilder"
DEFAULT_ROOT = "/data/familiekilder"
DEFAULT_COMMAND = ["mcp", "serve", "--http", "--host", "0.0.0.0", "--port", "8080"]


def _prepare(root: Path, account: pwd.struct_passwd) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for path in (root, *(p for p in root.rglob("*") if not p.is_symlink())):
        if path.stat().st_uid != account.pw_uid:
            os.chown(path, account.pw_uid, account.pw_gid)
    root.chmod(0o700)


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv) or DEFAULT_COMMAND
    root = Path(os.environ.setdefault("FAMILIEKILDER_ROOT", DEFAULT_ROOT))
    if os.geteuid() == 0:
        account = pwd.getpwnam(CONTAINER_USER)
        _prepare(root, account)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        os.environ["HOME"] = account.pw_dir
        os.environ["USER"] = CONTAINER_USER
    os.umask(0o077)
    os.execvp("familiekilder-mcp", ["familiekilder-mcp", *arguments])


if __name__ == "__main__":
    main()
