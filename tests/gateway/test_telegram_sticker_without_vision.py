"""A sticker never needs the vision tool to be importable: animated ones get a placeholder, static ones fall back."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg
from plugins.platforms.telegram import telegram_stickers


@pytest.fixture
def vision_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.vision_tools", None)  # an install without the optional vision stack


def _adapter():
    adapter = tg.TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _message(*, animated: bool, unique_id: str):
    return SimpleNamespace(sticker=SimpleNamespace(
        is_animated=animated, is_video=False, emoji="😀", set_name="pack", file_unique_id=unique_id, file_id="f"))


def test_animated_sticker_gets_its_placeholder(vision_missing):
    event = SimpleNamespace(text="")

    asyncio.run(telegram_stickers.describe_sticker(_adapter(), _message(animated=True, unique_id="anim-1"), event))

    assert "😀" in event.text


def test_static_sticker_falls_back_to_its_emoji(vision_missing, monkeypatch):
    monkeypatch.setattr(tg, "fetch_telegram_file", AsyncMock(return_value=(None, b"webp")))
    monkeypatch.setattr(tg, "cache_image_from_bytes_async", AsyncMock(return_value="/tmp/sticker.webp"))
    event = SimpleNamespace(text="")

    asyncio.run(telegram_stickers.describe_sticker(_adapter(), _message(animated=False, unique_id="static-1"), event))

    assert "a sticker with emoji 😀" in event.text
