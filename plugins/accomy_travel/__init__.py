"""Accomy AI Travel: trip-planning tools and the travel-assistant policy.

Tools search and price only (TMC hotels and flights, Seat Unique events, Google places and weather,
Brave web research); nothing here books, pays or cancels. ``policy`` adds the persona, the scope
rules and the suggested-reply instruction as frozen system-prompt sections.
"""

from __future__ import annotations

import functools

from . import policy, tools_events, tools_flights, tools_hotels, tools_places, tools_research
from ._common import TOOLSET, has_secrets

_TOOL_MODULES = (tools_places, tools_hotels, tools_flights, tools_events, tools_research)


def register(ctx) -> None:
    for module in _TOOL_MODULES:
        for spec in module.TOOLS:
            ctx.register_tool(
                name=spec.name, toolset=TOOLSET, schema=spec.schema, handler=spec.handler,
                check_fn=functools.partial(has_secrets, *spec.requires_env),
                requires_env=list(spec.requires_env), emoji=spec.emoji)
    policy.register(ctx)
