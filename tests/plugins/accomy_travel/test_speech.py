"""Speech to text through Amazon Transcribe: the signed address, the stream, the language strategy, the
allowance, and the provider the gateway calls for a voice note. No test reaches AWS: the socket is faked."""

import json
import shutil
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from plugins.accomy_travel import _common, attachments, speech
from plugins.accomy_travel._common import ApiError

VOICE = str(Path(__file__).parent / "fixtures" / "attachments" / "voice.ogg")
SECOND = speech._BYTES_PER_SECOND


class _Settings:
    def __init__(self, **values) -> None:
        self.values = {"transcribe_region": "ap-southeast-1", **values}

    def get_config(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    keys = {speech.ACCESS_KEY_ENV: "TESTKEYID", speech.SECRET_KEY_ENV: "test-secret"}
    monkeypatch.setattr(_common, "secret", lambda name: keys.get(name, ""))
    monkeypatch.setattr(speech, "_settings", _Settings())
    monkeypatch.setattr(speech, "_budget", _common.RateLimiter())
    monkeypatch.setattr(speech, "_recent", {})
    monkeypatch.setattr(speech.time, "sleep", lambda _seconds: None)
    return keys


def _event(kind: str, body: dict, **headers: str) -> bytes:
    """One event-stream message as Transcribe sends it."""
    packed = b"".join(
        bytes([len(name)]) + name.encode() + b"\x07" + struct.pack(">H", len(value)) + value.encode()
        for name, value in {":message-type": kind, **headers}.items())
    payload = json.dumps(body).encode()
    prelude = struct.pack(">II", 16 + len(packed) + len(payload), len(packed))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    message = prelude + packed + payload
    return message + struct.pack(">I", zlib.crc32(message))


def _result(text: str, *, partial: bool = False, language: str = "") -> bytes:
    result = {"IsPartial": partial, "Alternatives": [{"Transcript": text}]}
    if language:
        result["LanguageCode"] = language
    return _event("event", {"Transcript": {"Results": [result]}}, **{":event-type": "TranscriptEvent"})


class _Socket:
    """Answers with *frames*, then closes the way Transcribe does when the stream is over."""

    def __init__(self, frames, *, ending=None) -> None:
        self.frames = list(frames)
        self.ending = ending or ConnectionClosedOK(None, None)
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def send(self, message: bytes) -> None:
        self.sent.append(message)

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        raise self.ending


@pytest.fixture
def wire(monkeypatch):
    """Replaces the WebSocket: records each connection's address and options, answers from a queue of sockets."""
    opened = []

    def connect(url, **options):
        socket = opened_sockets.pop(0)
        opened.append((url, options, socket))
        return socket

    opened_sockets: list[_Socket] = []
    monkeypatch.setattr(speech, "connect", connect)
    return SimpleWire(opened, opened_sockets)


class SimpleWire:
    def __init__(self, opened, sockets) -> None:
        self.opened, self.sockets = opened, sockets

    def answer(self, *frames, ending=None) -> None:
        self.sockets.append(_Socket(frames, ending=ending))

    def query(self, index: int) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(urlsplit(self.opened[index][0]).query).items()}


def test_the_signed_address_is_the_one_awss_own_signer_produces():
    """The expected signature was computed with botocore's SigV4QueryAuth for the same key, time and query."""
    query = {
        "media-encoding": "pcm", "sample-rate": "16000", "identify-language": "true",
        "language-options": "zh-TW,en-US,ja-JP,ko-KR", "preferred-language": "zh-TW",
    }

    url = speech._signed_url(query, now=datetime(2027, 3, 12, 8, 50, 7, tzinfo=UTC))

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "wss", "transcribestreaming.ap-southeast-1.amazonaws.com:8443", "/stream-transcription-websocket")
    signed = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert signed["X-Amz-Signature"] == "0a67b07e37a10fefc387f19f15aaf64d29eb2e30183c0c93e9db841db4926a33"
    assert signed["X-Amz-Credential"] == "TESTKEYID/20270312/ap-southeast-1/transcribe/aws4_request"
    assert signed["language-options"] == "zh-TW,en-US,ja-JP,ko-KR"
    assert "test-secret" not in url


