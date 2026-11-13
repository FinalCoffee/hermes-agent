"""Ingress gate for the public bot: a turn budget per sender and in total, and a text length cap.

Registered as ``pre_gateway_dispatch``, which the gateway runs once per inbound message before auth and
before any model call (gateway/run_inbound_hooks.py). Every counted message is one model turn, so a refused
message costs the operator nothing. Only messaging platforms are gated; the local CLI never fires the hook.
A sender that auth refuses never becomes a turn, so it is not counted at all: strangers cannot spend the
budget that serves the people the bot is for.
"""

from __future__ import annotations

import logging
import re

from gateway.config import Platform

from . import _common, welcome

logger = logging.getLogger(__name__)

# Sizing, so that a real traveller never hits these. A planning session is bursts of short messages and button
# taps: a busy minute is about ten answers to a run of questions, so 15 a minute leaves headroom. A script that
# sends one message a second is held to a quarter of that rate. A full planning day is about 100 messages, so
# 150 a day leaves headroom. The global limits are totals across all senders, so many accounts that each stay
# under their own limit cannot exceed them: 180 a minute serves twenty people who each send nine messages in
# the same minute, or twelve senders at their full rate at once, and 5,000 a day is about fifty full planning
# days. The daily total is the bot's spend ceiling: size it to the model budget.
TURNS_PER_MINUTE = 15
TURNS_PER_DAY = 150
GLOBAL_TURNS_PER_MINUTE = 180
GLOBAL_TURNS_PER_DAY = 5000

# Telegram caps one message at 4096 characters. The adapter merges rapid chunks into one turn with no cap of its
# own, and that merge measured at 4 MB. One message's worth of text is enough for any travel request.
MAX_TEXT_CHARS = 4096

# Commands that cost no model call but are answered, so they have a loose budget of their own, per sender and in
# total: past it the bot stops replying instead of echoing a flood. /new and /stop are run by core. /start (with
# or without a deep-link payload, which Telegram sends as "/start <payload>"), /help, /whoami and /commands are
# answered by welcome.py and skipped here. The optional @botname suffix is accepted from any bot, since a command
# typed in a group names the bot it is for. Any other text that starts with "/" is an ordinary turn, because
# unknown slash text falls through to the model.
COMMANDS_PER_MINUTE = 20
GLOBAL_COMMANDS_PER_MINUTE = 120
_SESSION_COMMAND_RE = re.compile(r"/(?:stop|new)(?:@[A-Za-z0-9_]+)?")
_START_RE = re.compile(r"/start(?:@[A-Za-z0-9_]+)?(?:\s+\S.*)?", re.DOTALL)
_ANSWERED_RE = re.compile(r"/(?:help|whoami|commands)(?:@[A-Za-z0-9_]+)?")

# A /start repeated inside this window draws nothing, so a double tap is one welcome; after it every /start is
# welcomed again, whoever has been welcomed before.
WELCOME_WINDOW_SECONDS = 3.0
_welcome_limiter = _common.RateLimiter()

# Platforms that are not a chat with a person: the hook fires for them too, and they are not limited here.
_NOT_MESSAGING = frozenset({Platform.LOCAL, Platform.API_SERVER, Platform.WEBHOOK, Platform.MSGRAPH_WEBHOOK})

_WINDOWS = {"minute": 60.0, "day": 86400.0, "global": 60.0, "global_day": 86400.0, "length": 60.0}
_NOTICES = {
    "minute": "Too many messages in a row. Please wait a minute and try again. / 訊息發送太頻繁，請稍等一分鐘再試。",
    "day": "You have reached today's message limit. Please try again tomorrow. / 今天的訊息已達上限，請明天再試。",
    "global": "Accomy is busy right now. Please try again in a minute. / Accomy 目前忙碌中，請一分鐘後再試。",
    "global_day": "Accomy has reached today's capacity. Please try again tomorrow. / Accomy 今天的服務量已達上限，請明天再試。",
    "length": "That message is too long. Please send up to 4,096 characters. / 訊息太長，請分段傳送（最多 4,096 字元）。",
}

_gate_limiter = _common.RateLimiter()
# One notice per window per sender, and a bot-wide total: the gate drops silently after that, so neither one
# flooding sender nor many accounts turn the bot's own replies into the flood.
GLOBAL_NOTICES_PER_MINUTE = 20
_notice_limiter = _common.RateLimiter()


