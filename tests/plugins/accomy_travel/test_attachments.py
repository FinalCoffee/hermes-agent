"""Files a sender attaches: who may read which file, the daily allowance, and what the inbound gate does
with the files of a message it lets through or drops."""

import asyncio
import functools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.accomy_travel import _common, attachments, ingress
from plugins.accomy_travel._common import ApiError


def _run(test):
    @functools.wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return run


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    clock = _Clock()
    for name in ("_gate_limiter", "_notice_limiter", "_welcome_limiter"):
        monkeypatch.setattr(ingress, name, _common.RateLimiter(clock=clock))
    monkeypatch.setattr(attachments, "_budget", _common.RateLimiter(clock=clock))
    monkeypatch.setattr(attachments, "_notes", {})
    monkeypatch.setattr(attachments.time, "monotonic", clock)
    return clock


class _Gateway:
    def __init__(self) -> None:
        self.adapter = SimpleNamespace(send=AsyncMock())

    def _is_user_authorized_for_source(self, source) -> bool:
        return True

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _thread_metadata_for_source(self, source):
        return None


@pytest.fixture
def cache(tmp_path):
    """Makes files named the way the gateway names what it caches."""
    made = iter(range(1, 10_000))

    def make(name="itinerary.pdf", data=b"%PDF-1.4"):
        path = tmp_path / f"doc_{next(made):012x}_{name}"
        path.write_bytes(data)
        return str(path)

    return make


def _event(files=(), text="", user="42", inlined=None, kind=MessageType.DOCUMENT) -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, user_id=user, chat_id=user, user_name="t", chat_type="dm")
    return MessageEvent(
        text=text, message_id="m1", source=source, message_type=kind, media_urls=list(files),
        media_types=["application/octet-stream"] * len(files),
        media_text_inlined=list(inlined) if inlined is not None else [False] * len(files))


async def _gate(event, gateway=None):
    return await ingress.gate_inbound(event=event, gateway=gateway)


def _as(monkeypatch, caller):
    monkeypatch.setattr(_common, "caller_id", lambda: caller)


@_run
async def test_a_sender_reads_its_own_file_by_the_saved_path_or_by_its_name(cache, monkeypatch):
    path = cache("itinerary.pdf")
    assert await _gate(_event([path])) is None
    _as(monkeypatch, "42")

    assert attachments.find(path).path == path
    assert attachments.find("itinerary.pdf").path == path
    assert attachments.find(path).name == "itinerary.pdf"


@_run
async def test_another_sender_cannot_read_the_file_even_with_its_exact_path(cache, monkeypatch):
    path = cache("passport.pdf")
    assert await _gate(_event([path], user="42")) is None
    _as(monkeypatch, "77")

    for reference in (path, "passport.pdf"):
        with pytest.raises(ApiError) as refused:
            attachments.find(reference)
        assert refused.value.code == "attachment_unknown"


@_run
async def test_a_path_nobody_sent_is_unknown_even_when_the_file_exists(cache, monkeypatch, tmp_path):
    secrets = tmp_path / ".env"
    secrets.write_text("TOKEN=1")
    assert await _gate(_event([cache()])) is None
    _as(monkeypatch, "42")

    for reference in (str(secrets), "../.env", "", "   "):
        with pytest.raises(ApiError):
            attachments.find(reference)


@_run
async def test_of_two_files_with_one_name_the_newest_is_read(cache, monkeypatch):
    first, second = cache("plan.docx"), cache("plan.docx")
    assert await _gate(_event([first])) is None
    assert await _gate(_event([second])) is None
    _as(monkeypatch, "42")

    assert attachments.find("plan.docx").path == second
    assert attachments.find(first).path == first


@_run
async def test_a_file_is_forgotten_after_a_day_and_when_the_cache_has_removed_it(cache, monkeypatch, clock):
    kept, removed = cache("kept.pdf"), cache("removed.pdf")
    assert await _gate(_event([kept, removed])) is None
    _as(monkeypatch, "42")
    attachments.os.unlink(removed)

    with pytest.raises(ApiError):
        attachments.find(removed)
    assert attachments.find(kept).path == kept
    clock.now += 86400.0
    with pytest.raises(ApiError):
        attachments.find(kept)


@_run
async def test_a_sender_is_held_to_its_files_per_day_and_told_once(cache, monkeypatch, clock):
    monkeypatch.setattr(attachments, "FILES_PER_DAY", 3)
    gateway = _Gateway()
    for _ in range(3):
        assert await _gate(_event([cache()]), gateway) is None
        clock.now += 61.0  # stays clear of the turns-per-minute limit

    over = cache()
    assert await _gate(_event([over]), gateway) == {"action": "skip", "reason": "accomy_files_limit"}
    assert await _gate(_event([cache()]), gateway) == {"action": "skip", "reason": "accomy_files_limit"}

    assert gateway.adapter.send.await_count == 1
    assert "檔案" in gateway.adapter.send.await_args.args[1]
    assert await _gate(_event(text="找東京的飯店"), gateway) is None  # text is still welcome
    assert await _gate(_event([cache()], user="77"), gateway) is None  # and so is another sender's file
    clock.now += 86400.0
    assert await _gate(_event([cache()]), gateway) is None


