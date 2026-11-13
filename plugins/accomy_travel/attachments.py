"""The files senders attach: what each one is, who sent it, and how many a day.

The Telegram adapter downloads an accepted attachment into the gateway's media cache before any plugin code
runs. ``ingress`` hands every admitted message here. Each file is noted under the person who sent it, and
``read_attachment`` (tools_attachments) opens only a file noted for the caller of the turn: the model cannot
be talked into reading a path it made up, another sender's file, or anything outside the cache.

A file costs disk and, once read, model input, so each sender and the bot as a whole have a daily allowance.
The files of a message the gate drops are removed at once (``discard``): the adapter has already written them,
and a flood of dropped messages must not fill the disk. The gateway removes the others after a day, and a
note here lasts as long. Notes live in this process: after a restart the sender sends the file again.

The adapter puts the content of a small text file into the message itself. ``own_words`` takes it out again,
so every file reaches the model the same way, through ``read_attachment``, and the message is what the sender
typed: the length cap and the out-of-scope screen judge a person's words, never a file's.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass

from . import _common
from ._common import ApiError

logger = logging.getLogger(__name__)

# Files a day, per sender and for the whole bot. Telegram hands a bot files of up to 20 MB, so the second
# number bounds the cache at 30 GB however many accounts send.
FILES_PER_DAY = 40
GLOBAL_FILES_PER_DAY = 1500
_DAY = 86400.0
_ALLOWANCES = ("files", "global_files")

IMAGE, AUDIO, VIDEO, PDF, OFFICE, TEXT, MAIL = "image", "audio", "video", "pdf", "office", "text", "mail"
# What a file is, by its extension. A file with any other extension is kept but cannot be read.
_KINDS = {
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif", ".avif"), IMAGE),
    **dict.fromkeys((".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".amr"), AUDIO),
    **dict.fromkeys((".mp4", ".mov", ".webm", ".mkv", ".avi"), VIDEO),
    ".pdf": PDF,
    **dict.fromkeys(
        (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp", ".rtf", ".epub"), OFFICE),
    **dict.fromkeys(
        (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".ics", ".vcf", ".html",
         ".htm", ".srt"), TEXT),
    ".eml": MAIL,
}
# The line the adapter writes above a file's content when it puts that content into the message.
_CONTENT_HEADER_RE = re.compile(r"\[Content of [^\]\n]*\]:\n\Z")


@dataclass(frozen=True)
class Attachment:
    path: str
    name: str  # the sender's own file name when the gateway kept it, else the cache file's name
    sender: str  # the user id the gateway binds as the caller of that person's turns
    noted_at: float


_budget = _common.RateLimiter()
_lock = threading.Lock()
_notes: dict[str, Attachment] = {}  # by cache file name, which the gateway makes unique


def kind_of(path: str) -> str | None:
    return _KINDS.get(os.path.splitext(path)[1].lower())


def _files(event) -> list[tuple[int, str]]:
    """The message's attachments that are files in the local cache, each with its place in the message."""
    return [
        (index, path) for index, path in enumerate(event.media_urls or ())
        if os.path.isabs(path) and os.path.isfile(path)]


def own_words(event) -> str:
    """The message's text as its sender typed it: without the content of files the adapter put into it.

    Each such file is marked on the event as not included, so the gateway's note about it tells the model to
    read the file instead. A file whose content is not found in the text is left as the adapter delivered it."""
    text = event.text or ""
    included = event.media_text_inlined or []
    for index, path in _files(event):
        if index >= len(included) or included[index] is not True:
            continue
        try:
            with open(path, "rb") as handle:
                content = handle.read().decode("utf-8")  # as the adapter decoded it, byte-order mark and all
        except (OSError, UnicodeDecodeError):
            continue
        at = text.find(content) if content else -1
        if at < 0:
            continue
        text = _CONTENT_HEADER_RE.sub("", text[:at]) + text[at + len(content):]
        included[index] = False
    return text if text == (event.text or "") else text.strip()


def _allowance_checks(payer: str):
    return ((f"{payer}:files", FILES_PER_DAY, _DAY), ("global:files", GLOBAL_FILES_PER_DAY, _DAY))


def over_allowance(event, payer: str) -> str | None:
    """The file allowance this message would break, ``"files"`` or ``"global_files"``, or None. Charges nothing."""
    files = _files(event)
    refused = _budget.admit(_allowance_checks(payer), cost=len(files), charge=False) if files else None
    return None if refused is None else _ALLOWANCES[refused]


def admit(event, payer: str) -> str | None:
    """Note the files of an admitted message under its sender, charged to *payer*'s daily allowance.

    Returns None when they are noted, or there are none. Otherwise returns the allowance the message breaks,
    ``"files"`` or ``"global_files"``, and notes nothing."""
    files = _files(event)
    if not files:
        return None
    refused = _budget.admit(_allowance_checks(payer), cost=len(files))
    if refused is not None:
        return _ALLOWANCES[refused]
    sender = str(event.sender_id() or "")
    if not sender:  # a channel post has no sender: its files are kept and nobody's turn can read them
        return None
    names = event.media_filenames or ()
    now = time.monotonic()
    with _lock:
        for stale in [name for name, note in _notes.items() if now - note.noted_at >= _DAY]:
            del _notes[stale]
        for index, path in files:
            cached = os.path.basename(path)
            given = names[index] if index < len(names) else ""
            # The gateway names a cached document doc_<id>_<the sender's file name>.
            parts = cached.split("_", 2)
            shown = given or (parts[2] if cached.startswith("doc_") and len(parts) == 3 else cached)
            _notes[cached] = Attachment(path=path, name=shown, sender=sender, noted_at=now)
    return None


def discard(event) -> None:
    """Remove the cached files of a message that goes no further."""
    for _, path in _files(event):
        try:
            os.unlink(path)
        except OSError:
            logger.warning("Could not remove the file of a dropped message", exc_info=True)


def detach(event) -> None:
    """Remove the message's files and take them off the message: what is left is its text, for a command that
    core runs without reading any file."""
    discard(event)
    for listed in (event.media_urls, event.media_types, event.media_text_inlined, event.media_filenames):
        if listed:
            listed.clear()


def owner_of(path: str) -> str:
    """The sender whose file this is, or an empty string for a file nobody was noted for."""
    with _lock:
        note = _notes.get(os.path.basename(path))
    return note.sender if note is not None else ""


def find(reference: str) -> Attachment:
    """The caller's attachment that *reference* names, by its saved path or by its file name (the newest of
    that name). Another sender's file, a file older than a day and a made-up path are all unknown."""
    wanted = os.path.basename(reference.strip())
    caller = _common.caller_id()
    now = time.monotonic()
    with _lock:
        own = [note for note in _notes.values() if note.sender == caller and now - note.noted_at < _DAY]
    matches = [note for note in own if wanted in (os.path.basename(note.path), note.name)]
    if not wanted or not matches or not os.path.isfile(matches[-1].path):
        raise ApiError(
            "attachment_unknown",
            "No such file from this user is available: files are kept for a day. Ask the user to send it again.")
    return matches[-1]
