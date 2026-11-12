"""Telegram suggested replies: the keyboard on the delivered final, and a tap as the tapper's next message.

Behaviour contracts on a real ``TelegramAdapter`` with a fake bot. The keyboard is one live set of
buttons per (chat, topic); a tap is answered, the keyboard removed, and the option enters through
``handle_message`` as a message from whoever tapped.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter, _apply_yaml_config
from plugins.platforms.telegram import telegram_suggestions

CHAT = "100"


class _Button:
    def __init__(self, text, callback_data):
        self.text, self.callback_data = text, callback_data


class _Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


@pytest.fixture(autouse=True)
def _readable_keyboards(monkeypatch):
    """The suite runs with real PTB or with the conftest mock; either way a keyboard must be readable."""
    monkeypatch.setattr(telegram_suggestions, "InlineKeyboardButton", _Button)
    monkeypatch.setattr(telegram_suggestions, "InlineKeyboardMarkup", _Markup)


def _adapter(extra=None) -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra=extra if extra is not None else {"suggested_replies": True},
        )
    )
    adapter._bot = AsyncMock()
    adapter._bot.username = "accomy_bot"
    adapter._app = MagicMock()
    adapter.handle_message = AsyncMock()
    adapter._is_callback_user_authorized = lambda *a, **k: True
    adapter._bot.edit_message_reply_markup = AsyncMock(return_value=True)
    return adapter


def _source(thread_id=None, *, chat_type="dm", user_id="555"):
    """The delivering final's source: *user_id* is the asker."""
    return SimpleNamespace(
        chat_id=CHAT, chat_type=chat_type, thread_id=thread_id, user_id=user_id
    )


def _tap(
    data: str,
    *,
    tapper_id: int = 555,
    message_id: int = 900,
    thread_id=None,
    chat_type: str = "private",
) -> SimpleNamespace:
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=tapper_id, first_name="Ana", full_name="Ana Lee"),
        message=SimpleNamespace(
            message_id=message_id,
            chat_id=int(CHAT),
            message_thread_id=thread_id,
            chat=SimpleNamespace(id=int(CHAT), type=chat_type),
        ),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return query


def _token_of(adapter, options=("Book it", "Show more")) -> str:
    kb = adapter._bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":")[1]


@pytest.mark.asyncio
async def test_attach_puts_one_button_per_option_on_the_given_message():
    adapter = _adapter()
    assert await adapter.attach_suggested_replies(
        _source(), "900", ("Book it", "Show more")
    )

    kwargs = adapter._bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["chat_id"] == int(CHAT)
    assert kwargs["message_id"] == 900
    rows = kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == ["Book it", "Show more"]
    for index, row in enumerate(rows):
        data = row[0].callback_data
        assert data.startswith("sg:") and data.endswith(f":{index}")
        assert len(data.encode()) <= 64


@pytest.mark.asyncio
async def test_disabled_config_makes_no_api_call_at_all():
    adapter = _adapter(extra={})
    assert await adapter.attach_suggested_replies(_source(), "900", ("A", "B")) is False
    adapter._bot.edit_message_reply_markup.assert_not_called()


def test_enable_key_in_yaml_reaches_the_platform_extra():
    assert _apply_yaml_config({}, {"suggested_replies": True}) == {
        "suggested_replies": True
    }


@pytest.mark.asyncio
async def test_a_new_final_in_the_same_topic_retires_the_previous_keyboard():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    await adapter.attach_suggested_replies(_source(), "901", ())

    calls = adapter._bot.edit_message_reply_markup.await_args_list
    assert (
        len(calls) == 2
    )  # the first keyboard's removal; a final with no options adds no keyboard
    assert (
        calls[1].kwargs["message_id"] == 900 and calls[1].kwargs["reply_markup"] is None
    )


@pytest.mark.asyncio
async def test_topics_keep_separate_keyboards():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(thread_id="7"), "900", ("A", "B"))
    await adapter.attach_suggested_replies(_source(thread_id="8"), "901", ("C", "D"))

    assert (
        adapter._bot.edit_message_reply_markup.await_count == 2
    )  # the second topic retired nothing