@_run
async def test_an_album_counts_every_file_and_is_refused_whole_when_it_does_not_fit(cache, monkeypatch):
    monkeypatch.setattr(attachments, "FILES_PER_DAY", 3)
    album = [cache(f"page{number}.jpg") for number in range(4)]

    assert await _gate(_event(album, kind=MessageType.PHOTO)) == {"action": "skip", "reason": "accomy_files_limit"}

    assert await _gate(_event([cache("a.jpg"), cache("b.jpg"), cache("c.jpg")], kind=MessageType.PHOTO)) is None


@_run
async def test_the_bot_wide_file_total_holds_however_many_accounts_send(cache, monkeypatch):
    monkeypatch.setattr(attachments, "GLOBAL_FILES_PER_DAY", 4)
    for sender in ("1", "2", "3", "4"):
        assert await _gate(_event([cache()], user=sender)) is None

    assert await _gate(_event([cache()], user="5")) == {"action": "skip", "reason": "accomy_global_files_limit"}


@_run
async def test_the_files_of_a_dropped_message_are_deleted_and_those_of_an_admitted_one_stay(cache, monkeypatch):
    monkeypatch.setattr(attachments, "FILES_PER_DAY", 1)
    admitted, dropped = cache("first.pdf"), cache("second.pdf")

    assert await _gate(_event([admitted])) is None
    assert (await _gate(_event([dropped])))["action"] == "skip"

    assert attachments.os.path.exists(admitted)
    assert not attachments.os.path.exists(dropped)


@_run
async def test_a_flood_of_files_past_the_turn_limit_leaves_nothing_on_disk(cache):
    for _ in range(ingress.TURNS_PER_MINUTE):
        assert await _gate(_event(text="hi")) is None
    flood = [cache(f"flood{number}.bin", b"x" * 1024) for number in range(20)]

    for path in flood:
        assert (await _gate(_event([path])))["reason"] == "accomy_minute_limit"

    assert [path for path in flood if attachments.os.path.exists(path)] == []


@_run
async def test_a_file_sent_with_a_welcome_command_is_deleted(cache):
    path = cache("photo.jpg")
    gateway = _Gateway()
    gateway.adapter.send = AsyncMock()

    result = await _gate(_event([path], text="/help", kind=MessageType.PHOTO), gateway)

    assert result["action"] == "skip"
    assert not attachments.os.path.exists(path)


@_run
async def test_a_file_sent_with_a_session_command_is_removed_and_the_command_always_goes_through(cache, monkeypatch):
    monkeypatch.setattr(attachments, "FILES_PER_DAY", 1)
    assert await _gate(_event([cache()])) is None  # the day's allowance is now used up

    for command in ("/stop", "/new", "/stop"):
        path = cache("photo.jpg")
        event = _event([path], text=command, kind=MessageType.PHOTO)

        assert await _gate(event) is None  # a running turn can still be stopped
        assert not attachments.os.path.exists(path)
        assert (event.media_urls, event.media_types, event.media_text_inlined) == ([], [], [])


@_run
async def test_the_file_of_a_sender_auth_refuses_is_removed_and_the_message_is_left_to_the_gateway(cache):
    class Closed(_Gateway):
        def _is_user_authorized_for_source(self, source) -> bool:
            return False

    path = cache("stranger.pdf")

    assert await _gate(_event([path], user="666"), Closed()) is None
    assert not attachments.os.path.exists(path)


@_run
async def test_a_message_refused_for_its_files_costs_the_sender_no_turn(cache, monkeypatch):
    monkeypatch.setattr(attachments, "FILES_PER_DAY", 1)
    monkeypatch.setattr(ingress, "TURNS_PER_MINUTE", 3)
    assert await _gate(_event([cache()])) is None  # turn 1 of 3

    for _ in range(10):
        assert (await _gate(_event([cache()])))["reason"] == "accomy_files_limit"

    assert await _gate(_event(text="找東京的飯店")) is None  # turn 2
    assert await _gate(_event(text="找機票")) is None  # turn 3
    assert (await _gate(_event(text="還有嗎")))["reason"] == "accomy_minute_limit"


def _with_text_inside(path, caption=""):
    """The text the adapter gives a message when it puts a text file's content into it."""
    content = open(path, encoding="utf-8").read()
    name = attachments.os.path.basename(path).split("_", 2)[2]
    return f"[Content of {name}]:\n{content}" + (f"\n\n{caption}" if caption else "")