def test_a_stream_sends_the_audio_in_small_events_then_an_empty_one_and_joins_the_final_results(wire):
    wire.answer(
        _result("你好", partial=True), _result("你好，我想找機票", language="zh-TW"), _result("兩位大人", language="zh-TW"))
    pcm = b"\x01\x00" * (SECOND // 2)  # one second

    text, language = speech._stream(pcm, {"language-code": "zh-TW"})

    assert (text, language) == ("你好，我想找機票 兩位大人", "zh-TW")
    _, options, socket = wire.opened[0]
    assert options["ping_interval"] is None  # Transcribe answers no ping, and a pinging client would hang up
    audio = [speech._read_event(message) for message in socket.sent]
    assert [headers[":event-type"] for headers, _ in audio] == ["AudioEvent"] * len(audio)
    assert b"".join(payload for _, payload in audio) == pcm
    assert audio[-1][1] == b"" and all(0 < len(payload) <= speech._CHUNK for _, payload in audio[:-1])


def test_a_stream_transcribe_refuses_becomes_an_error_and_never_a_transcript(wire):
    wire.answer(_event("exception", {"Message": "denied"}, **{":exception-type": "BadRequestException"}))

    with pytest.raises(ApiError) as failed:
        speech._stream(b"\x00\x00" * 100, {"language-code": "en-US"})

    assert failed.value.code == "speech_failed"
    assert "denied" not in failed.value.message


@pytest.mark.parametrize("ending, code", [
    (ConnectionClosedError(None, None), "network"), (TimeoutError(), "timeout"), (OSError("refused"), "network")])
def test_a_broken_or_silent_connection_is_an_error_the_model_can_relay(wire, ending, code):
    wire.answer(_result("half a sent", partial=True), ending=ending)

    with pytest.raises(ApiError) as failed:
        speech._stream(b"\x00\x00" * 100, {"language-code": "en-US"})

    assert failed.value.code == code


def test_a_frame_that_is_not_an_event_stream_message_is_refused():
    for frame in ("text frame", b"short", struct.pack(">II", 999, 0) + b"\x00" * 12):
        with pytest.raises(ApiError):
            speech._read_event(frame)


def _heard(monkeypatch, *languages_found):
    """Fake the stream: record each pass as (seconds sent, query) and name the next language found."""
    found = list(languages_found)
    passes = []

    def stream(pcm, query):
        passes.append((len(pcm) // SECOND, query))
        return f"text{len(passes)}", (found.pop(0) if "identify-language" in query else "")

    monkeypatch.setattr(speech, "_stream", stream)
    return passes


def test_a_short_recording_is_identified_and_transcribed_in_one_pass(monkeypatch):
    passes = _heard(monkeypatch, "ja-JP")

    assert speech._hear(b"\x00" * (10 * SECOND), 10) == ("text1", "ja-JP")

    assert [seconds for seconds, _ in passes] == [10]
    assert passes[0][1]["language-options"] == "zh-TW,en-US,ja-JP,ko-KR"
    assert passes[0][1]["preferred-language"] == "zh-TW"


def test_a_long_recording_is_identified_on_its_opening_then_transcribed_whole_in_that_language(monkeypatch):
    passes = _heard(monkeypatch, "en-US")

    assert speech._hear(b"\x00" * (60 * SECOND), 60) == ("text2", "en-US")

    assert [seconds for seconds, _ in passes] == [speech._IDENTIFY_SECONDS, 60]
    assert passes[1][1]["language-code"] == "en-US" and "identify-language" not in passes[1][1]


def test_a_long_recording_that_opens_in_silence_is_identified_whole(monkeypatch):
    passes = _heard(monkeypatch, "", "ko-KR")

    assert speech._hear(b"\x00" * (60 * SECOND), 60) == ("text2", "ko-KR")

    assert [seconds for seconds, _ in passes] == [speech._IDENTIFY_SECONDS, 60]
    assert "identify-language" in passes[1][1]


def test_a_profile_with_one_language_skips_identification(monkeypatch):
    monkeypatch.setattr(speech, "_settings", _Settings(transcribe_languages=["en-GB"]))
    passes = _heard(monkeypatch)

    assert speech._hear(b"\x00" * (60 * SECOND), 60) == ("text1", "en-GB")

    assert passes == [(60, {"media-encoding": "pcm", "sample-rate": "16000", "language-code": "en-GB"})]


@pytest.fixture
def decoded(monkeypatch):
    """Skip ffmpeg: every file decodes to the given number of seconds."""
    def give(seconds, cut=False):
        monkeypatch.setattr(speech, "_pcm", lambda _path: (b"\x00" * (seconds * SECOND), cut))
    return give


def test_a_file_is_transcribed_once_however_often_it_is_asked_for(monkeypatch, decoded):
    decoded(5)
    passes = _heard(monkeypatch, "zh-TW", "zh-TW")

    first = speech.transcribe_file("/cache/audio_1.ogg")
    again = speech.transcribe_file("/cache/audio_1.ogg")
    other = speech.transcribe_file("/cache/audio_2.ogg")

    assert first == again == speech.Transcript(text="text1", language="zh-TW", seconds=5, cut=False)
    assert other.text == "text2" and len(passes) == 2


def test_each_sender_has_a_daily_allowance_of_speech_and_another_sender_is_not_held_to_it(monkeypatch, decoded):
    monkeypatch.setattr(speech, "SPEECH_SECONDS_PER_DAY", 100)
    monkeypatch.setattr(attachments, "owner_of", lambda path: "42" if "from42" in path else "77")
    decoded(40)
    passes = _heard(monkeypatch, *["zh-TW"] * 10)

    speech.transcribe_file("/cache/from42_a.ogg")
    speech.transcribe_file("/cache/from42_b.ogg")
    with pytest.raises(ApiError) as refused:
        speech.transcribe_file("/cache/from42_c.ogg")
    speech.transcribe_file("/cache/from77_a.ogg")

    assert refused.value.code == "rate_limited"
    assert "type" in refused.value.message
    assert len([seconds for seconds, query in passes if seconds == 40]) == 3  # the refused file reached no stream


def test_the_bot_wide_allowance_holds_across_senders(monkeypatch, decoded):
    monkeypatch.setattr(speech, "GLOBAL_SPEECH_SECONDS_PER_DAY", 50)
    monkeypatch.setattr(attachments, "owner_of", lambda path: path)
    decoded(30)
    _heard(monkeypatch, *["zh-TW"] * 4)

    speech.transcribe_file("/cache/a.ogg")
    with pytest.raises(ApiError) as refused:
        speech.transcribe_file("/cache/b.ogg")

    assert refused.value.code == "rate_limited"


def test_a_sender_with_nothing_left_has_no_file_decoded(monkeypatch, decoded):
    monkeypatch.setattr(speech, "SPEECH_SECONDS_PER_DAY", 10)
    decoded(10)
    _heard(monkeypatch, "zh-TW")
    speech.transcribe_file("/cache/first.ogg")
    monkeypatch.setattr(speech, "_pcm", lambda _path: pytest.fail("decoded a file the sender cannot have heard"))

    with pytest.raises(ApiError) as refused:
        speech.transcribe_file("/cache/second.ogg")

    assert refused.value.code == "rate_limited"


def test_a_long_recording_is_counted_as_transcribe_bills_it_with_its_opening_twice(monkeypatch, decoded):
    monkeypatch.setattr(speech, "SPEECH_SECONDS_PER_DAY", 60 + speech._IDENTIFY_SECONDS + 2)
    decoded(60)
    _heard(monkeypatch, *["en-US"] * 6)

    speech.transcribe_file("/cache/long_a.ogg")  # 60 seconds heard, 68 sent
    with pytest.raises(ApiError):
        speech.transcribe_file("/cache/long_b.ogg")
    decoded(2)
    assert speech.transcribe_file("/cache/short.ogg").seconds == 2  # exactly what is left
    decoded(1)
    with pytest.raises(ApiError):
        speech.transcribe_file("/cache/one_more.ogg")


def test_a_recording_that_could_not_be_transcribed_is_not_counted(monkeypatch, decoded):
    monkeypatch.setattr(speech, "SPEECH_SECONDS_PER_DAY", 10)
    decoded(10)
    monkeypatch.setattr(speech, "_stream", lambda *_args: (_ for _ in ()).throw(ApiError("network", "Not reachable.")))
    for attempt in range(5):
        with pytest.raises(ApiError) as failed:
            speech.transcribe_file(f"/cache/try_{attempt}.ogg")
        assert failed.value.code == "network"

    _heard(monkeypatch, "zh-TW")

    assert speech.transcribe_file("/cache/works_now.ogg").seconds == 10


def test_a_file_with_no_sound_is_an_error_and_spends_no_allowance(monkeypatch, decoded):
    monkeypatch.setattr(speech, "SPEECH_SECONDS_PER_DAY", 5)
    decoded(0)
    with pytest.raises(ApiError) as silent:
        speech.transcribe_file("/cache/silent.mp4")
    assert silent.value.code == "no_speech"

    decoded(5)
    _heard(monkeypatch, "en-US")
    assert speech.transcribe_file("/cache/voice.ogg").seconds == 5


def test_without_the_key_pair_or_the_region_nothing_is_decoded_or_sent(monkeypatch, configured):
    monkeypatch.setattr(speech, "_pcm", lambda _path: pytest.fail("decoded a file that cannot be transcribed"))
    configured.pop(speech.SECRET_KEY_ENV)

    assert not speech.available() and not speech.AccomyTranscribe().is_available()
    with pytest.raises(ApiError) as missing:
        speech.transcribe_file("/cache/voice.ogg")
    assert missing.value.code == "speech_unavailable"

    configured[speech.SECRET_KEY_ENV] = "test-secret"
    monkeypatch.setattr(speech, "_settings", _Settings(transcribe_region=""))
    assert not speech.available()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_real_voice_note_decodes_to_pcm_and_one_longer_than_the_limit_is_cut(monkeypatch):
    pcm, cut = speech._pcm(VOICE)
    assert 6 * SECOND < len(pcm) < 8 * SECOND and not cut and len(pcm) % 2 == 0

    monkeypatch.setattr(speech, "MAX_SPEECH_SECONDS", 3)
    pcm, cut = speech._pcm(VOICE)
    assert len(pcm) == 3 * SECOND and cut


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_file_that_is_not_sound_is_refused_whatever_its_name(tmp_path):
    playlist = tmp_path / "audio_1.ogg"
    playlist.write_text("#EXTM3U\n#EXTINF:1,\n/etc/hosts\n")
    document = tmp_path / "audio_2.mp3"
    document.write_bytes((Path(VOICE).parent / "booking.pdf").read_bytes())

    for path in (playlist, document):
        with pytest.raises(ApiError) as refused:
            speech._pcm(str(path))
        assert refused.value.code == "unreadable_audio"


def test_the_gateways_provider_returns_the_transcript_envelope(monkeypatch, decoded):
    decoded(5)
    _heard(monkeypatch, "zh-TW")

    result = speech.AccomyTranscribe().transcribe("/cache/audio_1.ogg", model=None, language="en", prompt="ignored")

    assert result == {"success": True, "transcript": "text1", "provider": "accomy-transcribe"}


def test_the_provider_says_when_a_recording_was_cut(monkeypatch, decoded):
    decoded(speech.MAX_SPEECH_SECONDS, cut=True)
    _heard(monkeypatch, "en-US")

    transcript = speech.AccomyTranscribe().transcribe("/cache/long.ogg")["transcript"]

    assert transcript.startswith("text") and f"first {speech.MAX_SPEECH_SECONDS} seconds" in transcript


def test_the_provider_never_raises_and_gives_the_gateway_an_error_envelope(monkeypatch, decoded):
    decoded(5)
    monkeypatch.setattr(speech, "_stream", lambda *_args: (_ for _ in ()).throw(ApiError("network", "Not reachable.")))
    assert speech.AccomyTranscribe().transcribe("/cache/a.ogg") == {
        "success": False, "transcript": "", "provider": "accomy-transcribe", "error": "Not reachable."}

    monkeypatch.setattr(speech, "_stream", lambda *_args: (_ for _ in ()).throw(RuntimeError("secret detail")))
    failed = speech.AccomyTranscribe().transcribe("/cache/b.ogg")
    assert failed["success"] is False and "secret detail" not in failed["error"]


def test_the_plugin_registers_the_provider_under_the_name_the_profile_selects(accomy_manager):
    from agent.transcription_registry import get_provider

    provider = get_provider("accomy-transcribe")

    # Discovery loads the plugin under its own namespace, so the class is compared by what it does.
    assert provider is not None and provider.name == "accomy-transcribe"
    assert provider.transcribe("/no/such/file.ogg")["success"] is False
