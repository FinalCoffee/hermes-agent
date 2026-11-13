"""What a voice note, an audio file or a video says, through Amazon Transcribe streaming.

ffmpeg turns the file into 16 kHz mono PCM, which goes over one WebSocket to Transcribe and comes back as
text. This is the plugin's only WebSocket (``_common.request_json`` stays its only HTTP function), and the
address is signed here with the profile's own key pair, so the plugin needs no AWS library. The key pair
should belong to an identity that may do one thing: ``transcribe:StartStreamTranscriptionWebSocket``.

Transcribe is told the languages the profile lists and picks the one it hears. It identifies a language at
the speed of speech, so a long recording is identified on its first seconds and then transcribed in one
faster pass. A recording is heard up to ``MAX_SPEECH_SECONDS``; what follows is left out and the caller is
told. Speech costs money by the second, so each sender and the bot as a whole have a daily allowance.

The gateway reaches this through ``AccomyTranscribe`` (``stt.provider: accomy-transcribe``) for a voice note,
and ``tools_attachments`` through ``transcribe_file`` for an audio file or a video.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidHandshake
from websockets.sync.client import connect

from agent.transcription_provider import TranscriptionProvider

from . import _common, attachments
from ._common import ApiError

logger = logging.getLogger(__name__)

PROVIDER_NAME = "accomy-transcribe"
ACCESS_KEY_ENV = "ACCOMY_TRANSCRIBE_ACCESS_KEY_ID"
SECRET_KEY_ENV = "ACCOMY_TRANSCRIBE_SECRET_ACCESS_KEY"
REGION_SETTING = "transcribe_region"
LANGUAGES_SETTING = "transcribe_languages"
# Transcribe accepts one dialect per language in a list it chooses from, so zh-TW stands for Chinese.
DEFAULT_LANGUAGES = ("zh-TW", "en-US", "ja-JP", "ko-KR")

# A spoken travel request is seconds long. Two minutes covers a long one, and bounds what one file costs.
MAX_SPEECH_SECONDS = 120
# Seconds of speech a day, per sender and for the whole bot, counted as Transcribe bills them: every second
# sent, the opening seconds of a long recording twice. The second number is the feature's daily spend
# ceiling: six hours.
SPEECH_SECONDS_PER_DAY = 600
GLOBAL_SPEECH_SECONDS_PER_DAY = 6 * 3600

_RATE = 16000
_BYTES_PER_SECOND = 2 * _RATE
_CHUNK = _BYTES_PER_SECOND // 5  # 200 ms, the largest piece Transcribe documents
# Audio is sent ten times faster than it plays. Transcribe keeps up with that for a known language.
_SEND_SPEED = 10
# Transcribe names the language only as fast as the speech itself plays. Past this length the first
# seconds are identified alone, then the whole recording is sent again with the language given.
_IDENTIFY_SECONDS = 8
_OPEN_TIMEOUT = 20.0
_DECODE_TIMEOUT = 60.0
_SIGNATURE_LIFE_SECONDS = 300
# The containers a chat sends speech in. ffmpeg picks a reader from the file's content, and without this list
# a playlist or a subtitle file named like audio would choose a reader that opens other files.
_DEMUXERS = "ogg,mp3,mov,matroska,wav,flac,aac,amr,avi"

_settings = None  # the plugin context, kept to read this profile's settings when a file is transcribed
_budget = _common.RateLimiter()
_recent: dict[str, Transcript] = {}
_RECENT_MAX = 256
_recent_lock = threading.Lock()


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str  # as Transcribe names it ("zh-TW"); empty when nothing was said
    seconds: int  # how much of the recording was heard
    cut: bool  # the recording is longer than MAX_SPEECH_SECONDS and the rest was left out


def configure(ctx) -> None:
    global _settings
    _settings = ctx
    ctx.register_transcription_provider(AccomyTranscribe())


def available() -> bool:
    return _common.has_secrets(ACCESS_KEY_ENV, SECRET_KEY_ENV) and bool(_region())


def _region() -> str:
    return str(_settings.get_config(REGION_SETTING, "") or "").strip() if _settings is not None else ""


def _languages() -> tuple[str, ...]:
    listed = _settings.get_config(LANGUAGES_SETTING, None) if _settings is not None else None
    if isinstance(listed, list) and listed:
        return tuple(str(code).strip() for code in listed if str(code).strip())
    return DEFAULT_LANGUAGES


def transcribe_file(path: str) -> Transcript:
    """The speech in the file at *path*. Raises ``ApiError`` with a message the model may read.

    A file already heard in this process is answered from memory: the gateway transcribes a voice note
    before the turn, and the model may ask for the same file again."""
    name = os.path.basename(path)
    with _recent_lock:
        known = _recent.get(name)
    if known is not None:
        return known
    if not available():
        raise ApiError("speech_unavailable", "Speech cannot be transcribed here. Ask the user to type the request.")
    sender = attachments.owner_of(path) or "unknown"
    allowance = (
        (f"{sender}:speech", SPEECH_SECONDS_PER_DAY, 86400.0), ("global:speech", GLOBAL_SPEECH_SECONDS_PER_DAY, 86400.0))
    used_up = ApiError(
        "rate_limited", "Today's allowance for voice messages is used up. Ask the user to type the request.")
    if _budget.admit(allowance, charge=False) is not None:  # nothing left: do not even decode the file
        raise used_up
    pcm, cut = _pcm(path)
    seconds = math.ceil(len(pcm) / _BYTES_PER_SECOND)
    if seconds == 0:
        raise ApiError("no_speech", "The file has no sound to transcribe.")
    billed = seconds + (_IDENTIFY_SECONDS if _identified_first(seconds) else 0)
    if _budget.admit(allowance, cost=billed) is not None:
        raise used_up
    try:
        text, language = _hear(pcm, seconds)
    except ApiError:
        _budget.refund([key for key, _, _ in allowance], billed)  # nothing was heard, so nothing is counted
        raise
    result = Transcript(text=text, language=language, seconds=seconds, cut=cut)
    with _recent_lock:
        _recent[name] = result
        while len(_recent) > _RECENT_MAX:
            del _recent[next(iter(_recent))]
    return result


def _pcm(path: str) -> tuple[bytes, bool]:
    """The file's sound as 16 kHz mono PCM, at most ``MAX_SPEECH_SECONDS`` of it, and whether more followed."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ApiError("speech_unavailable", "Speech cannot be transcribed here. Ask the user to type the request.")
    command = [
        ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", _DEMUXERS,
        "-i", path, "-vn", "-sn", "-dn", "-t", str(MAX_SPEECH_SECONDS + 1),
        "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "pipe:1",
    ]
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=_DECODE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ApiError("unreadable_audio", "The recording took too long to decode.") from exc
    if done.returncode != 0:
        logger.info("ffmpeg could not decode %s: %s", os.path.basename(path), done.stderr.decode(errors="replace")[:200])
        raise ApiError("unreadable_audio", "The recording could not be decoded. Ask the user to send it again or type the request.")
    limit = MAX_SPEECH_SECONDS * _BYTES_PER_SECOND
    return done.stdout[:limit], len(done.stdout) > limit


