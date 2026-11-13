"""Telegram intake for a text-only bot: forged update prompts write nothing, and refused attachments are never fetched.

Behaviour contracts on a real ``TelegramAdapter`` with a fake bot and fake messages. With
``telegram.accept_attachments: false`` an attachment reaches the agent as text plus one note: no
``get_file``, no cache write, no vision-model call. With the default, attachments are fetched as before.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.waiting import async_wait_until

NOTE = "does not read attachments"  # the model-visible note, whatever its exact wording
CHAT = "100"
_SESSION = "agent:main:telegram:dm:100"


def _adapter(extra=None) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra=extra if extra is not None else {}))
    adapter._bot = AsyncMock()
    adapter._bot.username = "accomy_bot"
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter._app = MagicMock()
    adapter.handle_message = AsyncMock()
    adapter._is_callback_user_authorized = lambda *a, **k: True
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0.01
    return adapter


def _file(data: bytes = b"bytes"):
    f = AsyncMock()
    f.file_path = "photos/file_1.jpg"
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    return f


def _media(file_obj=None):
    """A Telegram attachment object whose ``get_file`` is observable."""
    media = MagicMock()
    media.file_name = "report.pdf"
    media.mime_type = "application/pdf"
    media.file_size = 1024
    media.file_unique_id = "uniq-1"
    media.emoji = "🙂"
    media.set_name = "pack"
    media.is_animated = False
    media.is_video = False
    media.get_file = AsyncMock(return_value=file_obj or _file())
    return media


def _message(*, caption=None, photo=None, sticker=None, document=None, media_group_id=None, reply_to=None, text=None):
    msg = MagicMock()
    msg.message_id = 42
    msg.text = text
    msg.caption = caption
    msg.caption_entities = None
    msg.entities = None
    msg.date = None
    msg.photo = photo
    msg.video = msg.video_note = msg.audio = msg.voice = msg.animation = None
    msg.sticker = sticker
    msg.document = document
    msg.media_group_id = media_group_id
    msg.reply_to_message = reply_to
    msg.chat = SimpleNamespace(id=100, type="private", title=None, full_name="Ana")
    msg.from_user = SimpleNamespace(id=100, full_name="Ana", is_bot=False, username=None)
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    return msg


def _update(msg):
    update = MagicMock()
    update.message = msg
    update.effective_message = msg
    update.update_id = 7
    return update


async def _wait_for_dispatch(adapter) -> None:
    await async_wait_until(lambda: adapter.handle_message.await_count >= 1)
    # An album is one turn: a second flush, if it were wrong, lands after the debounce, so wait out that window.
    await asyncio.sleep(0.05)  # health: allow HX013 -- the post-debounce window in which a second album turn would show up


@pytest.fixture(autouse=True)
def _cache_dirs(tmp_path, monkeypatch):
    for name in ("DOCUMENT", "IMAGE", "AUDIO", "VIDEO"):
        monkeypatch.setattr(f"gateway.platforms.base.{name}_CACHE_DIR", tmp_path / name.lower())
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")


# ---------------------------------------------------------------------------
# 1. update_prompt callback: a forged tap writes nothing
# ---------------------------------------------------------------------------

def _tap(data: str) -> MagicMock:
    query = AsyncMock(data=data)
    query.message = MagicMock(chat_id=12345, text_html="<b>Update?</b>")
    query.from_user = MagicMock(first_name="Mallory", id="666")
    return MagicMock(callback_query=query)


@pytest.mark.asyncio
async def test_forged_update_prompt_with_no_card_sent_writes_nothing(tmp_path):
    adapter = _adapter()  # this process never sent an update card
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        await adapter._handle_callback_query(_tap("update_prompt:y:1"), MagicMock())
        await adapter._handle_callback_query(_tap("update_prompt:n"), MagicMock())
    assert not (tmp_path / ".update_response").exists()


@pytest.mark.asyncio
async def test_update_card_answer_must_be_y_or_n_and_the_card_still_answers(tmp_path):
    adapter = _adapter()
    await adapter.send_update_prompt(chat_id="12345", prompt="Restore stash?", session_key=_SESSION)
    card_id = adapter._update_prompt_id
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        await adapter._handle_callback_query(_tap(f"update_prompt:banana:{card_id}"), MagicMock())
        assert not (tmp_path / ".update_response").exists(), "a forged answer reached the updater"
        await adapter._handle_callback_query(_tap(f"update_prompt:y:{card_id}"), MagicMock())  # the legitimate tap
    assert (tmp_path / ".update_response").read_text() == "y"


# ---------------------------------------------------------------------------
# 2. telegram.accept_attachments: false refuses attachments before any download, cache or vision call
# ---------------------------------------------------------------------------

REFUSED = {"accept_attachments": False}


@pytest.mark.asyncio
async def test_refused_photo_with_caption_dispatches_text_and_note_without_download_or_vision():
    adapter = _adapter(REFUSED)
    photo = [_media(), _media()]
    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock()) as vision:
        await adapter._handle_media_message(_update(_message(caption="Is this place open?", photo=photo)), MagicMock())
    photo[-1].get_file.assert_not_called()
    vision.assert_not_called()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.TEXT and event.media_urls == []
    assert event.text.startswith("Is this place open?")
    assert NOTE in event.text


@pytest.mark.asyncio
async def test_refused_sticker_and_document_without_caption_dispatch_the_note_alone():
    adapter = _adapter(REFUSED)
    sticker, document = _media(), _media()
    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock()) as vision:
        await adapter._handle_media_message(_update(_message(sticker=sticker)), MagicMock())
        await adapter._handle_media_message(_update(_message(document=document)), MagicMock())
    sticker.get_file.assert_not_called()
    document.get_file.assert_not_called()
    vision.assert_not_called()
    texts = [c.args[0].text for c in adapter.handle_message.await_args_list]
    assert len(texts) == 2 and all(NOTE in t for t in texts)


@pytest.mark.asyncio
async def test_refused_album_is_one_turn_with_one_note():
    adapter = _adapter(REFUSED)
    items = [_message(caption="Two shots of the hotel", photo=[_media()], media_group_id="album-1"),
             _message(photo=[_media()], media_group_id="album-1"),
             _message(photo=[_media()], media_group_id="album-1")]
    for msg in items:
        await adapter._handle_media_message(_update(msg), MagicMock())
    await _wait_for_dispatch(adapter)
    assert adapter.handle_message.await_count == 1
    text = adapter.handle_message.await_args.args[0].text
    assert text.count(NOTE) == 1 and "Two shots of the hotel" in text
    for msg in items:
        msg.photo[-1].get_file.assert_not_called()


@pytest.mark.asyncio
async def test_refused_text_reply_to_media_is_not_redownloaded_and_gets_a_note():
    adapter = _adapter(REFUSED)
    replied = _message(photo=[_media()])
    reply = _message(text="what is in that picture?", reply_to=replied)
    await adapter._handle_text_message(_update(reply), MagicMock())
    await _wait_for_dispatch(adapter)
    replied.photo[-1].get_file.assert_not_called()
    text = adapter.handle_message.await_args.args[0].text
    assert "what is in that picture?" in text and NOTE in text


@pytest.mark.asyncio
async def test_default_still_downloads_a_document():
    adapter = _adapter({})  # accept_attachments unset: today's behaviour
    document = _media()
    await adapter._handle_media_message(_update(_message(document=document)), MagicMock())
    document.get_file.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
