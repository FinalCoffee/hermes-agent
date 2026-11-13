"""Accomy AI Travel: trip-planning tools and the travel-assistant policy.

Search tools price trips in US dollars (TMC hotels and flights, Seat Unique events, Google places and weather,
Brave web research). Order tools (``tools_orders``, offered when the order database is configured) place demonstration
orders for quotes the search tools showed: the orders are recorded in the order database and never sent to a supplier.
``policy`` adds the persona, the scope rules and the suggested-reply instruction as frozen system-prompt sections.
``orderstore`` is the order database behind ``hermes accomy-orders``. ``read_attachment`` (``tools_attachments``)
reads a file a user sent, and ``speech`` transcribes voice notes for the gateway through Amazon Transcribe.
"""

from __future__ import annotations

import functools

from . import (
    ingress, ocr, payment, policy, speech, tools_attachments, tools_events, tools_flights, tools_hotels,
    tools_orders, tools_places, tools_research,
)
from ._common import TOOLSET, has_secrets
from .orderstore import cli as orders_cli

_TOOL_MODULES = (
    tools_places, tools_hotels, tools_flights, tools_events, tools_research, tools_orders, tools_attachments)


def register(ctx) -> None:
    for module in _TOOL_MODULES:
        for spec in module.TOOLS:
            ctx.register_tool(
                name=spec.name, toolset=TOOLSET, schema=spec.schema, handler=spec.handler,
                check_fn=functools.partial(has_secrets, *spec.requires_env),
                requires_env=list(spec.requires_env), emoji=spec.emoji)
    ctx.register_hook("pre_gateway_dispatch", ingress.gate_inbound)
    ctx.register_cli_command(orders_cli.COMMAND, orders_cli.HELP, orders_cli.setup, orders_cli.run)
    payment.configure(ctx)
    speech.configure(ctx)
    ocr.configure(ctx)
    policy.register(ctx)
