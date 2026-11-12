"""The non-streamed final: the footer is stripped at extraction and the delivered message gets the buttons.

Covers the path a turn takes when nothing streamed (or streaming is off): ``_extract_response_content``
then ``_send_final_text``, with the ledgered send faked so the test sees exactly which message id the
keyboard is attached to.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="m")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _event() -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1"
    )
    return MessageEvent(text="plan a trip", source=source, message_id="in-1")


async def _extract(adapter, text: str, *, ephemeral: bool = False):
    return await adapter._extract_response_content(
        text, _event(), "s1", is_ephemeral_response=ephemeral
    )


@pytest.mark.asyncio
async def test_extraction_removes_the_footer_and_returns_its_options():
    adapter = _Adapter()
    extracted = await _extract(
        adapter, "Your flight leaves at 09:40.\n[suggestions: Book it | Show more]"
    )

    assert extracted.text_content == "Your flight leaves at 09:40."
    assert extracted.suggestions == ("Book it", "Show more")


@pytest.mark.asyncio
async def test_a_footer_mid_answer_is_ordinary_text():
    adapter = _Adapter()
    extracted = await _extract(adapter, "Use [suggestions: A | B] as the template.")

    assert "[suggestions: A | B]" in extracted.text_content
    assert extracted.suggestions == ()


@pytest.mark.asyncio
async def test_delivered_final_gets_the_options_on_its_last_chunk():
    adapter = _Adapter()
    adapter.send_final_ledgered = AsyncMock(
        return_value=(
            SendResult(
                success=True,
                message_id="30",
                raw_response={"message_ids": ["10", "20", "30"]},
            ),
            adapter,
        )
    )
    adapter.attach_suggested_replies = AsyncMock(return_value=True)
    recorded = []

    await adapter._send_final_text(
        _event(),
        "s1",
        "Long answer.",
        {},
        False,
        0,
        recorded.append,
        suggestions=("Yes", "No"),
    )

    adapter.attach_suggested_replies.assert_awaited_once()
    source, message_id, options = adapter.attach_suggested_replies.await_args.args
    assert source.chat_id == "c1"
    assert message_id == "30"
    assert options == ("Yes", "No")


@pytest.mark.asyncio
async def test_a_final_without_options_still_retires_the_previous_keyboard():
    adapter = _Adapter()
    adapter.send_final_ledgered = AsyncMock(
        return_value=(SendResult(success=True, message_id="7"), adapter)
    )
    adapter.attach_suggested_replies = AsyncMock(return_value=False)

    await adapter._send_final_text(
        _event(), "s1", "Plain answer.", {}, False, 0, lambda _r: None
    )

    adapter.attach_suggested_replies.assert_awaited_once()
    assert adapter.attach_suggested_replies.await_args.args[2] == ()


@pytest.mark.asyncio
async def test_failed_or_auto_deleting_reply_gets_no_keyboard():
    adapter = _Adapter()
    adapter.attach_suggested_replies = AsyncMock(return_value=True)
    adapter.send_final_ledgered = AsyncMock(
        return_value=(SendResult(success=False, error="down"), adapter)
    )
    await adapter._send_final_text(
        _event(), "s1", "Answer.", {}, False, 0, lambda _r: None, suggestions=("A", "B")
    )

    adapter.send_final_ledgered = AsyncMock(
        return_value=(SendResult(success=True, message_id="8"), adapter)
    )
    await adapter._send_final_text(
        _event(), "s1", "Answer.", {}, True, 30, lambda _r: None, suggestions=("A", "B")
    )

    adapter.attach_suggested_replies.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_platform_without_buttons_is_a_silent_no_op():
    adapter = _Adapter()
    adapter.send_final_ledgered = AsyncMock(
        return_value=(SendResult(success=True, message_id="9"), adapter)
    )

    await adapter._send_final_text(
        _event(), "s1", "Answer.", {}, False, 0, lambda _r: None, suggestions=("A", "B")
    )

    assert (
        await adapter.attach_suggested_replies(_event().source, "9", ("A", "B"))
        is False
    )