def _identified_first(seconds: int) -> bool:
    """Whether a recording of this length has its opening identified in a pass of its own."""
    return len(_languages()) > 1 and seconds > 2 * _IDENTIFY_SECONDS


def _hear(pcm: bytes, seconds: int) -> tuple[str, str]:
    """``(text, language)`` for *pcm*. One language listed: one pass. Several: Transcribe picks, on the whole
    recording when it is short, else on its first seconds followed by one pass in the language it named."""
    languages = _languages()
    fixed = {"media-encoding": "pcm", "sample-rate": str(_RATE)}
    if len(languages) == 1:
        return _stream(pcm, {**fixed, "language-code": languages[0]})[0], languages[0]
    identify = {
        **fixed, "identify-language": "true", "language-options": ",".join(languages),
        "preferred-language": languages[0],
    }
    if not _identified_first(seconds):
        return _stream(pcm, identify)
    _, language = _stream(pcm[: _IDENTIFY_SECONDS * _BYTES_PER_SECOND], identify)
    if not language:  # nothing was said in the opening seconds, so the whole recording is identified
        return _stream(pcm, identify)
    return _stream(pcm, {**fixed, "language-code": language})[0], language


def _stream(pcm: bytes, query: dict[str, str]) -> tuple[str, str]:
    """Send *pcm* over one Transcribe stream: the final text, and the language Transcribe reported.

    Everything is sent before anything is read, so the receive queue is unbounded: Transcribe answers with
    partial results while it listens, and a bounded queue would stop both ends."""
    deadline = time.monotonic() + _OPEN_TIMEOUT + 2.5 * len(pcm) / _BYTES_PER_SECOND + 30.0
    pieces: list[str] = []
    language = ""
    try:
        # No keepalive pings: Transcribe does not answer them, and the client would close a stream that
        # outlasts two ping intervals. The deadline bounds the stream instead.
        with connect(
            _signed_url(query), open_timeout=_OPEN_TIMEOUT, ping_interval=None, max_queue=None, max_size=2**20,
        ) as socket:
            for start in range(0, len(pcm), _CHUNK):
                socket.send(_audio_event(pcm[start:start + _CHUNK]))
                time.sleep(_CHUNK / _BYTES_PER_SECOND / _SEND_SPEED)
            socket.send(_audio_event(b""))
            while True:
                try:
                    frame = socket.recv(timeout=max(0.0, deadline - time.monotonic()))
                except ConnectionClosedOK:
                    break
                headers, payload = _read_event(frame)
                if headers.get(":message-type") != "event":
                    logger.warning("Transcribe refused the stream: %s", headers.get(":exception-type", "unknown"))
                    raise ApiError("speech_failed", "The recording could not be transcribed. Ask the user to type the request.")
                for result in json.loads(payload).get("Transcript", {}).get("Results", ()):
                    if not result.get("IsPartial") and result.get("Alternatives"):
                        pieces.append(result["Alternatives"][0].get("Transcript", ""))
                        language = result.get("LanguageCode") or language
    except TimeoutError as exc:
        raise ApiError("timeout", "Transcribing the recording took too long. Ask the user to type the request.") from exc
    except (OSError, ConnectionClosed, InvalidHandshake) as exc:
        logger.warning("Transcribe stream failed: %s", type(exc).__name__)
        raise ApiError("network", "The transcription service could not be reached. Ask the user to type the request.") from exc
    return " ".join(piece.strip() for piece in pieces if piece.strip()), language


