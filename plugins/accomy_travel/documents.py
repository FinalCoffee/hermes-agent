"""The text of a document a user sent, read the way its kind needs.

Plain text, tables, calendars and web pages are decoded here. A saved mail gives its headers and its body.
A PDF and an office file are parsed by ``_document_worker.py`` in a process of its own, with a time limit
(``parsers``): those parsers do real work on bytes a stranger chose. A PDF page with no text in it is a scan,
and ``ocr`` has the model read it. Nothing else here opens a network connection.

A file contributes at most ``MAX_DOCUMENT_CHARS`` characters, and the tool hands them out ``PART_CHARS`` at
a time, so one long file cannot fill a conversation.
"""

from __future__ import annotations

import email
import email.policy
import os
from dataclasses import dataclass, field
from html.parser import HTMLParser

from charset_normalizer import from_bytes

from . import attachments, ocr, parsers
from ._common import ApiError

# One read returns this much, about 3,000 tokens. A whole file may give ten reads' worth.
PART_CHARS = 12_000
MAX_DOCUMENT_CHARS = 10 * PART_CHARS
# A text file is cut here before decoding. Four bytes is the longest a character takes.
_MAX_TEXT_BYTES = 4 * MAX_DOCUMENT_CHARS
# How much of a file that is not UTF-8 is looked at to tell its encoding.
_SNIFF_BYTES = 64 * 1024
# A scanned page is read only while a file has at least this much of MAX_DOCUMENT_CHARS left for its text.
_SCAN_ROOM = 2_000


@dataclass(frozen=True)
class Document:
    text: str
    complete: bool  # False when the file holds more than MAX_DOCUMENT_CHARS
    pages: int | None = None
    scanned_pages: tuple[int, ...] = field(default=())  # PDF pages that are scans and were read by the model
    unread_pages: tuple[int, ...] = field(default=())  # scanned PDF pages that were not read
    unread_why: str | None = None  # ocr.ALLOWANCE or ocr.FAILED, when that is why


def read(path: str, kind: str) -> Document:
    """The text of the file at *path*, which is of *kind* (``attachments.kind_of``). Raises ``ApiError``."""
    if kind in (attachments.PDF, attachments.OFFICE):
        return _parsed(path, kind)
    with open(path, "rb") as handle:
        data = handle.read(_MAX_TEXT_BYTES + 1)
    whole = len(data) <= _MAX_TEXT_BYTES
    if kind == attachments.MAIL:
        text = _mail(data)
    else:
        text = _decode(data)
        if os.path.splitext(path)[1].lower() in (".html", ".htm"):
            text = _page_text(text)
    return Document(text=text[:MAX_DOCUMENT_CHARS], complete=whole and len(text) <= MAX_DOCUMENT_CHARS)


def _parsed(path: str, kind: str) -> Document:
    result = parsers.run(kind, path, MAX_DOCUMENT_CHARS)
    if kind == attachments.OFFICE:
        if not result["text"].strip():
            raise ApiError("unreadable_file", "The file has no text in it.")
        return Document(text=result["text"], complete=result["complete"])
    by_page = {page: text for page, text in result["texts"]}
    # A scanned page is read only while room is left for its text: past that it would be paid for and dropped.
    readable, used = [], 0
    for page in sorted([*by_page, *result["empty_pages"]]):
        if page in by_page:
            used += len(by_page[page])
        elif used + _SCAN_ROOM <= MAX_DOCUMENT_CHARS:
            readable.append(page)
    left_out = len(readable) < len(result["empty_pages"])
    scan = ocr.read(path, readable)
    by_page.update({page: text for page, text in scan.texts.items() if text})
    text, shown = "", []
    for page in sorted(by_page):
        if len(text) >= MAX_DOCUMENT_CHARS:
            break
        text += ("\n\n" if text else "") + f"[page {page}]\n{by_page[page]}"
        shown.append(page)
    if not text:
        if scan.why == ocr.ALLOWANCE:
            raise ApiError(
                "rate_limited",
                "Today's allowance for reading scanned pages is used up. Ask the user to send the page as a photo, "
                "which you can see, or to type what matters.")
        raise ApiError(
            "unreadable_file", parsers.UNREADABLE["no_text"] if scan.unread else "The file has no text in it.")
    return Document(
        text=text[:MAX_DOCUMENT_CHARS],
        complete=(
            result["complete"] and not left_out and len(shown) == len(by_page) and len(text) <= MAX_DOCUMENT_CHARS),
        pages=result["pages"], scanned_pages=tuple(page for page in shown if page in scan.texts),
        unread_pages=scan.unread, unread_why=scan.why)


def _decode(data: bytes) -> str:
    """Text in UTF-8 when it is UTF-8 (cut mid-character or not), else in the encoding it most looks like:
    a spreadsheet saved as CSV on a Chinese or Japanese Windows is not UTF-8. Zero bytes mean UTF-16, which
    would otherwise pass for UTF-8 with a NUL beside every letter."""
    if b"\x00" not in data[:_SNIFF_BYTES]:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(data):  # the read ended inside a character
                return data[: exc.start].decode("utf-8-sig")
    guess = from_bytes(data[:_SNIFF_BYTES]).best()
    return data.decode(guess.encoding if guess is not None else "utf-8", errors="replace")


class _PageText(HTMLParser):
    """The words of a web page: no tags, no scripts, no styles."""

    _HIDDEN = frozenset({"script", "style", "noscript", "template"})
    _BREAKS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"})

    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1
        elif tag in self._BREAKS:
            self.pieces.append("\n")
        elif tag in ("td", "th"):
            self.pieces.append(" | ")

    def handle_endtag(self, tag) -> None:
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data) -> None:
        if not self._hidden:
            self.pieces.append(data)


def _page_text(markup: str) -> str:
    parser = _PageText()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.pieces).splitlines())
    return "\n".join(line for line in lines if line)


def _mail(data: bytes) -> str:
    """A saved mail as its sender, recipients, subject, date and body. Attached files are named, not read."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    try:
        lines = [f"{name}: {message[name]}" for name in ("From", "To", "Cc", "Date", "Subject") if message[name]]
        body = message.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            lines += ["", _page_text(content) if body.get_content_type() == "text/html" else content.strip()]
        attached = [part.get_filename() for part in message.iter_attachments() if part.get_filename()]
    except (LookupError, ValueError) as exc:  # a header or a body in an encoding the mail names but nobody knows
        raise ApiError("unreadable_file", parsers.UNREADABLE["malformed"]) from exc
    if attached:
        lines += ["", "Attached to the mail (not read): " + ", ".join(attached)]
    return "\n".join(lines)