def _sender_key(source) -> str:
    """The sender, as the tool limiter identifies one: the user, else the chat. Keys are prefixed so a user
    ID and a chat ID never collide. A message with neither still gets a bucket of its own."""
    if source.user_id:
        return f"user:{source.user_id}"
    if source.chat_id:
        return f"chat:{source.chat_id}"
    return "unidentified"


_TURN_SCOPES = ("minute", "day", "global", "global_day")


def _refused_by(sender: str) -> str | None:
    """The first limit this turn breaks, or None. All are checked before any is charged, so a turn one limit
    refuses spends nothing from the others."""
    refused = _gate_limiter.admit((
        (f"{sender}:minute", TURNS_PER_MINUTE, _WINDOWS["minute"]),
        (f"{sender}:day", TURNS_PER_DAY, _WINDOWS["day"]),
        ("global:minute", GLOBAL_TURNS_PER_MINUTE, _WINDOWS["global"]),
        ("global:day", GLOBAL_TURNS_PER_DAY, _WINDOWS["global_day"]),
    ))
    return None if refused is None else _TURN_SCOPES[refused]


def _is_admitted(gateway, source) -> bool:
    """Whether auth lets this sender reach a turn. A refused sender is answered by core (decline or pairing,
    on its own cooldown), so it gets no notice from here and draws on no budget. With no gateway, every sender
    is counted."""
    if gateway is None:
        return True
    return gateway._is_user_authorized_for_source(source)


async def _drop(gateway, source, scope: str) -> dict:
    if _notice_limiter.admit((
        (f"{_sender_key(source)}:{scope}", 1, _WINDOWS[scope]), ("global", GLOBAL_NOTICES_PER_MINUTE, 60.0),
    )) is None:
        await _send_notice(gateway, source, _NOTICES[scope])
    return {"action": "skip", "reason": f"accomy_{scope}_limit"}


async def _send_notice(gateway, source, text: str) -> None:
    """Reply through the bot that answers this chat, as the unauthorized-DM decline does. A failed notice
    never changes the drop."""
    if gateway is None:
        return
    try:
        adapter = gateway._delivery_adapter_for(source)
        if adapter:
            await adapter.send(source.chat_id, text, metadata=gateway._thread_metadata_for_source(source))
    except Exception:  # health: allow BLE001 -- a notice is best effort; the message is dropped either way
        logger.warning("Could not deliver the travel limit notice", exc_info=True)


async def _answer_command(gateway, event, source, kind: str) -> dict:
    """Answer /start, /help, /whoami or /commands here, so core neither replies nor calls the model for them."""
    if kind == "start" and _welcome_limiter.admit(
        ((f"{_sender_key(source)}:welcome", 1, WELCOME_WINDOW_SECONDS),)
    ) is not None:
        return {"action": "skip", "reason": "accomy_welcome_repeat"}
    await welcome.send(gateway, source, event, kind)
    return {"action": "skip", "reason": f"accomy_{kind}"}


async def gate_inbound(*, event, gateway=None, **_kwargs) -> dict | None:
    """``pre_gateway_dispatch``: None lets the message through; a skip drops it before auth and the model."""
    source = event.source
    if source.platform in _NOT_MESSAGING or not _is_admitted(gateway, source):
        return None
    try:
        return await _gate_admitted(gateway, event, source)
    except Exception:
        # A raising hook is treated by the gateway as no hook at all, which would let the message through
        # uncounted. An admitted sender's message is dropped instead.
        logger.exception("accomy ingress gate failed; message dropped")
        return {"action": "skip", "reason": "accomy_gate_error"}


async def _gate_admitted(gateway, event, source) -> dict | None:
    text = event.text or ""
    if len(text) > MAX_TEXT_CHARS:
        return await _drop(gateway, source, "length")
    stripped = text.strip()
    start, answered = _START_RE.fullmatch(stripped), _ANSWERED_RE.fullmatch(stripped)
    if start or answered or _SESSION_COMMAND_RE.fullmatch(stripped):
        if _gate_limiter.admit((
            (f"{_sender_key(source)}:commands", COMMANDS_PER_MINUTE, 60.0),
            ("global:commands", GLOBAL_COMMANDS_PER_MINUTE, 60.0),
        )) is not None:
            return {"action": "skip", "reason": "accomy_command_flood"}
        if start or answered:
            return await _answer_command(gateway, event, source, "start" if start else "help")
        return None
    scope = _refused_by(_sender_key(source))
    if scope is None:
        return None
    return await _drop(gateway, source, scope)
