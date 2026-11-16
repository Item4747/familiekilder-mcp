"""Built-in client setup for sources whose app identity ships with the package.

Internal API. The values in ``data/client-defaults.json`` describe the official
app (client ID, endpoints, app version). They are the same for every user of
that app and give no access to anyone's account; a user still logs in.
"""

from __future__ import annotations

import copy
from functools import cache
from importlib import resources
import json
from typing import Any

DATA_FILE = "client-defaults.json"


@cache
def _defaults() -> dict[str, dict[str, Any]]:
    text = resources.files(__package__).joinpath("data", DATA_FILE).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict) or not all(
        isinstance(source, str) and isinstance(config, dict) for source, config in value.items()
    ):
        raise ValueError(f"{DATA_FILE} must map each source to an object")
    return value


def default_client_config(source: str) -> dict[str, Any] | None:
    """Return the built-in client setup for a source, or None."""

    if not isinstance(source, str):
        return None
    config = _defaults().get(source)
    return None if config is None else copy.deepcopy(config)