@_run
async def test_a_text_files_content_is_taken_out_of_the_message_and_the_model_is_told_to_read_the_file(cache):
    from gateway.run import GatewayRunner

    path = cache("notes.txt", b"Day 1: Tokyo\nDay 2: Kyoto\n")
    event = _event([path], text=_with_text_inside(path, "幫我整理這份行程"), inlined=[True])
    event.media_types = ["text/plain"]

    result = await _gate(event)

    assert result == {"action": "rewrite", "text": "幫我整理這份行程"}
    assert event.media_text_inlined == [False]
    note = GatewayRunner._prepend_inbound_document_notes(event, result["text"])
    assert "Its content is not inlined here" in note and path in note
    assert "Day 1: Tokyo" not in note


@_run
async def test_a_text_file_sent_without_a_caption_leaves_an_empty_message(cache):
    path = cache("plan.md", b"# Plan\n- flights\n- hotel\n")
    event = _event([path], text=_with_text_inside(path), inlined=[True])

    assert await _gate(event) == {"action": "rewrite", "text": ""}


@_run
async def test_the_length_cap_judges_what_was_typed_not_the_size_of_the_file(cache):
    big = cache("rows.csv", ("Taipei,Tokyo,2027-03-12\n" * 4000).encode())  # about 96,000 characters
    with_big_file = _event([big], text=_with_text_inside(big, "整理一下"), inlined=[True])
    small = cache("note.txt", b"hello")
    long_caption = _event([small], text=_with_text_inside(small, "a" * (ingress.MAX_TEXT_CHARS + 1)), inlined=[True])

    assert await _gate(with_big_file) == {"action": "rewrite", "text": "整理一下"}
    assert await _gate(long_caption) == {"action": "skip", "reason": "accomy_length_limit"}


@_run
async def test_an_album_of_text_files_keeps_only_the_caption(cache):
    first, second = cache("a.txt", b"alpha file"), cache("b.txt", b"beta file")
    text = f"{_with_text_inside(first)}\n\n{_with_text_inside(second, 'compare these')}"
    event = _event([first, second], text=text, inlined=[True, True])

    assert await _gate(event) == {"action": "rewrite", "text": "compare these"}
    assert event.media_text_inlined == [False, False]


@_run
async def test_a_file_whose_content_is_not_in_the_text_is_left_as_the_adapter_delivered_it(cache):
    path = cache("notes.txt", b"what the cache holds")
    event = _event([path], text="[Content of notes.txt]:\nsomething else\n\ncaption", inlined=[True])

    assert await _gate(event) is None
    assert event.media_text_inlined == [True]


@_run
async def test_typed_text_that_imitates_a_file_header_is_still_only_typed_text(cache):
    typed = "[Content of notes.txt]:\n" + "a" * (ingress.MAX_TEXT_CHARS + 1)

    assert await _gate(_event(text=typed)) == {"action": "skip", "reason": "accomy_length_limit"}
    assert await _gate(_event([cache("x.pdf")], text=typed)) == {"action": "skip", "reason": "accomy_length_limit"}


@_run
async def test_a_message_without_a_user_id_keeps_its_files_out_of_every_callers_reach(cache, monkeypatch):
    path = cache("anonymous.pdf")
    source = SessionSource(platform=Platform.TELEGRAM, user_id=None, chat_id="-100", user_name="", chat_type="channel")
    event = MessageEvent(text="", message_id="m", source=source, media_urls=[path], media_types=["application/pdf"])
    assert await _gate(event) is None

    for caller in ("", "local", "42", "agent:main:telegram:channel:-100"):
        _as(monkeypatch, caller)
        with pytest.raises(ApiError):
            attachments.find(path)


def test_what_a_file_is_follows_its_extension_whatever_the_case():
    assert attachments.kind_of("/cache/doc_1_Ticket.PDF") == attachments.PDF
    assert attachments.kind_of("/cache/doc_1_plan.XLSX") == attachments.OFFICE
    assert attachments.kind_of("/cache/audio_1.ogg") == attachments.AUDIO
    assert attachments.kind_of("/cache/video_1.mp4") == attachments.VIDEO
    assert attachments.kind_of("/cache/img_1.jpg") == attachments.IMAGE
    assert attachments.kind_of("/cache/doc_1_trip.ics") == attachments.TEXT
    assert attachments.kind_of("/cache/doc_1_mail.eml") == attachments.MAIL
    assert attachments.kind_of("/cache/doc_1_tool.exe") is None
    assert attachments.kind_of("/cache/doc_1_archive.zip") is None
    assert attachments.kind_of("/cache/doc_1_noextension") is None