@pytest.mark.asyncio
async def test_a_tap_becomes_the_tappers_next_message_and_clears_the_keyboard():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("Book it", "Show more"))
    token = _token_of(adapter)
    query = _tap(f"sg:{token}:1", tapper_id=555)

    await adapter._handle_suggestion_callback(
        query, query.data, adapter._callback_ctx(query)
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Show more"
    assert (
        event.source.user_id == "555"
    )  # the tapper, never the bot that owns the keyboard
    assert event.source.chat_id == CHAT
    query.answer.assert_awaited_once()
    assert "Show more" in query.answer.await_args.kwargs["text"]
    # The keyboard collapses to the picked option, so the chat keeps a record of the choice.
    query.edit_message_reply_markup.assert_awaited_once()
    (row,) = query.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for button in row] == ["✓ Show more"]


@pytest.mark.asyncio
async def test_tapping_the_record_of_a_past_choice_is_expired_and_starts_no_turn():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("Book it", "Show more"))
    first = _tap(f"sg:{_token_of(adapter)}:1")
    await adapter._handle_suggestion_callback(
        first, first.data, adapter._callback_ctx(first)
    )
    (row,) = first.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    again = _tap(row[0].callback_data)

    await adapter._handle_suggestion_callback(
        again, again.data, adapter._callback_ctx(again)
    )

    adapter.handle_message.assert_awaited_once()  # only the first tap became a message
    assert "expired" in again.answer.await_args.kwargs["text"].lower()
    again.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_a_second_tap_on_the_same_keyboard_is_expired_and_starts_no_turn():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("Book it", "Show more"))
    token = _token_of(adapter)
    first, second = _tap(f"sg:{token}:0"), _tap(f"sg:{token}:0")

    await adapter._handle_suggestion_callback(
        first, first.data, adapter._callback_ctx(first)
    )
    await adapter._handle_suggestion_callback(
        second, second.data, adapter._callback_ctx(second)
    )

    adapter.handle_message.assert_awaited_once()
    assert "expired" in second.answer.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_a_superseded_keyboard_is_expired():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("Old A", "Old B"))
    old_token = _token_of(adapter)
    await adapter.attach_suggested_replies(_source(), "901", ("New A", "New B"))
    query = _tap(f"sg:{old_token}:0")

    await adapter._handle_suggestion_callback(
        query, query.data, adapter._callback_ctx(query)
    )

    adapter.handle_message.assert_not_called()
    assert "expired" in query.answer.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_a_tap_after_a_restart_is_expired_and_the_stale_keyboard_is_removed():
    before = _adapter()
    await before.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(before)

    after = _adapter()  # a fresh process: the store is empty
    query = _tap(f"sg:{token}:0")
    await after._handle_suggestion_callback(
        query, query.data, after._callback_ctx(query)
    )

    after.handle_message.assert_not_called()
    assert "expired" in query.answer.await_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_an_unauthorized_tap_is_refused_and_does_not_consume_the_keyboard():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(adapter)

    denied = _tap(f"sg:{token}:0", tapper_id=999)
    adapter._is_callback_user_authorized = lambda *a, **k: False
    await adapter._handle_suggestion_callback(
        denied, denied.data, adapter._callback_ctx(denied)
    )
    adapter.handle_message.assert_not_called()
    denied.answer.assert_awaited_once()
    assert "expired" not in denied.answer.await_args.kwargs["text"].lower()

    adapter._is_callback_user_authorized = lambda *a, **k: True
    allowed = _tap(f"sg:{token}:0", tapper_id=555)
    await adapter._handle_suggestion_callback(
        allowed, allowed.data, adapter._callback_ctx(allowed)
    )
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_out_of_range_index_is_expired_not_an_error():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(adapter)
    query = _tap(f"sg:{token}:9")

    await adapter._handle_suggestion_callback(
        query, query.data, adapter._callback_ctx(query)
    )

    adapter.handle_message.assert_not_called()
    assert "expired" in query.answer.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_dispatch_routes_sg_callbacks_to_the_handler():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(adapter)
    query = _tap(f"sg:{token}:0")
    update = SimpleNamespace(callback_query=query)
    adapter._accept_update = MagicMock()

    await adapter._handle_callback_query(update, None)

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_refused_keyboard_edit_is_silent_and_records_nothing():
    adapter = _adapter()
    adapter._bot.edit_message_reply_markup = AsyncMock(
        side_effect=BadRequest("message can't be edited")
    )

    assert await adapter.attach_suggested_replies(_source(), "900", ("A", "B")) is False
    assert adapter.__dict__["_suggestion_store"].latest == {}


