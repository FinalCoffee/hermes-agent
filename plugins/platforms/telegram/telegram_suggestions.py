"""Telegram suggested replies: one inline keyboard per (chat, topic), on the latest delivered final.

The footer parser lives in ``gateway/suggested_replies.py``; this module puts its options on the
message as buttons and turns a tap into the next inbound user message. ``callback_data`` is
``sg:<token>:<idx>`` (13 bytes). The token names the stored entry, and an entry is live only on the
message that carries its keyboard (same chat and message id) and only for ``TTL_SECONDS``. A stale,
superseded, restarted or copied tap finds nothing and gets the "expired" toast.

Who may tap: in a group or channel only the asker, the person whose turn produced the options, may
use them. Anyone else gets a refusal toast and the keyboard stays usable for the asker. A DM has one
user, who is the asker.

Group addressing: a tap by the asker is an explicit interaction with the bot's own message, the same
as a reply to it, so it counts as addressed to the bot under ``require_mention``. The event is then
built the way a typed message in that chat is (group identity line, topic skill binding), so the turn
sees the same context.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from gateway.platforms.base import resolve_channel_prompt
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.telegram.callback_answer import answer_callback
from plugins.platforms.telegram.telegram_context import group_identity_prompt
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

logger = logging.getLogger(__name__)

TTL_SECONDS, MAX_ENTRIES = 24 * 3600, 256


@dataclass
class _Entry:
    token: str
    chat_id: str
    chat_type: str
    thread_id: Optional[str]
    message_id: str
    options: tuple[str, ...]
    created: float
    asker_id: Optional[str]


class _Store:
    """Live keyboards for one adapter. ``entries`` is ordered oldest first; ``latest`` maps a
    (chat, topic) to the one token whose keyboard is still on screen there."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, _Entry] = OrderedDict()
        self.latest: dict[tuple[str, str], str] = {}

    @staticmethod
    def key_of(entry: _Entry) -> tuple[str, str]:
        return entry.chat_id, entry.thread_id or ""

    def _drop(self, token: str) -> Optional[_Entry]:
        entry = self.entries.pop(token, None)
        if entry is not None and self.latest.get(self.key_of(entry)) == token:
            del self.latest[self.key_of(entry)]
        return entry

    def _prune(self, now: float) -> None:
        while self.entries:
            oldest = next(iter(self.entries.values()))
            if len(self.entries) < MAX_ENTRIES and now - oldest.created <= TTL_SECONDS:
                return
            self._drop(oldest.token)

    def retire(self, key: tuple[str, str]) -> Optional[_Entry]:
        token = self.latest.get(key)
        return self._drop(token) if token else None

    def add(self, entry: _Entry) -> None:
        self._prune(time.time())
        self.retire(self.key_of(entry))
        self.entries[entry.token] = entry
        self.latest[self.key_of(entry)] = entry.token

    def live(self, token: str, chat_id: str, message_id: str) -> Optional[_Entry]:
        """The entry for *token* if it is still on this message and inside its TTL. Synchronous with
        ``pop``: no await between the two, so a double tap cannot claim one entry twice."""
        entry = self.entries.get(token)
        if entry is None or entry.chat_id != chat_id or entry.message_id != message_id:
            return None
        if time.time() - entry.created > TTL_SECONDS:
            self._drop(token)
            return None
        return entry

    def pop(self, token: str) -> Optional[_Entry]:
        return self._drop(token)


def _store(adapter: Any) -> _Store:
    return adapter.__dict__.setdefault("_suggestion_store", _Store())


def _enabled(adapter: Any) -> bool:
    # A YAML string such as "false" is truthy: only a real boolean true turns the feature on.
    return (getattr(adapter.config, "extra", None) or {}).get("suggested_replies") is True


async def _edit_markup(
    adapter: Any, chat_id: Any, message_id: Any, markup: Optional[InlineKeyboardMarkup]
) -> bool:
    """One paced ``editMessageReplyMarkup``; a refused edit (message gone, rich message, flood) is
    False, never an error: the answer is already delivered and buttons are an extra."""
    if adapter._send_flood_cooldown_remaining(chat_id) is not None:
        return False
    slot_remaining = adapter._chat_outbound_slot_remaining(chat_id)
    if slot_remaining > 0:
        await asyncio.sleep(slot_remaining)
    adapter._hold_chat_outbound_slot(chat_id)
    try:
        await adapter._bot.edit_message_reply_markup(
            chat_id=normalize_telegram_chat_id(chat_id),
            message_id=int(message_id),
            reply_markup=markup,
        )
    except TelegramError as exc:
        logger.debug(
            "[%s] suggested-reply keyboard edit refused for message %s: %s",
            adapter.name,
            message_id,
            exc,
        )
        return False
    return True


