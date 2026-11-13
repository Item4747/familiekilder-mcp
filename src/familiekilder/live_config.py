"""Per-installation MCP configuration. No provider imports or network on load."""

from dataclasses import dataclass, field
from pathlib import Path
from .profiles import resolve_root

from .core import FamiliekilderError, SOURCES as SOURCES
from .private_state import (
    ensure_private_directory,
    read_private_json,
    atomic_write_private_json,
)

CATEGORIES = ("messages", "absence", "participation")


@dataclass(frozen=True)
class Config:
    root: Path
    services: dict[str, frozenset[str]]
    persistent: bool = field(default=False, repr=False, compare=False)

    @property
    def private(self):
        return self.root / ".private"

    def require(self, source, category=None):
        if source not in self.services:
            raise FamiliekilderError("Tjenesten er ikke aktivert")
        if category and category not in self.services[source]:
            raise FamiliekilderError("Denne skriverettigheten er ikke aktivert")

    @classmethod
    def load(cls, root=None, *, profile=None, default_root=None):
        root = resolve_root(root, profile=profile, default_root=default_root)
        path = root / ".private/mcp-config.json"
        value = (
            read_private_json(path) if path.exists() else {"version": 1, "services": {}}
        )
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or set(value) != {"version", "services"}
        ):
            raise FamiliekilderError("Ugyldig MCP-konfigurasjon")
        services = value["services"]
        if not isinstance(services, dict) or set(services) - set(SOURCES):
            raise FamiliekilderError("Ukjent tjeneste i MCP-konfigurasjonen")
        normalized = {}
        for name, settings in services.items():
            if not isinstance(settings, dict) or set(settings) != {"write"}:
                raise FamiliekilderError("Tjenesten krever en eksplisitt write-liste")
            writes = settings["write"]
            if not isinstance(writes, list) or any(x not in CATEGORIES for x in writes):
                raise FamiliekilderError("Ukjent skriverettighet")
            if name == "gmail" and writes:
                raise FamiliekilderError("Gmail støtter bare lesetilgang")
            normalized[name] = frozenset(writes)
        return cls(root, normalized, persistent=True)

    def save(self):
        if self.services.get("gmail"):
            raise FamiliekilderError("Gmail støtter bare lesetilgang")
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_private_directory(self.private)
        atomic_write_private_json(
            self.private / "mcp-config.json",
            {
                "version": 1,
                "services": {k: {"write": sorted(v)} for k, v in self.services.items()},
            },
        )