@pytest.mark.asyncio
async def test_an_active_flood_cooldown_skips_the_call_locally(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_send_flood_cooldown_remaining", lambda chat_id: 30.0)

    assert await adapter.attach_suggested_replies(_source(), "900", ("A", "B")) is False
    adapter._bot.edit_message_reply_markup.assert_not_called()


def test_the_store_is_bounded_by_count_oldest_first():
    store = telegram_suggestions._Store()
    now = time.time()
    for index in range(telegram_suggestions.MAX_ENTRIES + 5):
        store.add(
            telegram_suggestions._Entry(
                f"t{index}", CHAT, "dm", str(index), "900", ("A", "B"), now, "555"
            )
        )  # one topic per entry
    assert len(store.entries) == telegram_suggestions.MAX_ENTRIES
    assert (
        "t0" not in store.entries
        and f"t{telegram_suggestions.MAX_ENTRIES + 4}" in store.entries
    )


def test_entries_older_than_a_day_are_dropped_on_the_next_final():
    store = telegram_suggestions._Store()
    store.add(
        telegram_suggestions._Entry(
            "old",
            CHAT,
            "dm",
            "1",
            "900",
            ("A", "B"),
            time.time() - telegram_suggestions.TTL_SECONDS - 1,
            "555",
        )
    )
    store.add(
        telegram_suggestions._Entry(
            "new", CHAT, "dm", "2", "901", ("A", "B"), time.time(), "555"
        )
    )
    assert list(store.entries) == ["new"]


@pytest.mark.asyncio
async def test_the_enable_flag_must_be_a_real_boolean(monkeypatch):
    """A YAML string "false" is truthy in Python: it must not turn the feature on."""
    adapter = _adapter(extra={"suggested_replies": "false"})
    assert await adapter.attach_suggested_replies(_source(), "900", ("A", "B")) is False
    adapter._bot.edit_message_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_in_a_group_a_tap_by_someone_else_is_refused_and_the_asker_can_still_tap():
    adapter = _adapter()
    await adapter.attach_suggested_replies(
        _source(chat_type="group", user_id="555"), "900", ("Book it", "Show more")
    )
    token = _token_of(adapter)

    other = _tap(f"sg:{token}:1", tapper_id=777, chat_type="group")
    await adapter._handle_suggestion_callback(other, other.data, adapter._callback_ctx(other))
    adapter.handle_message.assert_not_called()
    assert "expired" not in other.answer.await_args.kwargs["text"].lower()
    other.edit_message_reply_markup.assert_not_called()

    asker = _tap(f"sg:{token}:1", tapper_id=555, chat_type="group")
    await adapter._handle_suggestion_callback(asker, asker.data, adapter._callback_ctx(asker))
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].source.user_id == "555"


@pytest.mark.asyncio
async def test_a_tap_older_than_the_ttl_is_expired_even_with_no_newer_keyboard():
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(adapter)
    adapter.__dict__["_suggestion_store"].entries[token].created -= (
        telegram_suggestions.TTL_SECONDS + 1
    )
    query = _tap(f"sg:{token}:0")

    await adapter._handle_suggestion_callback(query, query.data, adapter._callback_ctx(query))

    adapter.handle_message.assert_not_called()
    assert "expired" in query.answer.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_a_tap_from_another_message_with_the_same_token_is_expired_and_keeps_the_keyboard():
    """A copied keyboard (a forward) carries the same token on a different message: it must not
    inject into the original chat, and the original keyboard stays usable."""
    adapter = _adapter()
    await adapter.attach_suggested_replies(_source(), "900", ("A", "B"))
    token = _token_of(adapter)

    copied = _tap(f"sg:{token}:0", message_id=901)
    await adapter._handle_suggestion_callback(copied, copied.data, adapter._callback_ctx(copied))
    adapter.handle_message.assert_not_called()
    assert "expired" in copied.answer.await_args.kwargs["text"].lower()

    original = _tap(f"sg:{token}:0", message_id=900)
    await adapter._handle_suggestion_callback(original, original.data, adapter._callback_ctx(original))
    adapter.handle_message.assert_awaited_once()

