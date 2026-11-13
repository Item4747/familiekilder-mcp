"""Lazy per-service authentication status and sanitized failure categories."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import FamiliekilderError, normalize_source
from .sources import REGISTRY


class SourceAdapterError(FamiliekilderError):
    """Base class for sanitized, source-level adapter failures."""


class SourceLoginRequired(SourceAdapterError):
    """The source needs a new manual authentication bootstrap."""


class SourceFatalConfigurationError(SourceLoginRequired):
    """The provider rejected immutable client or device configuration."""


class SourceUnavailable(SourceAdapterError):
    """The source could not be collected for a non-authentication reason."""


class SourceBudgetExceeded(SourceUnavailable):
    """A bounded collection ended before the provider's final page/item."""


AUTH_MODULES = {name: f"{__package__}.{s.auth}" for name, s in REGISTRY.items()}


class LazySourceAdapter:
    def __init__(self, source):
        self.source = source

    @staticmethod
    def _import(module_name: str) -> Any:
        try:
            return importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            raise SourceUnavailable("Kildeadapteren er ikke installert") from None

    def auth_status(self, root: Path) -> Mapping[str, Any]:
        module = self._import(AUTH_MODULES[self.source])
        operation = getattr(module, "auth_status", None)
        if not callable(operation):
            raise SourceUnavailable("Kildeadapteren mangler autentiseringsstatus")
        try:
            result = operation(root)
        except SourceAdapterError:
            raise
        except Exception:
            raise SourceUnavailable(
                "Kildens autentiseringsstatus kunne ikke leses"
            ) from None
        if not isinstance(result, Mapping):
            raise SourceUnavailable(
                "Kildeadapteren returnerte ugyldig autentiseringsstatus"
            )
        return result


def adapter_sources():
    return tuple(AUTH_MODULES)


def get_adapter(source):
    return LazySourceAdapter(normalize_source(source))
