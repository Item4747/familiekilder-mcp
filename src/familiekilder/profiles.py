"""Resolve personal state independently of application code and working directory."""

import os
import re
from pathlib import Path


def resolve_root(root=None, *, profile=None, default_root=None):
    from .core import FamiliekilderError

    selected = (
        profile if profile is not None else
        (os.environ.get("FAMILIEKILDER_PROFILE") if default_root is None else None)
    )
    if selected is not None and not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", selected
    ):
        raise FamiliekilderError(
            "Profilnavn må være 1–64 bokstaver, tall, bindestrek eller understrek"
        )
    # An explicit root is already a complete profile location. Do not silently
    # override it using a process-wide profile environment variable.
    if root is not None:
        if profile is not None:
            raise FamiliekilderError("Velg enten --root eller --profile")
        return Path(root).expanduser().resolve()
    base = Path(
        default_root or os.environ.get("FAMILIEKILDER_ROOT")
        or Path.home() / "Library/Application Support/familiekilder-mcp"
    ).expanduser()
    if selected:
        profiles = base / "profiles"
        candidate = profiles / selected
        if profiles.is_symlink() or candidate.is_symlink():
            raise FamiliekilderError(
                "En navngitt profil kan ikke være en symbolsk lenke til en annen profil"
            )
        return candidate.resolve()
    return base.resolve()
