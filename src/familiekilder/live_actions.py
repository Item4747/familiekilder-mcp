"""Explicit provider writes. Only verified wire contracts can register tools."""

from .core import FamiliekilderError
from .sources import COMMON_ACTIONS, REGISTRY, module


def capabilities(source):
    # Unknown is distinct from unsupported. See docs/service-capabilities.md.
    entry = REGISTRY[source]
    if not entry.writes:
        return {"read": "implemented", "actions": {}, "messages": "unsupported", "absence": "unsupported", "participation": "unsupported"}
    return {
        "read": "implemented",
        "actions": {
            action: "implemented" if action in entry.implemented else "unverified"
            for action in COMMON_ACTIONS + entry.extra_actions
        },
        **{
            category: "implemented" if category in entry.verified else "unverified"
            for category in ("messages", "absence", "participation")
        },
    }


def register_actions(server, runtime, source):
    for name in REGISTRY[source].actions:
        module(name).register(server, runtime)


def register_source_context_reads(server, runtime, source):
    """Register existing source read factories without constructing another MCP server.

    The source runtime must expose no write permissions. Fail closed if a factory
    nevertheless tries to register anything other than an explicit read tool.
    """
    if any(runtime.config.services.values()):
        raise FamiliekilderError("Kildeleseren kan ikke ha skriverettigheter")

    class Reads:
        def tool(self, name=None, **kwargs):
            def register(fn):
                self.add_tool(fn, name=name, **kwargs)
                return fn
            return register

        def add_tool(self, fn, name=None, **kwargs):
            annotations = kwargs.get("annotations")
            if annotations is None or annotations.model_dump(by_alias=True).get("readOnlyHint") is not True:
                raise FamiliekilderError("Skyggeleseren kan bare registrere leseverktøy")
            return server.add_tool(fn, name=name, **kwargs)

    register_actions(Reads(), runtime, source)
