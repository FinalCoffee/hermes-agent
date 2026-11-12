"""Streaming a reply that ends with a suggested-reply footer: no byte of the footer is ever sent or edited.

Drives the real consumer against a recording platform adapter. Every payload the platform receives is
checked, so a marker fragment that reaches a draft, an edit, a split chunk or the finalize edit fails.
"""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, utf16_len
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

FOOTER = "[suggestions: Book it | Show more]"


class _RecordingAdapter:
    """Telegram-shaped transport: every send and edit payload is recorded in order, and an oversized
    payload is cut into 4,000-unit chunks the way Telegram's send and finalize edit cut them (the
    first chunk edits the message, the rest are new messages)."""

    LIMIT = 4000

    def __init__(self):
        self.payloads: list[str] = []
        self.visible: dict[str, str] = {}
        self._next = 0

        cls = type(
            "RecordingStreamAdapter",
            (BasePlatformAdapter,),
            {
                "MAX_MESSAGE_LENGTH": 4096,
                "message_len_fn": property(lambda self: utf16_len),
            },
        )
        cls.__abstractmethods__ = frozenset()
        self.adapter = cls.__new__(cls)
        self.adapter._typing_paused = set()
        self.adapter._fatal_error_message = None
        self.adapter.send = AsyncMock(side_effect=self._send)
        self.adapter.edit_message = AsyncMock(side_effect=self._edit)

    @classmethod
    def _chunks(cls, content: str) -> list[str]:
        chunks, current = [], ""
        for line in content.split("\n"):
            candidate = line if not current else current + "\n" + line
            if current and utf16_len(candidate) > cls.LIMIT:
                chunks.append(current)
                current = line
            else:
                current = candidate
        chunks.append(current)
        return chunks

    def _new_message(self, content: str) -> str:
        self._next += 1
        message_id = str(self._next)
        self.payloads.append(content)
        self.visible[message_id] = content
        return message_id

    async def _send(self, chat_id, content, **kwargs):
        last = None
        for chunk in self._chunks(content):
            last = self._new_message(chunk)
        return SimpleNamespace(success=True, message_id=last)

    async def _edit(self, chat_id, message_id, content, **kwargs):
        chunks = self._chunks(content)
        self.payloads.append(chunks[0])
        self.visible[str(message_id)] = chunks[0]
        last = str(message_id)
        for chunk in chunks[1:]:
            last = self._new_message(chunk)
        return SimpleNamespace(success=True, message_id=last)


def _assert_no_footer(payloads):
    for payload in payloads:
        assert "sugg" not in payload.lower(), payload
        assert "|" not in payload, payload


def test_every_prefix_of_the_footer_stays_off_screen_and_the_finalize_is_clean():
    recorder = _RecordingAdapter()
    consumer = GatewayStreamConsumer(
        recorder.adapter, "chat-1", StreamConsumerConfig(edit_interval=0.0)
    )
    answer = "Answer one."
    full = f"{answer}\n{FOOTER}"

    async def drive():
        for end in range(
            1, len(full) + 1
        ):  # one delta per character, as a model streams
            await consumer._send_or_edit(full[:end])
        await consumer._send_or_edit(full, finalize=True)

    asyncio.run(drive())

    _assert_no_footer(recorder.payloads)
    assert recorder.payloads[-1] == answer
    assert recorder.visible == {"1": answer}


@pytest.mark.parametrize("answer_length", range(3850, 4100, 17))
def test_footer_right_at_the_split_budget_never_leaks_into_a_chunk(answer_length):
    """A footer whose line starts near the per-message budget: whichever way the split falls (at the
    newline before the footer, or hard at the budget), no chunk shows a fragment of it."""
    recorder = _RecordingAdapter()
    consumer = GatewayStreamConsumer(recorder.adapter, "chat-1", StreamConsumerConfig(edit_interval=0.0))
    answer = "A" * answer_length  # one line: the footer's newline is the only split candidate
    consumer.on_delta(f"{answer}\n{FOOTER}")
    consumer.finish()
    asyncio.run(asyncio.wait_for(consumer.run(), timeout=10))

    _assert_no_footer(recorder.payloads)
    shown = "".join(recorder.visible[key] for key in sorted(recorder.visible, key=int))
    assert shown.count("A") == answer_length  # the platform adds its own "(1/2)" chunk markers, nothing else


def test_long_streamed_answer_with_a_footer_splits_without_leaking_it():
    recorder = _RecordingAdapter()
    consumer = GatewayStreamConsumer(recorder.adapter, "chat-1", StreamConsumerConfig(edit_interval=0.0))
    answer = "\n\n".join(f"Paragraph {i}: check the fare rules before you pay for the seat." for i in range(90))
    assert utf16_len(answer) > 4096  # the reply must cross Telegram's limit to exercise the split

    consumer.on_delta(f"{answer}\n{FOOTER}")
    consumer.finish()
    asyncio.run(asyncio.wait_for(consumer.run(), timeout=10))

    _assert_no_footer(recorder.payloads)
    assert len(recorder.visible) >= 2  # the answer really was split across messages
    shown = "".join(recorder.visible[key] for key in sorted(recorder.visible, key=int))
    shown = re.sub(r" \(\d+/\d+\)", "", shown)  # the platform's own chunk markers
    assert shown.replace("\n", "") == answer.replace("\n", "")


def test_prose_after_a_mid_text_footer_is_streamed_and_the_footer_never_is():
    recorder = _RecordingAdapter()
    consumer = GatewayStreamConsumer(
        recorder.adapter, "chat-1", StreamConsumerConfig(edit_interval=0.0)
    )
    full = "Here are hotels.\n[suggestions: A | B]\nNote: prices are indicative."

    async def drive():
        for end in range(1, len(full) + 1):
            await consumer._send_or_edit(full[:end])
        await consumer._send_or_edit(full, finalize=True)

    asyncio.run(drive())

    _assert_no_footer(recorder.payloads)
    assert recorder.payloads[-1] == "Here are hotels.\nNote: prices are indicative."