def _signed_url(query: dict[str, str], *, now: datetime | None = None) -> str:
    """The stream's address with an AWS Signature Version 4 in its query, good for five minutes."""
    region = _region()
    access_key, secret_key = _common.secret(ACCESS_KEY_ENV), _common.secret(SECRET_KEY_ENV)
    moment = now or datetime.now(UTC)
    stamp, day = moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")
    host = f"transcribestreaming.{region}.amazonaws.com:8443"
    scope = f"{day}/{region}/transcribe/aws4_request"
    signed = {
        **query, "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp, "X-Amz-Expires": str(_SIGNATURE_LIFE_SECONDS), "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in sorted(signed.items()))
    request = "\n".join((
        "GET", "/stream-transcription-websocket", canonical_query, f"host:{host}", "", "host",
        hashlib.sha256(b"").hexdigest()))
    to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()))
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, "transcribe", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"wss://{host}/stream-transcription-websocket?{canonical_query}&X-Amz-Signature={signature}"


def _string_header(name: str, value: str) -> bytes:
    return bytes([len(name)]) + name.encode() + b"\x07" + struct.pack(">H", len(value)) + value.encode()


_AUDIO_HEADERS = (
    _string_header(":content-type", "application/octet-stream") + _string_header(":event-type", "AudioEvent")
    + _string_header(":message-type", "event"))


def _audio_event(chunk: bytes) -> bytes:
    """One AWS event-stream message carrying *chunk*; an empty chunk ends the stream."""
    prelude = struct.pack(">II", 16 + len(_AUDIO_HEADERS) + len(chunk), len(_AUDIO_HEADERS))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    message = prelude + _AUDIO_HEADERS + chunk
    return message + struct.pack(">I", zlib.crc32(message))


def _read_event(frame: bytes | str) -> tuple[dict[str, str], bytes]:
    """The string headers and the payload of one event-stream message. Transcribe sends one per frame."""
    if isinstance(frame, str) or len(frame) < 16:
        raise ApiError("bad_response", "The transcription service returned an unreadable answer.")
    total, headers_length = struct.unpack(">II", frame[:8])
    if total != len(frame) or headers_length > total - 16:
        raise ApiError("bad_response", "The transcription service returned an unreadable answer.")
    headers: dict[str, str] = {}
    at, end = 12, 12 + headers_length
    while at < end:
        name_length = frame[at]
        name = frame[at + 1:at + 1 + name_length].decode()
        kind = frame[at + 1 + name_length]
        at += 2 + name_length
        if kind != 7:  # Transcribe sends string headers only; another type cannot be skipped without its size
            raise ApiError("bad_response", "The transcription service returned an unreadable answer.")
        (value_length,) = struct.unpack(">H", frame[at:at + 2])
        headers[name] = frame[at + 2:at + 2 + value_length].decode()
        at += 2 + value_length
    return headers, frame[end:total - 4]


class AccomyTranscribe(TranscriptionProvider):
    """The gateway's speech-to-text provider for a voice note, named by ``stt.provider``."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Amazon Transcribe (Accomy)"

    def is_available(self) -> bool:
        return available()

    def transcribe(self, file_path: str, *, model=None, language=None, **extra) -> dict:
        try:
            heard = transcribe_file(file_path)
        except ApiError as exc:
            return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": exc.message}
        except Exception as exc:  # health: allow BLE001 -- the provider contract: transcribe never raises
            logger.exception("Transcription failed")
            return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": type(exc).__name__}
        text = heard.text
        if text and heard.cut:
            text += f" [the recording continues: only its first {MAX_SPEECH_SECONDS} seconds were transcribed]"
        return {"success": True, "transcript": text, "provider": PROVIDER_NAME}