async def attach(
    adapter: Any, source: Any, message_id: Optional[str], options: tuple[str, ...]
) -> bool:
    """Put *options* on the delivered final *message_id*. Every confirmed final calls this, with or
    without options: the previous keyboard of the same (chat, topic) is retired either way."""
    if not _enabled(adapter):
        return False  # off by default: no API call at all
    store = _store(adapter)
    key = (str(source.chat_id), str(source.thread_id or ""))
    old = store.retire(key)
    if old is not None:
        await _edit_markup(adapter, old.chat_id, old.message_id, None)
    if not options or not message_id:
        return False
    # URL-safe alphabet: never a colon, so the callback splits cleanly.
    token = secrets.token_urlsafe(6)[:8]
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(option, callback_data=f"sg:{token}:{idx}")]
            for idx, option in enumerate(options)
        ]
    )
    if not await _edit_markup(adapter, source.chat_id, message_id, markup):
        return False
    store.add(
        _Entry(
            token,
            str(source.chat_id),
            str(source.chat_type),
            source.thread_id and str(source.thread_id),
            str(message_id),
            tuple(options),
            time.time(),
            None if source.user_id is None else str(source.user_id),
        )
    )  # recorded only once Telegram accepted it
    return True


async def _replace_tapped_keyboard(
    query: Any, markup: Optional[InlineKeyboardMarkup]
) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except TelegramError as exc:
        logger.debug("suggested-reply keyboard update refused: %s", exc)


def _chosen_markup(option: str) -> InlineKeyboardMarkup:
    """The keyboard collapsed to the option that was picked. The toast vanishes within seconds; without
    this the chat would show two bot messages in a row and no trace of what the user chose. The button
    carries no token, so tapping it again reads as expired."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"✓ {option}", callback_data="sg:chosen")]]
    )


async def handle_tap(adapter: Any, query: Any, data: str, cb: dict) -> None:
    from plugins.platforms.telegram.adapter import (
        _toast,
        _unauthorized,
    )  # late: the adapter imports this module
    # Authorization before the claim: a denied tapper must not consume the keyboard for the owner.
    if not await adapter._callback_authorized(query, cb, _unauthorized()):
        return
    parts = data.split(":")
    message = getattr(query, "message", None)
    store = _store(adapter)
    entry = (
        store.live(parts[1], str(message.chat_id), str(message.message_id))
        if len(parts) == 3 and message is not None
        else None
    )
    if entry is not None and entry.chat_type != "dm" and str(query.from_user.id) != entry.asker_id:
        # The keyboard is left as it is: the asker can still use it.
        await answer_callback(query, _toast("platform.telegram.suggestions.not_yours"))
        return
    if entry is not None:
        store.pop(entry.token)
    idx = (
        int(parts[2])
        if entry and parts[2].isdigit() and int(parts[2]) < len(entry.options)
        else None
    )
    if entry is None or idx is None:
        await answer_callback(query, _toast("platform.telegram.suggestions.expired"))
        await _replace_tapped_keyboard(query, None)
        return
    option = entry.options[idx]
    await answer_callback(
        query, _toast("platform.telegram.suggestions.sent", option=option)
    )
    await _replace_tapped_keyboard(query, _chosen_markup(option))
    chat_topic, topic_skill = adapter._resolve_topic_binding(
        message, entry.chat_type, entry.thread_id
    )
    source = adapter.build_source(
        chat_id=entry.chat_id,
        chat_type=entry.chat_type,
        thread_id=entry.thread_id,
        chat_topic=chat_topic,
        user_id=str(query.from_user.id),
        user_name=query.from_user.full_name,
        message_id=str(message.message_id),
    )
    # The tapper is the author (the keyboard message belongs to the bot). reply_expected: a tap is an answer.
    event = MessageEvent(
        text=option,
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        user_name=source.user_name,
        message_id=source.message_id,
        reply_expected=True,
        auto_skill=topic_skill,
        channel_prompt=group_identity_prompt(
            adapter,
            message,
            resolve_channel_prompt(
                adapter.config.extra,
                entry.thread_id or entry.chat_id,
                entry.chat_id if entry.thread_id else None,
            ),
        ),
    )
    await adapter.handle_message(
        event
    )  # not internal: the runner re-checks authorization for the tapper
