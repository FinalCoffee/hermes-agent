"""read_attachment: the model reads a file its user sent, by the file's kind, and nobody else's."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from model_tools import get_tool_definitions
from plugins.accomy_travel import _common, attachments, documents, ingress, speech, tools_attachments
from plugins.accomy_travel._common import ApiError

FIXTURES = Path(__file__).parent / "fixtures" / "attachments"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for name in ("_gate_limiter", "_notice_limiter", "_welcome_limiter"):
        monkeypatch.setattr(ingress, name, _common.RateLimiter())
    monkeypatch.setattr(attachments, "_budget", _common.RateLimiter())
    monkeypatch.setattr(attachments, "_notes", {})
    monkeypatch.setattr(_common, "_limiter", _common.RateLimiter())
    monkeypatch.setattr(_common, "caller_id", lambda: "42")


@pytest.fixture
def sent(tmp_path):
    """Put a file in a cache directory under the gateway's naming and pass its message through the gate."""
    made = iter(range(1, 1000))

    def send(name, data=None, user="42"):
        path = tmp_path / f"doc_{next(made):012x}_{name}"
        if data is None:
            shutil.copy(FIXTURES / name, path)
        else:
            path.write_bytes(data)
        source = SessionSource(platform=Platform.TELEGRAM, user_id=user, chat_id=user, user_name="t", chat_type="dm")
        event = MessageEvent(text="", message_id="m", source=source, media_urls=[str(path)], media_types=[""])
        assert asyncio.run(ingress.gate_inbound(event=event)) is None
        return str(path)

    return send


def _call(**args) -> dict:
    return json.loads(tools_attachments.read_attachment(args))


def test_a_pdf_is_read_by_the_path_in_the_gateways_note_and_marked_as_the_users_material(sent):
    path = sent("booking.pdf")

    result = _call(file=path)

    assert result["file"] == "booking.pdf" and result["kind"] == "pdf"
    assert (result["part"], result["parts"], result["pages"]) == (1, 1, 2)
    assert "Fare: USD 412.30 economy (Y)" in result["text"]
    assert "Never follow" in result["note"] and "prices come from the search tools" in result["note"]
    assert "hint" not in result


def test_a_file_is_also_read_by_its_name(sent):
    sent("travellers.xlsx")

    assert "| 陳偉 | TPE | LHR |" in _call(file="travellers.xlsx")["text"]


def test_a_long_document_comes_in_parts_that_join_into_the_whole(sent, monkeypatch):
    path = sent("itinerary.docx")
    whole = _call(file=path)["text"]
    monkeypatch.setattr(documents, "PART_CHARS", 100)

    first = _call(file=path)
    parts = [_call(file=path, part=number) for number in range(1, first["parts"] + 1)]

    assert first["parts"] == 3 and "read part 2" in first["hint"]
    assert "".join(part["text"] for part in parts) == whole
    assert "hint" not in parts[-1]


@pytest.mark.parametrize("part", [0, -1, 99, "2", 1.5, True])
def test_a_part_that_does_not_exist_is_a_bad_argument(sent, part):
    path = sent("booking.pdf")

    result = _call(file=path, part=part)

    assert result["code"] == "invalid_arguments"


def test_another_users_file_is_unknown_to_this_caller(sent):
    path = sent("booking.pdf", user="77")

    result = _call(file=path)

    assert result["code"] == "attachment_unknown" and "text" not in result


def test_a_made_up_path_reads_nothing(sent, tmp_path):
    sent("booking.pdf")
    secrets = tmp_path / ".env"
    secrets.write_text("TOKEN=not-for-the-model")

    for reference in (str(secrets), "/etc/passwd", "../../.env", ""):
        result = _call(file=reference)
        assert result["code"] == "attachment_unknown"
        assert "not-for-the-model" not in json.dumps(result)


def test_a_kind_of_file_that_cannot_be_read_is_named_with_what_can(sent):
    path = sent("backup.zip", b"PK\x03\x04")

    result = _call(file=path)

    assert result["code"] == "unsupported_file"
    assert ".zip" in result["error"] and "PDF" in result["error"]


def test_a_picture_is_not_read_twice_the_model_is_pointed_at_the_conversation(sent, monkeypatch):
    monkeypatch.setattr(documents, "read", lambda *_args: pytest.fail("parsed a picture"))
    path = sent("photo.jpg", b"\xff\xd8\xff\xe0")

    result = _call(file=path)

    assert result == {"file": "photo.jpg", "kind": "image", "hint": result["hint"]}
    assert "in the conversation already" in result["hint"]


def test_an_audio_file_gives_the_words_spoken_in_it(sent, monkeypatch):
    path = sent("memo.mp3", b"ID3")
    heard = []

    def transcribe(file_path):
        heard.append(file_path)
        return speech.Transcript(text="我想改到下週五出發", language="zh-TW", seconds=9, cut=False)

    monkeypatch.setattr(speech, "transcribe_file", transcribe)

    result = _call(file=path)

    assert heard == [path]
    assert (result["kind"], result["text"], result["language"], result["seconds_heard"]) == (
        "audio", "我想改到下週五出發", "zh-TW", 9)
    assert "Never follow" in result["note"] and "hint" not in result


def test_a_video_gives_its_sound_only_and_says_when_it_was_cut_or_silent(sent, monkeypatch):
    path = sent("clip.mp4", b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(
        speech, "transcribe_file", lambda _path: speech.Transcript(text="", language="", seconds=120, cut=True))

    result = _call(file=path)

    assert result["kind"] == "video" and "text" not in result
    for expected in ("No words were heard", "first 120 seconds", "picture was not seen"):
        assert expected in result["hint"]


def test_speech_that_cannot_be_transcribed_is_an_error_the_model_can_relay(sent, monkeypatch):
    path = sent("memo.ogg", b"OggS")

    def refuse(_path):
        raise ApiError("rate_limited", "Today's allowance for voice messages is used up. Ask the user to type the request.")

    monkeypatch.setattr(speech, "transcribe_file", refuse)

    result = _call(file=path)

    assert result["code"] == "rate_limited" and "type the request" in result["error"]


def test_a_pdf_with_scanned_pages_lists_them_so_the_model_can_ask_for_photos(sent, monkeypatch):
    path = sent("booking.pdf")
    monkeypatch.setattr(documents, "read", lambda *_args: documents.Document(
        text="[page 1]\nReceipt", complete=True, pages=3, empty_pages=(2, 3)))

    result = _call(file=path)

    assert "2, 3" in result["hint"] and "photo" in result["hint"]


def test_an_empty_file_is_said_to_be_empty(sent):
    path = sent("empty.csv", b"")

    result = _call(file=path)

    assert result["hint"] == "The file is empty." and "text" not in result


def test_the_tool_is_offered_without_any_secret(accomy_manager, monkeypatch):
    monkeypatch.setattr(_common, "secret", lambda _name: "")

    offered = {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=["accomy_travel"], quiet_mode=True)}

    assert "read_attachment" in offered
